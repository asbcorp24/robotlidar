# RobotLidar

Полностью офлайн-система автономного управления гусеничным трактором на базе Raspberry Pi 4, ROS 2 и отдельного ESP32-WROOM-32 для низкоуровневого управления приводами.

## Аппаратная конфигурация

- Raspberry Pi 4, 4 или 8 ГБ;
- Ubuntu Server 24.04 ARM64;
- ROS 2 Jazzy;
- ESP32-WROOM-32 — управление гусеницами, RC, Hall и аварийной цепью;
- лидар **LDROBOT STL-19P / D500**;
- MPU6050;
- GPS NEO-6M;
- локальная веб-панель управления.

Рабочий код хранится в ветке `main`.

> STL-19P использует протокол LD19. В ROS 2 проект запускает пакет `ldlidar_stl_ros2` с профилем `LDLiDAR_LD19`, скоростью порта `230400` бод и топиком `/scan`.

> **Важно:** Raspberry Pi НЕ управляет гусеницами напрямую через 40-pin GPIO. Команды движения передаются по USB Serial 115200 на ESP32. Все сигналы газа, Reverse, Brake, Hall, RC и аварийной цепи относятся к ESP32.

## Актуальная архитектура управления

```text
                              Raspberry Pi 4
                         ROS 2 / Nav2 / Web UI
                                 |
                +----------------+----------------+
                |                |                |
              USB              USB            40-pin
                |                |                |
            STL-19P           ESP32          MPU6050/GPS
                                 |
                  +--------------+--------------+
                  |              |              |
              TRACK LEFT     TRACK RIGHT      RC / ESTOP
```

Связь Raspberry Pi с ESP32:

```text
Raspberry Pi USB <---- USB Serial 115200 ----> ESP32-WROOM-32
```

ESP32 выполняет:

- аналоговый газ левой и правой гусеницы;
- Reverse LEFT/RIGHT;
- Low Brake LEFT/RIGHT;
- чтение Hall/Speed LEFT/RIGHT;
- чтение MC8RE-V2 CH1/CH2/CH5/CH6;
- аппаратно-программный ESTOP;
- watchdog команд ROS.

## Что подключается к 40-pin Raspberry Pi

В актуальной версии проекта 40-pin Raspberry Pi используется только для периферии самой Raspberry Pi.

| Физический pin | BCM | Назначение |
|---:|---:|---|
| 1 | — | 3.3 V -> MPU6050 VCC |
| 3 | GPIO2 / SDA1 | MPU6050 SDA |
| 5 | GPIO3 / SCL1 | MPU6050 SCL |
| 6 | — | GND -> MPU6050 GND |
| 8 | GPIO14 / TXD | GPS RX, опционально |
| 10 | GPIO15 / RXD | GPS TX -> Raspberry Pi |
| 9 | — | GND -> GPS GND |

Не подключать к Raspberry Pi GPIO напрямую:

- газ LEFT/RIGHT;
- Reverse LEFT/RIGHT;
- Low Brake LEFT/RIGHT;
- Hall/Speed LEFT/RIGHT;
- MC8RE-V2;
- аварийную петлю ESP32.

Лидар STL-19P/D500 подключается через USB-UART. ESP32 также подключается через USB.

## Поток данных

```text
Hall LEFT ─┐
           ├──> ESP32 ──USB Serial──> Raspberry Pi / ROS 2
Hall RIGHT ┘

MC8RE-V2 ─────> ESP32
ESTOP ────────> ESP32

ROS 2 / Nav2 / Web UI
          |
          v
   ESP32 track controller
          |
          +--> gas LEFT / RIGHT
          +--> Reverse LEFT / RIGHT
          +--> Low Brake LEFT / RIGHT

MPU6050 ──I2C────────────> Raspberry Pi ─> /imu/data_raw
GPS NEO-6M ─UART─────────> Raspberry Pi ─> /gps/fix
LDROBOT STL-19P ─USB─────> Raspberry Pi ─> /scan
```

## Структура проекта

```text
config/
  tractor.yaml
  ekf.yaml
  slam.yaml
  nav2.yaml

firmware/
  esp32_wroom_track_controller/

launch/
  tractor_base.launch.py
  tractor_sensors.launch.py
  ldrobot_stl19p.launch.py
  mapping.launch.py
  navigation.launch.py

robotlidar/
  route_recorder_node.py
  route_player_node.py
  web_app.py

web/static/
  index.html
  style.css
  app.js

scripts/
  install_web_service.sh
  uninstall_web_service.sh
```

## Raspberry Pi

Рекомендуемая конфигурация:

```text
Ubuntu Server 24.04 ARM64
ROS 2 Jazzy
Raspberry Pi 4
SSD через USB 3.0
Активное охлаждение
```

## ROS 2 Jazzy

После установки проверить:

```bash
source /opt/ros/jazzy/setup.bash
ros2 --help
```

## Зависимости

