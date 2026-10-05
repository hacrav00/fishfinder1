#!/bin/bash
set -e

echo "=== Installing FishFinder ROV Pi Backend ==="
sudo apt-get update
sudo apt-get install -y python3-lgpio python3-pip ustreamer libcamera-tools

# Copy server script
cp arm_server.py /home/i4mt/arm_server.py
chmod +x /home/i4mt/arm_server.py

# Install systemd services
sudo cp fishfinder-arm.service /etc/systemd/system/
sudo cp fishfinder-stream.service /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable fishfinder-arm.service fishfinder-stream.service
sudo systemctl restart fishfinder-arm.service fishfinder-stream.service

echo "=== Backend installed and running on ports 8000 (Stream) and 8080 (Servos) ==="
