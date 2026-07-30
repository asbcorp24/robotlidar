# RobotLidar

Полностью офлайн-система автономного управления гусеничным трактором на базе Raspberry Pi 4 и ROS 2.

## Аппаратная конфигурация

- Raspberry Pi 4, 4 или 8 ГБ;
- Ubuntu Server 24.04 ARM64;
- ROS 2 Jazzy;
- лидар **LDROBOT STL-19P / D500**;
- MPU6050;
- датчики Холла левого и правого приводов;
- четыре дискретных выхода управления гусеницами;
- локальная веб-панель управления.

Рабочий код хранится в ветке `main`.

> STL-19P использует протокол LD19. В ROS 2 проект запускает пакет `ldlidar_stl_ros2` с профилем `LDLiDAR_LD19`, скоростью порта `230400` бод и топиком `/scan`.

## Возможности

- ручное управление трактором из браузера;
- управление вперёд, назад, влево и вправо;
- программная остановка и watchdog команд;
- построение карты через SLAM Toolbox;
- запись первого ручного маршрута;
- сохранение нескольких карт;
- выбор карты для текущего запуска;
- назначение карты по умолчанию;
- автономное движение через Nav2;
- обнаружение и объезд препятствий;
- продолжение движения по непройденным точкам маршрута;
- одометрия по датчикам Холла;
- объединение Холлов и MPU6050 через `robot_localization`;
- автоматический запуск веб-панели через `systemd`;
- полностью локальная работа без интернета.

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

LDROBOT STL-19P ─> /scan ─> SLAM / AMCL / costmap Nav2

Телефон / ноутбук ─> FastAPI :8080 ─> ROS 2

Nav2 / веб-панель ─> /cmd_vel ─> motor_gpio_node ─> GPIO гусениц
```

## Структура проекта

```text
config/
  tractor.yaml              GPIO, Холлы, MPU6050 и маршруты
  ekf.yaml                  объединение Холлов и IMU
  slam.yaml                 построение карты
  nav2.yaml                 локализация, планирование и объезд

launch/
  tractor_base.launch.py
  tractor_sensors.launch.py
  ldrobot_stl19p.launch.py
  mapping.launch.py
  navigation.launch.py

robotlidar/
  motor_gpio_node.py
  hall_odometry_node.py
  mpu6050_node.py
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

# 1. Установка операционной системы

Рекомендуемая конфигурация:

```text
Ubuntu Server 24.04 ARM64
ROS 2 Jazzy
Raspberry Pi 4
SSD через USB 3.0
Активное охлаждение
```

# 2. Установка ROS 2 Jazzy

Установить ROS 2 Jazzy для Ubuntu 24.04. После установки проверить:

```bash
source /opt/ros/jazzy/setup.bash
ros2 --help
```

# 3. Установка системных зависимостей

```bash
sudo apt update
sudo apt install -y \
  git \
  python3-rosdep \
  python3-colcon-common-extensions \
  python3-gpiozero \
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

Инициализация `rosdep`, если она ещё не выполнялась:

```bash
sudo rosdep init
rosdep update
```

Если `sudo rosdep init` сообщает, что файл уже существует, повторять команду не нужно.

# 4. Создание рабочего пространства

```bash
mkdir -p ~/robotlidar_ws/src
cd ~/robotlidar_ws/src
```

# 5. Загрузка проекта из main

```bash
git clone --branch main \
  https://github.com/asbcorp24/robotlidar.git
```

Проверить ветку:

```bash
cd ~/robotlidar_ws/src/robotlidar
git branch --show-current
```

Ожидаемый результат:

```text
main
```

# 6. Загрузка драйвера LDROBOT STL-19P

STL-19P работает через профиль LD19 официального ROS 2-пакета LDROBOT:

```bash
cd ~/robotlidar_ws/src

git clone \
  https://github.com/ldrobotSensorTeam/ldlidar_stl_ros2.git
