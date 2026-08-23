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

## Привязка тракторов

```text
GET    /api/settings/devices
POST   /api/settings/devices
DELETE /api/settings/devices/{device_id}
```

Добавление:

```json
{"device_id":"TRACTOR-0001","alias":"Трактор №1"}
```

## Список моих тракторов

```text
GET /api/devices
```

## PTZ камеры

```text
POST /api/devices/{device_id}/ptz
POST /api/devices/{device_id}/center
POST /api/devices/{device_id}/request-idr
```

```json
{"pan_cdeg":2500,"tilt_cdeg":-1000,"speed_cdeg_s":4000,"request_idr":false}
```

## Движение трактора

```text
POST /api/devices/{device_id}/drive
POST /api/devices/{device_id}/drive-stop
```

`left` и `right` — целевые значения левой и правой гусеницы `-1000..+1000`:

```json
{"left":500,"right":500}
```

Примеры:

```text
вперёд:       left=+500 right=+500
назад:        left=-500 right=-500
поворот влево:left=-500 right=+500
поворот вправо:left=+500 right=-500
STOP:         left=0    right=0
```

В веб-интерфейсе движение выполняется только пока кнопка удерживается. `W/A/S/D` управляют трактором, `Space` отправляет STOP.

## Щётка

```text
POST /api/devices/{device_id}/brush
```

```json
{"spin":600,"lift":0}
```

- `spin`: `-1000..+1000`, знак задаёт направление вращения, модуль — скорость;
- `lift`: `+1000` подъём, `-1000` опускание, `0` остановить механизм подъёма.

Вращение может оставаться включённым. Подъём/опускание в web работает только пока соответствующая кнопка удерживается.

## UDP control protocol Server -> Radxa

Все управляющие команды отправляются на `ptz_port` зарегистрированного устройства одним фиксированным 16-байтным форматом:

```text
magic    u16 BE = 0x5354
version  u8     = 1
type     u8
seq      u32 BE
value1   i16 BE
value2   i16 BE
extra    4 bytes
```

Типы:

```text
1 = PTZ camera
2 = tractor drive: value1=left, value2=right
3 = brush:         value1=spin, value2=lift
```

Для будущей Radxa→ESP32 рекомендуется локальный watchdog: если команда движения/подъёма не обновлялась примерно 300–500 мс, Radxa/ESP32 обязаны перейти в безопасный STOP независимо от состояния браузера или сервера.

## WebRTC

```text
POST /api/devices/{device_id}/webrtc
```

Offer/answer SDP. Видео — H.264 RTP passthrough через Pion без decode/encode.

## Radxa / emulator API

Регистрация:

```text
POST /api/devices/{device_id}/register
```

```json
{"name":"Radxa ZERO 3E","ip":"192.168.1.50","rtp_port":5004,"ptz_port":6000}
```

Сервер возвращает `video_ingest_port`.

Телеметрия:

```text
POST /api/devices/{device_id}/telemetry
```

Статус видео:

```text
GET /api/devices/{device_id}/video-status
```
