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


# Ethernet-only build: Wi-Fi is intentionally disabled.
systemctl disable orange-pi-zero-network-fallback.service >/dev/null 2>&1 || true
systemctl stop orange-pi-zero-network-fallback.service >/dev/null 2>&1 || true
systemctl disable hostapd.service >/dev/null 2>&1 || true
systemctl stop hostapd.service >/dev/null 2>&1 || true
systemctl disable dnsmasq.service >/dev/null 2>&1 || true
systemctl stop dnsmasq.service >/dev/null 2>&1 || true
nmcli radio wifi off >/dev/null 2>&1 || true
rm -f /etc/systemd/system/orange-pi-zero-network-fallback.service
rm -f /etc/NetworkManager/conf.d/10-xr819.conf

systemctl restart NetworkManager.service || true

# Tapo SD-card archive support. The deployed web service uses the custom
# Python 3.8 runtime on this Orange Pi image when it is available.
PYTHON_BIN="/usr/local/bin/python3.8"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3)"
fi
if ! "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
  "$PYTHON_BIN" -m ensurepip --upgrade >/dev/null 2>&1 || true
fi
"$PYTHON_BIN" -m pip install --upgrade "pytapo==3.4.26" || \
  echo "WARNING: pytapo installation failed; Tapo SD archive will be unavailable until pytapo is installed."

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

systemctl daemon-reload
systemctl enable orange-pi-zero-camera.service
systemctl enable orange-pi-zero-web.service
systemctl enable orange-pi-zero-display.service
systemctl restart orange-pi-zero-web.service
systemctl restart orange-pi-zero-display.service || true

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
  "Streamer log: journalctl -u orange-pi-zero-camera -f" \
  "Web log: journalctl -u orange-pi-zero-web -f" \
  "OLED log: journalctl -u orange-pi-zero-display -f"
