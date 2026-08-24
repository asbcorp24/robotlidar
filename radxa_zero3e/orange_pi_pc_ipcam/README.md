# Orange Pi PC + Mercusys MC500 gateway

Клиент RobotLiDAR для трактора с IP-камерой Mercusys MC500.

MC500 официально поддерживает H.264, RTSP и ONVIF/PTZ. Сама камера подключается по Wi-Fi 2.4 ГГц; Orange Pi PC можно подключить к тому же роутеру по Ethernet.

```text
Mercusys MC500
  | Wi-Fi 2.4 GHz
  | RTSP H.264 :554 + ONVIF :2020
  v
LAN/router <---- Ethernet ---- Orange Pi PC
                              |-- FFmpeg -c:v copy -> H.264/RTP -> RobotLiDAR Go server
                              |-- UDP control <- RobotLiDAR Go server
                              |-- ONVIF -> PAN/TILT MC500
                              `-- USB-UART 115200 -> ESP32
                                                   |-- tracks
                                                   |-- brush spin
                                                   `-- brush lift
```

Сервер видит этот вариант точно так же, как Radxa: один постоянный `device_id`, один RTP stream и один control UDP port.

## Перед первым запуском MC500

В приложении MERCUSYS открой:

```text
Camera -> Device Settings -> Advanced Settings -> Camera Account
```

Создай отдельный username/password камеры. Это не пароль аккаунта MERCUSYS. Эти данные используются RTSP и ONVIF.

Высокое качество:

```text
rtsp://USER:PASS@CAMERA_IP:554/stream1
```

Низкое качество:

```text
rtsp://USER:PASS@CAMERA_IP:554/stream2
```

ONVIF service port у Mercusys: `2020`. Для MC500 в конфиге используется:

```text
http://CAMERA_IP:2020/onvif/device_service
```

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

  "rtsp_url": "rtsp://camerauser:camerapassword@192.168.1.60:554/stream1",

  "onvif_device_service": "http://192.168.1.60:2020/onvif/device_service",
  "onvif_username": "camerauser",
  "onvif_password": "camerapassword",

  "esp32_serial": "/dev/ttyUSB0",
  "esp32_baud": 115200
}
```

## Проверка MC500

Сначала зафиксируй IP камеры в DHCP reservation роутера, например `192.168.1.60`.

Проверка RTSP:

```bash
ffprobe -rtsp_transport tcp "rtsp://USER:PASS@192.168.1.60:554/stream1"
```

Для прямого passthrough codec должен определяться как H.264.

Если разрешение stream1 ниже 1920x1080, поставь в MERCUSYS app качество `Best Quality`.

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

Type 1 -> ONVIF MC500 PAN/TILT
Type 2 -> ESP32 DRV left/right
Type 3 -> ESP32 AUX lift/brush
```

Cardboard не требует специального протокола: гироскоп телефона формирует обычные PAN/TILT, Orange Pi переводит их в ONVIF `AbsoluteMove` MC500.
