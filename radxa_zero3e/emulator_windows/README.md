# Windows emulator for Radxa ZERO 3E camera node

Этот модуль нужен для тестирования `radxa_zero3e/server` до приезда Radxa ZERO 3E.

Он эмулирует устройство:

- регистрируется на Camera Hub по `device_id`;
- получает от сервера выделенный UDP-порт для RTP/H.264;
- захватывает обычную Windows web-камеру через DirectShow;
- кодирует видео `libx264` с низкой задержкой;
- отправляет RTP/H.264 на сервер;
- раз в секунду отправляет телеметрию;
- слушает UDP PTZ на порту 6000;
- принимает PAN/TILT/CENTER/REQUEST_IDR и отображает команды в консоли.

## 1. Установить Python

Нужен Python 3.10+ для Windows.

Проверка:

```bat
py -3 --version
```

## 2. Установить FFmpeg

`ffmpeg.exe` должен быть доступен через PATH либо полный путь нужно указать в `config.json`.

Проверка:

```bat
ffmpeg -version
```

## 3. Посмотреть название web-камеры

Из папки эмулятора:

```bat
python emulator.py --list-cameras
```

FFmpeg выведет DirectShow video devices. Точное имя нужно записать в `camera_name`.

Пример:

```text
"Integrated Camera"
"USB Camera"
"Logitech BRIO"
```

## 4. Настроить

Скопировать:

```bat
copy config.example.json config.json
```

Пример для сервера на том же Windows-компьютере:

```json
{
  "device_id": "camera-win-001",
  "device_name": "Windows Camera Emulator",
  "server_http": "http://127.0.0.1:8000",
  "server_rtp_host": "127.0.0.1",
  "server_rtp_port": 5004,
  "ptz_listen_port": 6000,
  "camera_name": "Integrated Camera",
  "ffmpeg": "ffmpeg.exe",
  "width": 1280,
  "height": 720,
  "fps": 30,
  "bitrate_kbps": 2000,
  "gop": 15,
  "preset": "ultrafast",
  "telemetry_period_sec": 1.0
}
```

`server_rtp_port` здесь только запасное значение. После регистрации Camera Hub сам назначает устройству порт, например `10000`.

## 5. Запустить сервер

В отдельной консоли:

```bat
cd radxa_zero3e\server
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

После регистрации эмулятора сервер покажет, например:

```text
RTP ingest camera-win-001: UDP 0.0.0.0:10000
```

## 6. Запустить эмулятор

Самый простой вариант:

```bat
run.bat
```

или вручную:

```bat
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python emulator.py --config config.json
```

Ожидаемый вывод:

```text
Device ID : camera-win-001
Camera    : Integrated Camera
Server    : http://127.0.0.1:8000
[SERVER] registered camera-win-001
[SERVER] assigned RTP ingest UDP 10000
RTP       : 127.0.0.1:10000
[PTZ] listening UDP 0.0.0.0:6000
```

## Проверить на сервере

Список устройств:

```text
GET http://127.0.0.1:8000/api/devices
```

Статус приема RTP:

```text
GET http://127.0.0.1:8000/api/devices/camera-win-001/video-status
```

При работающем видео должны увеличиваться:

- `video_packets`;
- `video_bytes`;
- `video_online` должен быть `true`.

## Проверка PTZ

Например POST:

```text
http://127.0.0.1:8000/api/devices/camera-win-001/ptz
```

JSON:

```json
{
  "pan_cdeg": 2500,
  "tilt_cdeg": -1000,
  "speed_cdeg_s": 4000,
  "request_idr": false
}
```

В консоли Windows-эмулятора появится примерно:

```text
[PTZ] pan=25.00 tilt=-10.00 speed=40.00 deg/s
```

Это тот же протокол, который позже будет использовать реальная Radxa ZERO 3E.
