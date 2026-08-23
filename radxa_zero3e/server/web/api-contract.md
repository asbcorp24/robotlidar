# RobotLiDAR Camera Hub API

Все пользовательские endpoints, кроме login/register, требуют:

```http
Authorization: Bearer <token>
```

## Авторизация

```text
POST /api/auth/register
POST /api/auth/login
GET  /api/auth/me
POST /api/auth/logout
```

Login/register JSON:

```json
{
  "username": "operator",
  "password": "secret"
}
```

## Привязка тракторов

```text
GET    /api/settings/devices
POST   /api/settings/devices
DELETE /api/settings/devices/{device_id}
```

Добавление:

```json
{
  "device_id": "TRACTOR-0001",
  "alias": "Трактор №1"
}
```

## Список моих тракторов

```text
GET /api/devices
```

Ответ:

```json
{
  "devices": [
    {
      "id": "TRACTOR-0001",
      "device_id": "TRACTOR-0001",
      "name": "Трактор №1",
      "online": true,
      "video_online": true,
      "streamType": "webrtc",
      "streamUrl": "/api/devices/TRACTOR-0001/webrtc",
      "pan": 12.5,
      "tilt": -4.0,
      "fps": 30,
      "bitrateKbps": 2000,
      "ethernet": "1000 Mbit/s",
      "uptimeSec": 12345
    }
  ]
}
```

## PTZ

```text
POST /api/devices/{device_id}/ptz
```

```json
{
  "pan_cdeg": 2500,
  "tilt_cdeg": -1000,
  "speed_cdeg_s": 4000,
  "request_idr": false
}
```

Углы передаются в сотых долях градуса.

Дополнительно:

```text
POST /api/devices/{device_id}/center
POST /api/devices/{device_id}/request-idr
```

## WebRTC

```text
POST /api/devices/{device_id}/webrtc
```

Offer:

```json
{
  "type": "offer",
  "sdp": "v=0..."
}
```

Answer:

```json
{
  "type": "answer",
  "sdp": "v=0..."
}
```

Видео — H.264 RTP passthrough через Pion. Сервер не выполняет decode/encode.

## Radxa / emulator API

Регистрация устройства:

```text
POST /api/devices/{device_id}/register
```

```json
{
  "name": "Radxa ZERO 3E",
  "ip": "192.168.1.50",
  "rtp_port": 5004,
  "ptz_port": 6000
}
```

Сервер возвращает назначенный UDP порт:

```json
{
  "ok": true,
  "video_ingest_port": 10000
}
```

Телеметрия:

```text
POST /api/devices/{device_id}/telemetry
```

```json
{
  "fps": 30,
  "bitrate_bps": 2000000,
  "dropped_frames": 0,
  "uptime_ms": 123456,
  "pan_cdeg": 0,
  "tilt_cdeg": 0,
  "link_mbps": 1000
}
```

Статус видео владельца:

```text
GET /api/devices/{device_id}/video-status
```
