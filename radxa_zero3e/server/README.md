# RobotLiDAR Camera Hub — Go server

Центральный сервер для тракторов с Radxa ZERO 3E полностью реализован на Go.

## Архитектура

```text
Radxa / Windows emulator
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

## Структура

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
HBVCAM SBS
 -> Radxa h264_rkmpp Baseline
 -> RTP/UDP
 -> Go/Pion TrackLocalStaticRTP
 -> WebRTC/SRTP
 -> Browser
```

## Управление трактором

В web добавлена отдельная панель движения:

- вперёд;
- назад;
- разворот/поворот влево;
- разворот/поворот вправо;
- STOP;
- скорость 10–100%;
- W/A/S/D;
- Space = аварийный STOP.

Сервер передаёт не конкретный PWM, а нормированные значения левой/правой гусеницы `-1000..+1000`. Это позволяет позже на Radxa/ESP32 независимо настроить драйверы моторов.

## Щётка

В web есть отдельная панель навесного оборудования:

- вращение щётки вперёд;
- реверс вращения;
- STOP вращения;
- регулировка скорости;
- поднять щётку;
- опустить щётку.

Вращение щётки — постоянная команда до STOP/смены направления. Подъём и опускание — hold-to-run: механизм движется только пока оператор удерживает кнопку, при отпускании отправляется `lift=0`.

## Управляющий UDP протокол

Сервер отправляет все команды на control/PTZ UDP port устройства фиксированными 16-байтными пакетами.

```text
type 1 = camera PTZ
type 2 = tractor drive: left/right
type 3 = brush: spin/lift
```

Drive и brush используют диапазон `-1000..+1000`.

Для будущей Radxa→ESP32 обязательно используется локальный watchdog: отсутствие свежей команды движения или подъёма 300–500 мс должно приводить к STOP. Это защищает от зависшего браузера, потери сети или падения сервера.

## Cardboard / VR

Кнопка `Cardboard` включает fullscreen SBS режим. Левая половина стереокадра идёт левому глазу, правая — правому. `DeviceOrientation` телефона преобразуется в PAN/TILT камеры примерно 10 раз/с.

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
