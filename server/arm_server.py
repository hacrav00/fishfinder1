#!/usr/bin/env python3
"""
arm_server.py — FishFinder Robotic Arm, Camera Focus/Zoom & Multi-Router Hub
=============================================================================
- Jitter-Free Servo Controller with Idle Sleep:
  * Tilt (GPIO 19 / Pin 35): 180° Positional Servo with auto-sleep after positioning
  * Pan  (GPIO 18 / Pin 12): 360° Continuous Rotation with zero-pulse stop (no creep)
- Camera Autofocus & Zoom Control (/api/focus, /api/zoom via V4L2)
- Universal Router Auto-Detection (COFE 192.168.150.1, D-Link 192.168.0.1,
  Office 192.168.1.1, Direct Cable 192.168.50.1, and any DHCP router)
"""
import os
import re
import sys
import json
import time
import socket
import threading
import subprocess
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import lgpio

PAN_PIN = 18    # GPIO 18, Physical Pin 12 (360° Continuous)
TILT_PIN = 19   # GPIO 19, Physical Pin 35 (180° Positional)

# Initialize native lgpio driver
try:
    h = lgpio.gpiochip_open(0)
except Exception:
    h = lgpio.gpiochip_open(4)

lgpio.gpio_claim_output(h, PAN_PIN)
lgpio.gpio_claim_output(h, TILT_PIN)

arm_lock = threading.Lock()
current_tilt = 90
current_pan = 90
current_zoom = 1.0
current_focus_mode = "continuous"
current_focus_val = 50

tilt_sleep_timer = None
pan_timer = None


def _run_cmd(cmd):
    try:
        r = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=4)
        return r.stdout.strip()
    except Exception:
        return ""


def get_pi_ips():
    out = _run_cmd("ip -4 -o addr show")
    ips = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[1] != "lo":
            ip = parts[3].split("/")[0]
            if not ip.startswith("169.254.") and not ip.startswith("127."):
                ips.append(ip)
    return ips


# --- Universal Router Auto-Connection (COFE 192.168.150.1 + Any Router) ---
KNOWN_ROUTER_IPS = [
    "192.168.150.131/24",  # COFE CF-707 WF Router (192.168.150.1)
    "192.168.0.131/24",    # D-Link Router (192.168.0.1)
    "192.168.1.131/24",    # Standard Router (192.168.1.1)
    "192.168.50.1/24",     # Direct Laptop Ethernet
]


def start_universal_router_daemon():
    try:
        import router_autoconnect
        router_autoconnect.start_background_autoconnect()
        return
    except Exception:
        pass

    def _router_loop():
        # Ensure NetworkManager profile persists static aliases + DHCP
        addrs_csv = ",".join(KNOWN_ROUTER_IPS)
        _run_cmd(f'sudo nmcli con mod "rov-eth" ipv4.method auto ipv4.addresses "{addrs_csv}" ipv4.may-fail yes 2>/dev/null || true')
        _run_cmd('sudo nmcli con up "rov-eth" 2>/dev/null || true')

        while True:
            try:
                _run_cmd("sudo ip link set eth0 up 2>/dev/null || true")
                active_ips = set(get_pi_ips())
                for cidr in KNOWN_ROUTER_IPS:
                    ip = cidr.split("/")[0]
                    if ip not in active_ips:
                        _run_cmd(f"sudo ip addr add {cidr} dev eth0 2>/dev/null || true")

                # Detect any new router subnet on eth0 and bind <subnet>.131/24
                routes = _run_cmd("ip -4 addr show dev eth0") + "\n" + _run_cmd("ip -4 route show dev eth0")
                for m in re.findall(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3})\.\d{1,3}\b", routes):
                    if not m.startswith(("169.254", "127.", "224.", "255.")):
                        target = f"{m}.131"
                        if target not in active_ips:
                            _run_cmd(f"sudo ip addr add {target}/24 dev eth0 2>/dev/null || true")
            except Exception:
                pass
            time.sleep(5.0)

    threading.Thread(target=_router_loop, daemon=True, name="router-daemon").start()


