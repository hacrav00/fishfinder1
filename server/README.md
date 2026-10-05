# 🐟 FishFinder ROV — Raspberry Pi Backend

This directory contains the complete backend services running on the Raspberry Pi:

### Components:
- **`arm_server.py`**: High-performance HTTP server & servo controller (`lgpio`).
  - **Pan Servo (GPIO 18 / Pin 12)**: 360° continuous rotation with zero-pulse cut (no creep, no jitter).
  - **Tilt Servo (GPIO 19 / Pin 35)**: 180° positional servo with 0.7s auto-sleep to eliminate undervoltage.
  - **REST API Endpoints**:
    - `POST /api/pan` (`{"action": "left"|"right"|"stop"}` or `{"angle": 0..180}`)
    - `POST /api/tilt` (`{"action": "up"|"down"}` or `{"angle": 0..180}`)
    - `GET /api/status`
- **`fishfinder-stream.service`**: Hardware MJPEG camera streamer (`ustreamer`) running on Port 8000 at 720p 30 FPS with <40ms latency.
- **`fishfinder-arm.service`**: Auto-restart daemon for `arm_server.py` on Port 8080.
- **`setup_pi.sh`**: One-line install script.
