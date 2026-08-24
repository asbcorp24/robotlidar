# RobotLiDAR Camera Hub — Go server

Центральный сервер RobotLiDAR полностью реализован на Go и обслуживает разные типы тракторных шлюзов под одним API/device_id.

## Архитектура

```text
Radxa + USB stereo camera
          или
Orange Pi PC + Mercusys MC500
          или
Windows emulator
        |
        | H.264/RTP + telemetry + control
        v
+--------------------------------------+
| robotlidar-server                    |
|                                      |
| HTTP/REST + users/sessions + SQLite  |
| user <-> tractor device_id           |
| RTP ingest + Pion WebRTC passthrough |
| PTZ camera                            |
| tractor drive                        |
| brush spin + lift                    |
| embedded web                         |
+------------------+-------------------+
                   |
                   | WebRTC
                   v
                Browser
```

H.264 на сервере не декодируется и не перекодируется.

## Клиенты

### Radxa ZERO 3E

`radxa_zero3e/device/` — USB stereo UVC camera + аппаратный H.264 RKMPP + локальные PAN/TILT servo.

### Orange Pi PC + Mercusys MC500

`radxa_zero3e/orange_pi_pc_ipcam/` — MC500 подключена к LAN по Wi-Fi 2.4 ГГц, Orange Pi к тому же роутеру по Ethernet.

```text
MC500 -- RTSP/H.264 --> Orange Pi -- RTP/H.264 --> Server
MC500 <-- ONVIF/PTZ -- Orange Pi <-- UDP control -- Server
ESP32 <--- USB-UART --- Orange Pi <-- drive/brush --- Server
```

MC500 официально поддерживает H.264, RTSP и ONVIF. Для неё используются RTSP `:554/stream1` и ONVIF service port `2020`; Camera Account создаётся в приложении MERCUSYS.

### Windows emulator

`radxa_zero3e/emulator_windows/` — тестовый клиент до подключения реального оборудования.

## Структура сервера

```text
radxa_zero3e/server/
├── main.go
├── auth.go
├── devices.go
├── media.go
├── control.go
├── util.go
├── go.mod
├── web/
└── camera_hub.db
```

## Запуск

Требуется Go 1.24+.

Windows:

```bat
cd radxa_zero3e\server
run_server.bat
```

или:

```bat
go mod tidy
go run .
```

Сборка EXE:

```bat
go build -trimpath -ldflags="-s -w" -o robotlidar-server.exe .
```

Переменные окружения:

```text
LISTEN_ADDR=0.0.0.0:8000
DB_PATH=camera_hub.db
STUN_URL=stun:stun.example.com:3478
```

## Device ID и пользователи

У каждого трактора постоянный уникальный ID, например `TRACTOR-0001`. Пользователь входит в web, добавляет этот ID в `Настройки` и после этого может видеть видео и управлять только своими тракторами.

## Видео

```text
Camera H.264
 -> tractor gateway
 -> RTP/UDP
 -> Go/Pion TrackLocalStaticRTP
 -> WebRTC/SRTP
 -> Browser
```

## Управление трактором

В web есть:

- вперёд;
- назад;
- поворот/разворот влево;
- поворот/разворот вправо;
- STOP;
- скорость 10–100%;
- W/A/S/D;
- Space = аварийный STOP.

Сервер передаёт нормированные значения левой/правой гусеницы `-1000..+1000`.

Пока кнопка движения удерживается, browser повторяет активную команду примерно каждые 180 мс. Если browser/сеть пропадает, heartbeat прекращается и watchdog Orange Pi/ESP32 переводит привод в STOP.

## Щётка

В web есть:

- вращение;
- реверс команды вращения;
- STOP;
- скорость;
- поднять;
- опустить.

Подъём/опускание — hold-to-run. Активные brush-команды также поддерживаются heartbeat.

Текущая ESP32 силовая схема щётки имеет скорость + Brake, без отдельной физической линии Reverse; до добавления этой линии отрицательное направление вращения не реализуется аппаратно.

## Управляющий UDP протокол

Сервер отправляет команды на control/PTZ UDP port устройства фиксированными 16-байтными пакетами.

```text
type 1 = camera PTZ
type 2 = tractor drive: left/right
type 3 = brush: spin/lift
```

Drive и brush используют диапазон `-1000..+1000`.

На Orange Pi:

```text
type 1 -> ONVIF MC500
type 2 -> ESP32 DRV
type 3 -> ESP32 AUX
```

## Cardboard / VR

Кнопка `Cardboard` включает fullscreen режим. Для Radxa SBS поток делится между глазами. `DeviceOrientation` телефона формирует PAN/TILT примерно 10 раз/с.

Для Orange Pi + MC500 те же PAN/TILT команды автоматически переводятся Orange Pi в ONVIF `AbsoluteMove` камеры.

Для мобильного гироскопа в реальной эксплуатации нужен HTTPS.

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
