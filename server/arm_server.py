#!/usr/bin/env python3
import os, sys, json, time, threading, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import lgpio

PAN_PIN = 18    # GPIO 18 (Pan)
TILT_PIN = 19   # GPIO 19 (Tilt)

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
target_tilt = float(current_tilt)
tilt_mode = 'idle'   # 'idle', 'target', 'continuous_up', 'continuous_down'
tilt_quiet_timer = None
pan_sleep_timer = None
last_pw_sent = -1

# CCPM Servo Tester standard calibration (800..2200us, 1500us center)
def deg_to_pw(deg):
    clamped = max(0.0, min(180.0, float(deg)))
    return int(round(800.0 + (clamped / 180.0) * 1400.0))

def stop_pwm(pin):
    try:
        lgpio.tx_pulse(h, pin, 0, 0)
    except Exception:
        pass

TILT_SLEW = 55.0   # deg/sec - fluid analog gliding speed
TILT_DT = 0.02     # 50 Hz PWM period (20ms)

def _silence():
    global last_pw_sent, tilt_mode
    with arm_lock:
        stop_pwm(TILT_PIN)
        last_pw_sent = -1
        tilt_mode = 'idle'

def _tilt_loop():
    global current_tilt, target_tilt, tilt_mode, tilt_quiet_timer, last_pw_sent
    while True:
        with arm_lock:
            if tilt_mode == 'continuous_up':
                if current_tilt < 180.0:
                    current_tilt = min(180.0, current_tilt + TILT_SLEW * TILT_DT)
                    pw = deg_to_pw(current_tilt)
                    if pw != last_pw_sent:
                        try:
                            lgpio.tx_servo(h, TILT_PIN, pw, 50)
                            last_pw_sent = pw
                        except Exception:
                            pass
            elif tilt_mode == 'continuous_down':
                if current_tilt > 0.0:
                    current_tilt = max(0.0, current_tilt - TILT_SLEW * TILT_DT)
                    pw = deg_to_pw(current_tilt)
                    if pw != last_pw_sent:
                        try:
                            lgpio.tx_servo(h, TILT_PIN, pw, 50)
                            last_pw_sent = pw
                        except Exception:
                            pass
            elif tilt_mode == 'target':
                diff = target_tilt - current_tilt
                if abs(diff) > 0.05:
                    if tilt_quiet_timer:
                        tilt_quiet_timer.cancel()
                        tilt_quiet_timer = None
                    step = TILT_SLEW * TILT_DT
                    current_tilt = target_tilt if abs(diff) <= step else current_tilt + (step if diff > 0 else -step)
                    pw = deg_to_pw(current_tilt)
                    if pw != last_pw_sent:
                        try:
                            lgpio.tx_servo(h, TILT_PIN, pw, 50)
                            last_pw_sent = pw
                        except Exception:
                            pass
                else:
                    if tilt_quiet_timer is None and last_pw_sent != -1:
                        tilt_quiet_timer = threading.Timer(0.8, _silence)
                        tilt_quiet_timer.daemon = True
                        tilt_quiet_timer.start()
        time.sleep(TILT_DT)

threading.Thread(target=_tilt_loop, daemon=True).start()

def set_tilt_target(deg):
    global target_tilt, tilt_mode, tilt_quiet_timer
    c = max(0.0, min(180.0, float(deg)))
    with arm_lock:
        if tilt_quiet_timer:
            tilt_quiet_timer.cancel()
            tilt_quiet_timer = None
        target_tilt = c
        tilt_mode = 'target'
    save_state(current_pan, c)

def set_tilt_continuous(direction):
    global tilt_mode, tilt_quiet_timer
    with arm_lock:
        if tilt_quiet_timer:
            tilt_quiet_timer.cancel()
            tilt_quiet_timer = None
        tilt_mode = 'continuous_up' if direction == 'up' else 'continuous_down'

def set_tilt_stop():
    global target_tilt, tilt_mode
    with arm_lock:
        target_tilt = current_tilt
        tilt_mode = 'target'
    save_state(current_pan, current_tilt)

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
            self.wfile.write(json.dumps({'status': 'ok', 'pan': current_pan, 'tilt': round(current_tilt, 1), 'mode': tilt_mode}).encode())
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
            self.wfile.write(json.dumps({'status': 'ok', 'pan': current_pan, 'tilt': round(current_tilt, 1)}).encode())
            return
        if self.path == '/api/tilt':
            if 'angle' in b:
                set_tilt_target(b['angle'])
            else:
                act = str(b.get('action', '')).lower()
                if act in ('hold_up', 'start_up'):
                    set_tilt_continuous('up')
                elif act in ('hold_down', 'start_down'):
                    set_tilt_continuous('down')
                elif act in ('stop', 'hold_stop'):
                    set_tilt_stop()
                elif act == 'up':
                    set_tilt_target(current_tilt + 5)
                elif act == 'down':
                    set_tilt_target(current_tilt - 5)
                elif act in ('center', 'recenter'):
                    set_tilt_target(90.0)
            self.send_response(200); self._c(); self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok', 'pan': current_pan, 'tilt': round(current_tilt, 1), 'mode': tilt_mode}).encode())
            return
        self.send_error(404)

if __name__ == '__main__':
    s = ThreadingHTTPServer(('0.0.0.0', 8080), H)
    print('FishFinder Arm running on 8080...')
    try: s.serve_forever()
    finally: lgpio.gpiochip_close(h)
