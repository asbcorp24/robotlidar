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
STREAM_SERVICE_FILE="/etc/systemd/system/orange-pi-zero-camera.service"
WEB_SERVICE_FILE="/etc/systemd/system/orange-pi-zero-web.service"

apt-get update
apt-get install -y python3 python3-websocket ffmpeg v4l-utils ca-certificates network-manager

mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

if [ ! -f "$CONFIG_FILE" ]; then
  cp "$APP_DIR/config.example.json" "$CONFIG_FILE"
  chmod 600 "$CONFIG_FILE"
  echo
  echo "Created $CONFIG_FILE"
fi

cp "$APP_DIR/orange-pi-zero-camera.service" "$STREAM_SERVICE_FILE"
cp "$APP_DIR/orange-pi-zero-web.service" "$WEB_SERVICE_FILE"

systemctl enable NetworkManager.service >/dev/null 2>&1 || true
systemctl restart NetworkManager.service || true
systemctl daemon-reload
systemctl enable orange-pi-zero-camera.service
systemctl enable orange-pi-zero-web.service
systemctl restart orange-pi-zero-web.service

echo
if ffmpeg -hide_banner -protocols 2>/dev/null | grep -qx '  srt'; then
  echo "FFmpeg SRT protocol: OK"
else
  echo "WARNING: this FFmpeg build does not appear to support SRT."
  echo "Run: ffmpeg -protocols | grep srt"
fi

echo
IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
printf '%s\n' \
  "Web config: http://${IP_ADDR:-ORANGE_PI_IP}:8088/" \
  "Config file: $CONFIG_FILE" \
  "Streamer: systemctl restart orange-pi-zero-camera" \
  "Web UI: systemctl restart orange-pi-zero-web" \
  "Streamer log: journalctl -u orange-pi-zero-camera -f" \
  "Web log: journalctl -u orange-pi-zero-web -f"
