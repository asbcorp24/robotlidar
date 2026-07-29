# RobotLidar

Полностью офлайн-система автономного управления гусеничным трактором на базе:

- Raspberry Pi 4;
- Ubuntu Server 24.04 ARM64;
- ROS 2 Jazzy;
- RPLIDAR C1;
- MPU6050;
- датчиков Холла левого и правого приводов;
- четырёх дискретных выходов управления гусеницами;
- локальной веб-панели для управления, карт и автозапуска.

Рабочий код хранится в ветке `main`.

## Возможности

- преобразование `/cmd_vel` в команды левой и правой гусениц;
- блокировка противоположных направлений;
- пауза перед реверсом;
- автоматический стоп при пропадании команд;
- одометрия по импульсам Холла `/wheel/odom`;
- публикация MPU6050 `/imu/data_raw`;
- объединение Холлов и гироскопа через `robot_localization`;
- итоговая одометрия `/odometry/filtered`;
- драйвер RPLIDAR C1;
- построение карты через SLAM Toolbox;
- запись первого ручного маршрута;
- автономное движение через Nav2;
- обнаружение и объезд новых препятствий;
- продолжение движения по непройденной части маршрута;
- локальная FastAPI-панель на порту `8080`;
- ручное управление с телефона или ноутбука;
- сохранение, просмотр и выбор карт;
- назначение карты по умолчанию;
- автозапуск веб-панели и выбранного рабочего режима через `systemd`.

Интернет при эксплуатации не требуется. Карты, маршрут и настройки хранятся на Raspberry Pi.

> До аппаратной проверки необходимо оставить `dry_run: true`. Веб-кнопка «СТОП» не заменяет физическую аварийную кнопку, силовой контактор и аппаратный watchdog.

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

Телефон / ноутбук ─> FastAPI :8080 ─> /cmd_vel и ROS-сервисы

Nav2 / веб-панель ─> /cmd_vel ─> motor_gpio_node ─> GPIO гусениц
```

## Структура проекта

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
  web_app.py

web/static/
  index.html
  style.css
  app.js

scripts/
  install_web_service.sh
  uninstall_web_service.sh
```

# Полная установка и сборка

## 1. Установка операционной системы

Рекомендуется:

```text
Ubuntu Server 24.04 ARM64
ROS 2 Jazzy
Raspberry Pi 4, 4 или 8 ГБ ОЗУ
```

Желательно использовать SSD через USB 3.0 и активное охлаждение.

## 2. Установка ROS 2 Jazzy

Сначала установите ROS 2 Jazzy по официальной инструкции для Ubuntu 24.04. После установки должна существовать команда:

```bash
source /opt/ros/jazzy/setup.bash
ros2 --help
```

## 3. Установка системных зависимостей

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

Если `rosdep` ещё не инициализирован:

```bash
sudo rosdep init
rosdep update
```

Если команда `sudo rosdep init` сообщает, что файл уже существует, повторно выполнять её не нужно.

## 4. Создание рабочего пространства

```bash
mkdir -p ~/robotlidar_ws/src
cd ~/robotlidar_ws/src
```

## 5. Загрузка проекта из main

```bash
git clone --branch main \
  https://github.com/asbcorp24/robotlidar.git
```

Загрузка официального ROS 2-драйвера Slamtec:

```bash
git clone https://github.com/Slamtec/sllidar_ros2.git
```

Проверка:

```bash
cd ~/robotlidar_ws/src/robotlidar
git branch --show-current
```

Ожидаемый результат:

```text
main
```

## 6. Установка ROS-зависимостей

```bash
cd ~/robotlidar_ws
source /opt/ros/jazzy/setup.bash
rosdep install --from-paths src --ignore-src -r -y
```

## 7. Компиляция проекта

Полная сборка:

```bash
cd ~/robotlidar_ws
source /opt/ros/jazzy/setup.bash
colcon build --symlink-install
```

После успешной сборки в конце должно быть сообщение без пакетов со статусом `Failed`.

