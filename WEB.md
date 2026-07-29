# Веб-панель RobotLidar

Веб-приложение работает непосредственно на Raspberry Pi 4 и доступно в
локальной сети по адресу:

```text
http://IP_RASPBERRY_PI:8080
```

Интернет и облачные сервисы не используются.

## Возможности

- ручное управление: вперёд, назад, поворот влево и вправо;
- программный аварийный стоп;
- запуск режима построения карты;
- запуск автономного режима по выбранной карте;
- начало, остановка, очистка и воспроизведение записанного маршрута;
- сохранение карты с заданным именем;
- просмотр списка и изображения карт;
- выбор карты для текущего запуска;
- назначение основной карты;
- автоматический запуск навигации по основной карте после включения Raspberry Pi;
- контроль наличия данных лидара, MPU6050, Холлов и EKF;
- просмотр текущих координат и журнала ROS 2.

## Архитектура

```text
Телефон / ноутбук
        │ локальная Wi-Fi или Ethernet сеть
        ▼
FastAPI :8080 на Raspberry Pi
        ├── /cmd_vel ───────────────► motor_gpio_node
        ├── ROS Trigger services ───► route_recorder / route_player
        ├── запускает mapping.launch.py
        ├── запускает navigation.launch.py
        ├── вызывает map_saver_cli
        └── читает /scan, /imu/data_raw и /odometry/filtered
```

Кнопки движения работают по принципу удержания. Браузер повторяет команду
примерно каждые 180 мс. Если обновления прекращаются, backend перестаёт
публиковать движение через 0,45 с, а `motor_gpio_node` дополнительно применяет
собственный watchdog.

## Сборка

После обновления репозитория:

```bash
cd ~/robotlidar_ws
source /opt/ros/jazzy/setup.bash
rosdep install --from-paths src --ignore-src -r -y
colcon build --symlink-install
source install/setup.bash
```

Для ручного тестового запуска:

```bash
sudo apt install -y python3-fastapi python3-uvicorn python3-pil
ros2 run robotlidar robotlidar_web
```

Открыть на телефоне или ноутбуке:

```text
http://IP_RASPBERRY_PI:8080
```

IP Raspberry Pi можно узнать:

```bash
hostname -I
```

## Установка автозапуска

Сервис устанавливается из исходного репозитория:

```bash
cd ~/robotlidar_ws/src/robotlidar
bash scripts/install_web_service.sh ~/robotlidar_ws
```

Проверка:

```bash
sudo systemctl status robotlidar-web.service
journalctl -u robotlidar-web.service -f
```

Перезапуск:

```bash
sudo systemctl restart robotlidar-web.service
```

Удаление сервиса без удаления карт:

```bash
bash scripts/uninstall_web_service.sh
```

## Автозапуск рабочего режима

Сама панель всегда запускается через systemd. В интерфейсе можно выбрать:

- только веб-панель;
- автоматически запустить картографирование;
- автоматически запустить автономный режим по основной карте.

Настройки сохраняются в:

```text
~/robotlidar_data/config/web_settings.json
```

Карты:

```text
~/robotlidar_data/maps/
```

Маршрут:

```text
~/robotlidar_data/routes/cleaning_route.yaml
```

## Локальная сеть

Варианты подключения:

1. Raspberry Pi и телефон подключены к одному роутеру.
2. Ноутбук подключён к Raspberry Pi напрямую по Ethernet.
3. Raspberry Pi создаёт собственную Wi-Fi-точку доступа без интернета.

В текущей версии панель не содержит авторизации. Используйте отдельную закрытую
локальную сеть и не публикуйте порт 8080 в интернет.

## Безопасность

Кнопка «СТОП» в веб-интерфейсе является программной. Она не заменяет:

- физическую кнопку аварийной остановки;
- силовой контактор безопасности;
- аппаратный watchdog;
- гальваническую развязку выходов Raspberry Pi;
- ручной режим с аппаратным приоритетом.
