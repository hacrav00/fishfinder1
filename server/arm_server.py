#!/usr/bin/env python3
import os, sys, json, time, threading, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import lgpio

PAN_PIN = 18    # GPIO 18 (Pan - Left/Right)
TILT_PIN = 19   # GPIO 19 (Tilt - Up/Down)

try:
    h = lgpio.gpiochip_open(0)
except Exception:
    h = lgpio.gpiochip_open(4)

lgpio.gpio_claim_output(h, PAN_PIN)
lgpio.gpio_claim_output(h, TILT_PIN)

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
    try:
        lgpio.tx_pulse(h, pin, 0, 0)
    except Exception:
        pass

# Full 180° Servo Range Calibration (500µs..2500µs centered at 1500µs)
def deg_to_pw(deg):
    clamped = max(0.0, min(180.0, float(deg)))
    return int(round(500.0 + (clamped / 180.0) * 2000.0))

def set_pan(deg_or_action):
    global current_pan, pan_sleep_timer
    with arm_lock:
        if pan_sleep_timer:
            pan_sleep_timer.cancel()
            pan_sleep_timer = None
        if isinstance(deg_or_action, str):
            act = deg_or_action.lower()
            if act == 'left': target_deg = max(0, current_pan - 10)
            elif act == 'right': target_deg = min(180, current_pan + 10)
            elif act in ('center', 'recenter'): target_deg = 90
            else:
                stop_pwm(PAN_PIN)
                return
            current_pan = target_deg
        else:
            current_pan = max(0, min(180, int(round(float(deg_or_action) / 10.0) * 10)))
        pw = deg_to_pw(current_pan)
        try:
            lgpio.tx_servo(h, PAN_PIN, pw, 50)
        except Exception:
            pass
        save_state(current_pan, current_tilt)
        def _sleep(): stop_pwm(PAN_PIN)
        pan_sleep_timer = threading.Timer(0.7, _sleep)
        pan_sleep_timer.daemon = True
        pan_sleep_timer.start()

def set_tilt(deg_or_action):
    global current_tilt, tilt_sleep_timer
    with arm_lock:
        if tilt_sleep_timer:
            tilt_sleep_timer.cancel()
            tilt_sleep_timer = None
        if isinstance(deg_or_action, str):
            act = deg_or_action.lower()
            if act in ('up', 'hold_up'): target_deg = min(180, current_tilt + 10)
            elif act in ('down', 'hold_down'): target_deg = max(0, current_tilt - 10)
            elif act in ('center', 'recenter'): target_deg = 90
            else:
                stop_pwm(TILT_PIN)
                return
            current_tilt = target_deg
        else:
            current_tilt = max(0, min(180, int(round(float(deg_or_action) / 10.0) * 10)))
        pw = deg_to_pw(current_tilt)
        try:
            lgpio.tx_servo(h, TILT_PIN, pw, 50)
        except Exception:
            pass
        save_state(current_pan, current_tilt)
        def _sleep(): stop_pwm(TILT_PIN)
        tilt_sleep_timer = threading.Timer(0.7, _sleep)
        tilt_sleep_timer.daemon = True
        tilt_sleep_timer.start()

# Clean boot: silent 0V ground, never twitch or snap to 90
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
        b = json.loads(self.rfile.read(l).decode()) if l else {}
        if self.path == '/api/pan':
            set_pan(b.get('angle', b.get('action', 'stop')))
            self.send_response(200); self._c(); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok', 'pan': current_pan, 'tilt': current_tilt}).encode())
            return
        if self.path == '/api/tilt':
            set_tilt(b.get('angle', b.get('action', 'stop')))
            self.send_response(200); self._c(); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok', 'pan': current_pan, 'tilt': current_tilt}).encode())
            return
        self.send_error(404)

if __name__ == '__main__':
    s = ThreadingHTTPServer(('0.0.0.0', 8080), H)
    print('FishFinder Arm running on 8080 (Pan & Tilt Symmetric Smooth Mode)...')
    try: s.serve_forever()
    finally: lgpio.gpiochip_close(h)
