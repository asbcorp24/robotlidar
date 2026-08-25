from __future__ import annotations

import json
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import zipfile
from pathlib import Path
from tkinter import messagebox, ttk

BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / "runtime"
MEDIAMTX_EXE = RUNTIME_DIR / "mediamtx.exe"
MEDIAMTX_CFG = BASE_DIR / "mediamtx.yml"


def find_ffmpeg() -> str | None:
    candidates = [
        shutil.which("ffmpeg"),
        str(BASE_DIR / "ffmpeg.exe"),
        str(RUNTIME_DIR / "ffmpeg.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def download_mediamtx(log) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    if MEDIAMTX_EXE.exists():
        return

    log("MediaMTX not found. Downloading latest Windows x64 release...")
    request = urllib.request.Request(
        "https://api.github.com/repos/bluenviron/mediamtx/releases/latest",
        headers={"User-Agent": "RobotLiDAR-RTSP-Emulator"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        release = json.loads(response.read().decode("utf-8"))

    asset_url = None
    asset_name = None
    for asset in release.get("assets", []):
        name = str(asset.get("name", ""))
        lower = name.lower()
        if "windows_amd64" in lower and lower.endswith(".zip"):
            asset_url = asset.get("browser_download_url")
            asset_name = name
            break
    if not asset_url:
        raise RuntimeError("Latest MediaMTX release has no Windows amd64 ZIP asset")

    archive = RUNTIME_DIR / (asset_name or "mediamtx.zip")
    log(f"Downloading {asset_name}...")
    urllib.request.urlretrieve(asset_url, archive)
    with zipfile.ZipFile(archive, "r") as zf:
        zf.extractall(RUNTIME_DIR)
    archive.unlink(missing_ok=True)
    if not MEDIAMTX_EXE.exists():
        raise RuntimeError("mediamtx.exe was not found after extracting archive")
    log("MediaMTX installed into runtime\\mediamtx.exe")


class Emulator:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("RobotLiDAR RTSP Camera Emulator")
        self.root.geometry("780x650")
        self.root.minsize(720, 580)

        self.logs: queue.Queue[str] = queue.Queue()
        self.mediamtx: subprocess.Popen[str] | None = None
        self.ffmpeg: subprocess.Popen[str] | None = None
        self.reader_threads: list[threading.Thread] = []
        self.running = False

        self.width = tk.StringVar(value="1280")
        self.height = tk.StringVar(value="720")
        self.fps = tk.StringVar(value="25")
        self.bitrate = tk.StringVar(value="2000")
        self.path = tk.StringVar(value="camera")
        self.status = tk.StringVar(value="Остановлено")
        self.lan_url = tk.StringVar()
        self.local_url = tk.StringVar()

        self.build_ui()
        self.refresh_urls()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(100, self.poll_logs)

    def build_ui(self) -> None:
        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="RobotLiDAR — эмулятор RTSP IP-камеры", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(frame, text="Windows → RTSP/H.264 → Raspberry Pi", font=("Segoe UI", 10)).pack(anchor="w", pady=(2, 14))

        settings = ttk.LabelFrame(frame, text="Параметры потока", padding=12)
        settings.pack(fill="x")
        grid = ttk.Frame(settings)
        grid.pack(fill="x")
        fields = [
            ("Ширина", self.width),
            ("Высота", self.height),
            ("FPS", self.fps),
            ("Битрейт, kbps", self.bitrate),
            ("RTSP path", self.path),
        ]
        for i, (label, var) in enumerate(fields):
            ttk.Label(grid, text=label).grid(row=0, column=i, sticky="w", padx=(0, 8))
            ttk.Entry(grid, textvariable=var, width=13).grid(row=1, column=i, sticky="ew", padx=(0, 8))
            grid.columnconfigure(i, weight=1)
        for var in (self.path,):
            var.trace_add("write", lambda *_: self.refresh_urls())

        urls = ttk.LabelFrame(frame, text="RTSP адреса", padding=12)
        urls.pack(fill="x", pady=12)
        ttk.Label(urls, text="Для Raspberry Pi:").grid(row=0, column=0, sticky="w")
        lan_entry = ttk.Entry(urls, textvariable=self.lan_url, state="readonly")
        lan_entry.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(urls, text="Копировать", command=lambda: self.copy(self.lan_url.get())).grid(row=1, column=1)
        ttk.Label(urls, text="Для проверки на этом Windows ПК:").grid(row=2, column=0, sticky="w", pady=(10, 0))
        local_entry = ttk.Entry(urls, textvariable=self.local_url, state="readonly")
        local_entry.grid(row=3, column=0, sticky="ew", padx=(0, 8))
        ttk.Button(urls, text="Копировать", command=lambda: self.copy(self.local_url.get())).grid(row=3, column=1)
        urls.columnconfigure(0, weight=1)

        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(0, 12))
        self.start_btn = ttk.Button(actions, text="▶ Запустить RTSP камеру", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="■ Остановить", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=8)
        ttk.Button(actions, text="Проверить FFmpeg", command=self.check_ffmpeg).pack(side="left")
        ttk.Label(actions, textvariable=self.status, font=("Segoe UI", 10, "bold")).pack(side="right")

        note = ttk.Label(
            frame,
            text="Поток: H.264 Baseline, yuv420p, B-frames=0, GOP≈1 сек. Windows Firewall должен разрешать TCP/8554.",
            wraplength=730,
        )
        note.pack(anchor="w", pady=(0, 8))

        log_frame = ttk.LabelFrame(frame, text="Журнал", padding=8)
        log_frame.pack(fill="both", expand=True)
        self.log_box = tk.Text(log_frame, height=16, wrap="word", state="disabled", font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True)

    def refresh_urls(self) -> None:
        path = self.path.get().strip().strip("/") or "camera"
        self.lan_url.set(f"rtsp://{local_ip()}:8554/{path}")
        self.local_url.set(f"rtsp://127.0.0.1:8554/{path}")

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

    def pipe_reader(self, name: str, proc: subprocess.Popen[str]) -> None:
        if not proc.stdout:
            return
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                self.log(f"{name}: {line}")

    def validate(self) -> tuple[int, int, int, int, str]:
        try:
            width = int(self.width.get())
            height = int(self.height.get())
            fps = int(self.fps.get())
            bitrate = int(self.bitrate.get())
        except ValueError as exc:
            raise ValueError("Resolution, FPS and bitrate must be integers") from exc
        if width < 320 or height < 240 or fps < 1 or fps > 60 or bitrate < 100:
            raise ValueError("Invalid stream parameters")
        path = self.path.get().strip().strip("/")
        if not path or any(ch in path for ch in " ?#\\"):
            raise ValueError("RTSP path may not contain spaces, \\, ?, or #")
        return width, height, fps, bitrate, path

    def check_ffmpeg(self) -> None:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            messagebox.showerror("FFmpeg", "ffmpeg.exe not found. Install FFmpeg and restart the program.")
            return
        try:
            out = subprocess.check_output([ffmpeg, "-version"], text=True, stderr=subprocess.STDOUT, timeout=5)
            self.log(out.splitlines()[0])
            messagebox.showinfo("FFmpeg", out.splitlines()[0])
        except Exception as exc:
            messagebox.showerror("FFmpeg", str(exc))

    def start(self) -> None:
        if self.running:
            return
        try:
            width, height, fps, bitrate, path = self.validate()
            ffmpeg = find_ffmpeg()
            if not ffmpeg:
                raise RuntimeError("ffmpeg.exe not found in PATH. Install FFmpeg first.")
            download_mediamtx(self.log)

            self.log("Starting MediaMTX RTSP server on TCP/8554...")
            self.mediamtx = subprocess.Popen(
                [str(MEDIAMTX_EXE), str(MEDIAMTX_CFG)],
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            threading.Thread(target=self.pipe_reader, args=("MediaMTX", self.mediamtx), daemon=True).start()
            time.sleep(0.8)
            if self.mediamtx.poll() is not None:
                raise RuntimeError("MediaMTX exited immediately. Port 8554 may already be in use.")

            publish_url = f"rtsp://127.0.0.1:8554/{path}"
            gop = max(1, fps)
            args = [
                ffmpeg,
                "-hide_banner", "-loglevel", "warning",
                "-re",
                "-f", "lavfi",
                "-i", f"testsrc2=size={width}x{height}:rate={fps}",
                "-an",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "zerolatency",
                "-profile:v", "baseline",
                "-pix_fmt", "yuv420p",
                "-b:v", f"{bitrate}k",
                "-maxrate", f"{bitrate}k",
                "-bufsize", f"{bitrate * 2}k",
                "-g", str(gop),
                "-keyint_min", str(gop),
                "-bf", "0",
                "-sc_threshold", "0",
                "-f", "rtsp",
                "-rtsp_transport", "tcp",
                publish_url,
            ]
            self.log("Starting H.264 test publisher...")
            self.ffmpeg = subprocess.Popen(
                args,
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            threading.Thread(target=self.pipe_reader, args=("FFmpeg", self.ffmpeg), daemon=True).start()
            time.sleep(0.8)
            if self.ffmpeg.poll() is not None:
                raise RuntimeError("FFmpeg publisher exited immediately")

            self.running = True
            self.refresh_urls()
            self.status.set("RTSP активен")
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
            self.log(f"READY: {self.lan_url.get()}")
            self.root.after(1000, self.health_check)
        except Exception as exc:
            self.stop()
            self.log(f"ERROR: {exc}")
            messagebox.showerror("RTSP emulator", str(exc))

    def health_check(self) -> None:
        if not self.running:
            return
        if self.mediamtx and self.mediamtx.poll() is not None:
            self.log("MediaMTX stopped unexpectedly")
            self.stop()
            return
        if self.ffmpeg and self.ffmpeg.poll() is not None:
            self.log("FFmpeg stopped unexpectedly")
            self.stop()
            return
        self.root.after(1000, self.health_check)

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

    def stop(self) -> None:
        was_running = self.running or self.ffmpeg is not None or self.mediamtx is not None
        self.running = False
        self.terminate(self.ffmpeg)
        self.terminate(self.mediamtx)
        self.ffmpeg = None
        self.mediamtx = None
        self.status.set("Остановлено")
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        if was_running:
            self.log("RTSP emulator stopped")

    def on_close(self) -> None:
        self.stop()
        self.root.destroy()


def main() -> int:
    if os.name != "nt":
        print("This emulator project is intended for Windows.")
        return 2
    root = tk.Tk()
    Emulator(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
