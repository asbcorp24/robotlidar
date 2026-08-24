#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root: sudo ./install.sh"
  exit 1
fi

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR=/opt/robotlidar-orange
CFG_DIR=/etc/robotlidar-orange
USER_NAME=robotlidar

apt-get update
apt-get install -y ffmpeg ca-certificates

if ! command -v go >/dev/null 2>&1; then
  echo "Go 1.24+ is required. Install Go, then run this script again."
  exit 2
fi

if ! id "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$USER_NAME"
fi

mkdir -p "$APP_DIR" "$CFG_DIR"
cd "$SRC_DIR"
go mod tidy
go build -trimpath -ldflags='-s -w' -o "$APP_DIR/orange-pi-ipcam" .

if [[ ! -f "$CFG_DIR/config.json" ]]; then
  cp config.example.json "$CFG_DIR/config.json"
fi
cp orange-pi-ipcam.service /etc/systemd/system/orange-pi-ipcam.service
chown -R "$USER_NAME:$USER_NAME" "$APP_DIR" "$CFG_DIR"

# USB-UART devices are commonly owned by dialout.
usermod -a -G dialout "$USER_NAME" || true

systemctl daemon-reload
systemctl enable orange-pi-ipcam.service

echo
echo "Installed. Edit: $CFG_DIR/config.json"
echo "Then start: sudo systemctl restart orange-pi-ipcam"
echo "Logs: sudo journalctl -u orange-pi-ipcam -f"
