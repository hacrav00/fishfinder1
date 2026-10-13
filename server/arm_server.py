#!/usr/bin/env python3
"""
arm_server.py — FishFinder Robotic Arm & Video Hub
===================================================
Jitter-Free Dual Servo Controller with Idle Auto-Sleep:
- Tilt (GPIO 19 / Pin 35): MG90S 180° Positional Metal Gear Servo with auto-sleep after positioning
- Pan  (GPIO 18 / Pin 12): SG90 360° Continuous Rotation Servo with hardware pulse cutoff (no creep)
"""
import os
import sys
import json
import time
import threading
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import lgpio

PAN_PIN = 18    # GPIO 18, Physical Pin 12 (SG90 360° Continuous Rotation)
TILT_PIN = 19   # GPIO 19, Physical Pin 35 (MG90S 180° Positional Metal Gear)

# Initialize native lgpio driver
try:
    h = lgpio.gpiochip_open(0)
except Exception:
    h = lgpio.gpiochip_open(4)

lgpio.gpio_claim_output(h, PAN_PIN)
lgpio.gpio_claim_output(h, TILT_PIN)

STATE_FILE = os.path.expanduser("~/.fishfinder_servo_state.json")

def load_saved_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
                return float(data.get("pan", 90.0)), float(data.get("tilt", 90.0))
    except Exception:
        pass
    return 90.0, 90.0

def save_current_state(pan, tilt):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"pan": pan, "tilt": tilt}, f)
    except Exception:
        pass

saved_pan, saved_tilt = load_saved_state()
current_pan = int(saved_pan)
current_tilt = float(saved_tilt)
target_tilt = float(saved_tilt)
tilt_engaged = False  # Stays FALSE on boot: Never jumps or moves to 90° on its own!

tilt_sleep_timer = None
pan_timer = None

def stop_pwm(pin):
    """Cleanly cuts off pulses using lgpio.tx_pulse(0, 0). Eliminates jitter, buzzing, and creeping!"""
    try:
        lgpio.tx_pulse(h, pin, 0, 0)
    except Exception:
        pass

def deg_to_pw_tilt(deg):
    """Maps 0..180 degrees to 600..2400 microseconds with microsecond precision."""
    clamped = max(0.0, min(180.0, float(deg)))
    return int(round(600.0 + (clamped / 180.0) * 1800.0))

# ── Smooth Tilt Motion Controller (Servo Tester Behavior) ────────────────────
# MG90S 180° Positional Metal Gear Servo on GPIO 19
# Slew rate: 70 deg/s (~1.4° per 20ms step) delivers fluid, cinematic gliding motion.
# Holds constant 50 Hz PWM carrier wave without cutting off power once active, preventing camera sag & snap-back.
TILT_SLEW_SPEED = 70.0   # deg/sec
TILT_TICK_HZ = 50        # 50 Hz matches standard 20ms servo PWM period
TILT_DT = 1.0 / TILT_TICK_HZ

def _tilt_interpolator_loop():
    global current_tilt, target_tilt, tilt_engaged
    last_pw_sent = -1

    while True:
        with arm_lock:
            # Only drive servo once user commands it (prevents boot snap to 90°)
            if tilt_engaged:
                diff = target_tilt - current_tilt
                if abs(diff) > 0.05:
                    step = TILT_SLEW_SPEED * TILT_DT
                    if abs(diff) <= step:
                        current_tilt = target_tilt
                    else:
                        current_tilt += (step if diff > 0 else -step)

                pw = deg_to_pw_tilt(current_tilt)
                if pw != last_pw_sent:
                    try:
                        lgpio.tx_servo(h, TILT_PIN, pw, 50)
                        last_pw_sent = pw
                    except Exception:
                        pass
        time.sleep(TILT_DT)

# Start background interpolator thread immediately
_tilt_thread = threading.Thread(target=_tilt_interpolator_loop, daemon=True)
_tilt_thread.start()

def set_tilt(deg):
    """
    Commands target tilt angle (0°–180°).
    Smoothly glides from current position to target without velocity shock or jitter,
    and maintains solid continuous PWM holding torque to prevent gravity sag.
    """
    global target_tilt, tilt_engaged
    raw = float(deg)
    clamped = max(0.0, min(180.0, raw))
    with arm_lock:
        target_tilt = clamped
        tilt_engaged = True
    save_current_state(current_pan, clamped)

pan_sleep_timer = None

