# RobotLiDAR H.264 RTP -> WebRTC relay

Лёгкий видеорелей на Go/Pion. Он **не декодирует и не перекодирует H.264**.

```text
Radxa / Windows emulator
        |
        | H.264 RTP/UDP
        v
Pion relay (Go)
        |
        | DTLS/SRTP WebRTC
        v
Browser
```

`TrackLocalStaticRTP` получает уже сформированные RTP-пакеты H.264 и передаёт их WebRTC-клиентам. Для каждого браузера Pion меняет транспортные SSRC/payload type, но H.264 payload остаётся исходным.

## Требования

- Go 1.24+
- открытые UDP ingest-порты `10000+` от тракторов к серверу
- WebRTC UDP/TCP доступ от браузеров к серверу

Используется Pion WebRTC `v4.2.18`.

## Запуск

```bash
cd radxa_zero3e/server/webrtc_relay
go mod tidy
go run .
```

По умолчанию внутренний HTTP API слушает только:

```text
127.0.0.1:8090
```

FastAPI обращается к нему через `WEBRTC_RELAY_URL=http://127.0.0.1:8090`.

Для STUN можно задать:

```text
STUN_URL=stun:stun.example.com:3478
```

Для реального публичного сервера рекомендуется собственный STUN/TURN (coturn) и HTTPS.

## Внутренний API

Создать RTP ingest для трактора:

```http
POST /api/streams/TRACTOR-0001
Content-Type: application/json

{"rtp_port":10000}
```

Статус:

```http
GET /api/streams/TRACTOR-0001/status
GET /api/streams
```

WebRTC offer -> answer:

```http
POST /api/streams/TRACTOR-0001/webrtc
Content-Type: application/json

{"type":"offer","sdp":"..."}
```

Этот API предназначен только для локального FastAPI Camera Hub и по умолчанию не публикуется наружу.
