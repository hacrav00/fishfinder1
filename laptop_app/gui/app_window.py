"""
app_window.py — PiCamPro Main Application Window
=================================================
Orchestrates:
  • Camera detection & switching
  • Live preview rendering
  • Recording, snapshots, timelapse
  • Periodic UI updates (FPS, storage, timelapse count)
  • Clean shutdown
"""

import tkinter as tk
from tkinter import ttk, messagebox
import threading
import queue
import logging
import time
import cv2
import numpy as np
from pathlib import Path
from typing import Optional, List, Tuple

from core.camera_manager import detect_all_cameras, CameraInfo
from core.recorder        import Recorder, StreamRecorder
from core.snapshot        import SnapshotEngine
from core.timelapse       import TimelapseCapturer
from core.storage         import (
    ensure_dirs, get_photo_path, get_video_path,
    get_timelapse_path, get_disk_usage, human_size, count_files,
    MEDIA_DIR, PHOTO_DIR, VIDEO_DIR, TIMELAPSE_DIR, LOG_DIR
)
from gui.preview_canvas   import PreviewCanvas
from gui.control_panel    import ControlPanel
from gui.status_bar       import StatusBar
from gui.settings_dialog  import SettingsDialog

log = logging.getLogger(__name__)

try:
    from libcamera import controls
except ImportError:
    controls = None

# ── Theme colours ────────────────────────────────────────────────────────────
C_BG     = "#0d1117"
C_PANEL  = "#161b22"
C_BORDER = "#21262d"
C_TEXT   = "#e6edf3"
C_ACCENT = "#00d4aa"

# How often (ms) the main thread updates status displays
_STATUS_INTERVAL_MS = 1500


