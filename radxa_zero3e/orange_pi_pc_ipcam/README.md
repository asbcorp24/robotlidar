# Orange Pi PC + Kingstar Smart IP camera gateway

Отдельный клиент RobotLiDAR для варианта, где камера и Orange Pi находятся в одной Ethernet-сети.

```text
Kingstar Smart IP camera
  | RTSP H.264 + ONVIF PTZ
  | Ethernet LAN
  v
Orange Pi PC
  |-- FFmpeg -c:v copy -> H.264/RTP -> RobotLiDAR Go server
  |-- UDP control <- RobotLiDAR Go server
  |-- ONVIF -> PAN/TILT Kingstar
  `-- USB-UART 115200 -> ESP32 track controller
                           |-- left/right tracks
                           |-- brush spin
                           `-- brush lift actuator
```

Сервер видит этот вариант так же, как Radxa: один постоянный `device_id`, один RTP stream и один control UDP port.

## Что реализовано

- регистрация `device_id` на `radxa_zero3e/server`;
- получение назначенного `video_ingest_port`;
- RTSP -> RTP через FFmpeg **без декодирования и перекодирования** (`-c:v copy`);
- автоматический перезапуск FFmpeg;
- `REQUEST_IDR` реализован перезапуском RTSP/RTP pipeline;
- ONVIF WS-Security UsernameToken PasswordDigest;
- автоматические ONVIF `GetCapabilities` и `GetProfiles`;
- ONVIF `AbsoluteMove` для PAN/TILT, включая Cardboard/гироскоп;
- команды гусениц -> существующий ESP32 протокол `DRV`;
- подъём щётки + скорость щётки -> существующий ESP32 протокол `AUX`;
- автоматическая отправка `ARM` перед первой ненулевой командой;
- локальный drive/lift watchdog;
- HTTP telemetry на центральный сервер;
- systemd service.

## ESP32 совместимость

Используется уже существующий протокол `firmware/esp32_wroom_track_controller`:

```text
DRV,seq,left,right*HH
AUX,seq,actuator,brush*HH
ARM,seq,1*HH
STOP,seq*HH
```

где:

- `left/right`: `-1000..+1000`;
- `actuator`: `-1/0/+1` — опустить/стоп/поднять;
- `brush`: `0..1000`.

Текущая аппаратная схема щётки ESP32 имеет DAC + Brake без линии Reverse. Поэтому отрицательный `spin` от сервера пока переводится в тот же модуль скорости. Для физического реверса щётки потребуется отдельный выход реверса в ESP32/силовой части.

ESP32 должен быть переключён пультом в режим `ROS`. Первый ненулевой web-командный пакет вызывает `ARM`, после чего идут `DRV/AUX`. При потере свежих команд ESP32 и Orange Pi независимо останавливают опасное движение watchdog'ом.

## Конфигурация

```bash
cp config.example.json config.json
nano config.json
```

Главные поля:

```json
{
  "device_id": "TRACTOR-ORANGE-0001",
  "server_http": "http://192.168.1.100:8000",
  "server_rtp_host": "192.168.1.100",
  "control_listen_port": 6000,

  "rtsp_url": "rtsp://admin:password@192.168.10.20:554/stream1",

  "onvif_device_service": "http://192.168.10.20/onvif/device_service",
  "onvif_username": "admin",
  "onvif_password": "password",

  "esp32_serial": "/dev/ttyUSB0",
  "esp32_baud": 115200
}
```

RTSP URL выше — пример. Нужно поставить реальный URL конкретной Kingstar Smart.

Если камера не отдаёт ONVIF capabilities корректно, можно вручную заполнить:

```json
{
  "onvif_ptz_url": "http://CAMERA_IP/onvif/ptz_service",
  "onvif_profile_token": "Profile_1"
}
```

## Проверка камеры до запуска gateway

Проверить RTSP:

```bash
ffprobe -rtsp_transport tcp "rtsp://USER:PASS@CAMERA_IP:554/STREAM"
```

Поток должен быть H.264 для прямого passthrough. Если Kingstar отдаёт только H.265, текущий browser H.264 passthrough потребует другой профиль камеры или отдельный транскодер.

Проверь синхронизацию времени Orange Pi:

```bash
timedatectl status
```

Для ONVIF WS-Security неправильное системное время может привести к отказу авторизации.

## Ручной запуск

Требуется Go 1.24+, FFmpeg и Linux.

```bash
cd radxa_zero3e/orange_pi_pc_ipcam
cp config.example.json config.json
nano config.json

go mod tidy
go build -o orange-pi-ipcam .
./orange-pi-ipcam --config config.json
```

Ожидаемый лог:

```text
device_id=TRACTOR-ORANGE-0001 local_ip=192.168.1.51 RTP=192.168.1.100:10000 control=UDP/6000
ONVIF PTZ: http://192.168.10.20/onvif/device_service
ESP32: /dev/ttyUSB0 @ 115200
FFmpeg RTSP copy started pid=...
```

## Установка как сервис

```bash
chmod +x install.sh
sudo ./install.sh
sudo nano /etc/robotlidar-orange/config.json
sudo systemctl restart orange-pi-ipcam
sudo journalctl -u orange-pi-ipcam -f
```

## Сеть

Orange Pi должна одновременно видеть:

1. IP-камеру Kingstar по Ethernet/LAN;
2. центральный RobotLiDAR server;
3. ESP32 по USB-UART.

Например:

```text
Camera:    192.168.10.20
Orange Pi: 192.168.10.10 / camera LAN
           192.168.1.51  / server LAN (если два интерфейса/VLAN)
Server:    192.168.1.100
```

Можно использовать одну общую Ethernet-подсеть, если это удобнее.

## Управляющий поток

```text
Web -> Go server -> UDP/6000 Orange Pi

Type 1 -> ONVIF Kingstar PAN/TILT
Type 2 -> ESP32 DRV left/right
Type 3 -> ESP32 AUX lift/brush
```

Cardboard работает без отдельной логики на сервере: гироскоп формирует обычные PTZ команды, а Orange Pi преобразует их в ONVIF `AbsoluteMove`.
