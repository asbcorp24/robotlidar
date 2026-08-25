# RobotLiDAR Windows RTSP Camera Emulator

Отдельный тестовый проект для Windows. Он имитирует обычную IP-камеру и отдаёт настоящий RTSP/H.264 поток в локальную сеть.

Не зависит от Raspberry Pi/ROS. Используется для проверки цепочки:

```text
Windows RTSP emulator
        |
        | RTSP/H.264
        v
Raspberry Pi RobotLiDAR
        |
        | RTP/H.264 passthrough
        v
Central Go server -> WebRTC -> Browser
```

## Быстрый запуск

1. Установите FFmpeg и убедитесь, что `ffmpeg.exe` доступен в PATH.
2. Запустите:

```bat
run.bat
```

При первом запуске программа сама скачает Windows x64 MediaMTX в подпапку `runtime`.

После запуска будет показан URL примерно:

```text
rtsp://192.168.1.25:8554/camera
```

Именно этот URL указывайте в локальной панели Raspberry Pi в поле **RTSP URL камеры**.

## Проверка на Windows

```bat
ffplay -rtsp_transport tcp rtsp://127.0.0.1:8554/camera
```

или:

```bat
ffprobe -rtsp_transport tcp rtsp://127.0.0.1:8554/camera
```

## Проверка с Raspberry Pi

```bash
ffprobe -rtsp_transport tcp rtsp://WINDOWS_IP:8554/camera
```

Должен определиться H.264 видеопоток.

## Возможности

- настоящий RTSP server через MediaMTX;
- H.264 Baseline, `yuv420p`, без B-frames;
- тестовая движущаяся картинка FFmpeg `testsrc2`;
- разрешение, FPS и битрейт меняются в GUI;
- показывает локальный и LAN RTSP URL;
- кнопки Start/Stop;
- автоматическая загрузка MediaMTX для Windows x64;
- никакого ROS/Python окружения Raspberry не требуется.

## Windows Firewall

Если Raspberry не видит поток, разрешите входящие TCP-соединения на порт 8554 в Windows Defender Firewall. При первом запуске Windows также может сама показать запрос на разрешение сетевого доступа для `mediamtx.exe`.

## FFmpeg

Если FFmpeg отсутствует, можно установить его любым обычным способом, например через winget, если пакет доступен в вашей системе:

```bat
winget install Gyan.FFmpeg
```

После установки закройте и заново откройте терминал.
