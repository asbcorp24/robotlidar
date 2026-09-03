# Orange Pi Zero Camera + PTZ

Отдельное приложение RobotLiDAR для Orange Pi Zero, которое занимается камерой, PTZ и локальной настройкой через веб-интерфейс.

В нём нет ROS, UART/ESP32, гусениц или щётки.

```text
IP/USB camera --H.264--> Orange Pi Zero --SRT--> Go server --WebRTC--> Browser
                                ^
                                |
                         ONVIF AbsoluteMove
                                ^
                                |
Browser --PTZ--> Go server --WSS

Local phone/notebook --HTTP :8088--> Orange Pi Zero web config
```

## Папка

```text
orange_pi_zero_camera/
├── camera_streamer.py
├── onvif_discovery.py
├── web_config.py
├── config.example.json
├── install.sh
├── orange-pi-zero-camera.service
├── orange-pi-zero-web.service
└── README.md
```

## Веб-настройка

После установки локальная панель работает на:

```text
http://ORANGE_PI_IP:8088/
```

В ней можно:

- просканировать доступные Wi-Fi сети;
- выбрать SSID и подключить Orange Pi к Wi-Fi;
- увидеть текущие IPv4 адреса;
- задать `Device ID` и название устройства;
- изменить адрес центрального сервера;
- выбрать RTSP / USB H.264 / USB encode / test source;
- изменить RTSP URL, разрешение, FPS и битрейт;
- включить PTZ и ONVIF auto-discovery;
- задать ONVIF логин/пароль;
- сохранить конфиг;
- перезапустить сервис трансляции.

Wi-Fi настраивается через NetworkManager / `nmcli`. Пароль Wi-Fi не хранится в конфиге RobotLiDAR — его сохраняет NetworkManager. ONVIF пароль хранится в `/etc/robotlidar/orange-pi-zero-camera.json` с правами `0600` и не возвращается обратно в браузер после сохранения.

### Первичная настройка Wi-Fi

Удобнее всего первый раз подключить Orange Pi кабелем Ethernet к роутеру. Узнать адрес:

```bash
hostname -I
```

Например, если Orange Pi получил `192.168.1.80`, открыть на ноутбуке/телефоне:

```text
http://192.168.1.80:8088/
```

В разделе Wi-Fi нажать **Обновить сети**, выбрать сеть, ввести пароль и нажать **Подключиться**. После успешного подключения можно отключить Ethernet и открыть панель уже по Wi-Fi адресу Orange Pi.

## Что умеет стример

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
- PTZ передаётся в камеру через ONVIF `AbsoluteMove` с WS-Security PasswordDigest;
- при `onvif_auto_discovery=true` автоматически ищет ONVIF Device Service, PTZ XAddr и ProfileToken.

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

Установщик ставит `python3`, `python3-websocket`, `ffmpeg`, `v4l-utils`, CA certificates и `network-manager`, создаёт конфиг, устанавливает два systemd сервиса и запускает локальную веб-панель.

После установки выводится адрес вида:

```text
Web config: http://192.168.1.80:8088/
```

## Конфигурационный файл

Веб-интерфейс работает с тем же файлом:

```bash
/etc/robotlidar/orange-pi-zero-camera.json
```

При необходимости его можно редактировать вручную:

```bash
sudo nano /etc/robotlidar/orange-pi-zero-camera.json
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
  "onvif_auto_discovery": true,
  "onvif_device_url": "",
  "onvif_url": "",
  "onvif_username": "admin",
  "onvif_password": "CHANGE_ME",
  "onvif_profile_token": ""
}
```

`device_id` должен быть уникальным и затем привязывается к пользователю на центральном сервере.

## ONVIF auto-discovery

При включённом `onvif_auto_discovery` приложение берёт IP камеры из RTSP URL и пробует стандартные ONVIF Device Service адреса. Затем выполняет `GetCapabilities` и `GetProfiles`, получает PTZ XAddr и ProfileToken.

Нормальный лог:

```text
ONVIF discovered: device=http://192.168.1.149/onvif/device_service; PTZ=http://192.168.1.149/onvif/ptz_service; profile=Profile_1
```

Если автопоиск не подходит конкретной камере, значения `onvif_device_url`, `onvif_url` и `onvif_profile_token` можно задать вручную через веб-панель.

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

## Сервисы

Стример:

```bash
systemctl restart orange-pi-zero-camera
systemctl status orange-pi-zero-camera --no-pager
journalctl -u orange-pi-zero-camera -f
```

Веб-панель:

```bash
systemctl restart orange-pi-zero-web
systemctl status orange-pi-zero-web --no-pager
journalctl -u orange-pi-zero-web -f
```

Нормальный лог стримера:

```text
REGISTERED CAM-OPIZERO-001; SRT tele.xn----7sbbd7e6b.xn--p1ai:12002; latency=200ms
ONVIF discovered: ...
FFMPEG START: ... srt://tele.xn----7sbbd7e6b.xn--p1ai:12002?mode=caller...
CONTROL/WSS connected: wss://tele.xn----7sbbd7e6b.xn--p1ai/api/devices/CAM-OPIZERO-001/control-ws
```

При управлении камерой с сайта:

```text
CONTROL/PTZ seq=123 pan=10.0 tilt=-5.0
```

## Автовосстановление

Приложение автоматически:

- повторяет регистрацию при недоступности сервера;
- переподключает WSS;
- перезапускает FFmpeg;
- регистрируется заново после 404 telemetry;
- дополнительно перезапускается systemd при аварийном завершении.

## Ограничение первичной Wi-Fi настройки

Локальная веб-панель требует уже существующего сетевого соединения, чтобы до неё можно было достучаться. Для полностью автономного первого запуска без Ethernet потребуется отдельный режим Wi-Fi Access Point / captive portal. Он пока не включён в эту папку.

## Что намеренно отсутствует

Это камера + PTZ приложение. Команды `DRIVE` и `BRUSH` принимаются по общему каналу только для совместимости протокола и сразу игнорируются. Управление ходовой частью остаётся в Raspberry/Radxa приложениях RobotLiDAR.
