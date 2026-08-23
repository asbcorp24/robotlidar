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
- принимает PAN/TILT/CENTER/REQUEST_IDR.

## GUI

Основной способ запуска теперь — графический интерфейс:

```bat
run_gui.bat
```

GUI позволяет:

- выбрать `device_id` и имя устройства;
- указать HTTP адрес Camera Hub и RTP host;
- автоматически найти DirectShow web-камеры;
- выбрать `ffmpeg.exe`;
- настроить разрешение, FPS, bitrate и GOP;
- запускать и останавливать эмуляцию;
- видеть назначенный сервером RTP ingest port;
- видеть последние PAN/TILT команды;
- просматривать лог работы FFmpeg, регистрации и телеметрии;
- сохранять настройки в `config.json`.

## 1. Установить Python

Нужен Python 3.10+ для Windows.

```bat
py -3 --version
```

## 2. Установить FFmpeg

`ffmpeg.exe` должен быть доступен через PATH либо его можно выбрать кнопкой `Обзор...` в GUI.

```bat
ffmpeg -version
```

## 3. Запустить сервер

В отдельной консоли:

```bat
cd radxa_zero3e\server
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

## 4. Запустить GUI эмулятора

```bat
cd radxa_zero3e\emulator_windows
run_gui.bat
```

В GUI:

1. Нажать `Найти камеры`.
2. Выбрать нужную web-камеру.
3. Проверить `HTTP сервера`, например `http://127.0.0.1:8000`.
4. Проверить `RTP host`, например `127.0.0.1`.
5. Указать уникальный `Device ID`, например `camera-win-001`.
6. Нажать `Старт`.

После регистрации сервер назначит отдельный RTP ingest port, например `10000`. GUI покажет его в поле состояния.

## Несколько эмуляторов

Можно открыть несколько копий программы, но каждой нужны собственные:

- `device_id`;
- `ptz_listen_port`.

Например:

```text
camera-win-001 / PTZ 6000
camera-win-002 / PTZ 6001
camera-win-003 / PTZ 6002
```

Сервер самостоятельно назначит каждой камере свой RTP ingest port.

## Консольный режим

Старый режим сохранён.

Список DirectShow камер:

```bat
python emulator.py --list-cameras
```

Запуск:

```bat
copy config.example.json config.json
python emulator.py --config config.json
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

При работающем видео должны увеличиваться `video_packets` и `video_bytes`, а `video_online` должен быть `true`.

Это тот же протокол регистрации, телеметрии, RTP и PTZ, который позже будет использовать реальная Radxa ZERO 3E.
