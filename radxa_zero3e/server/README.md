# Camera Hub Server

Центральный сервер тракторов Radxa ZERO 3E.

## Архитектура

```text
Radxa / Windows emulator
        |
        | H.264 RTP/UDP (готовый кодированный поток)
        v
+----------------------------+
| Pion WebRTC relay (Go)     |
| без decode / encode        |
+-------------+--------------+
              |
              | WebRTC H.264 / SRTP
              v
           Browser

FastAPI (Python)
  - пользователи / логин / пароль
  - SQLite
  - привязка tractor device_id к пользователю
  - проверка прав доступа
  - список тракторов / телеметрия
  - PTZ / CENTER / IDR
  - WebRTC signaling proxy к Pion
```

Видео больше не декодируется Python-сервером. В `requirements.txt` нет `PyAV` и `aiortc`.

## Папки

```text
radxa_zero3e/server/
├── main.py
├── requirements.txt
├── camera_hub.db              # создаётся автоматически
├── web/
└── webrtc_relay/
    ├── main.go
    ├── go.mod
    └── README.md
```

## 1. Запустить WebRTC relay

Нужен Go 1.23+.

Windows:

```bat
cd radxa_zero3e\server
run_relay.bat
```

или:

```bat
cd radxa_zero3e\server\webrtc_relay
go mod tidy
go run .
```

По умолчанию relay API слушает только `127.0.0.1:8090`.

## 2. Запустить FastAPI

```bat
cd radxa_zero3e\server
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Открыть:

```text
http://127.0.0.1:8000
```

## 3. Запустить эмулятор

```bat
cd radxa_zero3e\emulator_windows
run_gui.bat
```

Например ID:

```text
TRACTOR-WIN-0001
```

При регистрации Camera Hub назначит RTP ingest-порт, например `10000`. Сам UDP порт слушает Pion relay, а не FastAPI.

## Пользовательская модель

1. Пользователь создаёт аккаунт/входит.
2. В `Настройки` вводит постоянный ID трактора.
3. Сервер сохраняет привязку `user -> device_id`.
4. В списке камер пользователь видит только свои ID.
5. WebRTC и PTZ доступны только владельцу привязанного ID.

## Видео

Целевая цепочка:

```text
HBVCAM
  -> Radxa h264_rkmpp Baseline, B=0, GOP=15
  -> RTP/UDP
  -> Pion TrackLocalStaticRTP
  -> WebRTC DTLS/SRTP
  -> browser H.264 decoder
```

H.264 payload не перекодируется и не проходит через Python.

## Сеть

Для одного потока 2 Mbit/s:

- Radxa -> сервер: ~2 Mbit/s;
- сервер -> один браузер: ~2 Mbit/s;
- второй зритель того же трактора добавляет ещё ~2 Mbit/s исходящего трафика.

CPU сервера расходуется главным образом на сеть, RTP, DTLS/SRTP и WebRTC signaling, а не на H.264 codec.

## Интернет

Для локального теста STUN/TURN не нужен. Для публичного сервера понадобятся:

- HTTPS (nginx);
- публичные WebRTC UDP порты;
- STUN/TURN (желательно coturn);
- при необходимости `STUN_URL` для Pion relay.

Пример:

```text
STUN_URL=stun:stun.example.com:3478
```
