# RobotLiDAR web dashboard

Web-интерфейс встроен непосредственно в единый Go Camera Hub через `go:embed`.

Отдельно запускать статический HTTP-сервер не нужно.

## Возможности

- вход и регистрация пользователя;
- список только своих тракторов;
- добавление/удаление тракторов по постоянному `device_id`;
- online/offline;
- просмотр H.264 через WebRTC/Pion без серверного перекодирования;
- PAN/TILT;
- CENTER;
- управление стрелками/WASD;
- IDR request;
- FPS, bitrate, Ethernet, uptime.

## Запуск

```bat
cd radxa_zero3e\server
run_server.bat
```

Открыть:

```text
http://127.0.0.1:8000
```

HTML/CSS/JS компилируются внутрь `robotlidar-server` и доступны через тот же HTTP-порт, что и REST API.
