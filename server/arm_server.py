#!/usr/bin/env python3
"""
arm_server.py — FishFinder Robotic Arm & Video Hub
===================================================
Jitter-Free Servo Controller with Idle Sleep:
- Tilt (GPIO 19 / Pin 35): 180° Positional Servo with auto-sleep after positioning
- Pan  (GPIO 18 / Pin 12): 360° Continuous Rotation with zero-pulse stop (no creep)
"""
import os
import sys
import json
import time
import threading
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

tilt_sleep_timer = None
pan_timer = None

def stop_pwm(pin):
    """Cleanly cuts off pulses using lgpio.tx_pulse(0, 0). Eliminates jitter, buzzing, and creeping!"""
    try:
        lgpio.tx_pulse(h, pin, 0, 0)
    except Exception:
        pass

def deg_to_pw_tilt(deg):
    """Maps 0..180 degrees to 600..2400 microseconds."""
    clamped = max(0.0, min(180.0, float(deg)))
    return int(600 + (clamped / 180.0) * 1800)

def deg_to_pw_pan(deg):
    """Maps 0..180 to continuous servo speed/direction."""
    clamped = max(0.0, min(180.0, float(deg)))
    return int(600 + (clamped / 180.0) * 1800)

def set_tilt(deg):
    """
    Drives 180° positional servo to target angle, then sleeps after 0.7s
    to eliminate jitter, buzzing, overheating, and undervoltage.
    """
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
    """
    360° Continuous Rotation Servo:
    - 90 deg / "stop" = Completely cuts PWM so motor NEVER creeps or spins idle!
    - < 85 deg / "left" = Spins Left
    - > 95 deg / "right" = Spins Right
    """
    global current_pan, pan_timer
    with arm_lock:
        if pan_timer:
            pan_timer.cancel()
            pan_timer = None

        if isinstance(deg_or_action, str):
            act = deg_or_action.lower()
            if act == "left":
                current_pan = 40
                pw = 1200
                if duration is None: duration = 0.5
            elif act == "right":
                current_pan = 140
                pw = 1800
                if duration is None: duration = 0.5
            else: # "stop", "center"
                current_pan = 90
                stop_pwm(PAN_PIN)
                return
        else:
            deg = max(0, min(180, int(deg_or_action)))
            current_pan = deg
            # Deadband around 90: Stop completely!
            if 84 <= deg <= 96:
                current_pan = 90
                stop_pwm(PAN_PIN)
                return
            pw = deg_to_pw_pan(deg)

        lgpio.tx_servo(h, PAN_PIN, pw, 50)

        if duration and duration > 0:
            def _auto_stop():
                set_pan(90)
            pan_timer = threading.Timer(duration, _auto_stop)
            pan_timer.daemon = True
            pan_timer.start()