```bash
sudo apt update
sudo apt install -y \
  git \
  python3-rosdep \
  python3-colcon-common-extensions \
  python3-smbus2 \
  python3-yaml \
  python3-fastapi \
  python3-uvicorn \
  python3-pil \
  i2c-tools \
  ros-jazzy-navigation2 \
  ros-jazzy-nav2-bringup \
  ros-jazzy-slam-toolbox \
  ros-jazzy-robot-localization
```

## Workspace

```bash
mkdir -p ~/robotlidar_ws/src
cd ~/robotlidar_ws/src

git clone --branch main https://github.com/asbcorp24/robotlidar.git
```

## Драйвер LDROBOT STL-19P

```bash
cd ~/robotlidar_ws/src
git clone https://github.com/ldrobotSensorTeam/ldlidar_stl_ros2.git
```

Рекомендуемый стабильный тег:

```bash
cd ~/robotlidar_ws/src/ldlidar_stl_ros2
git fetch --tags
git checkout v3.0.3
```

## Сборка ROS workspace

```bash
cd ~/robotlidar_ws
source /opt/ros/jazzy/setup.bash
rosdep install --from-paths src --ignore-src -r -y
colcon build --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Release
source ~/robotlidar_ws/install/setup.bash
```

## Доступ к USB/UART/I2C

```bash
sudo usermod -aG dialout,i2c,gpio "$USER"
sudo reboot
```

После перезагрузки:

```bash
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
```

Типично:

```text
STL-19P USB-UART -> /dev/ttyUSB0
ESP32 USB-UART   -> /dev/ttyUSB1 или /dev/ttyACM0
```

Рекомендуется создать udev-правила и использовать стабильные символьные имена устройств.

## Проверка STL-19P

```bash
ros2 launch robotlidar ldrobot_stl19p.launch.py serial_port:=/dev/ttyUSB0
```

В другом терминале:

```bash
ros2 topic hz /scan
ros2 topic echo /scan --once
```

## Проверка MPU6050

Включить I2C и проверить адрес:

```bash
i2cdetect -y 1
```

Ожидаемый адрес:

```text
68
```

## GPS NEO-6M

Подключение:

```text
GPS TX -> Raspberry Pi physical pin 10 / GPIO15 RXD
GPS RX <- Raspberry Pi physical pin 8 / GPIO14 TXD   (опционально)
GPS GND -> Raspberry Pi GND
```

Порт:

```text
/dev/ttyS0
```

Скорость:

```text
9600 baud
```

## ESP32 track controller

Прошивка расположена в:

```text
firmware/esp32_wroom_track_controller
```

Сборка:

```bash
cd firmware/esp32_wroom_track_controller
pio run
pio run -t upload
pio device monitor
```

Архитектура ESP32:

```text
GPIO25 DAC -> gas LEFT
GPIO26 DAC -> gas RIGHT
GPIO16 -> TLP240A -> Reverse LEFT
GPIO17 -> TLP240A -> Reverse RIGHT
GPIO18 -> TLP240A -> Low Brake LEFT
GPIO19 -> TLP240A -> Low Brake RIGHT
GPIO34 <- Hall/Speed LEFT
GPIO35 <- Hall/Speed RIGHT
GPIO27 <- MC8RE CH1
GPIO33 <- MC8RE CH2
GPIO13 <- MC8RE CH5 RC/SAFE/ROS
GPIO14 <- MC8RE CH6 ARM
GPIO32 <- NC ESTOP loop
```

Полная распиновка и порядок безопасного первого запуска находятся в:

```text
firmware/esp32_wroom_track_controller/README.md
```

## Связь ROS -> ESP32

Физическая связь:

```text
Raspberry Pi USB -> ESP32 USB-UART
115200 baud
```

Основные команды протокола:

```text
DRV,seq,left,right*HH
ARM,seq,1*HH
ARM,seq,0*HH
STOP,seq*HH
PING,seq*HH
```

ESP32 возвращает телеметрию `TEL,...` с состоянием приводов, Hall, RC, режима и watchdog.

## Веб-интерфейс

Запуск:

```bash
source /opt/ros/jazzy/setup.bash
source ~/robotlidar_ws/install/setup.bash
ros2 run robotlidar robotlidar_web
```

IP Raspberry Pi:

```bash
hostname -I
```

Панель:

```text
http://IP_RASPBERRY_PI:8080
```

## Безопасность

Перед первым запуском силовой части:

1. Проверить ESP32 отдельно от моторов.
2. Проверить RC CH1/CH2/CH5/CH6.
3. Проверить режимы RC / SAFE / ROS.
4. Проверить ARM OFF -> ARM ON.
5. Проверить аппаратную аварийную кнопку.
6. Проверить Reverse и Brake без газа.
7. Измерить DAC мультиметром до подключения контроллеров.
8. Первый тест выполнять с вывешенными гусеницами и ограниченным газом.
9. Аварийная кнопка должна аппаратно разрывать силовой контактор независимо от ESP32 и Raspberry Pi.

## Ключевой принцип

```text
Raspberry Pi = высокоуровневое управление, ROS 2, Nav2, SLAM, Web
ESP32        = низкоуровневое управление приводами, RC, Hall, ESTOP
```

Raspberry Pi не должен непосредственно формировать силовые/управляющие GPIO-сигналы для контроллеров гусениц.
