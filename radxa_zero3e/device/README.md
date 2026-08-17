# Radxa ZERO 3E stereo camera device

This directory contains ONLY the additional camera/PTZ program for Radxa ZERO 3E. It is independent from the repository's existing Python/ROS robot code.

Hardware:
- Radxa ZERO 3E / RK3566
- HBVCAM-4M2214HD-2 USB stereo UVC camera
- Gigabit Ethernet
- PAN + TILT servos

Video path:

```text
HBVCAM -> USB UVC MJPEG 1280x480@30 -> Radxa ZERO 3E -> h264_rkmpp -> RTP/H.264 -> Ethernet -> central server
```

Control path:

```text
central server -> UDP PTZ -> Radxa ZERO 3E -> PWM -> PAN/TILT
```

Build:

```bash
cd radxa_zero3e/device
mkdir -p build && cd build
cmake ..
make -j$(nproc)
```

The existing ROS/Python robot project in the repository root is not a dependency of this module and is not modified by it.
