# Camera Dashboard API contract

## GET /api/devices

```json
[
  {
    "id": "cam-001",
    "name": "Robot 01",
    "location": "Workshop",
    "online": true,
    "streamType": "webrtc",
    "streamUrl": "/api/devices/cam-001/webrtc",
    "pan": 12.5,
    "tilt": -4.2,
    "fps": 30,
    "bitrateKbps": 2100,
    "latencyMs": 72,
    "ethernet": "1 Gbit/s",
    "uptimeSec": 128340
  }
]
```

## POST /api/devices/:id/ptz

```json
{
  "pan": 20.0,
  "tilt": -10.0,
  "speed": 30.0
}
```

or relative movement:

```json
{
  "dPan": 5.0,
  "dTilt": 0.0,
  "speed": 30.0
}
```

## POST /api/devices/:id/center

No request body required.

## POST /api/devices/:id/request-idr

Requests a fresh H.264 IDR frame from the selected Radxa node.

## WebSocket /ws

Suggested realtime messages:

```json
{"type":"telemetry","deviceId":"cam-001","fps":30,"bitrateKbps":2050,"latencyMs":68,"pan":12.5,"tilt":-4.0}
```

```json
{"type":"device-state","deviceId":"cam-002","online":false}
```

## Streaming

Preferred final mode: WebRTC from the central server to browsers. The dashboard only needs a per-device stream endpoint/session descriptor; Radxa nodes continue sending H.264 upstream to the server over Ethernet/Internet.
