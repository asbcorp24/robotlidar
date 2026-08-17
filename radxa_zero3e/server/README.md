# Camera Hub Server

Standalone web server for Radxa ZERO 3E camera devices.

Architecture:

```text
Radxa ZERO 3E #1 --H264/RTP + telemetry--> Camera Hub Server <--browser
Radxa ZERO 3E #2 --H264/RTP + telemetry--> Camera Hub Server <--browser
Radxa ZERO 3E #N --H264/RTP + telemetry--> Camera Hub Server <--browser
                                      |
                                      +-- PTZ commands --> selected Radxa
```

The server is intentionally separate from `firmware/` and from the static web UI.

## Stack

- Python 3.11+
- FastAPI
- Uvicorn
- WebSocket for live device status/PTZ signaling
- REST API for device list and control
- static frontend from `web/camera_dashboard`

For video transport from Radxa the target architecture is H.264/RTP over UDP into a per-device ingest port. Browser delivery will be added through WebRTC so latency stays low.

## Initial API

- `GET /api/devices`
- `POST /api/devices/{device_id}/register`
- `POST /api/devices/{device_id}/telemetry`
- `POST /api/devices/{device_id}/ptz`
- `POST /api/devices/{device_id}/center`
- `POST /api/devices/{device_id}/request-idr`
- `WS /ws/devices`

## Device model

Each Radxa device has:
- `device_id`
- display name
- IP address
- RTP ingest port
- PTZ UDP port
- online/offline state
- last seen time
- telemetry

The Radxa always initiates registration/telemetry toward this server. PTZ commands from the web interface are routed by the server to the selected Radxa.
