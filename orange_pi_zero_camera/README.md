# Orange Pi Zero Camera + PTZ

Отдельное приложение RobotLiDAR для Orange Pi Zero, которое занимается только камерой и её PTZ.

В нём нет ROS, UART/ESP32, гусениц или щётки.

```text
IP/USB camera --H.264--> Orange Pi Zero --SRT--> Go server --WebRTC--> Browser
                                ^
                                |
                         ONVIF AbsoluteMove
                                ^
                                |
Browser --PTZ--> Go server --WSS
```

## Папка

```text
orange_pi_zero_camera/
├── camera_streamer.py
├── config.example.json
├── install.sh
├── orange-pi-zero-camera.service
└── README.md
```

## Что умеет

Видео:

- `rtsp` — готовый H.264 от IP-камеры, без перекодирования;
- `v4l2_h264` — USB/UVC камера сама выдаёт H.264;
- `v4l2_encode` — кодирование на Orange Pi;
- `test` — тестовая картинка.

PTZ:

- Orange Pi открывает исходящий WSS к центральному серверу;
- принимает тот же 16-байтный RobotLiDAR control packet;
- обрабатывает только `type=1 PTZ` и `CENTER`;
- `DRIVE` и `BRUSH` намеренно игнорируются;
- PTZ передаётся в камеру через ONVIF `AbsoluteMove` с WS-Security PasswordDigest.

## Установка

```bash
cd /opt
git clone https://github.com/asbcorp24/robotlidar.git
cd /opt/robotlidar/orange_pi_zero_camera
chmod +x install.sh
sudo ./install.sh
```

Если репозиторий уже есть:

```bash
cd /opt/robotlidar
git pull origin main
cd orange_pi_zero_camera
sudo ./install.sh
```

Установщик ставит `python3`, `python3-websocket`, `ffmpeg`, `v4l-utils`, CA certificates, конфиг и systemd service.

## Конфигурация

```bash
nano /etc/robotlidar/orange-pi-zero-camera.json
```

Пример для IP/PTZ камеры:

```json
{
  "device_id": "CAM-OPIZERO-001",
  "device_name": "Передняя PTZ камера",
  "server_url": "https://tele.xn----7sbbd7e6b.xn--p1ai",

  "input_mode": "rtsp",
  "input_url": "rtsp://192.168.1.149:8554/camera",
  "video_device": "/dev/video0",

  "width": 1280,
  "height": 720,
  "fps": 20,
  "bitrate_kbps": 1500,

  "encoder": "h264_v4l2m2m",
  "ffmpeg": "ffmpeg",
  "srt_latency_ms": 200,
  "telemetry_period_sec": 2.0,
  "reconnect_delay_sec": 2.0,

  "ptz_enabled": true,
  "onvif_url": "http://192.168.1.149/onvif/ptz_service",
  "onvif_username": "admin",
  "onvif_password": "CHANGE_ME",
  "onvif_profile_token": "Profile_1"
}
```

`device_id` должен быть уникальным и затем привязывается к пользователю на центральном сервере.

## Важное про ONVIF URL и ProfileToken

`onvif_url` нельзя считать одинаковым для всех камер. Правильный PTZ XAddr обычно определяется через ONVIF `GetCapabilities`, а `onvif_profile_token` — через `GetProfiles`.

У разных камер встречаются, например:

```text
http://CAMERA_IP/onvif/ptz_service
http://CAMERA_IP:80/onvif/PTZ
```

Поэтому перед эксплуатацией нужно проверить конкретную модель камеры.

## USB камера

```bash
v4l2-ctl --list-devices
v4l2-ctl -d /dev/video0 --list-formats-ext
```

Если камера умеет H.264:

```json
"input_mode": "v4l2_h264"
```

Это предпочтительный вариант для слабого Orange Pi Zero.

Если есть только MJPEG/YUYV:

```json
"input_mode": "v4l2_encode",
"encoder": "h264_v4l2m2m"
```

Проверка encoder'ов:

```bash
ffmpeg -hide_banner -encoders | grep -E '264|v4l2'
```

## Проверка SRT

```bash
ffmpeg -hide_banner -protocols | grep -w srt
```

На сервере каждому устройству назначается SRT UDP-порт из диапазона `12000-12099`.

## Запуск

```bash
systemctl restart orange-pi-zero-camera
systemctl status orange-pi-zero-camera --no-pager
journalctl -u orange-pi-zero-camera -f
```

Нормальный лог:

```text
REGISTERED CAM-OPIZERO-001; SRT tele.xn----7sbbd7e6b.xn--p1ai:12002; latency=200ms
FFMPEG START: ... srt://tele.xn----7sbbd7e6b.xn--p1ai:12002?mode=caller...
CONTROL/WSS connected: wss://tele.xn----7sbbd7e6b.xn--p1ai/api/devices/CAM-OPIZERO-001/control-ws
```

При управлении камерой с сайта:

```text
CONTROL/PTZ seq=123 pan=10.0 tilt=-5.0
```

Если WSS работает, но ONVIF ещё не настроен:

```text
PTZ received pan=... tilt=...; onvif_url is empty
```

## Автовосстановление

Приложение автоматически:

- повторяет регистрацию при недоступности сервера;
- переподключает WSS;
- перезапускает FFmpeg;
- регистрируется заново после 404 telemetry;
- дополнительно перезапускается systemd при аварийном завершении.

## Что намеренно отсутствует

Это камера + PTZ приложение. Команды `DRIVE` и `BRUSH` принимаются по общему каналу только для совместимости протокола и сразу игнорируются. Управление ходовой частью остаётся в Raspberry/Radxa приложениях RobotLiDAR.
