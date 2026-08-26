from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
EMULATOR_PATH = BASE_DIR / "emulator.py"

CAM_RE = re.compile(r'\[dshow @ .*?\]\s+"([^"]+)"\s+\(video\)')
SRT_RE = re.compile(r"assigned SRT ingest UDP\s+(\d+)")
RTP_RE = re.compile(r"assigned RTP ingest UDP\s+(\d+)")
PTZ_RE = re.compile(r"pan=([-\d.]+) tilt=([-\d.]+)")
DRIVE_RE = re.compile(r"left=([-\d.]+)% right=([-\d.]+)%")
BRUSH_RE = re.compile(r"spin=([-\d.]+)% lift=([A-Z]+)")


class EmulatorGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("RobotLiDAR — Windows эмулятор трактора")
        self.geometry("1040x780")
        self.minsize(940, 690)

        self.proc: subprocess.Popen | None = None
        self.log_queue: queue.Queue[str] = queue.Queue()

        self.status_var = tk.StringVar(value="Остановлен")
        self.video_var = tk.StringVar(value="—")
        self.control_var = tk.StringVar(value="—")
        self.ptz_var = tk.StringVar(value="PAN 0.00° / TILT 0.00°")
        self.drive_var = tk.StringVar(value="L 0.0% / R 0.0%")
        self.brush_var = tk.StringVar(value="0.0% / STOP")

        self.device_id = tk.StringVar(value="TRACTOR-WIN-0001")
        self.device_name = tk.StringVar(value="Windows Tractor Emulator")
        self.server_http = tk.StringVar(value="https://tele.xn----7sbbd7e6b.xn--p1ai")
        self.ptz_port = tk.StringVar(value="6000")
        self.srt_latency = tk.StringVar(value="200")
        self.camera_name = tk.StringVar(value="")
        self.ffmpeg_path = tk.StringVar(value="ffmpeg.exe")
        self.width = tk.StringVar(value="1280")
        self.height = tk.StringVar(value="720")
        self.fps = tk.StringVar(value="30")
        self.bitrate = tk.StringVar(value="2000")
        self.gop = tk.StringVar(value="15")
        self.udp_fallback = tk.BooleanVar(value=True)

        self._build_ui()
        self._load_existing_config()
        self.after(100, self._drain_logs)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=12)
        root.pack(fill="both", expand=True)

        header = ttk.Frame(root)
        header.pack(fill="x")
        ttk.Label(header, text="RobotLiDAR — эмулятор трактора", font=("Segoe UI", 18, "bold")).pack(side="left")
        ttk.Label(header, textvariable=self.status_var).pack(side="right")

        ttk.Separator(root).pack(fill="x", pady=10)
        cfg = ttk.LabelFrame(root, text="Сервер и устройство", padding=10)
        cfg.pack(fill="x")

        fields = [
            ("Device ID", self.device_id),
            ("Имя устройства", self.device_name),
            ("Центральный сервер", self.server_http),
            ("Legacy UDP порт", self.ptz_port),
            ("SRT latency, мс", self.srt_latency),
        ]
        for i, (label, var) in enumerate(fields):
            row, col = divmod(i, 2)
            ttk.Label(cfg, text=label).grid(row=row, column=col * 2, sticky="w", padx=(0, 6), pady=4)
            ttk.Entry(cfg, textvariable=var, width=38).grid(row=row, column=col * 2 + 1, sticky="ew", padx=(0, 14), pady=4)
        cfg.columnconfigure(1, weight=1)
        cfg.columnconfigure(3, weight=1)
        ttk.Checkbutton(cfg, text="Оставить UDP :6000 как legacy fallback", variable=self.udp_fallback).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        ttk.Label(cfg, text="Основной режим: видео SRT caller, управление WSS client. NAT/проброс портов не нужен.", foreground="#666").grid(row=4, column=0, columnspan=4, sticky="w", pady=(6, 0))

        video = ttk.LabelFrame(root, text="Веб-камера", padding=10)
        video.pack(fill="x", pady=(10, 0))
        ttk.Label(video, text="Камера").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=4)
        self.camera_combo = ttk.Combobox(video, textvariable=self.camera_name, state="normal", width=52)
        self.camera_combo.grid(row=0, column=1, columnspan=3, sticky="ew", pady=4)
        ttk.Button(video, text="Найти камеры", command=self._find_cameras).grid(row=0, column=4, padx=(8, 0), pady=4)
        ttk.Label(video, text="FFmpeg").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=4)
        ttk.Entry(video, textvariable=self.ffmpeg_path).grid(row=1, column=1, columnspan=3, sticky="ew", pady=4)
        ttk.Button(video, text="Обзор...", command=self._browse_ffmpeg).grid(row=1, column=4, padx=(8, 0), pady=4)

        mini = [("Ширина", self.width), ("Высота", self.height), ("FPS", self.fps), ("Bitrate kbps", self.bitrate), ("GOP", self.gop)]
        for i, (label, var) in enumerate(mini):
            ttk.Label(video, text=label).grid(row=2, column=i, sticky="w", padx=(0, 6), pady=(8, 2))
            ttk.Entry(video, textvariable=var, width=12).grid(row=3, column=i, sticky="ew", padx=(0, 8), pady=(0, 4))
        video.columnconfigure(1, weight=1)
        video.columnconfigure(2, weight=1)
        video.columnconfigure(3, weight=1)

        state = ttk.LabelFrame(root, text="Текущее состояние", padding=10)
        state.pack(fill="x", pady=(10, 0))
        labels = [
            ("Видео", self.video_var),
            ("Управление", self.control_var),
            ("PTZ", self.ptz_var),
            ("Гусеницы", self.drive_var),
            ("Щётка", self.brush_var),
        ]
        for i, (label, var) in enumerate(labels):
            row, col = divmod(i, 2)
            ttk.Label(state, text=f"{label}:").grid(row=row, column=col * 2, sticky="w", pady=3)
            ttk.Label(state, textvariable=var, font=("Consolas", 10, "bold")).grid(row=row, column=col * 2 + 1, sticky="w", padx=(8, 30), pady=3)

        actions = ttk.Frame(root)
        actions.pack(fill="x", pady=10)
        self.start_btn = ttk.Button(actions, text="▶ Старт", command=self._start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(actions, text="■ Стоп", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Сохранить настройки", command=self._save_config).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Очистить лог", command=self._clear_log).pack(side="right")

        log_box = ttk.LabelFrame(root, text="Лог", padding=6)
        log_box.pack(fill="both", expand=True)
        self.log = tk.Text(log_box, wrap="none", height=20, font=("Consolas", 9))
        yscroll = ttk.Scrollbar(log_box, orient="vertical", command=self.log.yview)
        xscroll = ttk.Scrollbar(log_box, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        log_box.rowconfigure(0, weight=1)
        log_box.columnconfigure(0, weight=1)

    def _load_existing_config(self) -> None:
        if not CONFIG_PATH.exists():
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        mapping = {
            "device_id": self.device_id,
            "device_name": self.device_name,
            "server_http": self.server_http,
            "ptz_listen_port": self.ptz_port,
            "srt_latency_ms": self.srt_latency,
            "camera_name": self.camera_name,
            "ffmpeg": self.ffmpeg_path,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "bitrate_kbps": self.bitrate,
            "gop": self.gop,
        }
        for key, var in mapping.items():
            if key in data:
                var.set(str(data[key]))
        if "control_udp_fallback" in data:
            self.udp_fallback.set(bool(data["control_udp_fallback"]))

    def _config_dict(self) -> dict:
        return {
            "device_id": self.device_id.get().strip(),
            "device_name": self.device_name.get().strip(),
            "server_http": self.server_http.get().strip().rstrip("/"),
            "server_rtp_host": "",
            "server_rtp_port": 5004,
            "ptz_listen_port": int(self.ptz_port.get()),
            "video_transport": "srt",
            "srt_latency_ms": int(self.srt_latency.get()),
            "control_websocket": True,
            "control_udp_fallback": bool(self.udp_fallback.get()),
            "camera_name": self.camera_name.get().strip(),
            "ffmpeg": self.ffmpeg_path.get().strip(),
            "width": int(self.width.get()),
            "height": int(self.height.get()),
            "fps": int(self.fps.get()),
            "bitrate_kbps": int(self.bitrate.get()),
            "gop": int(self.gop.get()),
            "preset": "ultrafast",
            "telemetry_period_sec": 1.0,
        }

    def _save_config(self) -> bool:
        try:
            cfg = self._config_dict()
            if not cfg["device_id"] or not cfg["camera_name"]:
                raise ValueError("Укажи Device ID и камеру")
            if not cfg["server_http"].startswith(("http://", "https://")):
                raise ValueError("Адрес сервера должен начинаться с http:// или https://")
            CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
            self._append_log(f"[GUI] Настройки сохранены: {CONFIG_PATH}\n")
            return True
        except Exception as exc:
            messagebox.showerror("Ошибка", str(exc))
            return False

    def _browse_ffmpeg(self) -> None:
        p = filedialog.askopenfilename(title="Выбери ffmpeg.exe", filetypes=[("FFmpeg", "ffmpeg.exe"), ("EXE", "*.exe"), ("Все файлы", "*.*")])
        if p:
            self.ffmpeg_path.set(p)

    def _find_cameras(self) -> None:
        ffmpeg = self.ffmpeg_path.get().strip() or "ffmpeg.exe"
        self._append_log("[GUI] Поиск DirectShow камер...\n")

        def worker():
            try:
                cp = subprocess.run(
                    [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    timeout=15,
                )
                text = cp.stdout or ""
                cameras = list(dict.fromkeys(CAM_RE.findall(text)))
                self.after(0, lambda: self._set_cameras(cameras, text))
            except Exception as exc:
                self.after(0, lambda: messagebox.showerror("FFmpeg", f"Не удалось получить список камер:\n{exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _set_cameras(self, cameras, raw):
        self.camera_combo["values"] = cameras
        if cameras and not self.camera_name.get().strip():
            self.camera_name.set(cameras[0])
        self._append_log(raw + "\n")
        self._append_log(f"[GUI] Найдено камер: {len(cameras)}\n")

    def _start(self):
        if self.proc and self.proc.poll() is None:
            return
        if not self._save_config():
            return
        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-u", str(EMULATOR_PATH), "--config", str(CONFIG_PATH)],
                cwd=str(BASE_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
                bufsize=1,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except Exception as exc:
            messagebox.showerror("Запуск", str(exc))
            return
        self.status_var.set("Запущен")
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.video_var.set("ожидание регистрации...")
        self.control_var.set("WSS: подключение...")
        self._append_log("[GUI] Эмулятор запущен\n")
        threading.Thread(target=self._read_process_output, daemon=True).start()

    def _read_process_output(self):
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self.log_queue.put(line)
        code = self.proc.wait()
        self.log_queue.put(f"[GUI] Процесс завершён, код={code}\n")
        self.after(0, self._process_stopped)

    def _stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._process_stopped()

    def _process_stopped(self):
        self.status_var.set("Остановлен")
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.control_var.set("—")
        self.proc = None

    def _drain_logs(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self._append_log(line)
                m = SRT_RE.search(line)
                if m:
                    self.video_var.set(f"SRT UDP {m.group(1)}")
                m = RTP_RE.search(line)
                if m:
                    self.video_var.set(f"RTP fallback UDP {m.group(1)}")
                if "[CONTROL/WSS] connected" in line:
                    self.control_var.set("WSS подключён")
                elif "[CONTROL/WSS] disconnected" in line:
                    self.control_var.set("WSS переподключение...")
                p = PTZ_RE.search(line)
                if p:
                    self.ptz_var.set(f"PAN {p.group(1)}° / TILT {p.group(2)}°")
                d = DRIVE_RE.search(line)
                if d:
                    self.drive_var.set(f"L {d.group(1)}% / R {d.group(2)}%")
                b = BRUSH_RE.search(line)
                if b:
                    self.brush_var.set(f"{b.group(1)}% / {b.group(2)}")
        except queue.Empty:
            pass
        self.after(100, self._drain_logs)

    def _append_log(self, text):
        self.log.insert("end", text)
        self.log.see("end")

    def _clear_log(self):
        self.log.delete("1.0", "end")

    def _on_close(self):
        self._stop()
        self.destroy()


if __name__ == "__main__":
    EmulatorGUI().mainloop()