def stop_pwm(pin):
    """Cleanly cuts off pulses using lgpio.tx_pulse(0, 0). Eliminates jitter, buzzing, and creeping!"""
    try:
        lgpio.tx_pulse(h, pin, 0, 0)
    except Exception:
        pass


def deg_to_pw_tilt(deg):
    clamped = max(0.0, min(180.0, float(deg)))
    return int(600 + (clamped / 180.0) * 1800)


def deg_to_pw_pan(deg):
    clamped = max(0.0, min(180.0, float(deg)))
    return int(600 + (clamped / 180.0) * 1800)


def set_tilt(deg):
    global current_tilt, tilt_sleep_timer
    with arm_lock:
        if tilt_sleep_timer:
            tilt_sleep_timer.cancel()
            tilt_sleep_timer = None

        deg = max(0, min(180, int(deg)))
        current_tilt = deg
        pw = deg_to_pw_tilt(deg)
        lgpio.tx_servo(h, TILT_PIN, pw, 50)

        def _sleep():
            stop_pwm(TILT_PIN)

        tilt_sleep_timer = threading.Timer(0.7, _sleep)
        tilt_sleep_timer.daemon = True
        tilt_sleep_timer.start()


def set_pan(deg_or_action, duration=None):
    global current_pan, pan_timer
    with arm_lock:
        if pan_timer:
            pan_timer.cancel()
            pan_timer = None

        if isinstance(deg_or_action, str):
            act = deg_or_action.lower()
            if act == "left":
                current_pan = 75
                pw = 1360  # Gentle pulse near center for modified SG90 360° servo
                if duration is None:
                    duration = 0.040
            elif act == "right":
                current_pan = 105
                pw = 1640  # Gentle pulse near center for modified SG90 360° servo
                if duration is None:
                    duration = 0.040
            else:
                current_pan = 90
                stop_pwm(PAN_PIN)
                return
        else:
            deg = max(0, min(180, int(deg_or_action)))
            current_pan = deg
            if 84 <= deg <= 96:
                current_pan = 90
                stop_pwm(PAN_PIN)
                return
            pw = 1360 if deg < 90 else 1640
            if duration is None:
                duration = 0.040

        duration = max(0.015, min(0.250, float(duration)))
        lgpio.tx_servo(h, PAN_PIN, pw, 50)

        def _auto_stop():
            set_pan(90)
        pan_timer = threading.Timer(duration, _auto_stop)
        pan_timer.daemon = True
        pan_timer.start()


def apply_camera_focus(action="trigger", mode=None, value=None):
    """Triggers hardware V4L2 autofocus or manual focus across /dev/video0 and subdevs."""
    global current_focus_mode, current_focus_val
    devices = ["/dev/video0", "/dev/v4l-subdev0", "/dev/v4l-subdev1", "/dev/v4l-subdev2"]

    if mode:
        current_focus_mode = str(mode).lower()

    if action == "trigger":
        # Pulse autofocus cycle + optimize sharpness
        for dev in devices:
            if os.path.exists(dev):
                _run_cmd(f"v4l2-ctl -d {dev} --set-ctrl=focus_automatic_continuous=1 2>/dev/null || true")
                _run_cmd(f"v4l2-ctl -d {dev} --set-ctrl=auto_focus_start=1 2>/dev/null || true")
                _run_cmd(f"v4l2-ctl -d {dev} --set-ctrl=sharpness=180 2>/dev/null || true")
        current_focus_mode = "continuous"
    elif current_focus_mode == "continuous":
        for dev in devices:
            if os.path.exists(dev):
                _run_cmd(f"v4l2-ctl -d {dev} --set-ctrl=focus_automatic_continuous=1 2>/dev/null || true")
    elif current_focus_mode in ("manual", "macro"):
        if value is not None:
            current_focus_val = max(0, min(1023, int(float(value))))
        elif current_focus_mode == "macro":
            current_focus_val = 450
        for dev in devices:
            if os.path.exists(dev):
                _run_cmd(f"v4l2-ctl -d {dev} --set-ctrl=focus_automatic_continuous=0 2>/dev/null || true")
                _run_cmd(f"v4l2-ctl -d {dev} --set-ctrl=focus_absolute={current_focus_val} 2>/dev/null || true")


