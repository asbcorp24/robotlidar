# Windows emulator for RobotLiDAR tractor/camera node

Эмулятор нужен для проверки текущей интернет-архитектуры RobotLiDAR без реального Raspberry Pi и трактора.

Сейчас он повторяет актуальную схему Raspberry:

- имеет постоянный `device_id`;
- регистрируется на центральном Go-сервере;
- запрашивает `video_transport=srt`;
- получает назначенный `srt_ingest_port`;
- захватывает Windows web-камеру через DirectShow;
- кодирует H.264 Baseline через `libx264`;
- отправляет MPEG-TS/H.264 через SRT в режиме `caller`;
- сервер принимает SRT в чистом Go и передаёт H.264 в Pion WebRTC без серверного decode/encode;
- открывает исходящий WebSocket/WSS к серверу для управления через NAT;
- принимает тем же 16-байтным бинарным протоколом PTZ, DRIVE и BRUSH;
- показывает полученные команды в GUI;
- старый входящий UDP `:6000` оставлен только как необязательный legacy fallback.

Основная схема:

```text
Windows emulator
   ├── SRT caller ─────────────► Go server ──► Pion WebRTC ──► browser
   └── WSS client ◄──────────── Go server ◄────────────────── browser control
```

Проброс входящего UDP-порта для управления не нужен.

## GUI

Запуск:

```bat
run_gui.bat
```

`run_gui.bat` автоматически создаёт `.venv` и устанавливает зависимости из `requirements.txt`.

В GUI задаются:

- Device ID;
- имя устройства;
- адрес центрального сервера;
- SRT latency;
- legacy UDP порт;
- web-камера;
- `ffmpeg.exe`;
- разрешение, FPS, bitrate и GOP.

В состоянии отображаются:

- назначенный SRT ingest порт;
- состояние WSS;
- последний PTZ;
- команды левой/правой гусеницы;
- щётка/подъём.

## Тест с production сервером

В GUI укажи:

```text
Центральный сервер:
https://tele.xn----7sbbd7e6b.xn--p1ai

Device ID:
TRACTOR-WIN-0001
```

Выбери камеру и нажми `Старт`.

Нормальный лог выглядит примерно так:

```text
[SERVER] registered TRACTOR-WIN-0001 as 192.168.x.x
[SERVER] assigned SRT ingest UDP 12000, latency 200 ms
[VIDEO] starting:
...
[CONTROL/WSS] connected wss://tele.xn----7sbbd7e6b.xn--p1ai/api/devices/TRACTOR-WIN-0001/control-ws
```

После этого при нажатии кнопок сайта должны появляться сообщения:

```text
[PTZ] via WSS seq=... pan=... tilt=...
[DRIVE] via WSS seq=... left=... right=...
[BRUSH] via WSS seq=... spin=... lift=...
```

Это позволяет проверить всю цепочку `browser -> Go server -> WSS -> устройство` без Raspberry Pi.

## Nginx для production WSS

Для WebSocket в `location /` должны быть:

```nginx
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
proxy_read_timeout 3600s;
proxy_send_timeout 3600s;
```

Без этих заголовков эмулятор покажет `Handshake status 400 Bad Request`.

## Локальный тест

Можно использовать:

```text
http://127.0.0.1:8000
```

Тогда control URL автоматически станет `ws://127.0.0.1:8000/.../control-ws`, а видео пойдёт SRT на назначенный сервером UDP-порт.

## Требования

- Python 3.10+;
- FFmpeg с поддержкой `libsrt`;
- Go-сервер `radxa_zero3e/server`.

Проверка SRT в FFmpeg:

```bat
ffmpeg -protocols | findstr /I srt
```

Должен присутствовать протокол `srt`.

## Несколько эмуляторов

Каждой копии нужен собственный `device_id`. Legacy UDP-порт тоже должен отличаться, если UDP fallback включён.

Например:

```text
TRACTOR-WIN-0001 / legacy UDP 6000
TRACTOR-WIN-0002 / legacy UDP 6001
TRACTOR-WIN-0003 / legacy UDP 6002
```

SRT-порты `12000-12099` назначает сервер автоматически.

## Консольный режим

Список DirectShow камер:

```bat
python emulator.py --list-cameras
```

Запуск:

```bat
copy config.example.json config.json
python -m pip install -r requirements.txt
python emulator.py --config config.json
```

Эмулятор использует тот же транспорт управления и тот же бинарный формат команд, что и текущий Raspberry remote-control gateway.
