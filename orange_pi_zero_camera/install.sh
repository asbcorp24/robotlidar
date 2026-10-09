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
DISPLAY_SERVICE_FILE="/etc/systemd/system/orange-pi-zero-display.service"
FALLBACK_SERVICE_FILE="/etc/systemd/system/orange-pi-zero-network-fallback.service"

apt-get update
apt-get install -y python3 python3-websocket ffmpeg v4l-utils ca-certificates i2c-tools network-manager

# Let NetworkManager manage interfaces that are also present in /etc/network/interfaces.
# This is required for changing DHCP/static IPv4 from the local web panel.
if [ -f /etc/NetworkManager/NetworkManager.conf ]; then
  if grep -q '^\[ifupdown\]' /etc/NetworkManager/NetworkManager.conf; then
    if grep -q '^managed=' /etc/NetworkManager/NetworkManager.conf; then
      sed -i '/^\[ifupdown\]/,/^\[/{s/^managed=.*/managed=true/}' /etc/NetworkManager/NetworkManager.conf
    else
      sed -i '/^\[ifupdown\]/a managed=true' /etc/NetworkManager/NetworkManager.conf
    fi
  else
    printf '\n[ifupdown]\nmanaged=true\n' >> /etc/NetworkManager/NetworkManager.conf
  fi
fi
systemctl enable NetworkManager.service || true
systemctl restart NetworkManager.service || true

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
cp "$APP_DIR/orange-pi-zero-display.service" "$DISPLAY_SERVICE_FILE"
cp "$APP_DIR/orange-pi-zero-network-fallback.service" "$FALLBACK_SERVICE_FILE"
chmod +x "$APP_DIR/network_fallback.sh"

systemctl daemon-reload
systemctl enable orange-pi-zero-camera.service
systemctl enable orange-pi-zero-web.service
systemctl enable orange-pi-zero-display.service
systemctl enable orange-pi-zero-network-fallback.service
systemctl restart orange-pi-zero-web.service
systemctl restart orange-pi-zero-display.service || true
systemctl restart orange-pi-zero-network-fallback.service || true

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
  "Ethernet web config: http://${IP_ADDR:-ORANGE_PI_IP}:8088/" \
  "Config file: $CONFIG_FILE" \
  "Streamer: systemctl restart orange-pi-zero-camera" \
  "Web UI: systemctl restart orange-pi-zero-web" \
  "OLED: systemctl restart orange-pi-zero-display" \
  "Fallback AP: systemctl restart orange-pi-zero-network-fallback" \
  "Setup AP (when offline): RobotLiDAR-Setup / http://10.42.0.1:8088/" \
  "Streamer log: journalctl -u orange-pi-zero-camera -f" \
  "Web log: journalctl -u orange-pi-zero-web -f" \
  "OLED log: journalctl -u orange-pi-zero-display -f"
