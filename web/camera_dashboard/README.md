# Camera Dashboard

Web interface for selecting remote Radxa ZERO 3E camera devices, watching live video, and controlling PAN/TILT.

This first version is a frontend prototype with a small API contract. It is intentionally independent from the final streaming backend so that the UI can be wired later to WebRTC/HLS/MSE without changing the control surface.

## Features

- device list with online/offline state
- one-click switching between camera devices
- large video viewport
- PAN/TILT controls
- center button
- keyboard arrows / WASD for PTZ
- telemetry panel (FPS, bitrate, latency, ethernet, uptime)
- placeholder stream support via per-device `streamUrl`
- backend API contract documented in `api-contract.md`

## Run locally

Any static HTTP server is enough for the UI prototype:

```bash
cd web/camera_dashboard
python3 -m http.server 8080
```

Open `http://localhost:8080`.

The prototype uses demo device data when `/api/devices` is not available.