Подключение собранного рабочего пространства:

```bash
source ~/robotlidar_ws/install/setup.bash
```

Проверка, что пакет найден:

```bash
ros2 pkg prefix robotlidar
```

Проверка доступных исполняемых файлов:

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

## 8. Автоматическое подключение ROS в терминале

Чтобы не выполнять `source` после каждого входа:

```bash
echo 'source /opt/ros/jazzy/setup.bash' >> ~/.bashrc
echo 'source ~/robotlidar_ws/install/setup.bash' >> ~/.bashrc
source ~/.bashrc
```

# Настройка перед первым запуском

Открыть главный файл параметров:

```bash
nano ~/robotlidar_ws/src/robotlidar/config/tractor.yaml
```

Обязательно уточнить:

- GPIO управления двигателями;
- активный уровень управляющих сигналов;
- GPIO сигналов Холла;
- число импульсов Холла на оборот;
- передаточное отношение редуктора;
- перемещение гусеницы за оборот ведущей звезды;
- расстояние между продольными центрами гусениц;
- положение и ориентацию MPU6050;
- положение лидара относительно центра трактора;
- реальные размеры корпуса и стоп-зоны в `config/nav2.yaml`.

До проверки оставить:

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

# Проверка оборудования

## MPU6050

Включить I2C:

```bash
sudo raspi-config
```

Далее:

```text
Interface Options
I2C
Enable
```

Проверить адрес:

```bash
i2cdetect -y 1
```

Ожидаемый адрес:

```text
68
```

## RPLIDAR C1

Проверить USB-порт:

```bash
ls -l /dev/ttyUSB*
```

Обычно используется:

```text
/dev/ttyUSB0
```

Добавить пользователя в группы доступа:

```bash
sudo usermod -aG dialout,i2c,gpio "$USER"
```

После этого выполнить перезагрузку:

```bash
sudo reboot
```

# Ручной тестовый запуск

После перезагрузки:

```bash
source /opt/ros/jazzy/setup.bash
source ~/robotlidar_ws/install/setup.bash
```

## Запуск только лидара

```bash
ros2 launch robotlidar rplidar_c1.launch.py \
  serial_port:=/dev/ttyUSB0
```

В другом терминале:

```bash
source /opt/ros/jazzy/setup.bash
source ~/robotlidar_ws/install/setup.bash
ros2 topic hz /scan
ros2 topic echo /scan --once
```

## Запуск датчиков и одометрии

```bash
ros2 launch robotlidar tractor_sensors.launch.py
```

Проверка:

```bash
ros2 topic hz /imu/data_raw
ros2 topic echo /imu/data_raw --once
ros2 topic hz /wheel/odom
ros2 topic echo /wheel/odom --once
ros2 topic echo /odometry/filtered --once
ros2 run tf2_ros tf2_echo odom base_link
```

## Ручной запуск веб-приложения

```bash
ros2 run robotlidar robotlidar_web
```

Узнать IP Raspberry Pi:

```bash
hostname -I
```

Открыть на телефоне или ноутбуке:

```text
http://IP_RASPBERRY_PI:8080
```

Например:

```text
http://192.168.1.50:8080
```

# Установка веб-приложения в автозапуск

После успешного ручного запуска:

```bash
cd ~/robotlidar_ws/src/robotlidar
bash scripts/install_web_service.sh ~/robotlidar_ws
```

Скрипт:

- устанавливает зависимости веб-панели;
- создаёт каталоги карт, маршрутов и настроек;
- устанавливает `robotlidar-web.service`;
- включает запуск веб-панели после включения Raspberry Pi;
- сразу запускает сервис.

Проверка:

```bash
sudo systemctl status robotlidar-web.service
```

Журнал:

```bash
journalctl -u robotlidar-web.service -f
```

Перезапуск:

```bash
sudo systemctl restart robotlidar-web.service
```

Остановка:

```bash
sudo systemctl stop robotlidar-web.service
```

Удаление автозапуска без удаления карт:

