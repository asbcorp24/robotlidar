from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import struct
import subprocess
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path
from tkinter import messagebox, ttk

try:
    import websocket
except ImportError:
    websocket = None

BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / "runtime"
MEDIAMTX_EXE = RUNTIME_DIR / "mediamtx.exe"
MEDIAMTX_CFG = BASE_DIR / "mediamtx.yml"
CONTROL_MAGIC = 0x5354
CONTROL_VERSION = 1
TYPE_PTZ = 1
TYPE_DRIVE = 2
TYPE_BRUSH = 3
FLAG_CENTER = 1 << 0
FLAG_REQUEST_IDR = 1 << 1


def find_ffmpeg() -> str | None:
    for candidate in (
        shutil.which("ffmpeg"),
        str(BASE_DIR / "ffmpeg.exe"),
        str(RUNTIME_DIR / "ffmpeg.exe"),
    ):
        if candidate and Path(candidate).exists():
            return candidate
    return None


def preferred_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def download_mediamtx(log) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if MEDIAMTX_EXE.exists():
        return
    log("MediaMTX not found. Downloading latest Windows x64 release...")
    req = urllib.request.Request(
        "https://api.github.com/repos/bluenviron/mediamtx/releases/latest",
        headers={"User-Agent": "RobotLiDAR-RTSP-Emulator"},
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        release = json.loads(response.read().decode("utf-8"))
    asset_url = None
    asset_name = None
    for asset in release.get("assets", []):
        name = str(asset.get("name", ""))
        low = name.lower()
        if "windows_amd64" in low and low.endswith(".zip"):
            asset_url = asset.get("browser_download_url")
            asset_name = name
            break
    if not asset_url:
        raise RuntimeError("Latest MediaMTX release has no Windows amd64 ZIP asset")
    archive = RUNTIME_DIR / (asset_name or "mediamtx.zip")
    urllib.request.urlretrieve(asset_url, archive)
    with zipfile.ZipFile(archive, "r") as zf:
        zf.extractall(RUNTIME_DIR)
    archive.unlink(missing_ok=True)
    if not MEDIAMTX_EXE.exists():
        raise RuntimeError("mediamtx.exe not found after extracting archive")
    log("MediaMTX installed")


class Emulator:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("RobotLiDAR Camera / Tractor Emulator")
        self.root.geometry("980x850")
        self.root.minsize(900, 760)
        self.logs: queue.Queue[str] = queue.Queue()

        self.mediamtx: subprocess.Popen[str] | None = None
        self.rtsp_ffmpeg: subprocess.Popen[str] | None = None
        self.server_ffmpeg: subprocess.Popen[str] | None = None
        self.control_socket: socket.socket | None = None
        self.control_ws = None
        self.control_thread: threading.Thread | None = None
        self.legacy_control_thread: threading.Thread | None = None
        self.telemetry_thread: threading.Thread | None = None
        self.direct_stop = threading.Event()
        self.direct_running = False
        self.video_ingest_port: int | None = None
        self.srt_ingest_port: int | None = None
        self.srt_latency_ms = 200
        self.direct_start_time = 0.0

        self.width = tk.StringVar(value="1280")
        self.height = tk.StringVar(value="720")
        self.fps = tk.StringVar(value="25")
        self.bitrate = tk.StringVar(value="2000")
        self.path = tk.StringVar(value="camera")
        self.advertise_ip = tk.StringVar(value=preferred_ip())
        self.server_url = tk.StringVar(value="https://tele.xn----7sbbd7e6b.xn--p1ai")
        self.device_id = tk.StringVar(value=f"TRACTOR-WIN-{uuid.uuid4().hex[:10].upper()}")
        self.control_port = tk.StringVar(value="6000")

        self.rtsp_status = tk.StringVar(value="Остановлен")
        self.direct_status = tk.StringVar(value="Остановлен")
        self.lan_url = tk.StringVar()
        self.local_url = tk.StringVar()
        self.ptz_state = tk.StringVar(value="PAN 0.0° / TILT 0.0°")
        self.drive_state = tk.StringVar(value="L 0 / R 0")
        self.brush_state = tk.StringVar(value="Spin 0 / Lift 0")
        self.last_command = tk.StringVar(value="—")
        self.video_state = tk.StringVar(value="—")
        self.control_state = tk.StringVar(value="—")

        self.build_ui()
        self.refresh_urls()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.poll_logs)

    def build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="RobotLiDAR — Windows эмулятор", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="RTSP-камера для Raspberry + полный SRT/WSS эмулятор трактора для центрального сервера",
        ).pack(anchor="w", pady=(2, 10))

        video = ttk.LabelFrame(outer, text="Видео H.264", padding=10)
        video.pack(fill="x")
        row = ttk.Frame(video)
        row.pack(fill="x")
        for i, (label, var) in enumerate(
            (("Ширина", self.width), ("Высота", self.height), ("FPS", self.fps), ("kbps", self.bitrate), ("RTSP path", self.path))
        ):
            ttk.Label(row, text=label).grid(row=0, column=i, sticky="w", padx=(0, 7))
            ttk.Entry(row, textvariable=var, width=13).grid(row=1, column=i, sticky="ew", padx=(0, 7))
            row.columnconfigure(i, weight=1)
        self.path.trace_add("write", lambda *_: self.refresh_urls())

        rtsp = ttk.LabelFrame(outer, text="Режим 1 — RTSP IP-камера для Raspberry Pi", padding=10)
        rtsp.pack(fill="x", pady=10)
        ttk.Label(rtsp, text="IP Windows, доступный Raspberry:").grid(row=0, column=0, sticky="w")
        ttk.Entry(rtsp, textvariable=self.advertise_ip, width=20).grid(row=1, column=0, sticky="ew", padx=(0, 8))
        ttk.Label(rtsp, text="RTSP URL для Raspberry:").grid(row=0, column=1, sticky="w")
        ttk.Entry(rtsp, textvariable=self.lan_url, state="readonly", width=45).grid(row=1, column=1, sticky="ew", padx=(0, 8))
        ttk.Button(rtsp, text="Копировать", command=lambda: self.copy(self.lan_url.get())).grid(row=1, column=2)
        rtsp.columnconfigure(1, weight=1)
        buttons = ttk.Frame(rtsp)
        buttons.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(9, 0))
        ttk.Button(buttons, text="▶ Запустить RTSP", command=self.start_rtsp).pack(side="left")
        ttk.Button(buttons, text="■ Остановить RTSP", command=self.stop_rtsp).pack(side="left", padx=7)
        ttk.Label(buttons, textvariable=self.rtsp_status, font=("Segoe UI", 10, "bold")).pack(side="right")
        self.advertise_ip.trace_add("write", lambda *_: self.refresh_urls())

        direct = ttk.LabelFrame(outer, text="Режим 2 — полный эмулятор: SRT видео + WSS управление", padding=10)
        direct.pack(fill="x", pady=(0, 10))
        grid = ttk.Frame(direct)
        grid.pack(fill="x")
        fields = (("Server URL", self.server_url), ("Device ID", self.device_id), ("Legacy UDP port", self.control_port))
        for i, (label, var) in enumerate(fields):
            ttk.Label(grid, text=label).grid(row=0, column=i, sticky="w", padx=(0, 8))
            ttk.Entry(grid, textvariable=var).grid(row=1, column=i, sticky="ew", padx=(0, 8))
            grid.columnconfigure(i, weight=1)
        ttk.Button(grid, text="Новый ID", command=self.generate_id).grid(row=1, column=3)
        actions = ttk.Frame(direct)
        actions.pack(fill="x", pady=(9, 0))
        ttk.Button(actions, text="▶ Подключить эмулятор к серверу", command=self.start_direct).pack(side="left")
        ttk.Button(actions, text="■ Отключить", command=self.stop_direct).pack(side="left", padx=7)
        ttk.Label(actions, textvariable=self.direct_status, font=("Segoe UI", 10, "bold")).pack(side="right")

        states = ttk.LabelFrame(outer, text="Текущее состояние полного эмулятора", padding=10)
        states.pack(fill="x", pady=(0, 10))
        items = (
            ("Видео", self.video_state),
            ("Управление", self.control_state),
            ("Камера PTZ", self.ptz_state),
            ("Гусеницы", self.drive_state),
            ("Щётка", self.brush_state),
            ("Последняя команда", self.last_command),
        )
        for i, (title, var) in enumerate(items):
            box = ttk.Frame(states)
            box.grid(row=0, column=i, sticky="nsew", padx=5)
            ttk.Label(box, text=title).pack(anchor="w")
            ttk.Label(box, textvariable=var, font=("Segoe UI", 9, "bold")).pack(anchor="w")
            states.columnconfigure(i, weight=1)

        ttk.Label(
            outer,
            text=(
                "Режим 1: разрешите TCP/8554 в Windows Firewall, если Raspberry подключается к RTSP. "
                "Режим 2: входящие порты не нужны — SRT и WSS открываются с Windows наружу. "
                "UDP/6000 оставлен только как legacy fallback."
            ),
            wraplength=920,
        ).pack(anchor="w", pady=(0, 8))

        log_frame = ttk.LabelFrame(outer, text="Журнал", padding=8)
        log_frame.pack(fill="both", expand=True)
        self.log_box = tk.Text(log_frame, height=15, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True)

    def refresh_urls(self) -> None:
        path = self.path.get().strip().strip("/") or "camera"
        ip = self.advertise_ip.get().strip() or preferred_ip()
        self.lan_url.set(f"rtsp://{ip}:8554/{path}")
        self.local_url.set(f"rtsp://127.0.0.1:8554/{path}")

    def video_params(self) -> tuple[int, int, int, int]:
        width, height, fps, bitrate = map(int, (self.width.get(), self.height.get(), self.fps.get(), self.bitrate.get()))
        if width < 320 or height < 240 or not 1 <= fps <= 60 or bitrate < 100:
            raise ValueError("Некорректные параметры видео")
        return width, height, fps, bitrate

    def ffmpeg_testsrc_args(self) -> list[str]:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("ffmpeg.exe не найден. Добавьте FFmpeg в PATH или положите ffmpeg.exe рядом с emulator.py")
        width, height, fps, bitrate = self.video_params()
        return [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-re",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={width}x{height}:rate={fps}",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-profile:v",
            "baseline",
            "-pix_fmt",
            "yuv420p",
            "-b:v",
            f"{bitrate}k",
            "-maxrate",
            f"{bitrate}k",
            "-bufsize",
            f"{bitrate * 2}k",
            "-g",
            str(fps),
            "-keyint_min",
            str(fps),
            "-bf",
            "0",
            "-sc_threshold",
            "0",
            "-bsf:v",
            "dump_extra=freq=keyframe",
        ]

    def start_rtsp(self) -> None:
        if self.rtsp_ffmpeg and self.rtsp_ffmpeg.poll() is None:
            return
        try:
            download_mediamtx(self.log)
            if not self.mediamtx or self.mediamtx.poll() is not None:
                self.mediamtx = subprocess.Popen(
                    [str(MEDIAMTX_EXE), str(MEDIAMTX_CFG)],
                    cwd=str(BASE_DIR),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                threading.Thread(target=self.pipe_reader, args=("MediaMTX", self.mediamtx), daemon=True).start()
                time.sleep(0.7)
                if self.mediamtx.poll() is not None:
                    raise RuntimeError("MediaMTX сразу завершился; возможно занят TCP/8554")
            args = self.ffmpeg_testsrc_args() + ["-f", "rtsp", "-rtsp_transport", "tcp", self.local_url.get()]
            self.rtsp_ffmpeg = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            threading.Thread(target=self.pipe_reader, args=("RTSP FFmpeg", self.rtsp_ffmpeg), daemon=True).start()
            time.sleep(0.6)
            if self.rtsp_ffmpeg.poll() is not None:
                raise RuntimeError("FFmpeg RTSP publisher завершился")
            self.rtsp_status.set("RTSP активен")
            self.log(f"RTSP READY: {self.lan_url.get()}")
        except Exception as exc:
            self.stop_rtsp()
            messagebox.showerror("RTSP", str(exc))
            self.log(f"RTSP ERROR: {exc}")

    def stop_rtsp(self) -> None:
        self.terminate(self.rtsp_ffmpeg)
        self.rtsp_ffmpeg = None
        self.terminate(self.mediamtx)
        self.mediamtx = None
        self.rtsp_status.set("Остановлен")

    def start_direct(self) -> None:
        if self.direct_running:
            return
        try:
            if websocket is None:
                raise RuntimeError("Не установлен websocket-client. Запустите эмулятор через run.bat")
            server = self.server_url.get().strip().rstrip("/")
            device = self.device_id.get().strip().upper()
            ip = self.advertise_ip.get().strip() or preferred_ip()
            legacy_port = int(self.control_port.get())
            if not server.startswith(("http://", "https://")):
                raise ValueError("Server URL должен начинаться с http:// или https://")
            if not device or not 1 <= legacy_port <= 65535:
                raise ValueError("Заполните Device ID и Legacy UDP port")

            payload = {
                "name": f"Windows RobotLiDAR Emulator ({device})",
                "ip": ip,
                "rtp_port": 5004,
                "ptz_port": legacy_port,
                "device_type": "windows_robotlidar_emulator",
                "video_transport": "srt",
            }
            data = self.json_request(
                "POST",
                f"{server}/api/devices/{urllib.parse.quote(device, safe='')}/register",
                payload,
            )
            self.video_ingest_port = int(data.get("video_ingest_port") or 0)
            self.srt_ingest_port = int(data.get("srt_ingest_port") or 0)
            self.srt_latency_ms = max(80, min(2000, int(data.get("srt_latency_ms") or 200)))
            if not 1 <= self.srt_ingest_port <= 65535:
                raise RuntimeError("Сервер не вернул srt_ingest_port")

            host = urllib.parse.urlparse(server).hostname
            if not host:
                raise RuntimeError("Не удалось определить host сервера")
            latency_us = self.srt_latency_ms * 1000
            srt_target = (
                f"srt://{host}:{self.srt_ingest_port}?mode=caller&transtype=live&"
                f"latency={latency_us}&pkt_size=1316"
            )
            args = self.ffmpeg_testsrc_args() + [
                "-mpegts_flags",
                "+resend_headers",
                "-muxdelay",
                "0",
                "-f",
                "mpegts",
                srt_target,
            ]
            self.server_ffmpeg = subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            threading.Thread(target=self.pipe_reader, args=("SRT FFmpeg", self.server_ffmpeg), daemon=True).start()
            time.sleep(0.6)
            if self.server_ffmpeg.poll() is not None:
                raise RuntimeError("FFmpeg SRT publisher завершился. Проверьте, что FFmpeg собран с libsrt")

            # Legacy local UDP receiver remains available for compatibility only.
            self.control_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.control_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.control_socket.bind(("0.0.0.0", legacy_port))
            self.control_socket.settimeout(0.5)

            self.direct_stop.clear()
            self.direct_running = True
            self.direct_start_time = time.monotonic()
            self.control_thread = threading.Thread(target=self.websocket_control_loop, name="control-wss", daemon=True)
            self.legacy_control_thread = threading.Thread(target=self.legacy_control_loop, name="control-udp", daemon=True)
            self.telemetry_thread = threading.Thread(target=self.telemetry_loop, name="telemetry", daemon=True)
            self.control_thread.start()
            self.legacy_control_thread.start()
            self.telemetry_thread.start()

            self.video_state.set(f"SRT UDP/{self.srt_ingest_port}")
            self.control_state.set("WSS подключение...")
            self.direct_status.set("SRT активен / WSS подключается")
            self.log(
                f"DEVICE READY: {device}; SRT/{self.srt_ingest_port} latency={self.srt_latency_ms}ms; "
                f"legacy UDP/{legacy_port}"
            )
        except Exception as exc:
            self.stop_direct()
            messagebox.showerror("Полный эмулятор", str(exc))
            self.log(f"DIRECT ERROR: {exc}")

    def stop_direct(self) -> None:
        self.direct_stop.set()
        self.direct_running = False
        ws = self.control_ws
        self.control_ws = None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
        if self.control_socket:
            try:
                self.control_socket.close()
            except OSError:
                pass
        self.control_socket = None
        self.terminate(self.server_ffmpeg)
        self.server_ffmpeg = None
        self.srt_ingest_port = None
        self.video_ingest_port = None
        self.direct_status.set("Остановлен")
        self.video_state.set("—")
        self.control_state.set("—")

    def websocket_control_loop(self) -> None:
        while not self.direct_stop.is_set():
            server = self.server_url.get().strip().rstrip("/")
            device = self.device_id.get().strip().upper()
            try:
                ws_url = self.control_ws_url(server, device)
                self.root.after(0, self.control_state.set, "WSS подключение...")
                ws = websocket.create_connection(ws_url, timeout=8, enable_multithread=True)
                ws.settimeout(10)
                self.control_ws = ws
                self.root.after(0, self.control_state.set, "WSS подключён")
                self.root.after(0, self.direct_status.set, "Подключён: SRT + WSS")
                self.log(f"CONTROL/WSS connected: {ws_url}")
                while not self.direct_stop.is_set():
                    try:
                        message = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        ws.ping("robotlidar-emulator")
                        continue
                    if message is None:
                        raise RuntimeError("WebSocket closed by server")
                    if isinstance(message, str):
                        continue
                    self.handle_control_packet(bytes(message), "WSS")
            except Exception as exc:
                if not self.direct_stop.is_set():
                    self.root.after(0, self.control_state.set, "WSS нет — retry")
                    self.log(f"CONTROL/WSS disconnected: {exc}")
            finally:
                ws = self.control_ws
                self.control_ws = None
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            if not self.direct_stop.wait(1.0):
                continue

    @staticmethod
    def control_ws_url(server: str, device: str) -> str:
        parsed = urllib.parse.urlparse(server)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Некорректный Server URL")
        scheme = "wss" if parsed.scheme == "https" else "ws"
        base = parsed.path.rstrip("/")
        path = f"{base}/api/devices/{urllib.parse.quote(device, safe='')}/control-ws"
        return urllib.parse.urlunparse((scheme, parsed.netloc, path, "", "", ""))

    def legacy_control_loop(self) -> None:
        while not self.direct_stop.is_set():
            sock = self.control_socket
            if not sock:
                return
            try:
                data, addr = sock.recvfrom(256)
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                self.handle_control_packet(data, f"UDP {addr[0]}")
            except Exception as exc:
                self.log(f"CONTROL/UDP ERROR: {exc}")

    def handle_control_packet(self, data: bytes, transport: str) -> None:
        if len(data) != 16:
            raise ValueError(f"packet size {len(data)}, expected 16")
        magic, version, kind, seq, v1, v2, speed, flags = struct.unpack(">HBBIhhHH", data)
        if magic != CONTROL_MAGIC or version != CONTROL_VERSION:
            raise ValueError("bad control header")
        now = time.strftime("%H:%M:%S")
        if kind == TYPE_PTZ:
            if flags & FLAG_CENTER:
                v1 = 0
                v2 = 0
            self.root.after(0, self.ptz_state.set, f"PAN {v1 / 100:.1f}° / TILT {v2 / 100:.1f}°")
            text = f"PTZ pan={v1} tilt={v2} speed={speed} flags=0x{flags:04x}"
            if flags & FLAG_REQUEST_IDR:
                text += " REQUEST_IDR"
        elif kind == TYPE_DRIVE:
            v1 = max(-1000, min(1000, int(v1)))
            v2 = max(-1000, min(1000, int(v2)))
            self.root.after(0, self.drive_state.set, f"L {v1} / R {v2}")
            text = f"DRIVE L={v1} R={v2}"
        elif kind == TYPE_BRUSH:
            v1 = max(-1000, min(1000, int(v1)))
            v2 = max(-1000, min(1000, int(v2)))
            self.root.after(0, self.brush_state.set, f"Spin {v1} / Lift {v2}")
            text = f"BRUSH spin={v1} lift={v2}"
        else:
            text = f"UNKNOWN type={kind}"
        self.root.after(0, self.last_command.set, f"{now} #{seq} {text}")
        self.log(f"CONTROL/{transport}: seq={seq} {text}")

    def telemetry_loop(self) -> None:
        while not self.direct_stop.wait(1.0):
            server = self.server_url.get().strip().rstrip("/")
            device = self.device_id.get().strip().upper()
            fps = int(self.fps.get()) if self.server_ffmpeg and self.server_ffmpeg.poll() is None else 0
            bitrate = int(self.bitrate.get()) * 1000 if fps else 0
            payload = {
                "fps": fps,
                "bitrate_bps": bitrate,
                "dropped_frames": 0,
                "uptime_ms": int((time.monotonic() - self.direct_start_time) * 1000),
                "link_mbps": 1000,
            }
            try:
                self.json_request(
                    "POST",
                    f"{server}/api/devices/{urllib.parse.quote(device, safe='')}/telemetry",
                    payload,
                )
            except Exception as exc:
                self.log(f"TELEMETRY: {exc}")

    @staticmethod
    def json_request(method: str, url: str, payload: dict) -> dict:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            method=method,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=4) as response:
            raw = response.read()
        return json.loads(raw.decode("utf-8")) if raw else {}

    def generate_id(self) -> None:
        self.device_id.set(f"TRACTOR-WIN-{uuid.uuid4().hex[:10].upper()}")

    def pipe_reader(self, name: str, proc: subprocess.Popen[str]) -> None:
        if not proc.stdout:
            return
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                self.log(f"{name}: {line}")
        if self.direct_running and proc is self.server_ffmpeg and not self.direct_stop.is_set():
            self.root.after(0, self.video_state.set, f"SRT остановлен ({proc.returncode})")
            self.log(f"SRT FFmpeg exited with code {proc.returncode}")

    def terminate(self, proc: subprocess.Popen[str] | None) -> None:
        if not proc or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def copy(self, value: str) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.log(f"Copied: {value}")

    def log(self, text: str) -> None:
        self.logs.put(f"[{time.strftime('%H:%M:%S')}] {text}")

    def poll_logs(self) -> None:
        while True:
            try:
                line = self.logs.get_nowait()
            except queue.Empty:
                break
            self.log_box.configure(state="normal")
            self.log_box.insert("end", line + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.root.after(100, self.poll_logs)

    def on_close(self) -> None:
        self.stop_direct()
        self.stop_rtsp()
        self.root.destroy()


def main() -> int:
    if os.name != "nt":
        print("This emulator is intended for Windows.")
        return 2
    root = tk.Tk()
    Emulator(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
