# Windows emulator for Radxa ZERO 3E camera node

Этот модуль нужен для тестирования `radxa_zero3e/server` до приезда Radxa ZERO 3E.

Он эмулирует устройство:

- имеет постоянный `device_id` трактора;
- регистрируется на Camera Hub;
- получает от сервера выделенный UDP-порт для RTP/H.264;
- захватывает обычную Windows web-камеру через DirectShow;
- кодирует видео `libx264` с низкой задержкой;
- H.264 Baseline, B-frames=0, короткий GOP;
- повторяет SPS/PPS на keyframe для WebRTC passthrough;
- отправляет готовый RTP/H.264 в Pion relay без серверного перекодирования;
- раз в секунду отправляет телеметрию;
- слушает UDP PTZ;
- принимает PAN/TILT/CENTER/REQUEST_IDR.

## GUI

Основной запуск:

```bat
run_gui.bat
```

GUI позволяет выбрать постоянный ID трактора, камеру, сервер, FFmpeg, разрешение/FPS/bitrate/GOP, запускать поток и видеть RTP/PTZ/лог.

## Требования

- Python 3.10+
- FFmpeg в PATH или выбранный `ffmpeg.exe`
- Camera Hub FastAPI
- Pion WebRTC relay (`radxa_zero3e/server/webrtc_relay`)

## Порядок запуска для локального теста

### 1. Pion H.264 WebRTC relay

Нужен Go 1.24+.

```bat
cd radxa_zero3e\server
run_relay.bat
```

Должно появиться:

```text
RobotLiDAR Pion H264 relay listening on http://127.0.0.1:8090
```

### 2. FastAPI Camera Hub

В другой консоли:

```bat
cd radxa_zero3e\server
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

### 3. GUI эмулятора

```bat
cd radxa_zero3e\emulator_windows
run_gui.bat
```

В GUI:

1. Нажать `Найти камеры`.
2. Выбрать web-камеру.
3. `HTTP сервера`: `http://127.0.0.1:8000` для локального теста.
4. `RTP host`: `127.0.0.1` для локального теста.
5. Указать постоянный ID, например `TRACTOR-WIN-0001`.
6. Нажать `Старт`.

Camera Hub создаст для ID поток в Pion relay и вернёт, например:

```text
RTP ingest UDP 10000
```

FFmpeg начнёт отправлять готовые H.264 RTP пакеты непосредственно на этот порт.

### 4. Браузер

Открыть:

```text
http://127.0.0.1:8000
```

Создать пользователя, открыть `Настройки` и добавить тот же ID:

```text
TRACTOR-WIN-0001
```

После выбора трактора браузер получает H.264 через WebRTC от Pion relay.

## Несколько эмуляторов

Каждой копии нужны собственные:

- `device_id`;
- `ptz_listen_port`.

Например:

```text
TRACTOR-WIN-0001 / PTZ 6000
TRACTOR-WIN-0002 / PTZ 6001
TRACTOR-WIN-0003 / PTZ 6002
```

Pion relay самостоятельно слушает отдельный RTP ingest-порт для каждого ID (`10000`, `10001`, ...).

## Консольный режим

Список DirectShow камер:

```bat
python emulator.py --list-cameras
```

Запуск:

```bat
copy config.example.json config.json
python emulator.py --config config.json
```

Это тот же upstream H.264/RTP и PTZ-протокол, который позже будет использовать реальная Radxa ZERO 3E.
