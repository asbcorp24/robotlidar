# Sipeed M1s stereo camera node

Firmware subproject for Sipeed M1s / BL808 used as a low-latency stereo camera head.

Target hardware:
- Sipeed M1s / BL808
- HBVCAM-4M2214HD-2 USB stereo UVC camera
- 2 servos: PAN + TILT
- Wi-Fi Internet uplink

Target video path:

```text
HBVCAM (1280x480@30 MJPEG SBS)
        -> USB Host/UVC
        -> MJPEG decode
        -> BL808 HW H.264 encode
        -> RTP/H.264 over UDP
        -> Internet
        -> robot/server
```

Control path:

```text
server -> UDP PTZ command -> M1s -> PWM PAN/TILT
M1s -> UDP telemetry -> server
```

## Initial streaming profile

- Input: 1280x480, 30 fps, MJPEG, SBS (640x480 per eye)
- Output: H.264, 1280x480, 30 fps
- Start bitrate: 2 Mbit/s
- Max bitrate: 3 Mbit/s
- GOP: 15 frames
- B frames: 0
- RTP payload type: 96
- RTP clock: 90000 Hz
- Default video destination port: 5004/UDP
- PTZ command port: 6000/UDP
- Telemetry destination port: 6001/UDP

## Directory layout

```text
include/
  app_config.h       network/video/PTZ defaults
  platform_bl808.h   hardware abstraction interface
  protocol.h         UDP PTZ + telemetry protocol
  ptz.h              servo smoothing API
  rtp_h264.h         H.264 RTP packetizer API
src/
  main.c              application state machine
  ptz.c               PAN/TILT smoothing and limits
  rtp_h264.c          RFC 6184 single-NAL/FU-A packetizer
  platform_bl808.c    BL808 integration points
```

## Integration strategy

This repository intentionally keeps the BL808-specific UVC/MJPEG/H.264 calls behind `platform_bl808.*`. The transport, RTP packetization, PTZ control and protocol are independent of the SDK version.

The hardware adapter must provide:
1. Wi-Fi STA connect and UDP sockets.
2. USB 2.0 host + UVC enumeration for HBVCAM-4M2214HD-2.
3. Select MJPEG 1280x480@30.
4. Decode each MJPEG frame into an encoder-supported pixel buffer.
5. Feed BL808 hardware H.264 encoder.
6. Call `app_on_h264_nal()` for every produced Annex-B NAL unit.
7. Generate 50 Hz PWM for the two servos.

## Build base

Recommended base is Sipeed `M1s_BL808_example` for the BL808/M1s board support. The project is structured so `src/` and `include/` can be added to its C906 application. CherryUSB/UVC and the BL808 multimedia APIs should be wired in `platform_bl808.c`.

Do not power the servos from the M1s regulator. Use a separate 5-6 V servo supply with common GND.

## PTZ protocol

Binary packets use network byte order. See `include/protocol.h`.

PTZ command contains:
- magic/version/type
- sequence
- pan target in 0.01 degrees
- tilt target in 0.01 degrees
- max movement speed in 0.01 degrees/s
- flags (`CENTER`, `REQUEST_IDR`)

The M1s performs local smoothing, so network jitter does not cause servo jitter.

## Current state

Implemented in this folder:
- application architecture
- PTZ command format and validation
- local servo target/speed limiting
- H.264 RTP packetizer including FU-A fragmentation
- telemetry structure
- BL808 hardware abstraction boundary

Still hardware-dependent and intentionally isolated in `platform_bl808.c`:
- exact CherryUSB UVC binding for this camera
- MJPEG decoder buffer plumbing
- BL808 H.264 encoder API calls
- board-specific PWM pin mapping
- Wi-Fi credentials/provisioning

These are the next bring-up steps on the physical M1s board.
