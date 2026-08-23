# Radxa ZERO 3E stereo camera node

Target hardware:

- Radxa ZERO 3E / RK3566
- HBVCAM-4M2214HD-2 USB stereo UVC camera
- Gigabit Ethernet as the primary network path
- 2 servos: PAN + TILT

## Architecture

```text
HBVCAM-4M2214HD-2
  USB UVC / MJPEG 1280x480@30
              |
              v
       Radxa ZERO 3E
       /dev/video0
              |
              v
       ffmpeg-rockchip
       h264_rkmpp HW encoder
              |
              v
         RTP/H.264 UDP
              |
              v
       Gigabit Ethernet
              |
              v
            server

server --UDP PTZ--> ZERO 3E --PWM--> PAN servo
                           \--PWM--> TILT servo
ZERO 3E --UDP telemetry--> server
```

The 1280x480 image is kept as one SBS stereo frame, so synchronization performed by the USB camera is preserved.

## Network defaults

- video: server-assigned UDP port, RTP/H.264
- PTZ commands: ZERO 3E UDP 6000
- Ethernet interface default: `eth0`

ZERO 3E has onboard Gigabit Ethernet. No Wi-Fi is required by this project.

## Video defaults

- UVC input: MJPEG
- resolution: 1280x480
- frame rate: 30 fps
- hardware encoder: `h264_rkmpp`
- rate control: CBR
- bitrate: 2 Mbit/s
- max bitrate: 3 Mbit/s
- GOP: 15
- B frames: 0
- profile: Baseline
- SPS/PPS repeated on keyframes via `dump_extra`
- RTP packet size: 1200 bytes

The profile/GOP are intentionally browser-friendly because the central server performs H.264 RTP -> WebRTC passthrough without transcoding.

## Initial OS

Use Radxa OS / Debian 12 Bookworm for ZERO 3E. Connect Ethernet and verify:

```bash
ip -br a
ip route
ping -c 3 8.8.8.8
```

## Camera bring-up

Connect HBVCAM to the USB 3.0 HOST Type-C port using a suitable host adapter/cable.

```bash
lsusb
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video0 --list-formats-ext
```

We expect a mode close to:

```text
MJPG 1280x480 30 fps
```

Test raw camera capture before running the node:

```bash
ffmpeg -f v4l2 -input_format mjpeg -video_size 1280x480 -framerate 30 \
  -i /dev/video0 -frames:v 30 -f null -
```

## Install

```bash
cd radxa_zero3e/device
chmod +x install.sh
./install.sh
```

The script installs build tools, V4L2 utilities and Rockchip dependencies. If the installed FFmpeg does not expose `h264_rkmpp`, it builds `ffmpeg-rockchip` with RKMPP/RKRGA support.

Verify:

```bash
ffmpeg -hide_banner -encoders | grep rkmpp
ffmpeg -hide_banner -h encoder=h264_rkmpp
```

You should see `h264_rkmpp`; the encoder supports the H.264 baseline profile on compatible MPP hardware/builds.

## PWM / servo outputs

ZERO 3E uses 3.3 V GPIO logic. Do NOT power either servo from the GPIO header regulator. Use a separate 5-6 V servo supply and connect its GND to ZERO 3E GND.

Recommended physical header pins for the two hardware PWM signals:

- pin 16: `PWM8_M0`
- pin 18: `PWM9_M0`

Enable the appropriate PWM device-tree overlays using:

```bash
sudo rsetup
```

Then inspect the actual Linux PWM devices:

```bash
ls -l /sys/class/pwm/
for d in /sys/class/pwm/pwmchip*; do
  echo "=== $d ==="
  cat "$d/npwm"
done
```

The exact `pwmchipN` numbering depends on the enabled overlays/kernel, so the application takes the paths as command-line parameters instead of hardcoding them.

## Run manually

Example only; replace the server IP, assigned RTP port and PWM chip paths with the real values:

```bash
sudo /usr/local/bin/radxa_stereo_node \
  --server 192.168.1.100 \
  --eth-if eth0 \
  --camera /dev/video0 \
  --ffmpeg /usr/bin/ffmpeg \
  --video-port 10000 \
  --control-port 6000 \
  --telemetry-port 6001 \
  --width 1280 --height 480 --fps 30 \
  --bitrate 2000 --maxrate 3000 --gop 15 \
  --pan-chip /sys/class/pwm/pwmchip0 --pan-channel 0 \
  --tilt-chip /sys/class/pwm/pwmchip1 --tilt-channel 0
```

## PTZ ranges

Current defaults:

- PAN: -90..+90 degrees
- TILT: -45..+45 degrees
- PAN pulse: 700..2300 us
- TILT pulse: 900..2100 us
- PWM frequency: 50 Hz
- motion is smoothed locally on ZERO 3E

Servo limits must be calibrated for the actual mechanical mount before allowing full travel.

## systemd

Edit `radxa-stereo.service` and set server/camera/PWM parameters, then:

```bash
sudo cp radxa-stereo.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now radxa-stereo
sudo systemctl status radxa-stereo
journalctl -u radxa-stereo -f
```

## First bring-up order

1. Boot ZERO 3E and verify Ethernet.
2. Verify HBVCAM appears as UVC/V4L2.
3. Confirm 1280x480@30 MJPEG mode.
4. Confirm `h264_rkmpp` encoder and baseline profile support.
5. Start Camera Hub + Pion relay and register the device to obtain its RTP port.
6. Test H.264 RTP passthrough to the browser without servos.
7. Enable PWM overlays and identify the actual `pwmchipN` paths.
8. Connect servos using an external 5-6 V supply with common GND.
9. Test CENTER and limited PAN/TILT travel.
10. Enable the systemd service.