```bash
cd ~/robotlidar_ws/src/robotlidar
bash scripts/uninstall_web_service.sh
```

# Работа через веб-панель

## Первый ручной проезд

1. Открыть веб-панель.
2. Выбрать режим построения карты.
3. Нажать запуск картографирования.
4. Очистить старый маршрут.
5. Начать запись маршрута.
6. Управлять трактором кнопками направления.
7. После прохождения всей рабочей зоны остановить запись.
8. Ввести имя карты.
9. Сохранить карту.
10. Назначить её основной картой.

Карты сохраняются в:

```text
~/robotlidar_data/maps/
```

Маршрут сохраняется в:

```text
~/robotlidar_data/routes/cleaning_route.yaml
```

## Автономный проезд

1. Открыть веб-панель.
2. Выбрать сохранённую карту.
3. Запустить автономный режим.
4. Указать начальное положение трактора на карте, если оно не восстановилось автоматически.
5. Нажать запуск маршрута.

Nav2 использует лидар для обнаружения новых препятствий, строит объезд и затем продолжает движение к оставшимся точкам маршрута.

# Командный режим без веб-панели

## Построение карты

```bash
ros2 launch robotlidar mapping.launch.py
```

Начать запись маршрута:

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

## Автономная навигация

```bash
ros2 launch robotlidar navigation.launch.py \
  map:=$HOME/robotlidar_data/maps/cleaning_area.yaml
```

Запустить записанный маршрут:

```bash
ros2 service call /route/play std_srvs/srv/Trigger "{}"
```

Остановить маршрут:

```bash
ros2 service call /route/cancel std_srvs/srv/Trigger "{}"
```

# Обновление проекта из main

Остановить веб-сервис:

```bash
sudo systemctl stop robotlidar-web.service
```

Получить изменения:

```bash
cd ~/robotlidar_ws/src/robotlidar
git checkout main
git pull origin main
```

При необходимости обновить драйвер лидара:

```bash
cd ~/robotlidar_ws/src/sllidar_ros2
git pull
```

Пересобрать:

```bash
cd ~/robotlidar_ws
source /opt/ros/jazzy/setup.bash
rosdep install --from-paths src --ignore-src -r -y
colcon build --symlink-install
source install/setup.bash
```

Запустить сервис:

```bash
sudo systemctl restart robotlidar-web.service
sudo systemctl status robotlidar-web.service
```

# Диагностика

Проверка узлов:

```bash
ros2 node list
```

Проверка топиков:

```bash
ros2 topic list
```

Основные топики:

```text
/scan
/imu/data_raw
/wheel/odom
/odometry/filtered
/cmd_vel
```

Проверка TF:

```bash
ros2 run tf2_ros tf2_echo odom base_link
ros2 run tf2_ros tf2_echo base_link laser
ros2 run tf2_ros tf2_echo base_link imu_link
```

Проверка сервиса:

```bash
systemctl is-active robotlidar-web.service
journalctl -u robotlidar-web.service -n 100 --no-pager
```

Если лидар не открывает порт:

```bash
ls -l /dev/ttyUSB0
groups
sudo usermod -aG dialout "$USER"
sudo reboot
```

Если MPU6050 не найден:

```bash
i2cdetect -y 1
groups
sudo usermod -aG i2c "$USER"
sudo reboot
```

# Безопасность

Программный GPIO-стоп и кнопка «СТОП» в браузере не заменяют аппаратную безопасность. Обязательны:

- физическая аварийная кнопка, снимающая питание приводов;
- силовой контактор безопасности;
- гальваническая развязка GPIO;
- согласование уровней сигналов Холла;
- аппаратный watchdog;
- ручной режим с аппаратным приоритетом;
- испытание сначала с вывешенными гусеницами;
- испытание на закрытой площадке без людей;
- ограничение скорости при первых испытаниях.

Дополнительные сведения о веб-панели находятся в [WEB.md](WEB.md), об офлайн-развёртывании — в [OFFLINE.md](OFFLINE.md).