class AppWindow:
    """
    Top-level application controller.  Creates and wires together all GUI
    components, manages the camera capture thread, and handles all user events.
    """

    def __init__(self, root: tk.Tk, client_ip: str = None):
        self._root = root
        self._client_ip = client_ip
        
        if self._client_ip:
            self._root.title(f"FishFinder — Camera Viewer ({self._client_ip})")
        else:
            self._root.title("FishFinder — Camera Viewer")
            
        self._root.configure(bg=C_BG)
        self._root.minsize(900, 560)
        self._root.geometry("1280x720")

        # Apply dark ttk theme
        self._apply_theme()

        # Ensure storage directories exist
        ensure_dirs()

        # State
        self._cameras: List[CameraInfo]      = []
        self._active_cam: Optional[CameraInfo] = None
        self._picam2                          = None   # Picamera2 instance
        self._cap: Optional[cv2.VideoCapture] = None  # V4L2 capture
        self._capture_thread: Optional[threading.Thread] = None
        self._capture_running = False
        self._current_res: Tuple[int, int]   = (1280, 720)
        self._target_fps: int                = 30
        self._snap_format: str               = "jpg"
        self._focus_mode: str                = "continuous"
        self._lens_position: float           = 0.0
        self._focus_supported: bool          = False
        self._af_range: str                  = "normal"
        self._brightness: float              = 0.0
        self._contrast: float                = 1.0
        self._saturation: float              = 1.0
        self._exposure_value: float          = 0.0
        self._awb_mode: str                  = "auto"
        self._red_gain: float                = 1.5
        self._blue_gain: float               = 1.5
        self._underwater_mode: bool          = False
        self._low_light_mode: bool           = False
        self._capture_paused: bool           = False

        # Engines
        self._recorder:  Optional[Recorder]          = None
        self._stream_recorder: StreamRecorder       = StreamRecorder()
        self._snapshooter: Optional[SnapshotEngine]  = None
        self._timelapse:  TimelapseCapturer           = TimelapseCapturer(
            self._on_timelapse_tick
        )
        self._current_lidar: Tuple[float, float, int] = (0.0, 0.0, 0)
        self._current_pixhawk: dict                  = {}

        # Build UI
        self._build_layout()

        # Bind window events
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._root.bind("<Configure>", self._on_resize)

        if self._client_ip:
            # Create a simulated CameraInfo representing the remote camera
            from core.camera_manager import CameraInfo, SensorMode
            remote_modes = [
                SensorMode(1920, 1080, 30.0, format="MJPEG"),
                SensorMode(1280, 720, 30.0, format="MJPEG"),
                SensorMode(800, 600, 30.0, format="MJPEG"),
                SensorMode(640, 480, 30.0, format="MJPEG"),
                SensorMode(640, 360, 30.0, format="MJPEG")
            ]
            self._active_cam = CameraInfo(
                index=0, name="FishFinder HD Camera",
                source="v4l2", device_path="/dev/video0",
                modes=remote_modes, max_width=1920, max_height=1080, max_fps=30.0
            )
            self._cameras = [self._active_cam]
            self._focus_supported = True
            
            # Setup snapshooter for local stream frame captures (e.g. for timelapse)
            self._snapshooter = SnapshotEngine(self._active_cam)
            
            # Update panel cameras and current IP
            self._root.after(100, lambda: self._panel.update_cameras(self._cameras))
            self._root.after(120, lambda: self._panel.set_current_ip(self._client_ip))
            self._root.after(150, lambda: self._panel.set_focus_supported(True))
            self._status.update_camera(f"Connecting to {self._remote_display_endpoint()}…")
            self._status.update_resolution(640, 480)
            
            # Start client streaming loop
            self._start_client_stream()
            
            # Start client status polling loop & thumbnail fetch & camera discovery
            self._root.after(400, self._fetch_remote_cameras)
            self._root.after(1000, self._client_status_poll)
            self._root.after(1500, self._fetch_remote_thumbnail)
            return

        # Start scanning for cameras asynchronously
        self._root.after(200, self._async_scan_cameras)

        # Periodic status refresh
        self._root.after(_STATUS_INTERVAL_MS, self._refresh_status)

    def _remote_url(self, path: str) -> str:
        ip = str(self._client_ip or "127.0.0.1").strip()
        if ":" in ip:
            base = f"http://{ip}"
        else:
            base = f"http://{ip}:8000"
        if not path.startswith("/"):
            path = "/" + path
        return f"{base}{path}"

    def _remote_display_endpoint(self) -> str:
        ip = str(self._client_ip or "127.0.0.1").strip()
        if ":" in ip:
            return ip
        return f"{ip}:8000"

    def _start_client_stream(self):
        """Starts the background thread to pull and decode the MJPEG stream from the Pi."""
        self._capture_running = True
        self._capture_thread = threading.Thread(
            target=self._client_stream_loop, daemon=True, name="client-stream"
        )
        self._capture_thread.start()

    def _client_stream_loop(self):
        import urllib.request
        import numpy as np
        
        while self._capture_running:
            url = self._remote_url("/stream")
            log.info("Client connecting to remote stream: %s", url)
            try:
                self._root.after(0, lambda ep=self._remote_display_endpoint(): self._status.update_camera(f"Connecting to FishFinder ({ep})…"))
                stream = urllib.request.urlopen(url, timeout=4)
                self._root.after(0, lambda ep=self._remote_display_endpoint(): self._status.update_camera(f"FishFinder HD Camera [Live: {ep}]"))

                try:
                    import socket
                    raw_sock = getattr(getattr(getattr(stream, "fp", None), "raw", None), "_sock", None)
                    if raw_sock:
                        raw_sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                        raw_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
                except Exception:
                    pass

                bytes_data = b''
                while self._capture_running:
                    chunk = stream.read(32768)
                    if not chunk:
                        break
                    bytes_data += chunk
                    
                    # Zero-lag buffer drain: If tether delay caused multiple frames to arrive,
                    # skip all older frames and decode only the absolute freshest frame!
                    latest_jpg = None
                    while True:
                        a = bytes_data.find(b'\xff\xd8')
                        if a == -1:
                            bytes_data = bytes_data[-1:] if bytes_data.endswith(b'\xff') else b''
                            break
                        bytes_data = bytes_data[a:]
                        b = bytes_data.find(b'\xff\xd9', 2)
                        if b == -1:
                            break
                        latest_jpg = bytes_data[:b+2]
                        bytes_data = bytes_data[b+2:]

                    if latest_jpg is None:
                        continue
                    jpg = latest_jpg
                    
                    # Decode JPEG to BGR (OpenCV with PIL fallback)
                    frame = None
                    try:
                        if cv2 is not None and np is not None:
                            frame = cv2.imdecode(np.frombuffer(jpg, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if frame is None:
                            import io
                            from PIL import Image
                            pil_img = Image.open(io.BytesIO(jpg))
                            if np is not None:
                                frame = np.array(pil_img)[:, :, ::-1].copy()
                            else:
                                frame = pil_img
                    except Exception as de:
                        log.debug("Frame decode error: %s", de)
                        continue

                    if frame is not None:
                        # ── Zero-Lag Real-time Image Enhancement (Low Light & Underwater) ──
                        if getattr(self, "_low_light_mode", False) and cv2 is not None:
                            try:
                                lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
                                l_chan, a_chan, b_chan = cv2.split(lab)
                                clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
                                cl = clahe.apply(l_chan)
                                frame = cv2.cvtColor(cv2.merge((cl, a_chan, b_chan)), cv2.COLOR_LAB2BGR)
                            except Exception:
                                pass

                        if getattr(self, "_underwater_mode", False) and cv2 is not None:
                            try:
                                b_c, g_c, r_c = cv2.split(frame)
                                r_c = cv2.multiply(r_c, 1.35)
                                frame = cv2.merge((b_c, g_c, r_c))
                            except Exception:
                                pass

                        b_val = getattr(self, "_brightness", 0.0)
                        c_val = getattr(self, "_contrast", 1.0)
                        if (b_val != 0.0 or c_val != 1.0) and cv2 is not None:
                            try:
                                frame = cv2.convertScaleAbs(frame, alpha=max(0.1, c_val), beta=int(b_val * 60))
                            except Exception:
                                pass

                        self._last_client_frame = frame

                        # If local video recording is active, feed frame into StreamRecorder with LiDAR OSD
                        if getattr(self, "_stream_recorder", None) and self._stream_recorder.is_recording:
                            l_cm, l_m, l_str = getattr(self, "_current_lidar", (0.0, 0.0, 0))
                            parts = []
                            if l_cm > 0:
                                parts.append(f"LiDAR: {int(l_cm)} cm ({l_m:.2f} m)")
                            pix = getattr(self, "_current_pixhawk", {})
                            if pix and pix.get("connected"):
                                d_m = float(pix.get("depth_m", 0.0))
                                hdg = int(pix.get("heading", 0))
                                mode = pix.get("flight_mode", "MANUAL")
                                parts.append(f"Depth: {d_m:.2f}m HDG: {hdg} deg [{mode}]")
                            osd_txt = " | ".join(parts) if parts else None
                            self._stream_recorder.write_frame(frame, osd_text=osd_txt)

                        self._preview.push_frame(frame)
            except Exception as e:
                log.warning("Lost connection to stream (%s), scanning routers...", e)
                self._root.after(0, lambda ep=self._remote_display_endpoint(): self._status.update_camera(f"🔍 Scanning routers for FishFinder ({ep})…"))
                try:
                    from fishfinder import discover_pi_ip
                    found_ip = discover_pi_ip(quick=True)
                    if found_ip:
                        self._client_ip = f"{found_ip}:8000"
                        self._root.after(0, lambda cip=self._client_ip: self._panel.set_current_ip(cip))
                except Exception:
                    pass
                time.sleep(1.5)

    def _client_status_poll(self):
        """Periodically queries remote status to update indicators."""
        if not self._capture_running:
            return
            
        def _poll():
            import urllib.request
            import json
            url = self._remote_url("/api/status")
            try:
                with urllib.request.urlopen(url, timeout=2) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    
                # Update UI elements on main thread
                self._root.after(0, lambda: self._update_client_ui(data))
            except Exception:
                pass
                
        threading.Thread(target=_poll, daemon=True, name="client-poll").start()
        # Reschedule next poll
        self._root.after(1500, self._client_status_poll)

    def _fetch_remote_thumbnail(self):
        """Fetches the latest capture thumbnail from remote Pi and displays it in the sidebar."""
        if not self._client_ip:
            return
        def _fetch():
            import urllib.request
            import io
            from PIL import Image
            try:
                url = self._remote_url("/api/thumbnail")
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=3) as resp:
                    data = resp.read()
                    fn = resp.headers.get("X-Filename", "latest photo")
                img = Image.open(io.BytesIO(data))
                self._root.after(0, lambda: self._panel.set_thumbnail(img, f"✓ {fn}"))
            except Exception as e:
                log.debug("Remote thumbnail fetch error: %s", e)
        threading.Thread(target=_fetch, daemon=True, name="fetch-thumb").start()


    def _update_client_ui(self, data: dict):
        """Updates GUI widgets based on remote state data."""
        try:
            # Sync recording indicator (respect both local laptop recorder and remote server)
            local_rec = bool(getattr(self, "_stream_recorder", None) and self._stream_recorder.is_recording)
            remote_rec = bool(data.get("recording", False))
            is_rec = local_rec or remote_rec
            self._panel.set_recording(is_rec)
            self._preview.set_recording(is_rec)
            if is_rec:
                self._status.start_recording_indicator()
            else:
                self._status.stop_recording_indicator()
                
            # Update stream & snapshot resolution status
            res_str = data.get("stream_res", "1920x1080")
            snap_str = data.get("snap_quality", "")
            stream_qual = data.get("stream_quality", 90)
            if not snap_str and self._current_res:
                mp = round((self._current_res[0] * self._current_res[1]) / 1_000_000, 1)
                snap_str = f"{mp} MP"
            try:
                w, h = map(int, res_str.split("x"))
                self._status.update_resolution(w, h, snap_str=snap_str)
                self._panel.update_stream_indicators(w, h, stream_qual, snap_str)
            except Exception:
                pass
                
            # Update live LiDAR distance in Focus Control section & video HUD
            l_cm = float(data.get("lidar_cm", 0.0))
            l_m = float(data.get("lidar_m", 0.0))
            l_str = int(data.get("lidar_strength", 0))
            self._current_lidar = (l_cm, l_m, l_str)
            self._panel.update_lidar_reading(l_cm, l_m, l_str)
            self._preview.set_lidar_reading(l_cm, l_m, l_str)

            # Update Pixhawk MAVLink Telemetry HUD & Sidebar
            pix_data = data.get("pixhawk")
            if pix_data:
                self._current_pixhawk = pix_data
                self._preview.set_pixhawk_telemetry(pix_data)
                self._panel.update_pixhawk_telemetry(pix_data)

            # Update local laptop media storage indicators
            self._update_local_storage_stats()

            # Update hardware lens position telemetry
            lp = data.get("lens_position")
            if lp is not None:
                self._panel.set_current_lens_position(float(lp))
        except Exception as e:
            log.debug("Failed updating client UI: %s", e)

    def _update_local_storage_stats(self):
        """Refreshes storage indicator and photo/video counts for laptop media folder."""
        try:
            from core.storage import get_disk_usage, count_files, human_size
            used, total, pct = get_disk_usage()
            photos, videos, tl = count_files()
            self._panel.update_storage_display(
                human_size(used), human_size(total), pct,
                photos, videos, tl
            )
            self._status.update_storage(human_size(used), pct)
        except Exception:
            pass

    def _client_post(self, path: str, data: dict = None) -> None:
        """Sends an HTTP POST request to the remote server in a background thread."""
        if not self._client_ip:
            return
        
        def _thread_target():
            import urllib.request
            import json
            try:
                url = self._remote_url(path)
                headers = {"Content-Type": "application/json"}
                req_data = json.dumps(data or {}).encode("utf-8")
                req = urllib.request.Request(url, data=req_data, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=3) as resp:
                    resp.read()
            except Exception as e:
                log.warning("Client POST request failed to %s: %s", path, e)
                
        threading.Thread(target=_thread_target, daemon=True, name="client-post").start()

    # ──────────────────────────────────────────────────────────────────
    # Layout
    # ──────────────────────────────────────────────────────────────────

    def _build_layout(self):
        """Build the three-panel layout: preview | sidebar | status bar."""

        # Menu bar
        menubar = tk.Menu(self._root, bg=C_PANEL, fg=C_TEXT,
                          activebackground=C_ACCENT, activeforeground="#000",
                          relief=tk.FLAT)
        file_menu = tk.Menu(menubar, tearoff=0, bg=C_PANEL, fg=C_TEXT,
                            activebackground=C_ACCENT, activeforeground="#000")
        file_menu.add_command(label="Open Storage Folder",
                              command=self._open_storage_folder)
        file_menu.add_separator()
        file_menu.add_command(label="Settings…", command=self._open_settings)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)

        cam_menu = tk.Menu(menubar, tearoff=0, bg=C_PANEL, fg=C_TEXT,
                           activebackground=C_ACCENT, activeforeground="#000")
        cam_menu.add_command(label="Refresh Cameras",
                             command=self._async_scan_cameras)
        menubar.add_cascade(label="Camera", menu=cam_menu)

        # ── Image Adjustments Menu (upper menu, like Help) ──
        adj_menu = tk.Menu(menubar, tearoff=0, bg=C_PANEL, fg=C_TEXT,
                           activebackground=C_ACCENT, activeforeground="#000")
        adj_menu.add_command(label="🤿 Toggle Underwater Mode", command=lambda: self._on_underwater_toggle(not self._underwater_mode))
        adj_menu.add_command(label="🌙 Toggle Low Light Mode", command=lambda: self._on_low_light_toggle(not self._low_light_mode))
        adj_menu.add_separator()
        adj_menu.add_command(label="Reset All Image Settings", command=self._on_reset_camera)
        menubar.add_cascade(label="Image Adjustments", menu=adj_menu)

        help_menu = tk.Menu(menubar, tearoff=0, bg=C_PANEL, fg=C_TEXT,
                            activebackground=C_ACCENT, activeforeground="#000")
        help_menu.add_command(label="About PiCamPro", command=self._show_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        self._root.configure(menu=menubar)

        # ── Top Toolbar with ☰ Hamburger Button on upper left ──
        top_bar = tk.Frame(self._root, bg="#161b22", height=36, padx=8, pady=4)
        top_bar.pack(side=tk.TOP, fill=tk.X)

        self._panel_visible = True
        self._toggle_btn = tk.Button(
            top_bar, text="☰  Controls",
            command=self._toggle_control_panel,
            bg="#21262d", fg=C_ACCENT, activebackground="#2d333b", activeforeground=C_ACCENT,
            font=("Segoe UI", 9, "bold"), relief=tk.FLAT, bd=0, padx=10, pady=3, cursor="hand2"
        )
        self._toggle_btn.pack(side=tk.LEFT, padx=(2, 10))

        tk.Label(
            top_bar, text="PiCamPro — i4 Marine ROV Viewer",
            bg="#161b22", fg="#00d4aa", font=("Segoe UI", 9, "bold")
        ).pack(side=tk.LEFT)

        # Quick Image Adjustments Button on the upper bar
        tk.Button(
            top_bar, text="🎨 Image Adjustments",
            command=self._focus_image_adjustments,
            bg="#1f2937", fg="#38bdf8", activebackground="#374151", activeforeground="#38bdf8",
            font=("Segoe UI", 8, "bold"), relief=tk.FLAT, bd=0, padx=8, pady=2, cursor="hand2"
        ).pack(side=tk.RIGHT, padx=6)

        # Main container
        main = tk.Frame(self._root, bg=C_BG)
        main.pack(fill=tk.BOTH, expand=True)

        # Vertical separator
        self._panel_sep = tk.Frame(main, bg=C_BORDER, width=1)
        self._panel_sep.pack(side=tk.RIGHT, fill=tk.Y)

        # ── Control panel (right sidebar) ──
        self._panel = ControlPanel(
            main,
            on_camera_change             = self._on_camera_change,
            on_resolution_change         = self._on_resolution_change,
            on_fps_change                = self._on_fps_change,
            on_zoom_change               = self._on_zoom_change,
            on_record_toggle             = self._on_record_toggle,
            on_snapshot                  = self._on_snapshot,
            on_timelapse_toggle          = self._on_timelapse_toggle,
            on_timelapse_interval_change = self._on_interval_change,
            on_refresh_cameras           = self._async_scan_cameras,
            on_focus_mode_change         = self._on_focus_mode_change,
            on_lens_position_change      = self._on_lens_position_change,
            on_trigger_focus             = self._on_trigger_focus,
            on_af_range_change           = self._on_af_range_change,
            on_brightness_change         = self._on_brightness_change,
            on_contrast_change           = self._on_contrast_change,
            on_saturation_change         = self._on_saturation_change,
            on_ev_change                 = self._on_ev_change,
            on_awb_change                = self._on_awb_change,
            on_underwater_toggle         = self._on_underwater_toggle,
            on_low_light_toggle          = self._on_low_light_toggle,
            on_stream_resolution_change  = self._on_stream_resolution_change,
            on_stream_quality_change     = self._on_stream_quality_change,
            on_change_ip                 = self._on_change_ip,
            on_reset_camera              = self._on_reset_camera,
            on_open_media                = self._open_storage_folder,
            on_arm_pan                   = self._send_arm_pan,
            on_arm_tilt                  = self._send_arm_tilt,
        )
        self._panel.pack(side=tk.RIGHT, fill=tk.Y)

        # ── Preview (left, fills all available space) ──
        self._preview = PreviewCanvas(main, bg_colour=C_BG)
        self._preview.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._panel._cb_toggle_lidar_hud = self._preview.set_show_lidar_osd

        # ── Status bar (bottom) ──
        self._status = StatusBar(self._root)
        self._status.pack(side=tk.BOTTOM, fill=tk.X)

    def _toggle_control_panel(self):
        """Toggle showing or hiding the control panel sidebar."""
        if getattr(self, "_panel_visible", True):
            self._panel.pack_forget()
            if hasattr(self, "_panel_sep"):
                self._panel_sep.pack_forget()
            self._panel_visible = False
            self._toggle_btn.config(text="☰  Show Controls", bg="#21262d", fg="#8b949e")
        else:
            if hasattr(self, "_panel_sep"):
                self._panel_sep.pack(side=tk.RIGHT, fill=tk.Y)
            self._panel.pack(side=tk.RIGHT, fill=tk.Y)
            self._panel_visible = True
            self._toggle_btn.config(text="✕  Hide Controls", bg="#21262d", fg=C_ACCENT)

    def _focus_image_adjustments(self):
        """Toggle the floating Image Adjustments popup."""
        # If already open, close it
        if getattr(self, "_adj_popup", None):
            try:
                self._adj_popup.destroy()
            except Exception:
                pass
            self._adj_popup = None
            return

        # ── Build floating popup ──
        C_BG_POP  = "#0d1117"
        C_PANEL   = "#161b22"
        C_ACCENT  = "#00d4aa"
        C_MUTED   = "#8b949e"
        C_TEXT    = "#c9d1d9"
        C_BTN     = "#21262d"
        FONT_LBL  = ("Segoe UI", 9)
        FONT_BOLD = ("Segoe UI", 9, "bold")

        popup = tk.Toplevel(self._root)
        popup.title("Image Adjustments")
        popup.configure(bg=C_BG_POP)
        popup.resizable(False, False)
        popup.transient(self._root)
        # Position under the top bar, right-aligned
        self._root.update_idletasks()
        rx = self._root.winfo_rootx() + self._root.winfo_width() - 340
        ry = self._root.winfo_rooty() + 40
        popup.geometry(f"320x520+{rx}+{ry}")

        def _on_popup_close():
            self._adj_popup = None
            popup.destroy()
        popup.protocol("WM_DELETE_WINDOW", _on_popup_close)
        self._adj_popup = popup

        panel = self._panel  # convenience alias

        # Helper: slider row
        def _slider_row(parent, label, from_, to_, res, var, cb, fmt="{:.1f}"):
            row = tk.Frame(parent, bg=C_PANEL)
            row.pack(fill=tk.X, padx=12, pady=2)
            tk.Label(row, text=label, bg=C_PANEL, fg=C_MUTED,
                     font=FONT_LBL, width=13, anchor="w").pack(side=tk.LEFT)
            val_lbl = tk.Label(row, text=fmt.format(var.get()),
                               bg=C_PANEL, fg=C_ACCENT, font=FONT_LBL, width=7)
            val_lbl.pack(side=tk.RIGHT)
            def _on_move(v, _lbl=val_lbl, _fmt=fmt, _cb=cb):
                _lbl.config(text=_fmt.format(float(v)))
                _cb(float(v))
            sl = tk.Scale(row, from_=from_, to=to_, resolution=res,
                          orient=tk.HORIZONTAL, variable=var,
                          bg=C_PANEL, fg=C_TEXT, troughcolor=C_BTN,
                          highlightthickness=0, activebackground=C_ACCENT,
                          font=FONT_LBL, showvalue=False, command=_on_move)
            sl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
            return sl

        # Header
        hdr = tk.Frame(popup, bg="#0d2230", pady=8)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="🎨  Image Adjustments", bg="#0d2230",
                 fg=C_ACCENT, font=("Segoe UI", 11, "bold")).pack(padx=14, anchor="w")

        body = tk.Frame(popup, bg=C_PANEL)
        body.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)

        # ── Sliders ──
        tk.Frame(body, bg="#21262d", height=1).pack(fill=tk.X, pady=(4, 2))
        _slider_row(body, "Brightness:", -1.0,  1.0, 0.05, panel._brightness_var, self._on_brightness_change, "{:+.2f}")
        _slider_row(body, "Contrast:",    0.5,  3.0, 0.05, panel._contrast_var,   self._on_contrast_change,   "{:.2f}x")
        _slider_row(body, "Saturation:",  0.0,  3.0, 0.05, panel._saturation_var, self._on_saturation_change, "{:.2f}x")
        _slider_row(body, "Exposure EV:", -4.0, 4.0, 0.5,  panel._ev_var,         self._on_ev_change,         "{:+.1f}")

        # ── White Balance ──
        tk.Frame(body, bg="#21262d", height=1).pack(fill=tk.X, pady=(8, 2))
        tk.Label(body, text="White Balance:", bg=C_PANEL, fg=C_MUTED, font=FONT_LBL, anchor="w").pack(fill=tk.X, padx=12, pady=(2, 0))
        awb_combo = ttk.Combobox(
            body, textvariable=panel._awb_mode_var,
            state="readonly", values=["Auto", "Daylight", "Cloudy", "Tungsten", "Fluorescent", "Custom"],
            font=FONT_LBL
        )
        awb_combo.pack(fill=tk.X, padx=12, pady=4)

        gains_frame = tk.Frame(body, bg=C_PANEL)
        rg_sl = _slider_row(gains_frame, "  Red Gain:",  0.5, 8.0, 0.1, panel._red_gain_var,  lambda v: self._on_awb_change(panel._awb_mode_var.get().lower(), v, panel._blue_gain_var.get()), "{:.1f}")
        bg_sl = _slider_row(gains_frame, "  Blue Gain:", 0.5, 8.0, 0.1, panel._blue_gain_var, lambda v: self._on_awb_change(panel._awb_mode_var.get().lower(), panel._red_gain_var.get(), v),  "{:.1f}")

        def _awb_changed(_event=None):
            mode = panel._awb_mode_var.get()
            if mode == "Custom":
                gains_frame.pack(fill=tk.X, pady=2)
            else:
                gains_frame.pack_forget()
            self._on_awb_change(mode.lower(), panel._red_gain_var.get(), panel._blue_gain_var.get())
        awb_combo.bind("<<ComboboxSelected>>", _awb_changed)
        # Also set on panel for sidebar compatibility
        panel._awb_mode_combo = awb_combo
        panel._custom_gains_frame = gains_frame
        _awb_changed()  # apply current state

        # ── Modes ──
        tk.Frame(body, bg="#21262d", height=1).pack(fill=tk.X, pady=(8, 4))

        uw_chk = tk.Checkbutton(
            body, text="🤿  Underwater Color Correction",
            variable=panel._underwater_var,
            command=lambda: (panel._update_adjustments_ui(), self._on_underwater_toggle(panel._underwater_var.get())),
            bg=C_PANEL, fg=C_ACCENT, selectcolor=C_PANEL,
            activebackground=C_PANEL, activeforeground=C_ACCENT,
            font=FONT_BOLD, bd=0, highlightthickness=0, cursor="hand2"
        )
        uw_chk.pack(fill=tk.X, padx=12, pady=(2, 4))

        ll_chk = tk.Checkbutton(
            body, text="🌙  Low Light Optimization",
            variable=panel._low_light_var,
            command=lambda: self._on_low_light_toggle(panel._low_light_var.get()),
            bg=C_PANEL, fg=C_ACCENT, selectcolor=C_PANEL,
            activebackground=C_PANEL, activeforeground=C_ACCENT,
            font=FONT_BOLD, bd=0, highlightthickness=0, cursor="hand2"
        )
        ll_chk.pack(fill=tk.X, padx=12, pady=(0, 4))

        # Also store refs on panel so update_presets_from_underwater still works
        panel._underwater_chk = uw_chk
        panel._low_light_chk  = ll_chk

        # ── Reset button ──
        tk.Frame(body, bg="#21262d", height=1).pack(fill=tk.X, pady=(8, 4))
        tk.Button(
            body, text="↺  Reset to Defaults",
            command=self._on_reset_camera,
            bg=C_BTN, fg=C_MUTED, activebackground="#2d333b", activeforeground=C_TEXT,
            font=FONT_LBL, relief=tk.FLAT, bd=0, padx=8, pady=4, cursor="hand2"
        ).pack(fill=tk.X, padx=12, pady=4)

    def _on_change_ip(self, new_ip: str):
        if not new_ip:
            return
        log.info("Switching remote client IP from %s to: %s", self._client_ip, new_ip)
        self._client_ip = new_ip
        self._panel.set_current_ip(new_ip)
        self._status.update_camera(f"Connecting to {self._remote_display_endpoint()}…")
        self._fetch_remote_cameras()
        self._client_status_poll()
        self._fetch_remote_thumbnail()

    def _on_reset_camera(self):
        log.info("User requested clearing camera path on %s", self._client_ip)
        self._status.update_camera(f"Clearing camera path on {self._client_ip}…")
        def _do_reset():
            import urllib.request
            try:
                url = self._remote_url("/api/reset_camera")
                req = urllib.request.Request(url, method="POST")
                urllib.request.urlopen(req, timeout=4)
                log.info("Reset camera API call succeeded.")
            except Exception as e:
                log.debug("Reset camera API note: %s", e)
            time.sleep(0.5)
            self._start_client_stream()
            self._fetch_remote_cameras()
            self._client_status_poll()
            self._fetch_remote_thumbnail()
        threading.Thread(target=_do_reset, daemon=True, name="cam-reset").start()

    # ──────────────────────────────────────────────────────────────────
    # Camera detection & switching
    # ──────────────────────────────────────────────────────────────────

    def _fetch_remote_cameras(self):
        """Fetches the list of cameras from the remote Pi server and updates the UI dropdown."""
        if not self._client_ip:
            return
        def _query():
            import urllib.request
            import json
            from core.camera_manager import CameraInfo, SensorMode
            url = self._remote_url("/api/cameras")
            try:
                with urllib.request.urlopen(url, timeout=3) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                cam_list = []
                cams_data = data.get("cameras", [])
                active_id = str(data.get("active", "csi:0")).strip()
                active_cam = None
                for c in cams_data:
                    modes = [
                        SensorMode(int(m["w"]), int(m["h"]), float(m.get("fps", 30.0)))
                        for m in c.get("modes", [])
                    ]
                    if not modes:
                        modes = [SensorMode(int(c.get("max_width", 1920)), int(c.get("max_height", 1080)), 30.0)]
                    raw_name = c.get("name", "Camera")
                    supp_af = c.get("supports_focus", True)
                    if c.get("source") == "libcamera":
                        label_name = "📷 Arducam 64MP (CSI Ribbon - Auto Focus)"
                    else:
                        af_txt = "Auto Focus" if supp_af else "Fixed Focus"
                        if "4k" in raw_name.lower() or "uc60" in raw_name.lower():
                            label_name = f"🎥 4K Auto Focus Camera (UC60 - USB Port)"
                        else:
                            label_name = f"🎥 {raw_name} (USB Port - {af_txt})"
                    cam_obj = CameraInfo(
                        index=int(c.get("index", 0)) if str(c.get("index", "0")).isdigit() else 0,
                        name=label_name,
                        source=c.get("source", "libcamera"),
                        device_path=c.get("device_path", ""),
                        modes=modes,
                        max_width=int(c.get("max_width", 1920)),
                        max_height=int(c.get("max_height", 1080)),
                        max_fps=float(c.get("max_fps", 30.0)),
                        supports_focus=supp_af,
                    )
                    cam_obj._remote_id = c.get("id")
                    cam_list.append(cam_obj)
                    c_id = str(c.get("id", "")).strip()
                    if c_id == active_id:
                        active_cam = cam_obj
                    elif c.get("source") == "libcamera" and active_id in (c_id, f"csi:{c.get('index')}"):
                        active_cam = cam_obj
                    elif c.get("source") == "v4l2" and active_id in (c_id, c.get("device_path"), f"v4l2:{c.get('device_path')}"):
                        active_cam = cam_obj

                if cam_list:
                    self._cameras = cam_list
                    if not active_cam:
                        active_cam = cam_list[0]
                    self._active_cam = active_cam
                    self._root.after(0, lambda: self._apply_remote_cameras_to_ui(cam_list, active_cam))
            except Exception as e:
                log.debug("Fetch remote cameras note: %s", e)

        threading.Thread(target=_query, daemon=True, name="fetch-cams").start()

    def _apply_remote_cameras_to_ui(self, cam_list: List[CameraInfo], active_cam: CameraInfo):
        labels = [c.name for c in cam_list]
        self._panel._cameras = cam_list
        self._panel._cam_combo["values"] = labels
        try:
            idx = cam_list.index(active_cam)
        except ValueError:
            idx = 0
        self._panel._cam_combo.current(idx)
        self._panel._cam_var.set(labels[idx])
        self._panel._select_camera(active_cam, fire_callback=False)
        self._status.update_camera(f"{active_cam.name} [Connected: {self._client_ip}]")
        self._panel.set_focus_supported(getattr(active_cam, "supports_focus", True))

    def _async_scan_cameras(self):
        """Scan in background thread, clearing any process holding the camera device."""
        if self._client_ip:
            self._status.update_camera(f"Refreshing cameras from Pi ({self._client_ip})…")
            self._fetch_remote_cameras()
            self._client_status_poll()
            self._fetch_remote_thumbnail()
            return
        def _scan():
            import subprocess
            # If on Linux/Pi, cleanly release camera if another background service was holding it
            try:
                subprocess.run(["sudo", "-n", "systemctl", "stop", "picampro-stream"], capture_output=True, timeout=2)
            except Exception:
                pass
            cameras = detect_all_cameras()
            self._root.after(0, lambda: self._on_cameras_detected(cameras))

        t = threading.Thread(target=_scan, daemon=True, name="cam-scan")
        t.start()
        self._status.update_camera("Scanning for cameras…")

    def _on_cameras_detected(self, cameras: List[CameraInfo]):
        self._cameras = cameras
        self._panel.update_cameras(cameras)
        if cameras:
            self._status.update_camera(cameras[0].name)
        else:
            self._preview.set_no_signal()
            self._status.update_camera("No cameras found")
            messagebox.showwarning(
                "No Cameras",
                "No cameras detected.\n\n"
                "• For CSI cameras: check ribbon cable connection\n"
                "• For USB cameras: check USB connection\n"
                "• Run 'rpicam-hello --list-cameras' to diagnose\n\n"
                "Press  Camera → Refresh Cameras  to try again."
            )

    def _on_camera_change(self, cam: CameraInfo):
        """Called when user selects a different camera."""
        if self._client_ip:
            if getattr(self, "_active_cam", None) and getattr(self._active_cam, "_remote_id", None) == getattr(cam, "_remote_id", None):
                return
            log.info("Switching to camera: %s (id: %s)", cam.name, getattr(cam, "_remote_id", None))
            self._active_cam = cam
            self._status.update_camera(f"Switching to {cam.name}…")
            cam_id = getattr(cam, "_remote_id", None) or (f"csi:{cam.index}" if cam.source == "libcamera" else f"v4l2:{cam.device_path}")
            self._client_post("/api/switch_camera", {"camera_id": cam_id})
            self._panel.set_focus_supported(getattr(cam, "supports_focus", True))
            self._root.after(1000, self._fetch_remote_cameras)
            self._root.after(1500, self._fetch_remote_thumbnail)
            return

        if self._active_cam is cam:
            return
        log.info("Switching to camera: %s", cam.name)
        self._active_cam = cam

        self._stop_capture()
        self._recorder   = Recorder(cam)
        self._snapshooter = SnapshotEngine(cam)

        # Get selected resolution from control panel
        val = self._panel._res_var.get()
        import re
        match = re.search(r"(\d+)x(\d+)", val)
        if match:
            self._current_res = (int(match.group(1)), int(match.group(2)))
        else:
            # Pick default resolution (highest available, capped to 1920×1080)
            modes = cam.sorted_modes()
            default_res = (cam.max_width, cam.max_height)
            for m in modes:
                if m.width <= 1920:
                    default_res = (m.width, m.height)
                    break
            self._current_res = default_res

        self._status.update_camera(cam.name)
        self._status.update_resolution(*self._current_res)
        self._start_capture()

    def _on_resolution_change(self, res: Tuple[int, int]):
        self._current_res = res
        mp = round((res[0] * res[1]) / 1_000_000, 1)
        snap_label = f"{mp} MP"
        if self._client_ip:
            # Update snapshot quality on server, keep live stream resolution fast and smooth
            self._status.update_resolution(1920, 1080, snap_str=snap_label)
            self._client_post("/api/control", {"snap_res": {"w": res[0], "h": res[1]}})
            return
        self._status.update_resolution(res[0], res[1], snap_str=snap_label)
        if self._active_cam:
            log.info("Resolution changed to %dx%d", *res)
            self._stop_capture()
            self._start_capture()

    def _on_stream_resolution_change(self, res: Tuple[int, int], fps: int = 30, mode: str = "auto"):
        log.info("Live stream resolution changed to: %dx%d @ %d fps (mode: %s)", res[0], res[1], fps, mode)
        if self._client_ip:
            self._client_post("/api/control", {
                "stream_res": {"w": res[0], "h": res[1], "fps": fps, "mode": mode}
            })
        else:
            self._on_resolution_change(res)

    def _on_stream_quality_change(self, quality: int):
        log.info("Live stream quality changed to: %d%%", quality)
        if self._client_ip:
            self._client_post("/api/control", {"stream_quality": quality})

    def _on_fps_change(self, fps: int):
        self._target_fps = fps
        self._preview.set_target_fps(fps)
        if self._client_ip:
            self._client_post("/api/control", {"fps_target": fps})
            return

    def _on_zoom_change(self, zoom: float):
        self._preview.set_zoom(zoom)
        if self._client_ip:
            self._send_arm_api("/api/zoom", {"zoom": float(zoom)})

    def _on_focus_mode_change(self, mode: str):
        self._focus_mode = mode
        log.info("Focus mode changed to: %s", mode)
        if self._client_ip:
            self._send_arm_api("/api/focus", {"action": "mode", "mode": mode})
            self._client_post("/api/control", {"focus_mode": mode})
            return
        self._apply_focus_controls()

    def _on_lens_position_change(self, pos: float):
        self._lens_position = pos
        log.debug("Lens position changed to: %.1f", pos)
        if self._client_ip:
            self._send_arm_api("/api/focus", {"action": "manual", "mode": "manual", "value": int(float(pos) * 85)})
            self._client_post("/api/control", {"lens_position": pos})
            return
        self._apply_focus_controls()

    def _on_trigger_focus(self):
        log.info("Autofocus cycle triggered manually.")
        if self._client_ip:
            self._send_arm_api("/api/focus", {"action": "trigger"})
            self._client_post("/api/control", {"trigger_focus": True})
            return
        if self._picam2 is not None and controls is not None:
            try:
                self._picam2.set_controls({"AfTrigger": controls.AfTriggerEnum.Start})
            except Exception as e:
                log.error("Failed to trigger autofocus: %s", e)

    def _apply_focus_controls(self):
        if self._picam2 is None or controls is None or not self._focus_supported:
            return
        try:
            if self._focus_mode == "continuous":
                self._picam2.set_controls({
                    "AfMode":  controls.AfModeEnum.Continuous,
                    "AfSpeed": controls.AfSpeedEnum.Fast,   # move lens quickly
                    "AfRange": controls.AfRangeEnum.Full,   # cover macro → infinity
                })
            elif self._focus_mode == "auto":
                self._picam2.set_controls({
                    "AfMode":  controls.AfModeEnum.Auto,
                    "AfSpeed": controls.AfSpeedEnum.Fast,
                    "AfRange": controls.AfRangeEnum.Full,
                })
                # Trigger a single sweep immediately
                try:
                    self._picam2.set_controls({"AfTrigger": controls.AfTriggerEnum.Start})
                except Exception:
                    pass
            elif self._focus_mode == "manual":
                self._picam2.set_controls({
                    "AfMode":       controls.AfModeEnum.Manual,
                    "LensPosition": float(self._lens_position)
                })
        except Exception as e:
            log.warning("Could not apply focus controls: %s", e)

    def _on_af_range_change(self, range_val: str):
        self._af_range = range_val
        log.info("AF Range changed to: %s", range_val)
        if self._client_ip:
            self._client_post("/api/control", {"af_range": range_val})
            return
        self._apply_image_controls()

    def _on_brightness_change(self, val: float):
        self._brightness = val
        log.debug("Brightness changed to: %.2f", val)
        if self._client_ip:
            self._client_post("/api/control", {"brightness": val})
            return
        self._apply_image_controls()

    def _on_contrast_change(self, val: float):
        self._contrast = val
        log.debug("Contrast changed to: %.2f", val)
        if self._client_ip:
            self._client_post("/api/control", {"contrast": val})
            return
        self._apply_image_controls()

    def _on_saturation_change(self, val: float):
        self._saturation = val
        log.debug("Saturation changed to: %.2f", val)
        if self._client_ip:
            self._client_post("/api/control", {"saturation": val})
            return
        self._apply_image_controls()

    def _on_ev_change(self, val: float):
        self._exposure_value = val
        log.debug("EV changed to: %.2f", val)
        if self._client_ip:
            self._client_post("/api/control", {"ev": val})
            return
        self._apply_image_controls()

    def _on_awb_change(self, mode: str, red_gain: float, blue_gain: float):
        self._awb_mode = mode
        self._red_gain = red_gain
        self._blue_gain = blue_gain
        log.debug("AWB changed to: %s (Red: %.2f, Blue: %.2f)", mode, red_gain, blue_gain)
        if self._client_ip:
            self._client_post("/api/control", {
                "awb_mode": mode,
                "red_gain": red_gain,
                "blue_gain": blue_gain
            })
            return
        self._apply_image_controls()

    def _on_underwater_toggle(self, enabled: bool):
        self._underwater_mode = enabled
        log.info("Underwater mode toggled: %s", enabled)
        if enabled:
            self._awb_mode = "custom"
            self._brightness = 0.0
            self._contrast = 1.15
            self._saturation = 1.25
            self._exposure_value = 0.0
            self._red_gain = 2.6
            self._blue_gain = 1.15
            self._panel.update_presets_from_underwater(True)
            payload = {
                "underwater": True,
                "awb_mode": "custom",
                "red_gain": 2.6,
                "blue_gain": 1.15,
                "contrast": 1.15,
                "saturation": 1.25,
                "brightness": 0.0,
                "ev": 0.0
            }
        else:
            self._awb_mode = "auto"
            self._brightness = 0.0
            self._contrast = 1.0
            self._saturation = 1.0
            self._exposure_value = 0.0
            self._red_gain = 0.0
            self._blue_gain = 0.0
            self._panel.update_presets_from_underwater(False)
            payload = {
                "underwater": False,
                "awb_mode": "auto",
                "red_gain": 0.0,
                "blue_gain": 0.0,
                "brightness": 0.0,
                "contrast": 1.0,
                "saturation": 1.0,
                "ev": 0.0
            }
        if self._client_ip:
            self._client_post("/api/control", payload)
            return
        self._apply_image_controls()

    def _on_low_light_toggle(self, enabled: bool):
        self._low_light_mode = enabled
        log.info("Low light optimization toggled: %s", enabled)
        if self._client_ip:
            self._client_post("/api/control", {"low_light": enabled})
            return
        self._apply_image_controls()

    def _apply_image_controls(self):
        if self._picam2 is None or controls is None:
            return
        try:
            ctrls = {}
            
            # Base Exposure Value (EV) with Low Light boost if active
            ev_to_apply = self._exposure_value
            if self._low_light_mode:
                ev_to_apply = min(8.0, ev_to_apply + 1.2) # Boost target exposure by +1.2 EV

            if "Brightness" in self._picam2.camera_controls:
                ctrls["Brightness"] = float(self._brightness)
            if "Contrast" in self._picam2.camera_controls:
                ctrls["Contrast"] = float(self._contrast)
            if "Saturation" in self._picam2.camera_controls:
                ctrls["Saturation"] = float(self._saturation)
            if "ExposureValue" in self._picam2.camera_controls:
                ctrls["ExposureValue"] = float(ev_to_apply)

            # Low Light optimizations: shadows constraint & high-quality noise reduction
            if "AeConstraintMode" in self._picam2.camera_controls:
                if self._low_light_mode:
                    ctrls["AeConstraintMode"] = controls.AeConstraintModeEnum.Shadows
                else:
                    ctrls["AeConstraintMode"] = controls.AeConstraintModeEnum.Normal

            if "NoiseReductionMode" in self._picam2.camera_controls:
                if self._low_light_mode:
                    ctrls["NoiseReductionMode"] = controls.NoiseReductionModeEnum.HighQuality
                else:
                    ctrls["NoiseReductionMode"] = controls.NoiseReductionModeEnum.Fast

            # Low Light auto-framerate: allow exposure duration to expand to 100ms (10fps) for 3x brightness
            if "FrameDurationLimits" in self._picam2.camera_controls:
                frame_dur_us = int(1000000 / self._target_fps)
                if self._low_light_mode:
                    # Allow exposure up to 1 second (1 000 000 µs) in low light
                    ctrls["FrameDurationLimits"] = (frame_dur_us, 1_000_000)
                else:
                    ctrls["FrameDurationLimits"] = (frame_dur_us, frame_dur_us)

            if "AfRange" in self._picam2.camera_controls:
                af_range_map = {
                    "normal": controls.AfRangeEnum.Normal,
                    "full": controls.AfRangeEnum.Full,
                    "macro": controls.AfRangeEnum.Macro
                }
                mapped_range = af_range_map.get(self._af_range)
                if mapped_range is not None:
                    ctrls["AfRange"] = mapped_range

            if "AwbMode" in self._picam2.camera_controls:
                awb_map = {
                    "auto": controls.AwbModeEnum.Auto,
                    "daylight": controls.AwbModeEnum.Daylight,
                    "cloudy": controls.AwbModeEnum.Cloudy,
                    "tungsten": controls.AwbModeEnum.Tungsten,
                    "fluorescent": controls.AwbModeEnum.Fluorescent,
                    "custom": controls.AwbModeEnum.Custom
                }
                mapped_awb = awb_map.get(self._awb_mode)
                if mapped_awb is not None:
                    ctrls["AwbMode"] = mapped_awb

            if "ColourGains" in self._picam2.camera_controls:
                if self._awb_mode == "custom":
                    ctrls["ColourGains"] = (float(self._red_gain), float(self._blue_gain))
                else:
                    ctrls["ColourGains"] = (0.0, 0.0)

            if ctrls:
                log.debug("Applying camera controls: %s", list(ctrls.keys()))
                self._picam2.set_controls(ctrls)
        except Exception as e:
            log.warning("Could not apply image controls: %s", e)

    # ──────────────────────────────────────────────────────────────────
    # Capture thread
    # ──────────────────────────────────────────────────────────────────

    def _start_capture(self):
        if self._active_cam is None:
            return
        self._capture_running = True
        self._preview.set_no_signal()

        if self._active_cam.source == "libcamera":
            self._capture_thread = threading.Thread(
                target=self._libcamera_loop, daemon=True, name="capture-lc"
            )
        else:
            self._capture_thread = threading.Thread(
                target=self._v4l2_loop, daemon=True, name="capture-v4l2"
            )
        self._capture_thread.start()

    def _stop_capture(self):
        self._capture_running = False
        # Stop libcamera if running
        if self._picam2 is not None:
            try:
                self._picam2.stop()
                self._picam2.close()
            except Exception:
                pass
            self._picam2 = None
        # Release V4L2 capture
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        # Wait for thread
        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=3.0)
        self._capture_thread = None

    def _libcamera_loop(self):
        """Background thread: read frames from Picamera2."""
        try:
            from picamera2 import Picamera2
        except ImportError:
            log.error("picamera2 not installed. Falling back to V4L2.")
            self._root.after(0, self._v4l2_fallback)
            return

        try:
            picam2 = Picamera2(self._active_cam.index)
            w, h   = self._current_res
            rec_w, rec_h = w, h
            if rec_w * rec_h > 3840 * 2160:
                # Cap recording at 4K (3840x2160) for H.264 software encoder stability
                rec_w, rec_h = 3840, 2160
            
            config = None
            try:
                config = picam2.create_video_configuration(
                    main={"size": (w, h), "format": "RGB888"},
                    vid={"size": (rec_w, rec_h), "format": "YUV420"},
                    controls={"FrameRate": float(self._target_fps)},
                )
                log.info("Configured dual-stream (main + vid)")
            except Exception as ce:
                log.info("Dual-stream config failed (%s) — trying single RGB888 stream", ce)
                try:
                    config = picam2.create_video_configuration(
                        main={"size": (w, h), "format": "RGB888"},
                        controls={"FrameRate": float(self._target_fps)},
                    )
                    log.info("Configured single-stream video")
                except Exception as ce2:
                    log.warning("Single-stream video config failed (%s) — falling back to preview configuration", ce2)
                    config = picam2.create_preview_configuration(
                        main={"size": (w, h), "format": "RGB888"},
                    )

            picam2.configure(config)
            picam2.start()
            self._picam2 = picam2

            # Check for focus control support
            self._focus_supported = "LensPosition" in picam2.camera_controls
            log.info("Focus controls supported: %s", self._focus_supported)
            self._root.after(0, lambda: self._panel.set_focus_supported(self._focus_supported))

            if self._focus_supported:
                # Apply initial focus controls (e.g. continuous autofocus on startup)
                self._apply_focus_controls()

            # Apply initial image controls (e.g. brightness, contrast, AWB, etc. on startup)
            self._apply_image_controls()

            # Inject into recorder & snapshooter
            if self._recorder:
                self._recorder.attach_picam2(picam2)
            if self._snapshooter:
                self._snapshooter.attach_picam2(picam2)

            log.info("libcamera stream started  %dx%d @%dfps", w, h, self._target_fps)

            while self._capture_running:
                if self._capture_paused:
                    time.sleep(0.1)
                    continue
                frame_bgr = picam2.capture_array("main")
                
                # Apply real-time AI clarity enhancement
                try:
                    from core.ai_enhancer import ai_engine
                    frame_bgr = ai_engine.enhance(frame_bgr, ai_clarity=True, ai_low_light=False)
                except Exception:
                    pass

                self._preview.push_frame(frame_bgr)
                
                # Push frame to ROV streaming server state
                try:
                    from core.rov_server import rov_state
                    ok, buf = cv2.imencode(".jpg", frame_bgr, [cv2.IMWRITE_JPEG_QUALITY, 95, cv2.IMWRITE_JPEG_OPTIMIZE, 1])
                    if ok:
                        rov_state.update_frame(bytes(buf))
                except Exception:
                    pass

                if self._recorder and self._recorder.is_recording:
                    self._recorder.write_frame(frame_bgr)

        except Exception as e:
            err_msg = str(e)
            log.error("libcamera loop error: %s", err_msg)
            self._root.after(0, lambda msg=err_msg: messagebox.showerror(
                "Camera Error", f"libcamera error:\n{msg}"
            ))
        finally:
            if self._picam2 is not None:
                try:
                    self._picam2.stop()
                    self._picam2.close()
                except Exception:
                    pass
                self._picam2 = None

    def _v4l2_loop(self):
        """Background thread: read frames from a V4L2 / USB camera."""
        self._focus_supported = False
        self._root.after(0, lambda: self._panel.set_focus_supported(False))

        cam = self._active_cam
        if cam is None:
            return

        cap = cv2.VideoCapture(cam.device_path)
        if not cap.isOpened():
            log.error("Cannot open %s", cam.device_path)
            self._root.after(0, lambda: messagebox.showerror(
                "Camera Error",
                f"Cannot open camera:\n{cam.device_path}\n\n"
                "Check connection and permissions."
            ))
            return

        w, h = self._current_res
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        cap.set(cv2.CAP_PROP_FPS, self._target_fps)
        self._cap = cap

        log.info("V4L2 stream started  %dx%d  device=%s", w, h, cam.device_path)

        while self._capture_running:
            ret, frame = cap.read()
            if not ret:
                log.warning("V4L2 read failed, retrying…")
                time.sleep(0.05)
                continue
            self._preview.push_frame(frame)
            
            # Push frame to ROV streaming server state
            try:
                from core.rov_server import rov_state
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
                if ok:
                    rov_state.update_frame(bytes(buf))
            except Exception:
                pass

            if self._recorder and self._recorder.is_recording:
                self._recorder.write_frame(frame)

        cap.release()
        self._cap = None

    def _v4l2_fallback(self):
        """Switch to V4L2 mode when libcamera is unavailable."""
        if self._active_cam:
            self._active_cam.source = "v4l2"
            self._start_capture()

    def _save_media_metadata(self, media_type: str, path: Path, extra: dict = None) -> None:
        """Constructs and saves metadata sidecar for snapshots and recordings."""
        try:
            import datetime
            from core.storage import save_metadata
            
            # Determine actual resolution from file if it is a photo/timelapse
            w, h = self._current_res
            if media_type in ("photo", "timelapse") and path.exists():
                try:
                    from PIL import Image
                    with Image.open(path) as img:
                        w, h = img.size
                except Exception:
                    pass

            zoom_val = 1.0
            if hasattr(self, '_preview') and hasattr(self._preview, '_zoom'):
                zoom_val = self._preview._zoom

            meta = {
                "timestamp": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "filename": path.name,
                "media_type": media_type,
                "camera_name": self._active_cam.name if self._active_cam else "i4 Marine",
                "camera_source": self._active_cam.source if self._active_cam else "ethernet",
                "resolution": f"{w}x{h}",
                "framerate_fps": self._target_fps,
                "digital_zoom": zoom_val,
                "brightness": self._brightness,
                "contrast": self._contrast,
                "saturation": self._saturation,
                "exposure_ev": self._exposure_value,
                "focus_mode": self._focus_mode,
                "lens_position": self._lens_position if self._focus_supported else None,
                "af_range": self._af_range,
                "awb_mode": self._awb_mode,
                "red_gain": self._red_gain if (self._awb_mode == "custom" or self._underwater_mode) else None,
                "blue_gain": self._blue_gain if (self._awb_mode == "custom" or self._underwater_mode) else None,
                "underwater_mode": self._underwater_mode,
                "low_light_mode": self._low_light_mode,
                "lidar": {
                    "distance_cm": self._current_lidar[0],
                    "distance_m": self._current_lidar[1],
                    "formatted": f"{self._current_lidar[0]:.1f} cm ({self._current_lidar[1]:.2f} m)" if self._current_lidar[0] > 0 else "N/A"
                },
                "pixhawk_telemetry": self._current_pixhawk if self._current_pixhawk else {},
            }
            if extra:
                meta.update(extra)
            save_metadata(path, meta)
        except Exception as e:
            log.warning("Could not construct or save metadata: %s", e)

    # ──────────────────────────────────────────────────────────────────
    # Recording
    # ──────────────────────────────────────────────────────────────────

    def _on_record_toggle(self):
        if self._client_ip:
            # Record locally on the laptop with burned-in LiDAR data!
            if self._stream_recorder.is_recording:
                # Stop recording
                saved_path, duration, fcount = self._stream_recorder.stop()
                self._panel.set_recording(False)
                self._preview.set_recording(False)
                self._status.stop_recording_indicator()
                if saved_path and saved_path.exists():
                    log.info("Local stream recording saved: %s (duration: %.1fs, frames: %d)", saved_path, duration, fcount)
                    rec_w = self._stream_recorder.width
                    rec_h = self._stream_recorder.height
                    self._save_media_metadata("video", saved_path, extra={
                        "resolution": f"{rec_w}x{rec_h}",
                        "fps": self._stream_recorder.fps,
                        "duration_secs": round(duration, 1),
                        "frame_count": fcount,
                        "file_size_mb": round(saved_path.stat().st_size / (1024 * 1024), 2)
                    })
                    self._update_local_storage_stats()
                    messagebox.showinfo("Video Saved to Laptop",
                                        f"Video recorded & saved to laptop:\n{saved_path.name}\n\nFolder: media/videos\n(LiDAR data burned into video)")
                else:
                    messagebox.showwarning("Recording Warning", "Recording stopped, but no frames were saved.")
            else:
                # Start recording locally on laptop
                vid_path = get_video_path(ext="mp4", camera_name="i4Marine")
                rw, rh = 1920, 1080
                if getattr(self, "_current_res", None) and isinstance(self._current_res, (list, tuple)):
                    rw, rh = self._current_res
                ok = self._stream_recorder.start(vid_path, width=rw, height=rh, fps=30.0)
                if ok:
                    self._panel.set_recording(True)
                    self._preview.set_recording(True)
                    self._status.start_recording_indicator()
                else:
                    messagebox.showerror("Recording Error", "Failed to start local stream recording on laptop.")
            return

        if self._recorder is None:
            messagebox.showwarning("No Camera", "Select a camera first.")
            return

        if self._recorder.is_recording:
            # Stop
            out = self._recorder.stop_recording()
            self._panel.set_recording(False)
            self._preview.set_recording(False)
            self._status.stop_recording_indicator()
            if out:
                log.info("Recording saved: %s", out)
                rec_w, rec_h = self._current_res
                if self._active_cam and self._active_cam.source == "libcamera":
                    if rec_w * rec_h > 3840 * 2160:
                        rec_w, rec_h = 3840, 2160
                self._save_media_metadata("video", out, extra={"resolution": f"{rec_w}x{rec_h}"})
                self._update_local_storage_stats()
                messagebox.showinfo("Recording Saved",
                                    f"Video saved to laptop:\n{out}")
        else:
            # Start
            cam_name = self._active_cam.name if self._active_cam else "cam"
            w, h = self._current_res
            path = get_video_path(camera_name=cam_name)

            if self._active_cam and self._active_cam.source == "libcamera":
                # Use quality=17 for high-quality H.264 video encoding (QP 17 is visually lossless)
                ok = self._recorder.start_recording_libcamera(
                    path, quality=17, width=w, height=h, fps=float(self._target_fps)
                )
            else:
                ok = self._recorder.start_recording_v4l2(
                    path, w, h, float(self._target_fps)
                )

            if ok:
                self._panel.set_recording(True)
                self._preview.set_recording(True)
                self._status.start_recording_indicator()
            else:
                messagebox.showerror("Recording Error",
                                     "Failed to start recording.")

    # ──────────────────────────────────────────────────────────────────
    # Snapshot
    # ──────────────────────────────────────────────────────────────────

    def _on_snapshot(self):
        if self._client_ip:
            # Save snapshot locally to laptop media/photos folder with burned-in LiDAR data!
            frame = self._get_last_frame()
            if frame is None:
                messagebox.showwarning("No Frame", "No live video frame available from stream to snapshot.")
                return

            try:
                import cv2
                from PIL import Image
                import datetime
                
                path = get_photo_path(ext=self._snap_format, camera_name="i4Marine")
                snap_img = frame.copy()
                
                # Burn LiDAR and timestamp data onto snapshot image
                l_cm, l_m, l_str = getattr(self, "_current_lidar", (0.0, 0.0, 0))
                ts_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                osd_str = f"i4 Marine | {ts_str}"
                if l_cm > 0:
                    osd_str += f" | LiDAR: {int(l_cm)} cm ({l_m:.2f} m)"
                pix = getattr(self, "_current_pixhawk", {})
                if pix and pix.get("connected"):
                    d_m = float(pix.get("depth_m", 0.0))
                    hdg = int(pix.get("heading", 0))
                    osd_str += f" | Depth: {d_m:.2f}m HDG: {hdg} deg"

                # Draw high-contrast text overlay in bottom-left
                h_img, w_img = snap_img.shape[:2]
                font = cv2.FONT_HERSHEY_SIMPLEX
                font_scale = max(0.55, min(w_img / 1920.0 * 0.75, 1.2))
                thickness = max(1, int(font_scale * 2.2))
                x, y = 20, h_img - 25

                # Black outline
                cv2.putText(snap_img, osd_str, (x, y), font, font_scale, (0, 0, 0), thickness + 3, cv2.LINE_AA)
                # Bright yellow/gold text
                cv2.putText(snap_img, osd_str, (x, y), font, font_scale, (0, 235, 255), thickness, cv2.LINE_AA)

                # Save file locally on laptop
                ok = cv2.imwrite(str(path), snap_img)
                if ok:
                    # Save metadata sidecar
                    self._save_media_metadata("photo", path, extra={
                        "resolution": f"{w_img}x{h_img}",
                        "file_size_kb": round(path.stat().st_size / 1024, 1)
                    })
                    # Update thumbnail in control panel
                    try:
                        rgb = cv2.cvtColor(snap_img, cv2.COLOR_BGR2RGB)
                        im = Image.fromarray(rgb)
                        self._panel.set_thumbnail(im, f"✓ {path.name}")
                    except Exception:
                        pass
                    self._update_local_storage_stats()
                    messagebox.showinfo("Snapshot Saved to Laptop", f"Photo saved to laptop:\n{path.name}\n\nFolder: media/photos\n(LiDAR & telemetry details saved)")
                else:
                    messagebox.showerror("Snapshot Error", "Failed to write image file to disk.")
            except Exception as e:
                log.error("Snapshot error: %s", e)
                messagebox.showerror("Snapshot Error", f"Error saving snapshot:\n{e}")
            return

        if self._snapshooter is None:
            messagebox.showwarning("No Camera", "Select a camera first.")
            return

        cam_name = self._active_cam.name if self._active_cam else "cam"
        path = get_photo_path(ext=self._snap_format, camera_name=cam_name)

        # Check if recording is active
        recording_active = self._recorder is not None and self._recorder.is_recording

        if self._active_cam and self._active_cam.source == "libcamera" and self._picam2 is not None and not recording_active:
            # High-res mode switch capture
            self._status.update_camera("Capturing high-res still...")
            self._root.update_idletasks()
            
            def _capture():
                self._capture_paused = True
                try:
                    log.info("Performing high-res still capture using libcamera at %s...", self._current_res)
                    ok = self._snapshooter.capture_full_res(path, size=self._current_res)
                    if ok:
                        self._save_media_metadata("photo", path)
                except Exception as e:
                    log.error("Error in high-res capture thread: %s", e)
                    ok = False
                finally:
                    self._capture_paused = False
                
                def _done():
                    self._status.update_camera(self._active_cam.name if self._active_cam else "No Camera")
                    if ok:
                        try:
                            from PIL import Image
                            im = Image.open(str(path))
                            self._panel.set_thumbnail(im, f"✓ {path.name}")
                        except Exception:
                            pass
                        messagebox.showinfo("Snapshot Saved", f"Photo saved to:\n{path}")
                    else:
                        messagebox.showerror("Snapshot Failed", "Could not capture photo.")
                
                self._root.after(0, _done)

            threading.Thread(target=_capture, daemon=True, name="still-capture").start()
        else:
            # Preview frame capture
            log.info("Capturing still from preview frame (V4L2 or recording active)...")
            frame = self._get_last_frame()
            if frame is not None:
                ok = self._snapshooter.capture_from_frame(frame, path)
                if ok:
                    self._save_media_metadata("photo", path)
            else:
                ok = False
            
            if ok:
                messagebox.showinfo("Snapshot Saved", f"Photo saved to:\n{path}")
            else:
                messagebox.showerror("Snapshot Failed", "Could not capture photo.")

    def _get_last_frame(self) -> Optional[np.ndarray]:
        """Grab one frame from the current capture source (non-blocking)."""
        if self._client_ip:
            return getattr(self, "_last_client_frame", None)
        try:
            if self._cap is not None and self._cap.isOpened():
                ret, frame = self._cap.read()
                return frame if ret else None
            if self._picam2 is not None:
                rgb = self._picam2.capture_array("main")
                return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
        except Exception as e:
            log.warning("get_last_frame error: %s", e)
        return None

    # ──────────────────────────────────────────────────────────────────
    # Timelapse
    # ──────────────────────────────────────────────────────────────────

    def _on_timelapse_toggle(self):
        if self._timelapse.is_running:
            self._timelapse.stop()
            self._panel.set_timelapse(False)
        else:
            if self._active_cam is None:
                messagebox.showwarning("No Camera", "Select a camera first.")
                return
            try:
                interval = float(self._panel._tl_interval.get())
            except Exception:
                interval = 5.0
            self._timelapse.start(interval_secs=interval)
            self._panel.set_timelapse(True)

    def _on_interval_change(self, interval: float):
        self._timelapse.set_interval(interval)

    def _on_timelapse_tick(self, frame_idx: int, session_id: str):
        """Called from the timelapse thread — capture one frame."""
        if self._snapshooter is None or self._active_cam is None:
            return
        cam_name = self._active_cam.name
        path = get_timelapse_path(session_id, frame_idx,
                                  ext=self._snap_format,
                                  camera_name=cam_name)
        
        recording_active = self._recorder is not None and self._recorder.is_recording
        interval = self._timelapse.interval_secs
        
        if self._active_cam.source == "libcamera" and self._picam2 is not None and not recording_active and interval >= 3.0:
            log.info("Capturing high-res still for timelapse...")
            self._capture_paused = True
            try:
                ok = self._snapshooter.capture_full_res(path)
                if ok:
                    self._save_media_metadata("timelapse", path, extra={"timelapse_frame": frame_idx, "timelapse_session": session_id})
            except Exception as e:
                log.error("Timelapse high-res capture failed: %s", e)
                ok = False
            finally:
                self._capture_paused = False
        else:
            log.info("Capturing fast preview frame for timelapse...")
            frame = self._get_last_frame()
            if frame is not None:
                ok = self._snapshooter.capture_from_frame(frame, path)
                if ok:
                    self._save_media_metadata("timelapse", path, extra={"timelapse_frame": frame_idx, "timelapse_session": session_id})
            else:
                ok = False

        # Update count on UI thread
        self._root.after(0,
            lambda c=frame_idx: self._panel.update_timelapse_count(c))

    # ──────────────────────────────────────────────────────────────────
    # Periodic status refresh
    # ──────────────────────────────────────────────────────────────────

    def _refresh_status(self):
        try:
            fps = self._preview.get_display_fps()
            self._status.update_fps(fps)

            used, total, pct = get_disk_usage()
            photos, videos, tl = count_files()
            self._panel.update_storage_display(
                human_size(used), human_size(total), pct,
                photos, videos, tl
            )
            self._status.update_storage(human_size(used), pct)
            try:
                from core.lidar_sensor import lidar
                d_cm, d_m, d_str = lidar.get_reading()
                self._panel.update_lidar_reading(d_cm, d_m, d_str)
            except Exception:
                pass
        except Exception as e:
            log.debug("Status refresh error: %s", e)
        finally:
            self._root.after(_STATUS_INTERVAL_MS, self._refresh_status)

    # ──────────────────────────────────────────────────────────────────
    # Menu actions
    # ──────────────────────────────────────────────────────────────────

    def _open_settings(self):
        from core.storage import BASE_DIR
        dlg = SettingsDialog(
            self._root,
            current_storage_path=str(BASE_DIR),
            current_snap_format=self._snap_format,
        )
        self._root.wait_window(dlg)
        if dlg.accepted:
            self._snap_format = dlg.snap_format.get()
            log.info("Settings updated: fmt=%s  path=%s",
                     self._snap_format, dlg.storage_path)

    def _open_storage_folder(self):
        import subprocess, platform
        from core.storage import MEDIA_DIR
        try:
            MEDIA_DIR.mkdir(parents=True, exist_ok=True)
            folder = str(MEDIA_DIR)
            if platform.system() == "Windows":
                subprocess.Popen(["explorer", folder])
            elif platform.system() == "Linux":
                subprocess.Popen(["xdg-open", folder])
            elif platform.system() == "Darwin":
                subprocess.Popen(["open", folder])
        except Exception as e:
            log.warning("Could not open media folder: %s", e)
            messagebox.showinfo("Media Storage Location", f"Media files are stored on laptop at:\n{MEDIA_DIR}")

    def _show_about(self):
        messagebox.showinfo(
            "About PiCamPro — i4 Marine",
            "PiCamPro — i4 Marine  v1.0.0\n\n"
            "Universal ROV & Marine Subsea Camera System\n"
            "Developed for i4 Marine\n\n"
            "Supports Pi 5, Pixhawk MAVLink, QGroundControl,\n"
            "Arducam 64MP, TFmini-S LiDAR, and USB cameras.\n\n"
            "Operating entirely over Ethernet (192.168.50.1)"
        )

    # ──────────────────────────────────────────────────────────────────
    # Window lifecycle
    # ──────────────────────────────────────────────────────────────────

    def _on_resize(self, _event=None):
        pass  # Canvas handles its own scaling

    def _on_close(self):
        log.info("Shutting down PiCamPro…")
        if self._recorder and self._recorder.is_recording:
            if messagebox.askyesno("Recording Active",
                                   "A recording is in progress.\n"
                                   "Stop recording and quit?"):
                self._recorder.stop_recording()
            else:
                return
        if self._timelapse.is_running:
            self._timelapse.stop()
        self._stop_capture()
        self._root.destroy()

    # ──────────────────────────────────────────────────────────────────
    # Theme
    # ──────────────────────────────────────────────────────────────────

    def _apply_theme(self):
        style = ttk.Style(self._root)
        try:
            style.theme_use("clam")
        except Exception:
            pass

        style.configure(".", background=C_BG, foreground=C_TEXT,
                         fieldbackground=C_PANEL, troughcolor=C_PANEL,
                         selectbackground=C_ACCENT, selectforeground="#000",
                         insertcolor=C_TEXT, font=("Segoe UI", 10))

        style.configure("TCombobox",
                         fieldbackground=C_PANEL, background=C_PANEL,
                         foreground=C_TEXT, selectbackground=C_ACCENT,
                         arrowcolor=C_TEXT)
        style.map("TCombobox",
                  fieldbackground=[("readonly", C_PANEL)],
                  foreground=[("readonly", C_TEXT)],
                  selectbackground=[("readonly", C_ACCENT)])

        style.configure("TScrollbar",
                         background=C_PANEL, troughcolor=C_BG,
                         arrowcolor=C_TEXT, borderwidth=0)

    def _send_arm_pan(self, target):
        """Sends Pan command or angle to Pi Arm controller on port 8080."""
        ip = (self._client_ip or "192.168.50.1").split(":")[0]
        def _post():
            import urllib.request, json
            try:
                url = f"http://{ip}:8080/api/pan"
                payload = {"angle": int(target)} if isinstance(target, (int, float)) else {"action": str(target)}
                req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    pass
            except Exception as e:
                log.debug("Pan post error: %s", e)
        threading.Thread(target=_post, daemon=True, name="arm-pan").start()

    def _send_arm_tilt(self, target):
        """Sends Tilt command to Pi Arm controller on port 8080."""
        ip = (self._client_ip or "192.168.50.1").split(":")[0]
        def _post():
            import urllib.request, json
            try:
                url = f"http://{ip}:8080/api/tilt"
                payload = {"angle": int(target)} if isinstance(target, (int, float)) else {"action": str(target)}
                req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    pass
            except Exception as e:
                log.debug("Tilt post error: %s", e)
        threading.Thread(target=_post, daemon=True, name="arm-tilt").start()

    def _send_arm_api(self, path: str, payload: dict):
        """Sends focus/zoom/arm API request to Pi on port 8080."""
        ip = (self._client_ip or "192.168.150.131").split(":")[0]
        def _post():
            import urllib.request, json
            try:
                url = f"http://{ip}:8080{path}"
                req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    pass
            except Exception as e:
                log.debug("Arm API post error (%s): %s", path, e)
        threading.Thread(target=_post, daemon=True, name="arm-api").start()

