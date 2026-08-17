#!/usr/bin/env bash
set -euo pipefail

sudo apt-get update
sudo apt-get install -y \
  build-essential cmake git pkg-config \
  v4l-utils libdrm-dev librga-dev librockchip-mpp-dev \
  libx264-dev libx265-dev

if ! ffmpeg -hide_banner -encoders 2>/dev/null | grep -q h264_rkmpp; then
  work="${HOME}/src/ffmpeg-rockchip"
  mkdir -p "$(dirname "$work")"
  if [ ! -d "$work/.git" ]; then
    git clone https://github.com/nyanmisaka/ffmpeg-rockchip "$work"
  else
    git -C "$work" pull --ff-only
  fi
  pushd "$work"
  ./configure --prefix=/usr \
    --enable-gpl --enable-version3 \
    --enable-libdrm --enable-rkmpp --enable-rkrga \
    --enable-libx264 --enable-libx265
  make -j"$(nproc)"
  sudo make install
  popd
fi

cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(nproc)"
sudo cmake --install build

echo
echo "Installed /usr/local/bin/radxa_stereo_node"
echo "Check camera: v4l2-ctl --list-devices"
echo "Check formats: v4l2-ctl -d /dev/video0 --list-formats-ext"
echo "Check encoder: ffmpeg -hide_banner -encoders | grep rkmpp"
echo "Check Ethernet: ip -br a"
echo "Check PWM: ls -l /sys/class/pwm/"
