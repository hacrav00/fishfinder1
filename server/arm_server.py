#!/usr/bin/env python3
import os, sys, json, time, threading, urllib.request, socket
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

try:
    import lgpio
except ImportError:
    lgpio = None

PAN_PIN = 18    # GPIO 18 (Pan - Left/Right)
TILT_PIN = 19   # GPIO 19 (Tilt - Up/Down, MG90S)

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

def output_servo(pin, pw):
    if lgpio and h is not None:
        try:
            lgpio.tx_servo(h, pin, int(pw), 50)
        except Exception:
            pass

def stop_pwm(pin):
    if lgpio and h is not None:
        try:
            lgpio.tx_pulse(h, pin, 0, 0)
            lgpio.gpio_write(h, pin, 0)
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

init_p, init_t = load_state()

# Target angles set by incoming HTTP requests
target_pan = float(init_p)
target_tilt = float(init_t)

# Actual interpolated angles for cinematic smooth gliding
actual_pan = float(init_p)
actual_tilt = float(init_t)

last_sent_pan_pw = -1
last_sent_tilt_pw = -1

# Telemetry broadcast socket (UDP 8888)
udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    udp_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
except Exception:
    pass

# Persistent hardware serial handles for GPIO 14/15 UART (/dev/serial0) and USB
serial_fds = []
for sp in ['/dev/serial0', '/dev/ttyUSB0', '/dev/ttyACM0']:
    if os.path.exists(sp):
        try:
            os.system(f"stty -F {sp} 115200 raw -echo 2>/dev/null")
            f = open(sp, 'w', buffering=1)
            serial_fds.append(f)
        except Exception:
            pass

# Strict standard RC pulse mapping (NEVER exceeds 2000µs, NEVER drops below 1000µs)
# 0° = 1000µs, 90° = 1500µs (Center / Level), 180° = 2000µs
def pan_deg_to_pw(deg):
    clamped = max(0.0, min(180.0, float(deg)))
    pw = int(round(1000.0 + (clamped / 180.0) * 1000.0))
    return max(1000, min(2000, pw))

def tilt_deg_to_pw(deg):
    # Safe mechanical bounds for camera gimbal: 1050µs (Down) to 1950µs (Up), Center = 1500µs
    clamped = max(10.0, min(170.0, float(deg)))
    pw = int(round(1000.0 + (clamped / 180.0) * 1000.0))
    return max(1050, min(1950, pw))

def deg_to_pw(deg):
    return pan_deg_to_pw(deg)

def broadcast_telemetry(p_deg, t_deg, p_pw, t_pw):
    line = f"Pan_Angle:{p_deg},Tilt_Angle:{t_deg},Pan_us:{p_pw},Tilt_us:{t_pw}\r\n"
    print(f"[PLOTTER] {line.strip()}", flush=True)

    # 1. UDP broadcast
    try:
        udp_sock.sendto(line.encode('ascii'), ('<broadcast>', 8888))
    except Exception:
        try:
            udp_sock.sendto(line.encode('ascii'), ('255.255.255.255', 8888))
        except Exception:
            pass

    # 2. Hardware UART to ESP32 (GPIO 14 TX / GPIO 15 RX)
    for sf in serial_fds:
        try:
            sf.write(line)
            sf.flush()
        except Exception:
            pass

# ── CINEMATIC SMOOTH MOTION INTERPOLATION THREAD ──────────────────────────────
def motion_loop():
    """
    Dedicated 30Hz motion controller:
    - Decouples HTTP request rate from physical PWM generation.
    - Smoothly glides angles toward targets using exponential smoothing.
    - Eliminates pulse chopping, mechanical jumps, and voltage surge spikes.
    """
    global actual_pan, actual_tilt, last_sent_pan_pw, last_sent_tilt_pw
    last_save_time = time.time()

    while True:
        time.sleep(0.035)  # 28 updates/sec

        pan_moved = False
        tilt_moved = False

        with arm_lock:
            # Smooth Pan
            p_diff = target_pan - actual_pan
            if abs(p_diff) > 0.4:
                step = max(-4.5, min(4.5, p_diff * 0.4))
                actual_pan += step
                pan_moved = True
            elif abs(p_diff) > 0:
                actual_pan = target_pan
                pan_moved = True

            # Smooth Tilt
            t_diff = target_tilt - actual_tilt
            if abs(t_diff) > 0.4:
                step = max(-4.5, min(4.5, t_diff * 0.4))
                actual_tilt += step
                tilt_moved = True
            elif abs(t_diff) > 0:
                actual_tilt = target_tilt
                tilt_moved = True

        if pan_moved or tilt_moved:
            p_pw = pan_deg_to_pw(actual_pan)
            t_pw = tilt_deg_to_pw(actual_tilt)

            if p_pw != last_sent_pan_pw:
                output_servo(PAN_PIN, p_pw)
                last_sent_pan_pw = p_pw

            if t_pw != last_sent_tilt_pw:
                output_servo(TILT_PIN, t_pw)
                last_sent_tilt_pw = t_pw

            p_round = int(round(actual_pan))
            t_round = int(round(actual_tilt))
            broadcast_telemetry(p_round, t_round, p_pw, t_pw)

            if time.time() - last_save_time > 2.0:
                save_state(p_round, t_round)
                last_save_time = time.time()