```

Рекомендуется использовать стабильный тег драйвера:

```bash
cd ~/robotlidar_ws/src/ldlidar_stl_ros2
git fetch --tags
git checkout v3.0.3
```

Если тег отсутствует в используемой копии репозитория, оставить ветку `master` и собирать текущую версию.

# 7. Установка ROS-зависимостей

```bash
cd ~/robotlidar_ws
source /opt/ros/jazzy/setup.bash
rosdep install --from-paths src --ignore-src -r -y
```

# 8. Компиляция проекта

```bash
cd ~/robotlidar_ws
source /opt/ros/jazzy/setup.bash
colcon build --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Release
```

После успешной сборки подключить workspace:

```bash
source ~/robotlidar_ws/install/setup.bash
```

Проверить пакеты:

```bash
ros2 pkg prefix robotlidar
ros2 pkg prefix ldlidar_stl_ros2
```

Проверить исполняемые файлы проекта:

```bash
ros2 pkg executables robotlidar
```

В списке должны присутствовать:

```text
robotlidar motor_gpio_node
robotlidar hall_odometry_node
robotlidar mpu6050_node
robotlidar route_recorder_node
robotlidar route_player_node
robotlidar robotlidar_web
```

# 9. Автоматическое подключение ROS в терминале

```bash
echo 'source /opt/ros/jazzy/setup.bash' >> ~/.bashrc
echo 'source ~/robotlidar_ws/install/setup.bash' >> ~/.bashrc
source ~/.bashrc
```

# 10. Настройка доступа к устройствам

Добавить пользователя в системные группы:

```bash
sudo usermod -aG dialout,i2c,gpio "$USER"
```

Перезагрузить Raspberry Pi:

```bash
sudo reboot
```

После перезагрузки проверить порт лидара:

```bash
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
```

Обычно USB-UART адаптер STL-19P определяется как:

```text
/dev/ttyUSB0
```

Не рекомендуется постоянно использовать `chmod 777`. Доступ должен выдаваться через группу `dialout` или отдельное правило `udev`.

# 11. Проверка LDROBOT STL-19P

## Запуск через launch проекта

```bash
source /opt/ros/jazzy/setup.bash
source ~/robotlidar_ws/install/setup.bash

ros2 launch robotlidar ldrobot_stl19p.launch.py \
  serial_port:=/dev/ttyUSB0
```

В другом терминале:

```bash
source /opt/ros/jazzy/setup.bash
source ~/robotlidar_ws/install/setup.bash

ros2 topic hz /scan
ros2 topic echo /scan --once
```

## Проверка через официальный launch LDROBOT

```bash
ros2 launch ldlidar_stl_ros2 ld19.launch.py
```

У официального launch-файла порт обычно записан как `/dev/ttyUSB0`. При другом имени порта изменить `port_name` в launch-файле драйвера либо запускать launch проекта `robotlidar`, где порт передаётся аргументом.

## Направление сканирования

По умолчанию проект использует:

```text
laser_scan_dir:=true
```

Это соответствует направлению против часовой стрелки в драйвере LDROBOT.

При зеркальной карте или обратном направлении углов запустить:

```bash
ros2 launch robotlidar ldrobot_stl19p.launch.py \
  serial_port:=/dev/ttyUSB0 \
  laser_scan_dir:=false
```

Параметр также можно передать общему запуску датчиков:

```bash
ros2 launch robotlidar tractor_sensors.launch.py \
  laser_scan_dir:=false
```

# 12. Проверка MPU6050

Включить I2C:

```bash
sudo raspi-config
```

Далее выбрать:

```text
Interface Options
I2C
Enable
```

Проверить адрес:

```bash
i2cdetect -y 1
```

Ожидаемый адрес MPU6050:

```text
68
```

# 13. Настройка параметров трактора

Открыть:

```bash
nano ~/robotlidar_ws/src/robotlidar/config/tractor.yaml
```

Уточнить:

- GPIO управления левой и правой гусеницами;
- активный уровень выходов;
- GPIO сигналов Холла;
- число импульсов на оборот;
- передаточное отношение редуктора;
- перемещение гусеницы за оборот ведущей звезды;
- расстояние между центрами гусениц;
- ориентацию MPU6050;
- фактические размеры корпуса.

До проверки силовой части оставить:

```yaml
motor_gpio_node:
  ros__parameters:
    dry_run: true

hall_odometry_node:
  ros__parameters:
    dry_run: true

mpu6050_node:
  ros__parameters:
    dry_run: true
```

Координаты установки лидара задаются в:

```text
launch/tractor_sensors.launch.py
```

Временные значения:

```text
x = 0.35 м
y = 0.00 м
z = 0.55 м
```

Их необходимо заменить реальными расстояниями от `base_link` до центра STL-19P.

# 14. Запуск датчиков и одометрии

```bash
source /opt/ros/jazzy/setup.bash
source ~/robotlidar_ws/install/setup.bash

ros2 launch robotlidar tractor_sensors.launch.py \
  serial_port:=/dev/ttyUSB0
