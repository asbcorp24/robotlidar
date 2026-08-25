# RobotLiDAR Windows Camera / Tractor Emulator

Отдельный тестовый проект для Windows. В одном GUI есть два независимых режима.

## Режим 1 — RTSP IP-камера для Raspberry Pi

Эмулятор поднимает MediaMTX и публикует настоящий H.264/RTSP поток:

```text
Windows emulator -> RTSP/H.264 -> Raspberry Pi -> RTP/H.264 -> Go server -> WebRTC -> Browser
```

URL имеет вид:

```text
rtsp://WINDOWS_IP:8554/camera
```

В поле **IP Windows, доступный Raspberry** укажите IPv4 физического Ethernet/Wi-Fi адаптера, находящегося в одной сети с Raspberry. Это поле редактируемое специально потому, что VPN/VirtualBox/Hyper-V могут иметь адреса вроде `10.x.x.x` и автоматически выбираться Windows как маршрут по умолчанию.

## Режим 2 — полный эмулятор трактора

Этот режим вообще не требует Raspberry Pi или ESP32. Windows программа сама:

1. регистрирует `Device ID` на центральном Go-сервере;
2. получает `video_ingest_port`;
3. отправляет H.264/RTP тестовое видео прямо на сервер;
4. слушает UDP control port (по умолчанию `6000`);
5. принимает те же команды, что реальный трактор;
6. показывает в GUI:
   - PAN/TILT камеры;
   - левую и правую гусеницу;
   - скорость вращения щётки;
   - подъём/опускание щётки;
   - номер и время последней команды.

Формат команд полностью совпадает с Raspberry/Orange Pi gateway:

```text
type 1 = PTZ
type 2 = drive left/right
type 3 = brush spin/lift
```

Для запуска полного режима укажите:

```text
Server URL: http://SERVER_IP:8000
Device ID:  TRACTOR-WIN-XXXXXXXXXX
IP Windows: реальный LAN IPv4 этого ПК
UDP port:   6000
```

Затем нажмите **Подключить эмулятор к серверу** и добавьте этот Device ID в аккаунт на центральном сайте.

После этого можно прямо с веб-сайта проверить кнопки PTZ, W/A/S/D, STOP, щётку и подъём. Значения будут меняться в окне Windows-эмулятора.

## Быстрый запуск

Установите Python 3 и FFmpeg. FFmpeg должен быть доступен в PATH либо `ffmpeg.exe` можно положить рядом с `emulator.py`.

```bat
run.bat
```

При первом запуске RTSP-режима программа сама скачает Windows x64 MediaMTX в подпапку `runtime`.

## Проверка RTSP на Windows

```bat
ffplay -rtsp_transport tcp rtsp://127.0.0.1:8554/camera
```

или:

```bat
ffprobe -rtsp_transport tcp rtsp://127.0.0.1:8554/camera
```

## Проверка RTSP с Raspberry Pi

```bash
ffprobe -rtsp_transport tcp rtsp://WINDOWS_IP:8554/camera
```

Должен определиться H.264 Baseline поток.

## Windows Firewall

Для режима RTSP разрешите входящий TCP `8554`.

Для полного эмулятора разрешите входящий UDP `6000` (или выбранный вами control port).

Пример PowerShell от администратора:

```powershell
New-NetFirewallRule -DisplayName "RobotLiDAR RTSP Emulator" -Direction Inbound -Protocol TCP -LocalPort 8554 -Action Allow
New-NetFirewallRule -DisplayName "RobotLiDAR Control Emulator" -Direction Inbound -Protocol UDP -LocalPort 6000 -Action Allow
```

## Видео

Тестовый поток:

- H.264 Baseline;
- `yuv420p`;
- без B-frames;
- GOP около 1 секунды;
- `testsrc2`;
- разрешение/FPS/битрейт задаются в GUI.

## FFmpeg через winget

```bat
winget install Gyan.FFmpeg
```

После установки заново откройте терминал/`run.bat`.
