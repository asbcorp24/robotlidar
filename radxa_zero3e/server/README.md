# Camera Hub Server

Сервер удалённого просмотра и управления тракторами Radxa ZERO 3E.

## Архитектура

```text
Radxa / Windows emulator
  device_id + telemetry + H264/RTP
               |
               v
        Camera Hub / FastAPI
        SQLite users/devices
        RTP H264 decode
        aiortc WebRTC
               |
               v
          Web browser
               |
               +---- PTZ commands ----> selected tractor
```

## Возможности

- регистрация и вход пользователя по логину/паролю;
- привязка постоянного ID трактора к аккаунту;
- один `device_id` может принадлежать только одному аккаунту;
- пользователь видит только свои тракторы;
- отдельный RTP ingest UDP-порт на каждый трактор;
- H.264/RTP от Radxa или Windows-эмулятора;
- WebRTC выдача живого видео в браузер;
- PAN/TILT/CENTER/REQUEST_IDR только для тракторов текущего пользователя;
- SQLite база `camera_hub.db`;
- статический web UI из `server/web`.

## Установка

```bash
cd radxa_zero3e/server
python -m venv .venv
```

Windows:

```bat
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Linux:

```bash
source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Открыть:

```text
http://127.0.0.1:8000/
```

## Проверка до приезда Radxa

1. Запустить Camera Hub.
2. Запустить `radxa_zero3e/emulator_windows/run_gui.bat`.
3. В эмуляторе задать постоянный ID, например `TRACTOR-WIN-0001`.
4. В web-интерфейсе создать пользователя или войти.
5. Открыть `Настройки`.
6. Добавить `TRACTOR-WIN-0001`.
7. Перейти в `Камеры` и выбрать трактор.
8. При поступающем RTP сервер откроет WebRTC и покажет живое видео.
9. PAN/TILT кнопки отправляются только выбранному трактору.

## Основные API

Пользовательские:

- `POST /api/auth/register`
- `POST /api/auth/login`
- `GET /api/auth/me`
- `POST /api/auth/logout`
- `GET /api/devices`
- `GET /api/settings/devices`
- `POST /api/settings/devices`
- `DELETE /api/settings/devices/{device_id}`
- `POST /api/devices/{device_id}/webrtc`
- `POST /api/devices/{device_id}/ptz`
- `POST /api/devices/{device_id}/center`

Устройство:

- `POST /api/devices/{device_id}/register`
- `POST /api/devices/{device_id}/telemetry`

## Видео

Вход:

```text
H.264 over RTP/UDP
```

Сервер принимает RTP на выделенном порту, разбирает H.264 NAL/FU-A/STAP-A, декодирует поток через PyAV/FFmpeg и передаёт кадры в `aiortc`.

Выход в браузер:

```text
WebRTC
```

Это первая рабочая версия. Она декодирует H.264 на сервере и затем кодирует видео в WebRTC-совместимый поток через aiortc. Позже при необходимости можно оптимизировать relay, чтобы уменьшить нагрузку CPU и приблизиться к H.264 passthrough.

## Интернет

Для теста в локальной сети WebRTC должен работать напрямую. Для сервера за NAT и клиентов из интернета следующим инфраструктурным шагом будет STUN/TURN (обычно coturn) и HTTPS/WSS через nginx.
