# Windows emulator for Radxa ZERO 3E camera node

Эмулятор нужен для тестирования системы до приезда Radxa ZERO 3E.

Он ведёт себя как отдельный трактор:

- имеет постоянный `device_id`;
- регистрируется на центральном Go Camera Hub;
- получает выделенный RTP ingest-порт;
- захватывает Windows web-камеру через DirectShow;
- кодирует `libx264` в H.264 Baseline с низкой задержкой;
- B-frames=0, короткий GOP, SPS/PPS на keyframe;
- отправляет готовый H.264/RTP на сервер без серверного перекодирования;
- отправляет телеметрию;
- принимает PTZ/CENTER/REQUEST_IDR.

## GUI

Запуск:

```bat
run_gui.bat
```

GUI позволяет выбрать постоянный ID трактора, web-камеру, адрес сервера, `ffmpeg.exe`, разрешение/FPS/bitrate/GOP и видеть RTP/PTZ/лог.

## Требования

- Python 3.10+;
- FFmpeg;
- запущенный `radxa_zero3e/server` на Go.

## Порядок локального теста

### 1. Запустить единый Go-сервер

Нужен Go 1.24+.

```bat
cd radxa_zero3e\server
run_server.bat
```

или:

```bat
go mod tidy
go run .
```

Сервер одновременно выполняет:

```text
HTTP/API + SQLite + login + device_id + telemetry + PTZ + RTP ingest + Pion WebRTC
```

Отдельный FastAPI и отдельный relay больше не запускаются.

### 2. Запустить GUI эмулятора

```bat
cd radxa_zero3e\emulator_windows
run_gui.bat
```

В GUI:

1. Нажать `Найти камеры`.
2. Выбрать web-камеру.
3. `HTTP сервера`: для локального теста `http://127.0.0.1:8000`.
4. `RTP host`: для локального теста `127.0.0.1`.
5. Указать постоянный ID, например `TRACTOR-WIN-0001`.
6. Нажать `Старт`.

Сервер вернёт, например:

```text
RTP ingest UDP 10000
```

и FFmpeg начнёт отправлять готовые H.264/RTP пакеты на этот UDP-порт.

### 3. Открыть личный кабинет

```text
http://127.0.0.1:8000
```

Создать пользователя и в `Настройки` добавить тот же ID:

```text
TRACTOR-WIN-0001
```

После выбора трактора браузер получает этот же H.264 через WebRTC/Pion без decode/encode на сервере.

## Несколько эмуляторов

Каждой копии нужны собственные:

- `device_id`;
- `ptz_listen_port`.

Например:

```text
TRACTOR-WIN-0001 / PTZ 6000 / RTP 10000
TRACTOR-WIN-0002 / PTZ 6001 / RTP 10001
TRACTOR-WIN-0003 / PTZ 6002 / RTP 10002
```

RTP-порты назначает сервер автоматически.

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

Это тот же upstream H.264/RTP и PTZ-протокол, который будет использовать реальная Radxa ZERO 3E.
