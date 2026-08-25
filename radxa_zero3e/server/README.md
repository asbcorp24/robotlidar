# RobotLiDAR Camera Hub — Go server

Центральный сервер RobotLiDAR полностью реализован на Go и обслуживает разные типы тракторных шлюзов под одним API/device_id.

## Архитектура

```text
Raspberry Pi + RTSP camera
        |
        | SRT/MPEG-TS/H.264 (reliable, no transcode)
        v
+--------------------------------------+
| robotlidar-server                    |
|                                      |
| HTTP/REST + users/sessions + SQLite  |
| pure-Go SRT + MPEG-TS/PES parser     |
| H.264 -> RTP packetizer in memory    |
| Pion WebRTC                          |
| PTZ / drive / brush control          |
| embedded web                         |
+------------------+-------------------+
                   |
                   | WebRTC
                   v
                Browser

Legacy Radxa / Orange Pi / Windows emulator clients can still use direct H.264/RTP ingest.
```

H.264 на сервере не декодируется и не перекодируется. Для Raspberry серверу не требуется FFmpeg: SRT принимает pure-Go библиотека, MPEG-TS/PES разбирается в Go, а исходный Annex-B H.264 сразу пакетизуется в RTP и записывается в Pion track.

## Клиенты

### Raspberry Pi RobotLiDAR

Корневой проект `robotlidar/` использует IP-камеру по RTSP/TCP. Raspberry запускает FFmpeg только как лёгкий remux:

```text
RTSP/TCP -> H.264 copy -> MPEG-TS -> SRT -> Go server
```

Используется `-c:v copy`, поэтому на Raspberry нет декодирования/повторного кодирования видео.

### Radxa ZERO 3E

`radxa_zero3e/device/` — USB stereo UVC camera + аппаратный H.264 RKMPP + локальные PAN/TILT servo. Старый RTP uplink сохраняется для совместимости.

### Orange Pi PC + IP camera

`radxa_zero3e/orange_pi_pc_ipcam/` — IP-камера и ESP32 через Orange Pi. Старый RTP uplink сохраняется для совместимости.

### Windows emulator

`tools/rtsp_camera_emulator_windows/` — RTSP-камера для Raspberry и полный тестовый трактор для центрального сервера.

## Структура сервера

```text
radxa_zero3e/server/
├── main.go
├── auth.go
├── devices.go
├── media.go
├── srt_bridge.go
├── control.go
├── util.go
├── webrtc_config.go
├── go.mod
└── web/
```

## Запуск

Требуется Go 1.24+.

```bash
cd radxa_zero3e/server
go mod download
go build -trimpath -ldflags="-s -w" -o robotlidar-server .
./robotlidar-server
```

Переменные окружения:

```text
LISTEN_ADDR=0.0.0.0:8000
DB_PATH=camera_hub.db
STUN_URL=stun:stun.example.com:3478
WEBRTC_UDP_MIN=40000
WEBRTC_UDP_MAX=40100
```

## Device ID и пользователи

У каждого трактора постоянный уникальный ID, например `TRACTOR-0001`. Пользователь входит в web, добавляет этот ID в `Настройки` и после этого может видеть видео и управлять только своими тракторами.

## Видео

Новый Raspberry uplink:

```text
Camera H.264
 -> RTSP/TCP
 -> Raspberry FFmpeg (-c:v copy)
 -> MPEG-TS/SRT
 -> Go SRT receiver
 -> Go MPEG-TS/PES parser
 -> Go H.264 RTP packetizer
 -> Pion TrackLocalStaticRTP
 -> WebRTC/SRTP
 -> Browser
```

SRT использует восстановление потерянных пакетов и небольшой latency buffer, поэтому интернет-канал устойчивее обычного RTP/UDP.

Legacy uplink остаётся доступным:

```text
H.264/RTP/UDP -> Go RTP ingest -> Pion WebRTC
```

## Управление трактором

В web есть вперёд/назад/влево/вправо/STOP, скорость, W/A/S/D и Space. Сервер передаёт нормированные значения левой/правой гусеницы `-1000..+1000`.

Пока кнопка движения удерживается, browser повторяет активную команду примерно каждые 180 мс. Если browser/сеть пропадает, heartbeat прекращается и watchdog Raspberry/ESP32 переводит привод в STOP.

## Щётка

Поддерживаются вращение, STOP, скорость, поднять и опустить. Подъём/опускание — hold-to-run. Активные brush-команды также поддерживаются heartbeat.

## Управляющий UDP протокол

Сервер отправляет команды на control/PTZ UDP port устройства фиксированными 16-байтными пакетами.

```text
type 1 = camera PTZ
type 2 = tractor drive: left/right
type 3 = brush: spin/lift
```

Drive и brush используют диапазон `-1000..+1000`.

## Основные API

```text
POST /api/auth/register
POST /api/auth/login
GET  /api/auth/me
POST /api/auth/logout

GET    /api/settings/devices
POST   /api/settings/devices
DELETE /api/settings/devices/{device_id}

GET  /api/devices
POST /api/devices/{id}/webrtc
POST /api/devices/{id}/ptz
POST /api/devices/{id}/center
POST /api/devices/{id}/request-idr
POST /api/devices/{id}/drive
POST /api/devices/{id}/drive-stop
POST /api/devices/{id}/brush

POST /api/devices/{id}/register
POST /api/devices/{id}/telemetry
```

Подробный формат находится в `web/api-contract.md`.
