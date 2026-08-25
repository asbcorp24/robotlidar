#!/usr/bin/env bash
set -euo pipefail

sudo apt update
sudo apt install -y \
  python3-gi \
  gir1.2-gst-rtsp-server-1.0 \
  gstreamer1.0-tools \
  gstreamer1.0-plugins-base \
  gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-ugly \
  ffmpeg

echo
echo "RTSP camera emulator dependencies installed."
echo "Run:"
echo "  python3 scripts/rtsp_camera_emulator.py"
echo
echo "Default RTSP URL:"
echo "  rtsp://127.0.0.1:8554/test"
