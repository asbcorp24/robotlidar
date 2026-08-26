# RobotLiDAR Windows Camera / Tractor Emulator

Отдельный тестовый проект для Windows. В одном GUI есть два независимых режима.

## Режим 1 — RTSP IP-камера для Raspberry Pi

Эмулятор поднимает MediaMTX и публикует H.264/RTSP поток:

```text
Windows emulator -> RTSP/H.264 -> Raspberry Pi -> SRT/MPEG-TS/H.264 -> Go server -> WebRTC -> Browser
```

URL имеет вид:

```text
rtsp://WINDOWS_IP:8554/camera
```

В поле **IP Windows, доступный Raspberry** укажите IPv4 физического Ethernet/Wi-Fi адаптера, находящегося в одной сети с Raspberry. VPN/VirtualBox/Hyper-V могут иметь собственные адреса, поэтому поле оставлено редактируемым.

Для этого режима Raspberry работает точно так же, как с реальной IP-камерой: читает RTSP и сама устанавливает исходящее SRT-соединение к центральному серверу.

## Режим 2 — полный эмулятор трактора

Этот режим не требует Raspberry Pi или ESP32. Windows программа моделирует текущую сетевую архитектуру реального трактора:

```text
Windows emulator
   ├── SRT/MPEG-TS/H.264 ─────► central Go server ──► WebRTC ──► Browser
   └── WSS control channel ◄── central Go server ◄────────────── Browser
```

При запуске полного режима программа:

1. регистрирует `Device ID` на центральном Go-сервере;
2. запрашивает `video_transport = srt`;
3. получает `srt_ingest_port` и `srt_latency_ms`;
4. запускает FFmpeg как SRT caller и отправляет MPEG-TS/H.264 на сервер;
5. сама открывает исходящее `WSS` соединение `/api/devices/<DEVICE_ID>/control-ws`;
6. принимает тот же фиксированный 16-байтный бинарный протокол, что Raspberry;
7. показывает в GUI PTZ, гусеницы, щётку, транспорт видео и состояние WSS.

Формат команды:

```text
magic:u16 = 0x5354
version:u8 = 1
type:u8
seq:u32
value1:i16
value2:i16
speed:u16
flags:u16
```

Типы:

```text
type 1 = PTZ
type 2 = drive left/right
type 3 = brush spin/lift
```

`flags bit0` — CENTER, `flags bit1` — REQUEST_IDR.

### Настройки полного режима

Для production-теста:

```text
Server URL: https://tele.xn----7sbbd7e6b.xn--p1ai
Device ID:  TRACTOR-WIN-XXXXXXXXXX
Legacy UDP port: 6000
```

`Legacy UDP port` оставлен только для совместимости и локальных тестов. Нормальное удалённое управление через Интернет использует WSS и не требует входящего UDP/6000 на Windows.

После запуска в GUI должно появиться примерно:

```text
Видео:       SRT UDP/12000
Управление:  WSS подключён
Статус:      Подключён: SRT + WSS
```

В журнале:

```text
DEVICE READY: ... SRT/12000 latency=200ms ...
CONTROL/WSS connected: wss://tele.../api/devices/.../control-ws
```

После этого добавьте тот же `Device ID` в аккаунт на центральном сайте. При нажатии PTZ, W/A/S/D, STOP, щётки и подъёма значения должны меняться прямо в GUI.

## Быстрый запуск

Нужны Python 3 и FFmpeg.

```bat
run.bat
```

`run.bat` автоматически устанавливает:

```text
websocket-client
truststore
```

`truststore` нужен, чтобы WSS использовал штатное хранилище сертификатов Windows. Проверка TLS не отключается.

HTTPS регистрация/телеметрия запускается через системный `curl.exe` / Schannel, как и раньше.

## Проверка поддержки SRT в FFmpeg

Полный режим требует FFmpeg с libsrt:

```bat
ffmpeg -protocols | findstr /I srt
```

В выводе должен присутствовать `srt` как поддерживаемый протокол.

Если его нет, установите полноценную Windows-сборку FFmpeg с libsrt.

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

Для **режима 1** разрешите входящий TCP `8554`, потому что Raspberry подключается к Windows RTSP-серверу:

```powershell
New-NetFirewallRule -DisplayName "RobotLiDAR RTSP Emulator" -Direction Inbound -Protocol TCP -LocalPort 8554 -Action Allow
```

Для **режима 2** входящие правила обычно не нужны: SRT и WSS инициируются самим Windows-компьютером наружу.

UDP/6000 требуется только если специально тестируется старый legacy UDP control.

## Видео полного режима

Тестовый поток:

- H.264 Baseline;
- `yuv420p`;
- без B-frames;
- GOP около 1 секунды;
- SPS/PPS повторяются на keyframe;
- MPEG-TS поверх SRT;
- `testsrc2`;
- разрешение/FPS/битрейт задаются в GUI.

На Go-сервере FFmpeg не используется: SRT/MPEG-TS/H.264 разбирается в Go и передаётся в Pion WebRTC без decode/encode.

## FFmpeg через winget

```bat
winget install Gyan.FFmpeg
```

После установки заново откройте терминал и проверьте поддержку SRT командой выше.
