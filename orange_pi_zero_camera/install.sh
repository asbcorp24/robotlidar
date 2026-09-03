#!/usr/bin/env bash
set -euo pipefail

if [ "${EUID}" -ne 0 ]; then
  echo "Run as root: sudo ./install.sh"
  exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APP_DIR="$REPO_DIR/orange_pi_zero_camera"
CONFIG_DIR="/etc/robotlidar"
CONFIG_FILE="$CONFIG_DIR/orange-pi-zero-camera.json"
SERVICE_FILE="/etc/systemd/system/orange-pi-zero-camera.service"

apt-get update
apt-get install -y python3 ffmpeg v4l-utils ca-certificates

mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

if [ ! -f "$CONFIG_FILE" ]; then
  cp "$APP_DIR/config.example.json" "$CONFIG_FILE"
  chmod 600 "$CONFIG_FILE"
  echo
  echo "Created $CONFIG_FILE"
  echo "EDIT IT BEFORE STARTING THE SERVICE."
fi

cp "$APP_DIR/orange-pi-zero-camera.service" "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable orange-pi-zero-camera.service

echo
if ffmpeg -hide_banner -protocols 2>/dev/null | grep -qx '  srt'; then
  echo "FFmpeg SRT protocol: OK"
else
  echo "WARNING: this FFmpeg build does not appear to support SRT."
  echo "Run: ffmpeg -protocols | grep srt"
fi

echo
printf '%s\n' \
  "1. Edit: nano $CONFIG_FILE" \
  "2. Test camera: v4l2-ctl --list-devices" \
  "3. Start: systemctl restart orange-pi-zero-camera" \
  "4. Log: journalctl -u orange-pi-zero-camera -f"
