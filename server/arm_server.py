#!/usr/bin/env python3
import os, sys, json, time, threading, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
try:
    import lgpio
except ImportError:
    lgpio = None

PAN_PIN = 18    # GPIO 18 (Pan - Left/Right, 360° Continuous or 180° Positional)
TILT_PIN = 19   # GPIO 19 (Tilt - Up/Down, MG90S Positional)

h = None
if lgpio:
    try:
        h = lgpio.gpiochip_open(0)
    except Exception:
        try:
            h = lgpio.gpiochip_open(4)
        except Exception:
            h = None

    if h is not None:
        try:
            lgpio.gpio_claim_output(h, PAN_PIN)
            lgpio.gpio_claim_output(h, TILT_PIN)
        except Exception:
            pass

arm_lock = threading.Lock()
STATE_FILE = os.path.expanduser('~/.fishfinder_servo_state.json')

def load_state():
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                d = json.load(f)
                return float(d.get('pan', 90.0)), float(d.get('tilt', 90.0))
    except Exception:
        pass
    return 90.0, 90.0

def save_state(p, t):
    try:
        with open(STATE_FILE, 'w') as f:
            json.dump({'pan': p, 'tilt': t}, f)
    except Exception:
        pass

current_pan, current_tilt = load_state()
current_pan = int(current_pan)
current_tilt = int(current_tilt)

pan_sleep_timer = None
tilt_sleep_timer = None

def stop_pwm(pin):
    if lgpio and h is not None:
        try:
            lgpio.tx_pulse(h, pin, 0, 0)
        except Exception:
            pass

# Full Range Pulse Mapping for MG90S Positional Tilt (500µs - 2500µs)
def deg_to_pw(deg):
    clamped = max(0.0, min(180.0, float(deg)))
    return int(round(500.0 + (clamped / 180.0) * 2000.0))

def set_pan(data):
    """
    Smooth, full-range Pan control:
    - Supports 10° stepping for 360° continuous rotation or positional servos.
    - Uses ultra-clean 32ms timed pulse burst per 10° step, then cuts PWM to 0V.
    - Zero hunting vibration, zero buzzing, unlimited full rotation!
    """
    global current_pan, pan_sleep_timer
    with arm_lock:
        if pan_sleep_timer:
            pan_sleep_timer.cancel()
            pan_sleep_timer = None

        action = None
        target_deg = None

        if isinstance(data, dict):
            action = data.get('action')
            if 'step' in data and not action:
                s = float(data['step'])
                action = 'right' if s > 0 else 'left'
            elif 'angle' in data:
                try: target_deg = float(data['angle'])
                except Exception: pass
        elif isinstance(data, str):
            try:
                target_deg = float(data)
            except ValueError:
                action = data.lower()
        elif isinstance(data, (int, float)):
            target_deg = float(data)

        if action:
            act = action.lower()
            if act in ('left', 'step_left'):
                current_pan = (current_pan - 10) % 360
                if lgpio and h is not None:
                    try: lgpio.tx_servo(h, PAN_PIN, 600, 50)
                    except Exception: pass
                time.sleep(0.032) # Clean 32ms pulse = 10° step
                stop_pwm(PAN_PIN)
            elif act in ('right', 'step_right'):
                current_pan = (current_pan + 10) % 360
                if lgpio and h is not None:
                    try: lgpio.tx_servo(h, PAN_PIN, 2400, 50)
                    except Exception: pass
                time.sleep(0.032) # Clean 32ms pulse = 10° step
                stop_pwm(PAN_PIN)
            elif act in ('center', 'recenter'):
                current_pan = 90
                stop_pwm(PAN_PIN)
            else:
                stop_pwm(PAN_PIN)
                return
        elif target_deg is not None:
            # Handle direct angle command or slider seek
            diff = int(target_deg) - (current_pan % 180)
            if abs(diff) >= 5:
                pulse_pw = 2400 if diff > 0 else 600
                burst = max(0.025, min(0.35, abs(diff) * 0.0032))
                if lgpio and h is not None:
                    try: lgpio.tx_servo(h, PAN_PIN, pulse_pw, 50)
                    except Exception: pass
                time.sleep(burst)
                stop_pwm(PAN_PIN)
            current_pan = int(target_deg)

        save_state(current_pan, current_tilt)