```

Проверка:

```bash
ros2 topic hz /scan
ros2 topic hz /imu/data_raw
ros2 topic hz /wheel/odom
ros2 topic echo /odometry/filtered --once
ros2 run tf2_ros tf2_echo odom base_link
```

# 15. Ручной запуск веб-приложения

```bash
source /opt/ros/jazzy/setup.bash
source ~/robotlidar_ws/install/setup.bash
ros2 run robotlidar robotlidar_web
```

Узнать IP Raspberry Pi:

```bash
hostname -I
```

Открыть с телефона или ноутбука:

```text
http://IP_RASPBERRY_PI:8080
```

Например:

```text
http://192.168.1.50:8080
```

# 16. Установка веб-приложения в автозапуск

После успешного ручного запуска:

```bash
cd ~/robotlidar_ws/src/robotlidar
bash scripts/install_web_service.sh ~/robotlidar_ws
```

Проверить сервис:

```bash
sudo systemctl status robotlidar-web.service
```

Посмотреть журнал:

```bash
journalctl -u robotlidar-web.service -f
```

Перезапустить:

```bash
sudo systemctl restart robotlidar-web.service
```

Остановить:

```bash
sudo systemctl stop robotlidar-web.service
```

Удалить автозапуск без удаления карт:

```bash
cd ~/robotlidar_ws/src/robotlidar
bash scripts/uninstall_web_service.sh
```

# 17. Первый ручной проезд и создание карты

Через веб-панель:

1. Открыть панель.
2. Запустить режим картографирования.
3. Очистить старый маршрут.
4. Начать запись маршрута.
5. Проехать всю рабочую площадку.
6. Остановить запись маршрута.
7. Ввести имя карты.
8. Сохранить карту.
9. Назначить её основной картой.

Командный запуск:

```bash
ros2 launch robotlidar mapping.launch.py \
  serial_port:=/dev/ttyUSB0
```

Начать запись:

```bash
ros2 service call /route/clear std_srvs/srv/Trigger "{}"
ros2 service call /route/start_recording std_srvs/srv/Trigger "{}"
```

Остановить запись:

```bash
ros2 service call /route/stop_recording std_srvs/srv/Trigger "{}"
```

Сохранить карту:

```bash
mkdir -p ~/robotlidar_data/maps
ros2 run nav2_map_server map_saver_cli \
  -f ~/robotlidar_data/maps/cleaning_area
```

Карта:

```text
~/robotlidar_data/maps/cleaning_area.yaml
~/robotlidar_data/maps/cleaning_area.pgm
```

Маршрут:

```text
~/robotlidar_data/routes/cleaning_route.yaml
```

# 18. Автономный запуск

```bash
ros2 launch robotlidar navigation.launch.py \
  serial_port:=/dev/ttyUSB0 \
  map:=$HOME/robotlidar_data/maps/cleaning_area.yaml
```

Запустить записанный маршрут:

```bash
ros2 service call /route/play std_srvs/srv/Trigger "{}"
```

Отменить маршрут:

```bash
ros2 service call /route/cancel std_srvs/srv/Trigger "{}"
```

# 19. Обновление проекта из main

```bash
cd ~/robotlidar_ws/src/robotlidar
git checkout main
git pull origin main

cd ~/robotlidar_ws/src/ldlidar_stl_ros2
git pull

cd ~/robotlidar_ws
source /opt/ros/jazzy/setup.bash
rosdep install --from-paths src --ignore-src -r -y
colcon build --symlink-install --cmake-args -DCMAKE_BUILD_TYPE=Release
source install/setup.bash

sudo systemctl restart robotlidar-web.service
```

# 20. Диагностика STL-19P

## Порт не найден

```bash
dmesg --follow
```

Подключить лидар и посмотреть, какое устройство появилось.

Также проверить:

```bash
lsusb
ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null
groups
```

В списке групп должна быть `dialout`.

## Ошибка `ldlidar communication is abnormal`

Проверить:

- правильный путь к порту;
- питание лидара;
- USB-кабель;
- USB-UART адаптер;
- наличие данных на RX;
- скорость `230400` бод;
- запуск профиля `LDLiDAR_LD19`;
- отсутствие второго процесса, открывшего тот же порт.

Проверить процессы:

```bash
lsof /dev/ttyUSB0
```

## Нет топика `/scan`

```bash
ros2 node list
ros2 topic list
ros2 topic info /scan
```

## Карта отображается зеркально

Поменять направление сканирования:

```bash
laser_scan_dir:=false
```

Также проверить физическую ориентацию лидара и трансформацию `base_link -> laser`.

# 21. Безопасность

Программная кнопка «СТОП» не заменяет аппаратную безопасность.

Обязательны:

- физическая аварийная кнопка;
- силовой контактор безопасности;
- гальваническая развязка GPIO;
- согласование уровней Холлов;
- аппаратный watchdog;
- ручной режим с аппаратным приоритетом;
- испытание сначала с вывешенными гусеницами;
- испытание на закрытой площадке без людей.

Интернет после установки и сборки можно отключить. Вся навигация, карты, маршруты и веб-панель работают локально на Raspberry Pi.
