# LDROBOT STL-19P с платой LD_CONTROL_SPEED_BOARD_V1.0

## Назначение платы

`LD_CONTROL_SPEED_BOARD_V1.0` используется как готовый интерфейс между
LDROBOT STL-19P и Raspberry Pi:

- подаёт питание 5 В на лидар;
- принимает UART-поток лидара;
- преобразует UART в USB;
- обеспечивает управление/стабилизацию вращения;
- определяется Linux как последовательный порт, обычно `/dev/ttyUSB0`.

Отдельно подключать `TX`, `PWM`, `5V` и `GND` лидара к GPIO Raspberry Pi при
использовании этой платы не нужно.

## Подключение

1. Подключить штатный кабель STL-19P к разъёму платы.
2. Подключить плату USB-кабелем к Raspberry Pi.
3. Проверить появление порта:

```bash
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
lsusb
```

Обычно плата использует USB-UART CP210x и появляется как `/dev/ttyUSB0`.

## Постоянное имя устройства

Чтобы номер не менялся между `/dev/ttyUSB0` и `/dev/ttyUSB1`, в проект добавлен
установщик правила `udev`.

При подключённой плате выполнить:

```bash
cd ~/robotlidar_ws/src/robotlidar
bash scripts/install_lidar_udev.sh /dev/ttyUSB0
```

Переподключить USB и проверить:

```bash
ls -l /dev/ldlidar
```

После этого основное имя устройства:

```text
/dev/ldlidar
```

Правило создаётся на основании фактических VID/PID и, если он доступен,
серийного номера именно подключённой платы.

## Параметры драйвера

Проект использует:

```text
ROS 2 package: ldlidar_stl_ros2
Product profile: LDLiDAR_LD19
Serial port: /dev/ldlidar
Baud rate: 230400
Data format: 8N1
Flow control: none
Topic: /scan
Frame: laser
```

STL-19P передаёт данные в одном направлении. Драйверу не требуется отправлять
команды запуска в лидар.

## Проверка

```bash
source /opt/ros/jazzy/setup.bash
source ~/robotlidar_ws/install/setup.bash

ros2 launch robotlidar ldrobot_stl19p.launch.py
```

В другом терминале:

```bash
ros2 topic hz /scan
ros2 topic echo /scan --once
```

При отсутствии правила `udev` можно временно указать исходный порт:

```bash
ros2 launch robotlidar ldrobot_stl19p.launch.py \
  serial_port:=/dev/ttyUSB0
```

## Если плата не определяется

```bash
dmesg --follow
lsusb
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
groups
```

Пользователь должен состоять в группе `dialout`:

```bash
sudo usermod -aG dialout "$USER"
sudo reboot
```

Не запускать одновременно два процесса, использующих один порт:

```bash
lsof /dev/ldlidar
lsof /dev/ttyUSB0
```

## Питание

Если лидар периодически отключается или выдаёт ошибку связи:

- проверить USB-кабель;
- подключить плату к другому USB-порту Raspberry Pi;
- исключить просадку питания Raspberry Pi;
- при необходимости использовать качественный USB-хаб с внешним питанием;
- не питать силовые двигатели трактора от общей линии питания Raspberry Pi без
  фильтрации и развязки.