def set_tilt(data):
    """
    Smooth, full-range Tilt control:
    - Supports 10° incremental stepping across full physical range.
    - Direct angle support (0° Down - 90° Level - 180° Up).
    - Auto-sleeps after 0.7s to completely eliminate 50 Hz hunting vibration and buzzing.
    """
    global current_tilt, tilt_sleep_timer
    with arm_lock:
        if tilt_sleep_timer:
            tilt_sleep_timer.cancel()
            tilt_sleep_timer = None

        action = None
        target_deg = None

        if isinstance(data, dict):
            action = data.get('action')
            if 'step' in data and not action:
                s = float(data['step'])
                action = 'up' if s > 0 else 'down'
            elif 'angle' in data:
                try: target_deg = float(data['angle'])
                except Exception: pass
        elif isinstance(data, str):
            try:
                target_deg = float(data)
            except ValueError:
                action = data.lower()
        elif isinstance(data, (int, float)):
            target_deg = float(data)

        if action:
            act = action.lower()
            if act in ('up', 'hold_up', 'step_up'):
                current_tilt = min(180, current_tilt + 10)
            elif act in ('down', 'hold_down', 'step_down'):
                current_tilt = max(0, current_tilt - 10)
            elif act in ('center', 'recenter', 'level'):
                current_tilt = 90
            else:
                stop_pwm(TILT_PIN)
                return
        elif target_deg is not None:
            current_tilt = max(0, min(180, int(round(target_deg / 10.0) * 10)))

        pw = deg_to_pw(current_tilt)
        if lgpio and h is not None:
            try:
                lgpio.tx_servo(h, TILT_PIN, pw, 50)
            except Exception:
                pass

        save_state(current_pan, current_tilt)

        def _sleep():
            stop_pwm(TILT_PIN)

        tilt_sleep_timer = threading.Timer(0.7, _sleep)
        tilt_sleep_timer.daemon = True
        tilt_sleep_timer.start()

# Clean boot: silent 0V ground, never twitch or snap
stop_pwm(PAN_PIN)
stop_pwm(TILT_PIN)

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _c(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
    def do_OPTIONS(self):
        self.send_response(200); self._c(); self.end_headers()
    def do_GET(self):
        if self.path in ('/api/status', '/status'):
            self.send_response(200); self._c(); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok', 'pan': current_pan, 'tilt': current_tilt}).encode())
            return
        if self.path.startswith('/snapshot'):
            try:
                up = urllib.request.urlopen('http://127.0.0.1:8000/snapshot', timeout=3)
                self.send_response(200); self._c(); self.send_header('Content-Type', 'image/jpeg'); self.end_headers()
                self.wfile.write(up.read())
            except Exception: self.send_error(502)
            return
        if self.path.startswith('/stream'):
            try:
                up = urllib.request.urlopen('http://127.0.0.1:8000/stream', timeout=4)
                self.send_response(200); self._c()
                for k, v in up.getheaders():
                    if k.lower() not in ('server', 'transfer-encoding'): self.send_header(k, v)
                self.end_headers()
                while True:
                    c = up.read(4096)
                    if not c: break
                    self.wfile.write(c); self.wfile.flush()
            except Exception: self.send_error(502)
            return
        self.send_response(200); self._c(); self.end_headers(); self.wfile.write(b'FishFinder Arm Server OK')
    def do_POST(self):
        l = int(self.headers.get('Content-Length', 0))
        b = json.loads(self.rfile.read(l).decode('utf-8')) if l else {}
        if self.path == '/api/pan':
            set_pan(b)
            self.send_response(200); self._c(); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok', 'pan': current_pan, 'tilt': current_tilt}).encode())
            return
        if self.path == '/api/tilt':
            set_tilt(b)
            self.send_response(200); self._c(); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok', 'pan': current_pan, 'tilt': current_tilt}).encode())
            return
        self.send_error(404)

if __name__ == '__main__':
    s = ThreadingHTTPServer(('0.0.0.0', 8080), H)
    print('FishFinder Arm running on 8080 (10° Smooth Full-Rotation Engine)...')
    try: s.serve_forever()
    finally:
        if lgpio and h is not None:
            lgpio.gpiochip_close(h)