# Initialize both motors to completely STOPPED and QUIET on startup
stop_pwm(PAN_PIN)
stop_pwm(TILT_PIN)

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
  header { width: 100%; max-width: 900px; display: flex; justify-content: space-between; align-items: center; padding: 10px 0; border-bottom: 1px solid #21262d; margin-bottom: 12px; }
  h1 { font-size: 1.2rem; color: #00d4aa; display: flex; align-items: center; gap: 8px; }
  .badge { background: #161b22; border: 1px solid #30363d; padding: 4px 10px; border-radius: 12px; font-size: 0.8rem; color: #58a6ff; font-weight: 600; }
  .container { width: 100%; max-width: 900px; display: grid; grid-template-columns: 1fr; gap: 14px; }
  @media(min-width: 768px) { .container { grid-template-columns: 2fr 1fr; } }
  .video-card { background: #010409; border: 1px solid #21262d; border-radius: 8px; overflow: hidden; display: flex; flex-direction: column; }
  .video-container { position: relative; width: 100%; padding-top: 56.25%; background: #000; }
  .video-container img { position: absolute; top: 0; left: 0; width: 100%; height: 100%; object-fit: contain; }
  .controls-card { background: #161b22; border: 1px solid #21262d; border-radius: 8px; padding: 16px; display: flex; flex-direction: column; gap: 16px; }
  .section-title { font-size: 0.85rem; font-weight: 700; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; border-bottom: 1px solid #21262d; padding-bottom: 6px; }
  .dpad { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; width: 100%; max-width: 220px; margin: 0 auto; }
  .btn { background: #21262d; border: 1px solid #30363d; color: #c9d1d9; border-radius: 6px; padding: 12px; font-weight: 600; cursor: pointer; text-align: center; transition: all 0.1s ease; user-select: none; }
  .btn:hover { background: #30363d; color: #fff; }
  .btn:active { background: #00d4aa; color: #000; transform: scale(0.96); }
  .btn-accent { background: #0f766e; color: #99f6e4; border-color: #14b8a6; }
  .btn-accent:active { background: #14b8a6; color: #000; }
  .slider-row { display: flex; flex-direction: column; gap: 6px; }
  .slider-lbl { display: flex; justify-content: space-between; font-size: 0.85rem; color: #c9d1d9; font-weight: 500; }
  .slider-val { font-family: monospace; font-weight: 700; color: #38bdf8; }
  input[type=range] { width: 100%; accent-color: #00d4aa; height: 10px; border-radius: 5px; cursor: pointer; }
  .hint-row { display: flex; justify-content: space-between; font-size: 10px; color: #64748b; margin-top: 2px; }
  .hint-row span { cursor: pointer; padding: 2px 4px; border-radius: 3px; }
  .hint-row span:hover { color: #38bdf8; background: #21262d; }
  .status-bar { font-size: 0.8rem; color: #8b949e; text-align: center; margin-top: 4px; }
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
      <div id="gesture-indicator" style="display:none; position:absolute; top:50%; left:50%; transform:translate(-50%, -50%); background:rgba(11,15,25,0.85); color:#00d4aa; padding:10px 18px; border-radius:12px; font-weight:bold; font-size:15px; pointer-events:none; border:1px solid #00d4aa; z-index:10; box-shadow:0 4px 12px rgba(0,0,0,0.5);">
        <span id="gesture-text">SWIPE</span>
      </div>
      <div style="position:absolute; bottom:8px; right:10px; background:rgba(15,23,42,0.75); padding:3px 8px; border-radius:6px; font-size:10px; color:#94a3b8; pointer-events:none; backdrop-filter:blur(4px);">
        👆 Swipe screen to move camera
      </div>
    </div>
    <div style="padding: 10px 14px; font-size: 0.85rem; color: #8b949e; display: flex; justify-content: space-between;">
      <span>Camera Feed (Port 8000)</span>
      <span id="pos-info" style="color:#00d4aa; font-weight:600;">Pan: STOPPED | Tilt: 90°</span>
    </div>
  </div>
  <div class="controls-card">
    <div class="section-title">Pan & Tilt Servo Controls</div>
    <div class="dpad">
      <div></div>
      <button class="btn" onclick="sendTiltAction('up')">▲ Up</button>
      <div></div>
      <button class="btn" onclick="triggerPanTimed('left')">◀ Left</button>
      <button class="btn btn-accent" onclick="recenter()">⨁ Center</button>
      <button class="btn" onclick="triggerPanTimed('right')">Right ▶</button>
      <div></div>
      <button class="btn" onclick="sendTiltAction('down')">▼ Down</button>
      <div></div>
    </div>
    
    <!-- Pan Slider with Spring Return to Stop -->
    <div class="slider-row">
      <div class="slider-lbl">
        <span>Pan (360° Joystick Slider)</span>
        <span id="pan-val" class="slider-val">STOPPED</span>
      </div>
      <input type="range" id="pan-slider" min="0" max="180" value="90" oninput="onPanInput(this.value)">
      <div class="hint-row">
        <span onclick="triggerPanTimed('left')">◀ Nudge Left (0.5s)</span>
        <span onclick="triggerPanStop()" style="color:#00d4aa; font-weight:bold;">⏹ STOP</span>
        <span onclick="triggerPanTimed('right')">Nudge Right (0.5s) ▶</span>
      </div>
    </div>

    <!-- Tilt 180 Positional Slider -->
    <div class="slider-row" style="margin-top: 6px;">
      <div class="slider-lbl">
        <span>Tilt Angle (0°–180° Positional)</span>
        <span id="tilt-val" class="slider-val">90° (Level)</span>
      </div>
      <input type="range" id="tilt-slider" min="0" max="180" value="90" oninput="onTiltSlider(this.value)" onchange="onTiltSlider(this.value)">
      <div class="hint-row">
        <span onclick="setTiltDirect(0)">0° Down</span>
        <span onclick="setTiltDirect(90)" style="color:#00d4aa; font-weight:bold;">90° Level</span>
        <span onclick="setTiltDirect(180)">180° Up</span>
      </div>
    </div>

    <div class="status-bar">Idle Auto-Sleep active: Motors stay completely still and silent when not touched.</div>
  </div>
</div>
<script>
const feed = document.getElementById("stream-img");
feed.src = "http://" + window.location.hostname + ":8000/stream";
feed.onerror = function() {
  feed.src = "/stream?t=" + Date.now();
};

let lastPanSent = 90;
let lastTiltSent = 90;
let panTimer = null;
let tiltTimer = null;

function post(url, data) {
  return fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(data)
  }).then(r => r.json()).then(d => {
    updateLabels(d.pan, d.tilt);
  }).catch(e => console.error(e));
}

function updateLabels(p, t) {
  if (p !== undefined) {
    let tag = (p >= 85 && p <= 95) ? 'STOPPED' : (p < 85 ? '◀ SPINNING L' : 'SPINNING R ▶');
    document.getElementById('pan-val').innerText = tag;
    document.getElementById('pan-slider').value = p;
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
  const pText = (curP >= 85 && curP <= 95) ? 'STOPPED' : (curP < 85 ? 'SPIN L' : 'SPIN R');
  document.getElementById('pos-info').innerText = 'Pan: ' + pText + ' | Tilt: ' + curT + '°';
}

function sendTiltAction(act) { post('/api/tilt', {action: act}); }

function triggerPanTimed(dir) {
  post('/api/pan', {action: dir, duration: 0.5});
  setTimeout(() => { updateLabels(90, undefined); }, 550);
}

function triggerPanStop() {
  updateLabels(90, undefined);
  post('/api/pan', {angle: 90});
}

function setTiltDirect(val) {
  updateLabels(undefined, parseInt(val));
  post('/api/tilt', {angle: parseInt(val)});
}

function onPanInput(val) {
  const p = parseInt(val);
  updateLabels(p, undefined);
  if (panTimer) clearTimeout(panTimer);
  panTimer = setTimeout(() => {
    post('/api/pan', {angle: p});
  }, 35);
}

// Auto-Spring Return to STOP on finger/mouse release
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
  tiltTimer = setTimeout(() => {
    post('/api/tilt', {angle: t});
  }, 35);
}

function recenter() {
  updateLabels(90, 90);
  post('/api/pan', {angle: 90});
  post('/api/tilt', {angle: 90});
}

window.addEventListener('keydown', e => {
  if (e.repeat) return;
  if (e.key === 'w' || e.key === 'ArrowUp') sendTiltAction('up');
  else if (e.key === 's' || e.key === 'ArrowDown') sendTiltAction('down');
  else if (e.key === 'a' || e.key === 'ArrowLeft') triggerPanTimed('left');
  else if (e.key === 'd' || e.key === 'ArrowRight') triggerPanTimed('right');
  else if (e.key === ' ') recenter();
});

// --- Screen Touching / Swipe Camera Movement ---
const touchArea = document.getElementById("video-touch-area");
const indicator = document.getElementById("gesture-indicator");
const indicatorText = document.getElementById("gesture-text");

let touchStartX = 0, touchStartY = 0;
let isTouching = false;
let currentSwipeAction = null;
let tiltThrottleTimer = null;

function showIndicator(txt) {
  indicatorText.innerText = txt;
  indicator.style.display = "block";
}
function hideIndicator() {
  indicator.style.display = "none";
}

function handleSwipeMove(curX, curY) {
  const dx = curX - touchStartX;
  const dy = curY - touchStartY;
  
  if (Math.abs(dx) > Math.abs(dy)) {
    // Horizontal swipe -> Pan Left / Right
    if (dx < -25) {
      if (currentSwipeAction !== 'left') {
        currentSwipeAction = 'left';
        showIndicator('◀ PAN LEFT');
        post('/api/pan', {action: 'left'});
        updateLabels(40, undefined);
      }
    } else if (dx > 25) {
      if (currentSwipeAction !== 'right') {
        currentSwipeAction = 'right';
        showIndicator('PAN RIGHT ▶');
        post('/api/pan', {action: 'right'});
        updateLabels(140, undefined);
      }
    }
  } else {
    // Vertical swipe -> Tilt Up / Down
    if (dy < -20) {
      if (!tiltThrottleTimer) {
        tiltThrottleTimer = setTimeout(() => { tiltThrottleTimer = null; }, 110);
        const nextTilt = Math.min(180, lastTiltSent + 5);
        showIndicator('▲ TILT UP (' + nextTilt + '°)');
        updateLabels(undefined, nextTilt);
        post('/api/tilt', {angle: nextTilt});
      }
    } else if (dy > 20) {
      if (!tiltThrottleTimer) {
        tiltThrottleTimer = setTimeout(() => { tiltThrottleTimer = null; }, 110);
        const nextTilt = Math.max(0, lastTiltSent - 5);
        showIndicator('▼ TILT DOWN (' + nextTilt + '°)');
        updateLabels(undefined, nextTilt);
        post('/api/tilt', {angle: nextTilt});
      }
    }
  }
}

function handleSwipeEnd() {
  if (isTouching) {
    isTouching = false;
    hideIndicator();
    if (currentSwipeAction === 'left' || currentSwipeAction === 'right') {
      currentSwipeAction = null;
      post('/api/pan', {action: 'stop'});
      updateLabels(90, undefined);
    }
  }
}

touchArea.addEventListener('touchstart', e => {
  if (e.touches.length === 1) {
    touchStartX = e.touches[0].clientX;
    touchStartY = e.touches[0].clientY;
    isTouching = true;
    currentSwipeAction = null;
  }
}, {passive: false});

touchArea.addEventListener('touchmove', e => {
  if (!isTouching || e.touches.length !== 1) return;
  e.preventDefault();
  handleSwipeMove(e.touches[0].clientX, e.touches[0].clientY);
}, {passive: false});

touchArea.addEventListener('touchend', handleSwipeEnd);
touchArea.addEventListener('touchcancel', handleSwipeEnd);

// Mouse dragging support on desktop:
touchArea.addEventListener('mousedown', e => {
  touchStartX = e.clientX;
  touchStartY = e.clientY;
  isTouching = true;
  currentSwipeAction = null;
});

window.addEventListener('mousemove', e => {
  if (!isTouching) return;
  handleSwipeMove(e.clientX, e.clientY);
});

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
            resp = {"tilt": current_tilt, "pan": current_pan, "status": "ok"}
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
                    if not chunk: break
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

        self.send_error(404)

def run():
    server = ThreadingHTTPServer(("0.0.0.0", 8080), RequestHandler)
    print("FishFinder Arm Controller running on port 8080 (Idle Auto-Sleep active)...")
    try:
        server.serve_forever()
    finally:
        lgpio.gpiochip_close(h)

if __name__ == "__main__":
    run()