def set_pan(deg_or_action, duration=None, left_pw=600, right_pw=2400):
    """
    GPIO 18: Pan Servo Controller (Capable of full 0°–180° positional rotation):
    - When given an angle (0°–180°): Drives to target position in 10° steps, holds for 0.7s
      so the motor smoothly completes transit, then auto-sleeps via stop_pwm to eliminate jitter/buzzing.
    - When given an action ('left', 'right'): Steps by 10°, drives to target position, holds for 0.7s, auto-sleeps.
    - Stop ('stop'): Cuts PWM pulses completely via stop_pwm.
    """
    global current_pan, pan_sleep_timer
    with arm_lock:
        if pan_sleep_timer:
            pan_sleep_timer.cancel()
            pan_sleep_timer = None

        if isinstance(deg_or_action, str):
            act = deg_or_action.lower()
            if act == "left":
                target_deg = max(0, current_pan - 10)
                current_pan = target_deg
                pw = deg_to_pw_tilt(target_deg)
            elif act == "right":
                target_deg = min(180, current_pan + 10)
                current_pan = target_deg
                pw = deg_to_pw_tilt(target_deg)
            elif act in ("center", "recenter"):
                current_pan = 90
                pw = deg_to_pw_tilt(90)
            else: # "stop"
                stop_pwm(PAN_PIN)
                return
        else:
            deg = float(deg_or_action)
            clean_deg = int(round(deg / 10.0) * 10)
            clean_deg = max(0, min(180, clean_deg))
            current_pan = clean_deg
            pw = deg_to_pw_tilt(clean_deg)

        lgpio.tx_servo(h, PAN_PIN, pw, 50)
        save_current_state(current_pan, current_tilt)

        def _sleep():
            stop_pwm(PAN_PIN)

        pan_sleep_timer = threading.Timer(0.7, _sleep)
        pan_sleep_timer.daemon = True
        pan_sleep_timer.start()

# Initialize Pan and Tilt to quiet/off on startup (No auto-move to 90°)
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
    <div class="section-title">Pan & Tilt Servo Controls (Smooth Continuous Gliding)</div>
    <div class="dpad">
      <div></div>
      <button class="btn" onclick="sendTiltAction('up')">▲ 5° Up</button>
      <div></div>
      <button class="btn" onclick="sendPanAction('left')">◀ 10° Left</button>
      <button class="btn btn-accent" onclick="recenter()">⨁ Center (90°)</button>
      <button class="btn" onclick="sendPanAction('right')">10° Right ▶</button>
      <div></div>
      <button class="btn" onclick="sendTiltAction('down')">▼ 5° Down</button>
      <div></div>
    </div>
    
    <!-- Pan Positional Slider (0°–180° in 10° steps) -->
    <div class="slider-row">
      <div class="slider-lbl">
        <span>Pan Position (0°–180° Full Rotation)</span>
        <span id="pan-val" class="slider-val">90° (Center)</span>
      </div>
      <input type="range" id="pan-slider" min="0" max="180" step="10" value="90" oninput="onPanInput(this.value)">
      <div class="hint-row">
        <span onclick="setPanDirect(0)">0° Full Left</span>
        <span onclick="setPanDirect(90)" style="color:#00d4aa; font-weight:bold;">90° Center</span>
        <span onclick="setPanDirect(180)">180° Full Right</span>
      </div>
    </div>

    <!-- Tilt Positional Slider (0°–180° Continuous 1° Precision) -->
    <div class="slider-row" style="margin-top: 6px;">
      <div class="slider-lbl">
        <span>Tilt Angle (0°–180° Smooth Slew)</span>
        <span id="tilt-val" class="slider-val">90° (Level)</span>
      </div>
      <input type="range" id="tilt-slider" min="0" max="180" step="1" value="90" oninput="onTiltSlider(this.value)">
      <div class="hint-row">
        <span onclick="setTiltDirect(0)">0° Full Down</span>
        <span onclick="setTiltDirect(90)" style="color:#00d4aa; font-weight:bold;">90° Level</span>
        <span onclick="setTiltDirect(180)">180° Full Up</span>
      </div>
    </div>

    <div class="status-bar">Smooth Slew-Rate Controller: Hardware PWM holding torque active, completely fluid and jitter-free.</div>
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
    let cleanP = Math.round(p / 10) * 10;
    let tag = cleanP == 90 ? ' (Center)' : (cleanP < 90 ? ' (' + (90 - cleanP) + '° Left)' : ' (' + (cleanP - 90) + '° Right)');
    document.getElementById('pan-val').innerText = cleanP + '°' + tag;
    document.getElementById('pan-slider').value = cleanP;
    lastPanSent = cleanP;
  }
  if (t !== undefined) {
    let cleanT = Math.round(t);
    let tag = cleanT == 90 ? ' (Level)' : (cleanT < 90 ? ' (' + (90 - cleanT) + '° Down)' : ' (' + (cleanT - 90) + '° Up)');
    document.getElementById('tilt-val').innerText = cleanT + '°' + tag;
    document.getElementById('tilt-slider').value = cleanT;
    lastTiltSent = cleanT;
  }
  const curP = p !== undefined ? p : lastPanSent;
  const curT = t !== undefined ? t : lastTiltSent;
  document.getElementById('pos-info').innerText = 'Pan: ' + curP + '° | Tilt: ' + curT + '°';
}

