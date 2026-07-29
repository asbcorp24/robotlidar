# RobotLidar

Полностью офлайн-система автономного управления гусеничным трактором на базе:

- Raspberry Pi 4;
- ROS 2 Jazzy;
- RPLIDAR C1;
- MPU6050;
- датчиков Холла левого и правого приводов;
- четырёх дискретных выходов управления гусеницами.

## Текущее состояние

Реализовано:

- преобразование `/cmd_vel` в команды левой и правой гусениц;
- аппаратная взаимная блокировка программных направлений;
- пауза перед реверсом и watchdog команд;
- одометрия по импульсам Холла `/wheel/odom`;
- публикация MPU6050 `/imu/data_raw`;
- объединение Холлов и гироскопа через `robot_localization`;
- итоговая одометрия `/odometry/filtered` и TF `odom -> base_link`;
- драйвер RPLIDAR C1 через официальный `sllidar_ros2`;
- SLAM Toolbox для первого построения карты;
- запись маршрута в координатах карты;
- Nav2 для локализации, объезда препятствий и движения через точки;
- повтор маршрута через `NavigateThroughPoses`.

Интернет при эксплуатации не требуется. Все карты, маршруты и настройки
хранятся на Raspberry Pi.

> Код ещё не испытан на конкретном тракторе. Перед включением силовой части
> необходимо измерить механику, проверить уровни сигналов Холла и оставить
> `dry_run: true` до проверки всех ROS-топиков.

## Поток данных

```text
Холл слева ─┐
             ├─> hall_odometry_node ─> /wheel/odom ─┐
Холл справа ─┘                                       │
                                                     ├─> EKF
MPU6050 ───────> mpu6050_node ─────> /imu/data_raw ─┘
                                                         │
                                                         ├─> /odometry/filtered
                                                         └─> odom -> base_link

RPLIDAR C1 ─> /scan ─> SLAM / AMCL / карты препятствий Nav2

Nav2 ─> /cmd_vel ─> motor_gpio_node ─> GPIO гусениц
```

## Структура

```text
config/
  tractor.yaml       GPIO, Холлы, MPU6050 и маршруты
  ekf.yaml           объединение Холлов и IMU
  slam.yaml          построение карты
  nav2.yaml          локализация, планирование и объезд

launch/
  tractor_base.launch.py
  tractor_sensors.launch.py
  rplidar_c1.launch.py
  mapping.launch.py
  navigation.launch.py

robotlidar/
  motor_gpio_node.py
  hall_odometry_node.py
  mpu6050_node.py
  route_recorder_node.py
  route_player_node.py
```

## Установка

Рекомендуемая система: Ubuntu Server 24.04 ARM64 и ROS 2 Jazzy.

```bash
sudo apt update
sudo apt install -y \
  ros-jazzy-navigation2 \
  ros-jazzy-nav2-bringup \
  ros-jazzy-slam-toolbox \
  ros-jazzy-robot-localization \
  python3-colcon-common-extensions \
  python3-gpiozero \
  python3-smbus2 \
  python3-yaml \
  i2c-tools

mkdir -p ~/robotlidar_ws/src
cd ~/robotlidar_ws/src
git clone https://github.com/asbcorp24/robotlidar.git
git clone https://github.com/Slamtec/sllidar_ros2.git

cd ~/robotlidar_ws
source /opt/ros/jazzy/setup.bash
rosdep install --from-paths src --ignore-src -r -y
colcon build --symlink-install
source install/setup.bash
```

После установки и сборки интернет можно отключить. Желательно сохранить полный
образ SSD или microSD.

## Перед первым аппаратным запуском

Открыть:

```bash
nano ~/robotlidar_ws/src/robotlidar/config/tractor.yaml
```

Обязательно уточнить:

- GPIO управления;
- активный уровень выходов;
- GPIO Холлов;
- число импульсов на оборот;
- передаточное отношение;
- перемещение гусеницы за оборот ведущей звезды;
- расстояние между гусеницами;
- положение и ориентацию MPU6050;
- реальные габариты трактора в `config/nav2.yaml`.

Только после проверки поменять:

```yaml
motor_gpio_node:
  ros__parameters:
    dry_run: false

hall_odometry_node:
  ros__parameters:
    dry_run: false

mpu6050_node:
  ros__parameters:
    dry_run: false
```

## Проверка MPU6050

Включить I2C и проверить адрес:

```bash
sudo raspi-config
i2cdetect -y 1
```

Ожидаемый адрес: `68`.

Запуск датчиков:

```bash
ros2 launch robotlidar tractor_sensors.launch.py
```

Проверка:

```bash
ros2 topic hz /imu/data_raw
ros2 topic echo /imu/data_raw --once
ros2 topic hz /wheel/odom
ros2 topic echo /odometry/filtered --once
ros2 run tf2_ros tf2_echo odom base_link
```

## Проверка RPLIDAR C1

```bash
ros2 launch robotlidar rplidar_c1.launch.py serial_port:=/dev/ttyUSB0
ros2 topic hz /scan
ros2 topic echo /scan --once
```

C1 использует 460800 бод. По умолчанию публикуется фрейм `laser`.

## Первый проезд: карта и маршрут

Запустить:

```bash
ros2 launch robotlidar mapping.launch.py
```

Очистить старый маршрут и включить запись:

```bash
ros2 service call /route/clear std_srvs/srv/Trigger "{}"
ros2 service call /route/start_recording std_srvs/srv/Trigger "{}"
```

После ручного проезда:

```bash
ros2 service call /route/stop_recording std_srvs/srv/Trigger "{}"

mkdir -p ~/robotlidar_data/maps
ros2 run nav2_map_server map_saver_cli \
  -f ~/robotlidar_data/maps/cleaning_area
```

Маршрут сохраняется в:

```text
~/robotlidar_data/routes/cleaning_route.yaml
```

Карта сохраняется в:

```text
~/robotlidar_data/maps/cleaning_area.yaml
~/robotlidar_data/maps/cleaning_area.pgm
```

## Автономный запуск

```bash
ros2 launch robotlidar navigation.launch.py \
  map:=$HOME/robotlidar_data/maps/cleaning_area.yaml
```

После определения начальной позиции:

```bash
ros2 service call /route/play std_srvs/srv/Trigger "{}"
```

Остановка маршрута:

```bash
ros2 service call /route/cancel std_srvs/srv/Trigger "{}"
```

Nav2 сохраняет последовательность точек маршрута. При новом препятствии
локальная карта добавляет его, планировщик строит объезд, после чего трактор
продолжает движение к следующей непройденной точке.

## Безопасность

Программный GPIO-стоп не заменяет аппаратную безопасность. Обязательны:

- аварийная кнопка, физически снимающая питание приводов;
- контактор безопасности;
- гальваническая развязка GPIO;
- согласование уровней сигналов Холла;
- аппаратный watchdog;
- ручной режим с приоритетом над автоматическим;
- испытание сначала с вывешенными гусеницами;
- испытание на малой закрытой площадке без людей.
