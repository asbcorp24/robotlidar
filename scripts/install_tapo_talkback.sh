#!/usr/bin/env bash
set -euo pipefail

VERSION="${GO2RTC_VERSION:-1.9.14}"
DEST="${GO2RTC_DEST:-/usr/local/bin/go2rtc}"

arch="$(uname -m)"
case "$arch" in
  aarch64|arm64) asset="go2rtc_linux_arm64" ;;
  armv7l|armhf|armv8l) asset="go2rtc_linux_arm" ;;
  armv6l) asset="go2rtc_linux_armv6" ;;
  x86_64|amd64) asset="go2rtc_linux_amd64" ;;
  *)
    echo "Unsupported architecture: $arch" >&2
    exit 2
    ;;
esac

url="https://github.com/AlexxIT/go2rtc/releases/download/v${VERSION}/${asset}"
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

echo "Downloading $url"
curl -fL "$url" -o "$tmp"
sudo install -m 0755 "$tmp" "$DEST"
"$DEST" -version || true

echo "go2rtc installed to $DEST"
echo "FFmpeg 5+ is also required for Tapo talkback."
ffmpeg -version | head -n 1 || true
