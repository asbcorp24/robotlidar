# RobotLiDAR: две H.264 RTSP-камеры

Поддерживается единая схема для Orange Pi One и Raspberry Pi:

```text
RTSP Camera 1 --\
                 +--> Orange Pi / Raspberry --> ONE SRT/MPEG-TS --> Go server --> WebRTC
RTSP Camera 2 --/
                       ^
                       |
              TYPE_CAMERA = 4
              Camera 1 / Camera 2
```

Одновременно наружу передаётся только одна выбранная камера. Второй SRT-порт не создаётся.

## Требования к камерам

Обе камеры должны отдавать H.264 RTSP. На Orange Pi/Raspberry поток передаётся через FFmpeg с `-c:v copy`, без декодирования и повторного кодирования.

Желательно использовать одинаковые:
- разрешение;
- FPS;
- H.264 profile/level;
- GOP/keyframe interval около 1 секунды.

Это сокращает паузу при переключении и упрощает восстановление WebRTC-декодера.

## Протокол переключения

Формат управляющего пакета не изменён:

```text
>HBBIhhHH
magic:u16
version:u8
type:u8
seq:u32
value1:i16
value2:i16
speed:u16
flags:u16
```

Для выбора камеры:

```text
magic   = 0x5354
version = 1
type    = 4
value1  = 1  -> Camera 1
value1  = 2  -> Camera 2
value2  = 0
speed   = 0
flags   = 0
```

Команда идёт через тот же `/api/devices/<DEVICE_ID>/control-ws`. На Raspberry остаётся UDP fallback.

## Центральный сервер

HTTP API:

```http
POST /api/devices/<DEVICE_ID>/camera
Content-Type: application/json

{"camera": 1}
```

или:

```json
{"camera": 2}
```

В пользовательском веб-интерфейсе рядом с PTZ добавлены кнопки Camera 1 и Camera 2.

Сервер хранит последнее выбранное значение и уточняет его по телеметрии устройства.

## Orange Pi One

Поля `/etc/robotlidar/orange-pi-zero-camera.json`:

```json
{
  "input_mode": "rtsp",
  "camera1_name": "Передняя",
  "camera1_url": "rtsp://192.168.1.149:554/stream1",
  "camera2_name": "Задняя",
  "camera2_url": "rtsp://192.168.1.150:554/stream1",
  "active_camera": 1
}
```

Старое поле `input_url` сохранено для обратной совместимости и используется как Camera 1, если `camera1_url` ещё пуст.

Локальная панель `:8088` позволяет:
- задать обе камеры;
- выбрать стартовую камеру;
- сканировать локальную сеть;
- положить найденный RTSP URL отдельно в Camera 1 или Camera 2.

SSD1315 показывает фактически активную Camera 1/2.

## Raspberry Pi

Локальная панель RobotLiDAR содержит:
- Camera 1 name + RTSP URL;
- Camera 2 name + RTSP URL;
- выбор стартовой камеры;
- кнопки локального переключения Camera 1 / Camera 2.

Удалённая команда с центрального сервера использует тот же TYPE_CAMERA=4.

## Что происходит при переключении

1. Сервер отправляет TYPE_CAMERA=4.
2. Устройство меняет активный RTSP URL.
3. Только локальный FFmpeg-процесс видеореле перезапускается.
4. SRT ingest port на сервере не меняется.
5. Центральное устройство/device_id не меняется.
6. Web UI повторно подключает WebRTC после короткой паузы.
7. Устройство отправляет фактическую `active_camera` в телеметрии.

DRIVE, BRUSH и PTZ используют прежние packet type 2, 3 и 1 соответственно и не изменены.