function sendPanAction(dir) {
  const nextPan = dir === 'left' ? Math.max(0, lastPanSent - 10) : Math.min(180, lastPanSent + 10);
  setPanDirect(nextPan);
}

function sendTiltAction(act) {
  const nextTilt = act === 'up' ? Math.min(180, lastTiltSent + 5) : Math.max(0, lastTiltSent - 5);
  setTiltDirect(nextTilt);
}

function setPanDirect(val) {
  const cleanP = Math.max(0, Math.min(180, Math.round(parseInt(val) / 10) * 10));
  updateLabels(cleanP, undefined);
  post('/api/pan', {angle: cleanP});
}

function setTiltDirect(val) {
  const cleanT = Math.max(0, Math.min(180, Math.round(parseFloat(val))));
  updateLabels(undefined, cleanT);
  post('/api/tilt', {angle: cleanT});
}

function onPanInput(val) {
  const p = Math.round(parseInt(val) / 10) * 10;
  updateLabels(p, undefined);
  if (panTimer) clearTimeout(panTimer);
  panTimer = setTimeout(() => {
    post('/api/pan', {angle: p});
  }, 40);
}

function onTiltSlider(val) {
  const t = Math.max(0, Math.min(180, Math.round(parseFloat(val))));
  updateLabels(undefined, t);
  if (tiltTimer) clearTimeout(tiltTimer);
  tiltTimer = setTimeout(() => {
    post('/api/tilt', {angle: t});
  }, 35);
}

function recenter() {
  setPanDirect(90);
  setTiltDirect(90);
}

window.addEventListener('keydown', e => {
  if (e.repeat) return;
  if (e.key === 'w' || e.key === 'ArrowUp') sendTiltAction('up');
  else if (e.key === 's' || e.key === 'ArrowDown') sendTiltAction('down');
  else if (e.key === 'a' || e.key === 'ArrowLeft') sendPanAction('left');
  else if (e.key === 'd' || e.key === 'ArrowRight') sendPanAction('right');
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
    if (Math.abs(dx) > 28 && !currentSwipeAction) {
      currentSwipeAction = dx < 0 ? 'left' : 'right';
      sendPanAction(currentSwipeAction);
      showIndicator(currentSwipeAction === 'left' ? '◀ 10° LEFT' : '10° RIGHT ▶');
    }
  } else {
    if (Math.abs(dy) > 28 && !tiltThrottleTimer) {
      tiltThrottleTimer = setTimeout(() => { tiltThrottleTimer = null; }, 180);
      const act = dy < 0 ? 'up' : 'down';
      sendTiltAction(act);
      showIndicator(act === 'up' ? '▲ 10° UP' : '▼ 10° DOWN');
    }
  }
}

function handleSwipeEnd() {
  if (isTouching) {
    isTouching = false;
    currentSwipeAction = null;
    setTimeout(hideIndicator, 350);
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

        if self.path.startswith("/snapshot"):
            try:
                upstream = urllib.request.urlopen("http://127.0.0.1:8000/snapshot", timeout=3)
                self.send_response(200)
                self._send_cors()
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.end_headers()
                self.wfile.write(upstream.read())
            except Exception:
                self.send_error(502, "Snapshot unavailable")
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
                    chunk = upstream.read(4096)
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
            dur = float(data.get("duration", 0))
            duration = dur if dur > 0 else None
            left_pw = int(data.get("left_pw", 600))
            right_pw = int(data.get("right_pw", 2400))

            if "angle" in data:
                raw_angle = float(data["angle"])
                set_pan(raw_angle, duration=duration)
            elif "action" in data:
                act = str(data["action"]).lower()
                set_pan(act, duration=duration, left_pw=left_pw, right_pw=right_pw)
            else:
                set_pan("stop")

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
                    set_tilt(target_tilt + 5)
                elif act == "down":
                    set_tilt(target_tilt - 5)
                elif act == "center":
                    set_tilt(90)

            self.send_response(200)
            self._send_cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "pan": current_pan, "tilt": round(current_tilt, 1), "target_tilt": target_tilt}).encode("utf-8"))
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
