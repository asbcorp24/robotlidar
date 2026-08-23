# RobotLiDAR Camera Hub — Go server

Центральный сервер для тракторов с Radxa ZERO 3E полностью реализован на Go.

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

H.264 на сервере **не декодируется и не перекодируется**. Готовые RTP-пакеты от Radxa передаются в `Pion TrackLocalStaticRTP`, а браузер декодирует H.264 штатным WebRTC-декодером.

## Структура

```text
radxa_zero3e/server/
├── main.go
├── auth.go
├── devices.go
├── media.go
├── util.go
├── go.mod
├── web/
└── camera_hub.db
```

Python/FastAPI и отдельный `webrtc_relay` больше не нужны.

## Требования

- Go 1.24+
- открытый HTTP/HTTPS порт сервера;
- UDP `10000+` от Radxa к серверу для RTP;
- доступные WebRTC UDP-порты сервера для браузеров.

Используются Pion WebRTC v4 и pure-Go SQLite `modernc.org/sqlite`.

## Запуск

Windows:

```bat
cd radxa_zero3e\server
go mod tidy
go run .
```

или `run_server.bat`.

Сборка EXE:

```bat
go build -trimpath -ldflags="-s -w" -o robotlidar-server.exe .
robotlidar-server.exe
```

Linux:

```bash
cd radxa_zero3e/server
go mod tidy
go build -trimpath -ldflags='-s -w' -o robotlidar-server .
./robotlidar-server
```

Переменные окружения:

```text
LISTEN_ADDR=0.0.0.0:8000
DB_PATH=camera_hub.db
STUN_URL=stun:stun.example.com:3478
```

## Device ID

У каждого трактора постоянный уникальный ID, например `TRACTOR-0001`. При регистрации сервер назначает ему RTP ingest-порт `10000+`.

## Пользователи

Пользователь входит по логину/паролю, в `Настройки` добавляет ID своего трактора и после этого видит только свои устройства. WebRTC и PTZ проверяют принадлежность выбранного ID текущему пользователю.

Пароли сохраняются как `PBKDF2-SHA256`; формат базы совместим с предыдущей реализацией.

## Видео

```text
HBVCAM SBS
  -> Radxa h264_rkmpp Baseline
  -> B-frames=0
  -> GOP=15
  -> SPS/PPS на keyframe
  -> RTP/UDP
  -> Go/Pion passthrough
  -> WebRTC/SRTP
  -> Browser
```

## Cardboard / VR

В веб-интерфейсе есть кнопка `Cardboard` для выбранного онлайн-трактора.

После нажатия:

1. браузер запрашивает доступ к датчикам ориентации телефона;
2. включается полноэкранный landscape VR-режим;
3. SBS-кадр делится: левая половина идёт левому глазу, правая — правому;
4. исходное направление головы запоминается как центр;
5. относительное вращение телефона преобразуется в PAN/TILT выбранного трактора;
6. PTZ-команды ограничиваются диапазонами PAN `-90..+90` и TILT `-45..+45` и отправляются примерно 10 раз/с;
7. кнопка `Центр` в VR повторно фиксирует текущее положение головы как нулевое, не делая механический CENTER;
8. `Выход` возвращает обычный интерфейс.

Важно: `DeviceOrientation` на мобильных браузерах обычно доступен только в secure context. Для локального теста `localhost` допускается, а при открытии сервера по обычному `http://IP-адрес` датчик на части телефонов может быть заблокирован. Для реального использования нужен HTTPS.

На iPhone Safari разрешение на датчики запрашивается только после пользовательского нажатия — кнопка `Cardboard` выполняет этот запрос корректно.

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

Для эксплуатации через Интернет нужны HTTPS, STUN/TURN (coturn), firewall для RTP ingest и ICE/WebRTC UDP, а также отдельный секрет устройства в дополнение к `device_id`.
