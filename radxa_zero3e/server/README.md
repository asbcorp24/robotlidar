# RobotLiDAR Camera Hub — Go server

Центральный сервер для тракторов с Radxa ZERO 3E теперь полностью реализован на Go.

## Архитектура

```text
Radxa / Windows emulator
        |
        | готовый H.264 / RTP / UDP
        v
+--------------------------------------+
| robotlidar-server (один Go-процесс)  |
|                                      |
|  HTTP + REST API                     |
|  users / login / sessions            |
|  SQLite                              |
|  user <-> tractor device_id          |
|  telemetry                           |
|  PTZ / CENTER / IDR                  |
|  RTP ingest 10000+                   |
|  Pion WebRTC H.264 passthrough       |
|  встроенный web-интерфейс            |
+------------------+-------------------+
                   |
                   | WebRTC / DTLS / SRTP
                   v
                Browser
```

H.264 на сервере **не декодируется и не перекодируется**. Готовые RTP-пакеты от Radxa передаются в `Pion TrackLocalStaticRTP`, а браузер декодирует H.264 аппаратно/штатным WebRTC-декодером.

## Структура

```text
radxa_zero3e/server/
├── main.go        # запуск HTTP, SQLite, встроенного web
├── auth.go        # пользователи, пароли, сессии, привязка ID
├── devices.go     # регистрация тракторов, telemetry, список устройств
├── media.go       # RTP ingest, Pion WebRTC, PTZ
├── util.go
├── go.mod
├── web/
└── camera_hub.db  # создаётся автоматически
```

Python/FastAPI и отдельный `webrtc_relay` больше не нужны.

## Требования

- Go 1.24+
- открытый HTTP/HTTPS порт сервера;
- UDP `10000+` от Radxa к серверу для RTP;
- доступные WebRTC UDP-порты сервера для браузеров.

Используются:

- Pion WebRTC v4;
- pure-Go SQLite `modernc.org/sqlite` — CGO не требуется;
- существующий формат SQLite базы совместим с предыдущей Python-версией.

## Запуск на Windows

```bat
cd radxa_zero3e\server
go mod tidy
go run .
```

или:

```bat
run_server.bat
```

Открыть:

```text
http://127.0.0.1:8000
```

## Сборка одного EXE

```bat
go build -trimpath -ldflags="-s -w" -o robotlidar-server.exe .
robotlidar-server.exe
```

Web-интерфейс встроен в бинарник через `go:embed`, поэтому рядом с EXE не требуется отдельный web-сервер.

## Linux

```bash
cd radxa_zero3e/server
go mod tidy
go build -trimpath -ldflags='-s -w' -o robotlidar-server .
./robotlidar-server
```

Настройки окружения:

```text
LISTEN_ADDR=0.0.0.0:8000
DB_PATH=camera_hub.db
STUN_URL=stun:stun.example.com:3478
```

`STUN_URL` для локального теста не обязателен.

## Device ID

У каждого трактора постоянный уникальный ID, например:

```text
TRACTOR-0001
TRACTOR-0002
TRACTOR-0003
```

При старте Radxa/эмулятор делает:

```http
POST /api/devices/TRACTOR-0001/register
```

Сервер выделяет RTP ingest-порт:

```json
{
  "ok": true,
  "video_ingest_port": 10000
}
```

После этого трактор отправляет H.264/RTP непосредственно на UDP `10000`.

Следующие устройства получают `10001`, `10002` и т.д.

## Пользователи

Пользователь:

1. входит по логину/паролю;
2. открывает `Настройки`;
3. добавляет ID своего трактора;
4. видит только привязанные к своему аккаунту тракторы;
5. может смотреть их видео и отправлять PTZ-команды.

Один `device_id` нельзя одновременно привязать к двум аккаунтам.

Пароли сохраняются как `PBKDF2-SHA256`, совместимо с ранее созданной `camera_hub.db`.

## Видео

```text
HBVCAM
  -> Radxa h264_rkmpp Baseline
  -> B-frames=0
  -> GOP=15
  -> SPS/PPS на keyframe
  -> RTP/UDP
  -> Go/Pion passthrough
  -> WebRTC/SRTP
  -> Chrome / Edge / другой H.264 WebRTC browser
```

При 30 fps и GOP 15 новый IDR появляется примерно каждые 0.5 секунды, поэтому новый зритель быстро начинает декодирование.

## API

Пользовательские:

```text
POST   /api/auth/register
POST   /api/auth/login
GET    /api/auth/me
POST   /api/auth/logout
GET    /api/settings/devices
POST   /api/settings/devices
DELETE /api/settings/devices/{device_id}
GET    /api/devices
GET    /api/devices/{device_id}/video-status
POST   /api/devices/{device_id}/webrtc
POST   /api/devices/{device_id}/ptz
POST   /api/devices/{device_id}/center
POST   /api/devices/{device_id}/request-idr
```

Для Radxa/эмулятора:

```text
POST /api/devices/{device_id}/register
POST /api/devices/{device_id}/telemetry
```

## Публичный сервер

Для эксплуатации через Интернет далее нужны:

- HTTPS, например nginx;
- собственный STUN/TURN (coturn);
- firewall для диапазона RTP ingest;
- ограничение диапазона ICE/WebRTC UDP портов;
- отдельный секрет/ключ устройства в дополнение к публичному `device_id`.
