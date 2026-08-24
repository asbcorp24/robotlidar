# Orange Pi PC + Botslab W311 gateway

Клиент RobotLiDAR для трактора с IP-камерой **Botslab W311**.

По официальным данным W311 имеет 4MP-видео, 360° pan/tilt, подключение по LAN или Wi-Fi и поддержку NVR через ONVIF/RTSP. Поэтому Orange Pi может использовать камеру как сетевой источник H.264 и как PTZ-устройство, не меняя серверный протокол RobotLiDAR.

```text
Botslab W311
  | Ethernet/LAN или Wi-Fi
  | RTSP + ONVIF
  v
LAN/router <---- Ethernet ---- Orange Pi PC
                              |-- FFmpeg -c:v copy -> H.264/RTP -> RobotLiDAR Go server
                              |-- UDP control <- RobotLiDAR Go server
                              |-- ONVIF -> PAN/TILT W311
                              `-- USB-UART 115200 -> ESP32
                                                   |-- tracks
                                                   |-- brush spin
                                                   `-- brush lift
```

Сервер видит этот вариант точно так же, как Radxa: один постоянный `device_id`, один RTP stream и один control UDP port.

## Перед первым запуском W311

1. Подключи W311 к той же локальной сети, где находится Orange Pi.
2. Зафиксируй IP камеры в DHCP reservation роутера.
3. В приложении Botslab включи/настрой доступ для NVR/ONVIF/RTSP, если прошивка требует отдельного включения.
4. Определи учётные данные камеры для ONVIF/RTSP.
5. Проверь фактический RTSP URL именно своей W311.

Официальный сайт подтверждает поддержку ONVIF/RTSP для NVR, но публично не публикует универсальный RTSP path для W311. Поэтому в `config.example.json` путь намеренно оставлен как `RTSP_PATH`, а не захардкожен неподтверждённый `/stream1`.

Пример формы URL:

```text
rtsp://USER:PASS@CAMERA_IP:554/RTSP_PATH
```

ONVIF Device Service по стандартной схеме пробуем так:

```text
http://CAMERA_IP/onvif/device_service
```

Если камера использует другой порт, укажи его в `onvif_device_service`.

## Что реализовано

- регистрация `device_id` на центральном Go server;
- получение `video_ingest_port`;
- RTSP -> RTP через FFmpeg без decode/encode (`-c:v copy`);
- автоматический рестарт FFmpeg;
- ONVIF WS-Security UsernameToken PasswordDigest;
- автоматические `GetCapabilities` / `GetProfiles`;
- ONVIF `AbsoluteMove` для обычного PTZ и Cardboard/гироскопа;
- гусеницы -> ESP32 `DRV`;
- подъём щётки + скорость вращения -> ESP32 `AUX`;
- автоматический `ARM` перед первой ненулевой командой;
- локальные watchdog для движения и подъёма;
- HTTP telemetry;
- systemd service.

## ESP32

Используется существующий протокол `firmware/esp32_wroom_track_controller`:

```text
DRV,seq,left,right*HH
AUX,seq,actuator,brush*HH
ARM,seq,1*HH
STOP,seq*HH
```

- `left/right`: `-1000..+1000`;
- `actuator`: `-1/0/+1`;
- `brush`: `0..1000`.

Текущая ESP32-схема щётки имеет DAC + Brake, но не отдельный Reverse. Поэтому отрицательный `spin` пока передаётся как тот же модуль скорости. Для настоящего реверса щётки потребуется добавить аппаратную линию Reverse и поддержку её в ESP32.

ESP32 должен быть переведён пультом в режим `ROS`.

## Конфигурация

```bash
cp config.example.json config.json
nano config.json
```

Пример:

```json
{
  "device_id": "TRACTOR-ORANGE-0001",
  "server_http": "http://192.168.1.100:8000",
  "server_rtp_host": "192.168.1.100",
  "control_listen_port": 6000,

  "rtsp_url": "rtsp://USER:PASS@192.168.1.60:554/RTSP_PATH",

  "onvif_device_service": "http://192.168.1.60/onvif/device_service",
  "onvif_username": "USER",
  "onvif_password": "PASS",

  "esp32_serial": "/dev/ttyUSB0",
  "esp32_baud": 115200
}
```

## Проверка W311

Проверь, какие порты слушает камера:

```bash
sudo apt install -y nmap
nmap -sT -p 80,443,554,8000,8080,8899,2020 CAMERA_IP
```

Проверка RTSP после определения URL:

```bash
ffprobe -rtsp_transport tcp "rtsp://USER:PASS@CAMERA_IP:554/RTSP_PATH"
```

Для прямого passthrough желательно, чтобы codec определялся как H.264. Если выбранный профиль W311 отдаёт H.265, переключи профиль камеры на H.264; иначе текущему browser passthrough понадобится транскодирование.

Можно также проверить ONVIF через ONVIF Device Manager с Windows или любым ONVIF discovery tool в той же LAN. После обнаружения `GetCapabilities` наш gateway сам получает PTZ XAddr и profile token.

Проверь время Orange Pi:

```bash
timedatectl status
```

ONVIF WS-Security чувствителен к неверному системному времени.

## Запуск

Требуются Linux, FFmpeg и Go 1.24+.

```bash
cd radxa_zero3e/orange_pi_pc_ipcam
cp config.example.json config.json
nano config.json

go mod tidy
go build -o orange-pi-ipcam .
./orange-pi-ipcam --config config.json
```

## Установка как systemd service

```bash
chmod +x install.sh
sudo ./install.sh
sudo nano /etc/robotlidar-orange/config.json
sudo systemctl restart orange-pi-ipcam
sudo journalctl -u orange-pi-ipcam -f
```

## Управляющий поток

```text
Web -> Go server -> UDP/6000 Orange Pi

Type 1 -> ONVIF Botslab W311 PAN/TILT
Type 2 -> ESP32 DRV left/right
Type 3 -> ESP32 AUX lift/brush
```

Cardboard не требует специального протокола: гироскоп телефона формирует обычные PAN/TILT, Orange Pi переводит их в ONVIF `AbsoluteMove` W311.

## Что проверить на реальной W311

Поддержка ONVIF/RTSP у модели подтверждена производителем, но конкретная прошивка может отличаться по:

- RTSP path;
- порту ONVIF;
- отдельной настройке включения NVR/ONVIF;
- поддерживаемому типу PTZ-команд (`AbsoluteMove`, `RelativeMove`, `ContinuousMove`).

Gateway сейчас сначала рассчитан на `AbsoluteMove`. Если W311 объявит только `ContinuousMove`, добавим автоматический fallback после первого теста `GetCapabilities/GetProfiles` на твоей камере.