def apply_camera_zoom(zoom_val):
    """Applies V4L2 hardware zoom if supported and tracks current zoom state."""
    global current_zoom
    current_zoom = max(1.0, min(4.0, round(float(zoom_val), 2)))
    zoom_int = int(100 + (current_zoom - 1.0) * 100)
    if os.path.exists("/dev/video0"):
        _run_cmd(f"v4l2-ctl -d /dev/video0 --set-ctrl=zoom_absolute={zoom_int} 2>/dev/null || true")


# Initialize both motors to completely STOPPED and QUIET on startup
stop_pwm(PAN_PIN)
stop_pwm(TILT_PIN)
start_universal_router_daemon()

HTML_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no, viewport-fit=cover">
<meta name="theme-color" content="#0a0e14">
<title>FishFinder ROV Controller</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; -webkit-tap-highlight-color: transparent; }
  body { background: #0a0e14; color: #e6edf3; display: flex; flex-direction: column; align-items: center; min-height: 100vh; padding: 12px; }
  header { width: 100%; max-width: 960px; display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #21262d; margin-bottom: 12px; }
  h1 { font-size: 1.2rem; color: #00d4aa; display: flex; align-items: center; gap: 8px; }
  .badge { background: #161b22; border: 1px solid #30363d; padding: 4px 10px; border-radius: 12px; font-size: 0.8rem; color: #58a6ff; font-weight: 600; }
  .container { width: 100%; max-width: 960px; display: grid; grid-template-columns: 1fr; gap: 14px; }
  @media(min-width: 768px) { .container { grid-template-columns: 2fr 1fr; } }
  .video-card { background: #010409; border: 1px solid #21262d; border-radius: 8px; overflow: hidden; display: flex; flex-direction: column; }
  .video-container { position: relative; width: 100%; padding-top: 56.25%; background: #000; overflow: hidden; }
  .video-container img { position: absolute; top: 0; left: 0; width: 100%; height: 100%; object-fit: contain; transform-origin: center center; transition: transform 0.15s ease; }
  .controls-card { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 16px; display: flex; flex-direction: column; gap: 14px; }
  .section-title { font-size: 0.82rem; font-weight: 700; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid #21262d; padding-bottom: 6px; }
  .dpad { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; width: 100%; max-width: 220px; margin: 0 auto; }
  .btn { background: #21262d; border: 1px solid #30363d; color: #c9d1d9; border-radius: 6px; padding: 10px; font-weight: 600; cursor: pointer; text-align: center; transition: all 0.1s ease; user-select: none; }
  .btn:hover { background: #30363d; color: #fff; }
  .btn:active { background: #00d4aa; color: #000; transform: scale(0.96); }
  .btn-accent { background: #0f766e; color: #99f6e4; border-color: #14b8a6; }
  .quick-bar { display: flex; gap: 8px; justify-content: space-between; flex-wrap: wrap; }
  .quick-bar .btn { flex: 1; min-width: 68px; font-size: 0.82rem; padding: 9px 6px; }
  .slider-row { display: flex; flex-direction: column; gap: 5px; }
  .slider-lbl { display: flex; justify-content: space-between; font-size: 0.82rem; color: #c9d1d9; font-weight: 500; }
  .slider-val { font-family: monospace; font-weight: 700; color: #38bdf8; }
  input[type=range] { width: 100%; accent-color: #00d4aa; height: 10px; border-radius: 5px; cursor: pointer; }
  .hint-row { display: flex; justify-content: space-between; font-size: 10px; color: #64748b; margin-top: 2px; }
  .hint-row span { cursor: pointer; padding: 2px 4px; border-radius: 3px; }
  .hint-row span:hover { color: #38bdf8; background: #21262d; }
  #af-box { display: none; position: absolute; top: 50%; left: 50%; width: 90px; height: 90px; transform: translate(-50%, -50%); border: 2px solid #facc15; border-radius: 8px; pointer-events: none; z-index: 12; box-shadow: 0 0 12px rgba(250,204,21,0.5); }
</style>
</head>
<body>
<header>
  <h1><span>🐟</span> FishFinder ROV Controller</h1>
  <div class="badge" id="driver-badge">● LIVE FEED</div>
</header>
<div class="container">
  <div class="video-card">
    <div class="video-container" id="video-touch-area" style="touch-action: none; cursor: grab;">
      <img id="stream-img" src="" alt="Camera Feed">
      <div id="af-box"></div>
      <div id="gesture-indicator" style="display:none; position:absolute; top:50%; left:50%; transform:translate(-50%, -50%); background:rgba(11,15,25,0.85); color:#00d4aa; padding:10px 18px; border-radius:12px; font-weight:bold; font-size:15px; pointer-events:none; border:1px solid #00d4aa; z-index:10;">
        <span id="gesture-text">SWIPE</span>
      </div>
      <div style="position:absolute; bottom:8px; right:10px; background:rgba(15,23,42,0.75); padding:3px 8px; border-radius:6px; font-size:10px; color:#94a3b8; pointer-events:none;">
        👆 Swipe to Pan/Tilt | Pinch to Zoom
      </div>
    </div>
    <div style="padding: 10px 14px; font-size: 0.85rem; color: #8b949e; display: flex; justify-content: space-between;">
      <span id="zoom-info" style="color:#38bdf8; font-weight:600;">Zoom: 1.0x | AF: Ready</span>
      <span id="pos-info" style="color:#00d4aa; font-weight:600;">Pan: STOPPED | Tilt: 90°</span>
    </div>
  </div>
  <div class="controls-card">
    <div class="section-title">Autofocus & Zoom Controls</div>
    <div class="quick-bar">
      <button class="btn btn-accent" onclick="triggerAutofocus()">🎯 Autofocus</button>
      <button class="btn" onclick="stepZoom(-0.5)">🔍- Zoom Out</button>
      <button class="btn" onclick="stepZoom(0.5)">🔍+ Zoom In</button>
      <button class="btn" onclick="cycleCamRotate()">🙃 Rotate/Hang</button>
    </div>
    <div class="slider-row">
      <div class="slider-lbl">
        <span>Digital & Optical Zoom</span>
        <span id="zoom-val" class="slider-val">1.0x</span>
      </div>
      <input type="range" id="zoom-slider" min="10" max="40" value="10" oninput="setZoomLevel(this.value/10)">
    </div>

    <div class="section-title">Pan (360° Little-by-Little) & Tilt Controls</div>
    <div class="dpad">
      <div></div>
      <button class="btn" onclick="sendTiltAction('up')">▲ Up</button>
      <div></div>
      <button class="btn" onclick="triggerPanTimed('left')">◀ Step L</button>
      <button class="btn btn-accent" onclick="recenter()">⨁ Center</button>
      <button class="btn" onclick="triggerPanTimed('right')">Step R ▶</button>
      <div></div>
      <button class="btn" onclick="sendTiltAction('down')">▼ Down</button>
      <div></div>
    </div>

    <div class="slider-row">
      <div class="slider-lbl">
        <span>Pan (360° Micro-Step Slider)</span>
        <span id="pan-val" class="slider-val">STOPPED</span>
      </div>
      <input type="range" id="pan-slider" min="0" max="180" value="90" oninput="onPanInput(this.value)">
      <div class="hint-row">
        <span onclick="triggerPanTimed('left')">◀ Step Left (~10°)</span>
        <span onclick="triggerPanStop()" style="color:#00d4aa; font-weight:bold;">⏹ STOP</span>
        <span onclick="triggerPanTimed('right')">Step Right (~10°) ▶</span>
      </div>
    </div>

    <div class="slider-row">
      <div class="slider-lbl">
        <span>Tilt Angle (0°–180° Positional)</span>
        <span id="tilt-val" class="slider-val">90° (Level)</span>
      </div>
      <input type="range" id="tilt-slider" min="0" max="180" value="90" oninput="onTiltSlider(this.value)">
      <div class="hint-row">
        <span onclick="setTiltDirect(0)">0° Down</span>
        <span onclick="setTiltDirect(90)" style="color:#00d4aa; font-weight:bold;">90° Level</span>
        <span onclick="setTiltDirect(180)">180° Up</span>
      </div>
    </div>
  </div>
</div>
<script>
const feed = document.getElementById("stream-img");
feed.src = "http://" + window.location.hostname + ":8000/stream";
feed.onerror = function() { feed.src = "/stream?t=" + Date.now(); };

let lastPanSent = 90, lastTiltSent = 90, currentZoom = 1.0, camRot = parseInt(localStorage.getItem('rov_cam_rot') || '0', 10);
let panTimer = null, tiltTimer = null, swipePanCooldown = false;

function applyFeedTransform() {
  feed.style.transform = `rotate(${camRot}deg) scale(${currentZoom})`;
}
applyFeedTransform();

function cycleCamRotate() {
  const order = [0, 180, 90, 270];
  camRot = order[(order.indexOf(camRot) + 1) % order.length];
  localStorage.setItem('rov_cam_rot', String(camRot));
  applyFeedTransform();
  showIndicator(camRot === 180 ? '🙃 HANGING 180°' : `🔄 ROTATE ${camRot}°`);
  setTimeout(hideIndicator, 700);
}

function post(url, data) {
  return fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data)
  }).then(r => r.json()).then(d => {
    updateLabels(d.pan, d.tilt);
  }).catch(e => console.error(e));
}

function triggerAutofocus() {
  const box = document.getElementById("af-box");
  box.style.borderColor = "#facc15";
  box.style.display = "block";
  showIndicator("🎯 AUTOFOCUSING...");
  post('/api/focus', {action: 'trigger'});
  setTimeout(() => {
    box.style.borderColor = "#00d4aa";
    showIndicator("✓ AF LOCKED");
    setTimeout(() => { box.style.display = "none"; hideIndicator(); }, 600);
  }, 550);
}

function setZoomLevel(z) {
  currentZoom = Math.max(1.0, Math.min(4.0, parseFloat(z)));
  applyFeedTransform();
  document.getElementById("zoom-val").innerText = currentZoom.toFixed(1) + "x";
  document.getElementById("zoom-slider").value = Math.round(currentZoom * 10);
  document.getElementById("zoom-info").innerText = `Zoom: ${currentZoom.toFixed(1)}x | View: ${camRot}°`;
  post('/api/zoom', {zoom: currentZoom});
}

function stepZoom(delta) {
  setZoomLevel(currentZoom + delta);
}

function updateLabels(p, t) {
  if (p !== undefined) {
    let tag = (p >= 84 && p <= 96) ? 'STOPPED' : (p < 84 ? '◀ STEP L' : 'STEP R ▶');
    document.getElementById('pan-val').innerText = tag;
    lastPanSent = p;
  }
  if (t !== undefined) {
    let tag = t == 90 ? ' (Level)' : (t < 90 ? ' (Down)' : ' (Up)');
    document.getElementById('tilt-val').innerText = t + '°' + tag;
    document.getElementById('tilt-slider').value = t;
    lastTiltSent = t;
  }
  const curP = p !== undefined ? p : lastPanSent;
  const curT = t !== undefined ? t : lastTiltSent;
  const pText = (curP >= 84 && curP <= 96) ? 'STOPPED' : (curP < 84 ? 'STEP L' : 'STEP R');
  document.getElementById('pos-info').innerText = 'Pan: ' + pText + ' | Tilt: ' + curT + '°';
}

function sendTiltAction(act) { post('/api/tilt', {action: act}); }
function triggerPanTimed(dir) {
  showIndicator(dir === 'left' ? '◀ STEP LEFT (~10°)' : 'STEP RIGHT (~10°) ▶');
  post('/api/pan', {action: dir, duration: 0.040});
  setTimeout(() => { post('/api/pan', {angle: 90}); updateLabels(90, undefined); hideIndicator(); }, 95);
}
function triggerPanStop() { updateLabels(90, undefined); post('/api/pan', {angle: 90}); }
function setTiltDirect(val) { updateLabels(undefined, parseInt(val)); post('/api/tilt', {angle: parseInt(val)}); }
function onPanInput(val) {
  const p = parseInt(val);
  if (p >= 82 && p <= 98) {
    triggerPanStop();
    return;
  }
  if (!panTimer) {
    triggerPanTimed(p < 90 ? 'left' : 'right');
    panTimer = setTimeout(() => { panTimer = null; }, 190);
  }
}
const panSlider = document.getElementById('pan-slider');
function releasePanToStop() {
  if (panSlider.value != 90) {
    panSlider.value = 90;
    updateLabels(90, undefined);
    post('/api/pan', {angle: 90});
  }
}
panSlider.addEventListener('mouseup', releasePanToStop);
panSlider.addEventListener('touchend', releasePanToStop);
panSlider.addEventListener('pointerup', releasePanToStop);
panSlider.addEventListener('touchcancel', releasePanToStop);

function onTiltSlider(val) {
  const t = parseInt(val);
  updateLabels(undefined, t);
  if (tiltTimer) clearTimeout(tiltTimer);
  tiltTimer = setTimeout(() => { post('/api/tilt', {angle: t}); }, 35);
}
function recenter() {
  updateLabels(90, 90);
  post('/api/pan', {angle: 90});
  post('/api/tilt', {angle: 90});
}

const touchArea = document.getElementById("video-touch-area");
const indicator = document.getElementById("gesture-indicator");
const indicatorText = document.getElementById("gesture-text");
let touchStartX = 0, touchStartY = 0, isTouching = false, tiltThrottleTimer = null;

function showIndicator(txt) { indicatorText.innerText = txt; indicator.style.display = "block"; }
function hideIndicator() { indicator.style.display = "none"; }

function handleSwipeMove(curX, curY) {
  const dx = curX - touchStartX, dy = curY - touchStartY;
  if (Math.abs(dx) > Math.abs(dy)) {
    if (Math.abs(dx) >= 26 && !swipePanCooldown) {
      swipePanCooldown = true;
      setTimeout(() => { swipePanCooldown = false; }, 170);
      touchStartX = curX;
      touchStartY = curY;
      triggerPanTimed(dx < 0 ? 'left' : 'right');
    }
  } else {
    if (Math.abs(dy) >= 22 && !tiltThrottleTimer) {
      tiltThrottleTimer = setTimeout(() => { tiltThrottleTimer = null; }, 110);
      touchStartY = curY;
      touchStartX = curX;
      const nextTilt = dy < 0 ? Math.min(180, lastTiltSent + 5) : Math.max(0, lastTiltSent - 5);
      showIndicator((dy < 0 ? '▲ TILT UP (' : '▼ TILT DOWN (') + nextTilt + '°)');
      updateLabels(undefined, nextTilt);
      post('/api/tilt', {angle: nextTilt});
    }
  }
}
function handleSwipeEnd() {
  if (isTouching) {
    isTouching = false;
    setTimeout(hideIndicator, 200);
  }
}
touchArea.addEventListener('touchstart', e => {
  if (e.touches.length === 1) { touchStartX = e.touches[0].clientX; touchStartY = e.touches[0].clientY; isTouching = true; }
}, {passive: false});
touchArea.addEventListener('touchmove', e => {
  if (!isTouching || e.touches.length !== 1) return;
  e.preventDefault(); handleSwipeMove(e.touches[0].clientX, e.touches[0].clientY);
}, {passive: false});
touchArea.addEventListener('touchend', handleSwipeEnd);
touchArea.addEventListener('touchcancel', handleSwipeEnd);
touchArea.addEventListener('mousedown', e => { touchStartX = e.clientX; touchStartY = e.clientY; isTouching = true; });
window.addEventListener('mousemove', e => { if (isTouching) handleSwipeMove(e.clientX, e.clientY); });
window.addEventListener('mouseup', handleSwipeEnd);
</script>
</body>
</html>
"""


class RequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors()
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))
            return

        if self.path == "/api/status":
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            resp = {
                "service": "fishfinder-rov",
                "status": "ok",
                "tilt": current_tilt,
                "pan": current_pan,
                "zoom": current_zoom,
                "focus_mode": current_focus_mode,
                "focus_val": current_focus_val,
                "ips": get_pi_ips(),
            }
            self.wfile.write(json.dumps(resp).encode("utf-8"))
            return

        if self.path == "/api/pan":
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"angle": current_pan}).encode("utf-8"))
            return

        if self.path == "/api/tilt":
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"angle": current_tilt}).encode("utf-8"))
            return

        if self.path.startswith("/stream"):
            try:
                upstream = urllib.request.urlopen("http://127.0.0.1:8000/stream", timeout=4)
                self.send_response(200)
                self._send_cors()
                for header, value in upstream.getheaders():
                    if header.lower() not in ("server", "transfer-encoding"):
                        self.send_header(header, value)
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                while True:
                    chunk = upstream.read(2048)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except Exception:
                self.send_error(502, "Stream unavailable")
            return

        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
        try:
            data = json.loads(body)
        except Exception:
            data = {}

        if self.path == "/api/pan":
            if "angle" in data:
                raw_angle = float(data["angle"])
                if raw_angle > 180:
                    raw_angle = raw_angle / 2.0
                set_pan(raw_angle)
            elif "action" in data:
                dur = float(data.get("duration", 0))
                set_pan(data["action"], duration=dur if dur > 0 else None)

            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "pan": current_pan, "tilt": current_tilt}).encode("utf-8"))
            return

        if self.path == "/api/tilt":
            if "angle" in data:
                set_tilt(data["angle"])
            elif "action" in data:
                act = str(data["action"]).lower()
                if act == "up":
                    set_tilt(current_tilt + 10)
                elif act == "down":
                    set_tilt(current_tilt - 10)
                elif act == "center":
                    set_tilt(90)

            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "pan": current_pan, "tilt": current_tilt}).encode("utf-8"))
            return

        if self.path == "/api/focus":
            act = data.get("action", "trigger")
            mode = data.get("mode", None)
            val = data.get("value", None)
            apply_camera_focus(action=act, mode=mode, value=val)
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "focus_mode": current_focus_mode,
                "focus_val": current_focus_val,
                "pan": current_pan,
                "tilt": current_tilt
            }).encode("utf-8"))
            return

        if self.path == "/api/zoom":
            z = data.get("zoom", 1.0)
            apply_camera_zoom(z)
            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "zoom": current_zoom,
                "pan": current_pan,
                "tilt": current_tilt
            }).encode("utf-8"))
            return

        self.send_error(404)


def run():
    server = ThreadingHTTPServer(("0.0.0.0", 8080), RequestHandler)
    print("FishFinder Arm & Camera Hub running on port 8080 (Universal Router Auto-Connect active)...")
    try:
        server.serve_forever()
    finally:
        lgpio.gpiochip_close(h)


if __name__ == "__main__":
    run()