# Start background motion thread
motion_thread = threading.Thread(target=motion_loop, daemon=True)
motion_thread.start()

# Initialize servos to saved position
init_p_pw = pan_deg_to_pw(actual_pan)
init_t_pw = tilt_deg_to_pw(actual_tilt)
output_servo(PAN_PIN, init_p_pw)
output_servo(TILT_PIN, init_t_pw)
last_sent_pan_pw = init_p_pw
last_sent_tilt_pw = init_t_pw
broadcast_telemetry(int(actual_pan), int(actual_tilt), init_p_pw, init_t_pw)

def set_pan_target(data):
    global target_pan
    with arm_lock:
        if isinstance(data, dict):
            act = str(data.get('action', '')).lower()
            if 'step' in data and not act:
                act = 'right' if float(data['step']) > 0 else 'left'
            if act in ('left', 'step_left', 'pan_left'):
                target_pan = max(0.0, target_pan - 10.0)
            elif act in ('right', 'step_right', 'pan_right'):
                target_pan = min(180.0, target_pan + 10.0)
            elif act in ('center', 'recenter', 'level'):
                target_pan = 90.0
            elif 'angle' in data:
                try: target_pan = max(0.0, min(180.0, float(data['angle'])))
                except: pass
        elif isinstance(data, (int, float)):
            target_pan = max(0.0, min(180.0, float(data)))
        elif isinstance(data, str):
            try: target_pan = max(0.0, min(180.0, float(data)))
            except:
                act = data.lower()
                if 'left' in act: target_pan = max(0.0, target_pan - 10.0)
                elif 'right' in act: target_pan = min(180.0, target_pan + 10.0)
                elif 'center' in act: target_pan = 90.0

def set_tilt_target(data):
    global target_tilt
    with arm_lock:
        if isinstance(data, dict):
            act = str(data.get('action', '')).lower()
            if 'step' in data and not act:
                act = 'up' if float(data['step']) > 0 else 'down'
            if act in ('up', 'hold_up', 'step_up', 'tilt_up'):
                target_tilt = min(170.0, target_tilt + 10.0)
            elif act in ('down', 'hold_down', 'step_down', 'tilt_down'):
                target_tilt = max(10.0, target_tilt - 10.0)
            elif act in ('center', 'recenter', 'level'):
                target_tilt = 90.0
            elif 'angle' in data:
                try: target_tilt = max(10.0, min(170.0, float(data['angle'])))
                except: pass
        elif isinstance(data, (int, float)):
            target_tilt = max(10.0, min(170.0, float(data)))
        elif isinstance(data, str):
            try: target_tilt = max(10.0, min(170.0, float(data)))
            except:
                act = data.lower()
                if 'up' in act: target_tilt = min(170.0, target_tilt + 10.0)
                elif 'down' in act: target_tilt = max(10.0, target_tilt - 10.0)
                elif 'center' in act: target_tilt = 90.0

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _c(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
    def do_OPTIONS(self):
        self.send_response(200); self._c(); self.end_headers()
    def do_GET(self):
        if self.path in ('/api/status', '/status', '/api/telemetry', '/telemetry'):
            self.send_response(200); self._c(); self.send_header('Content-Type', 'application/json'); self.end_headers()
            p_deg = int(round(actual_pan))
            t_deg = int(round(actual_tilt))
            payload = {
                'status': 'ok',
                'pan': p_deg,
                'tilt': t_deg,
                'pan_pw': deg_to_pw(actual_pan),
                'tilt_pw': deg_to_pw(actual_tilt),
                'plotter': f"Pan_Angle:{p_deg},Tilt_Angle:{t_deg},Pan_us:{deg_to_pw(actual_pan)},Tilt_us:{deg_to_pw(actual_tilt)}"
            }
            self.wfile.write(json.dumps(payload).encode())
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
        # Serve mobile web app UI
        for html_path in ['/home/i4mt/www/index.html', '/home/i4mt/index.html', os.path.join(os.path.dirname(__file__), 'index.html')]:
            if os.path.exists(html_path):
                try:
                    with open(html_path, 'rb') as hf:
                        content = hf.read()
                    self.send_response(200); self._c(); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers()
                    self.wfile.write(content)
                    return
                except Exception: pass
        self.send_response(200); self._c(); self.end_headers(); self.wfile.write(b'FishFinder Arm Server OK')
    def do_POST(self):
        l = int(self.headers.get('Content-Length', 0))
        b = json.loads(self.rfile.read(l).decode('utf-8')) if l else {}
        if self.path == '/api/pan':
            set_pan_target(b)
            self.send_response(200); self._c(); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok', 'pan': int(round(target_pan)), 'tilt': int(round(target_tilt))}).encode())
            return
        if self.path == '/api/tilt':
            set_tilt_target(b)
            self.send_response(200); self._c(); self.send_header('Content-Type', 'application/json'); self.end_headers()
            self.wfile.write(json.dumps({'status': 'ok', 'pan': int(round(target_pan)), 'tilt': int(round(target_tilt))}).encode())
            return
        self.send_error(404)

if __name__ == '__main__':
    s = ThreadingHTTPServer(('0.0.0.0', 8080), H)
    print('FishFinder Arm running on 8080 (Cinematic Smooth Glide Active)...', flush=True)
    try: s.serve_forever()
    finally:
        if lgpio and h is not None:
            lgpio.gpiochip_close(h)
