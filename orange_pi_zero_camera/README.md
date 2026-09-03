# Orange Pi Zero Camera Streamer

Отдельное минимальное приложение RobotLiDAR только для видеокамеры на Orange Pi Zero.

В нём нет ROS, PTZ, UART/ESP32, гусениц, щётки или удалённого управления. Orange Pi только получает видео от камеры и отправляет его на центральный Go-сервер.

```text
Camera / RTSP / USB V4L2
          |
          v
    Orange Pi Zero
          |
          | SRT / MPEG-TS / H.264
          v
      Go server
          |
          v
     Pion WebRTC
          |
          v
       Browser
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

## Поддерживаемые источники

`input_mode`:

- `rtsp` — рекомендуется, если IP-камера уже выдаёт H.264. FFmpeg делает `-c:v copy`, поэтому Orange Pi почти не тратит CPU на видео;
- `v4l2_h264` — USB/UVC камера на `/dev/video0`, которая умеет сама выдавать H.264;
- `v4l2_encode` — USB-камера выдаёт сырой поток, Orange Pi кодирует его. По умолчанию используется `h264_v4l2m2m`; если он недоступен, можно поставить `libx264`, но на слабом Orange Pi Zero это заметно тяжелее;
- `test` — генератор тестовой картинки для проверки сети/SRT без камеры.

## Установка

Репозиторий предполагается в `/opt/robotlidar`:

```bash
cd /opt
git clone https://github.com/asbcorp24/robotlidar.git
cd /opt/robotlidar/orange_pi_zero_camera
chmod +x install.sh
sudo ./install.sh
```

Если репозиторий уже установлен:

```bash
cd /opt/robotlidar
git pull origin main
cd orange_pi_zero_camera
sudo ./install.sh
```

Установщик ставит `python3`, `ffmpeg`, `v4l-utils`, создаёт конфиг и systemd service.

## Конфигурация

```bash
nano /etc/robotlidar/orange-pi-zero-camera.json
```

Для IP/RTSP камеры:

```json
{
  "device_id": "CAM-OPIZERO-001",
  "device_name": "Передняя камера",
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
  "reconnect_delay_sec": 2.0
}
```

`device_id` должен быть уникальным. Именно этот ID затем привязывается к пользователю на центральном сервере.

## USB камера

Посмотреть камеры:

```bash
v4l2-ctl --list-devices
ls -l /dev/video*
```

Посмотреть форматы `/dev/video0`:

```bash
v4l2-ctl -d /dev/video0 --list-formats-ext
```

Если камера умеет H.264, использовать:

```json
"input_mode": "v4l2_h264"
```

Это лучший вариант для Orange Pi Zero, потому что H.264 формирует сама камера.

Если есть только MJPEG/YUYV:

```json
"input_mode": "v4l2_encode",
"encoder": "h264_v4l2m2m"
```

Проверить доступные H.264 encoder'ы:

```bash
ffmpeg -hide_banner -encoders | grep -E '264|v4l2'
```

## Проверка SRT

```bash
ffmpeg -hide_banner -protocols | grep -w srt
```

Строка `srt` обязательна. Сервер назначает каждому устройству отдельный UDP SRT port из диапазона `12000-12099`.

## Запуск

```bash
systemctl restart orange-pi-zero-camera
systemctl status orange-pi-zero-camera --no-pager
journalctl -u orange-pi-zero-camera -f
```

Нормальный старт выглядит примерно так:

```text
REGISTERED CAM-OPIZERO-001; SRT tele.xn----7sbbd7e6b.xn--p1ai:12002; latency=200ms
FFMPEG START: ... srt://tele.xn----7sbbd7e6b.xn--p1ai:12002?mode=caller...
```

На центральном сервере:

```bash
journalctl -u robotlidar -f
```

ожидается подключение SRT publisher для этого Device ID.

## Автовосстановление

Приложение рассчитано на автономную работу:

- если сервер временно недоступен — повторяет регистрацию;
- если FFmpeg завершился — перезапускает его;
- если сервер после рестарта потерял регистрацию устройства — telemetry получает 404 и приложение регистрируется заново;
- systemd дополнительно перезапускает процесс приложения при аварийном завершении.

## Что намеренно отсутствует

Это камера-only приложение. Оно не открывает WSS control channel и не принимает команды `DRIVE`, `BRUSH` или `PTZ`. Для полноценного трактора используются Raspberry/Radxa приложения проекта.
