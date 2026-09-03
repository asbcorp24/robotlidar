#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.json"


@dataclass
class Config:
    device_id: str = "CAM-OPIZERO-001"
    device_name: str = "Orange Pi Zero Camera"
    server_url: str = "https://tele.xn----7sbbd7e6b.xn--p1ai"
    input_mode: str = "rtsp"  # rtsp | v4l2_h264 | v4l2_encode | test
    input_url: str = "rtsp://192.168.1.149:8554/camera"
    video_device: str = "/dev/video0"
    width: int = 1280
    height: int = 720
    fps: int = 20
    bitrate_kbps: int = 1500
    encoder: str = "h264_v4l2m2m"
    ffmpeg: str = "ffmpeg"
    srt_latency_ms: int = 200
    telemetry_period_sec: float = 2.0
    reconnect_delay_sec: float = 2.0

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class CameraStreamer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.proc: Optional[subprocess.Popen] = None
        self.stop = False
        self.start_monotonic = time.monotonic()
        self.srt_port = 0
        self.srt_latency_ms = cfg.srt_latency_ms
        self.server_host = urllib.parse.urlparse(cfg.server_url).hostname or ""
        self.last_register = 0.0
        self.restart_count = 0

    def log(self, msg: str) -> None:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)

    def local_ip(self) -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((self.server_host or "8.8.8.8", 443))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            s.close()

    def json_request(self, method: str, url: str, payload: dict) -> tuple[int, dict]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, method=method, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=6) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw.decode("utf-8")) if raw else {}
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                data = {"detail": raw.decode("utf-8", errors="replace")}
            return exc.code, data

    def register(self) -> bool:
        url = f"{self.cfg.server_url.rstrip('/')}/api/devices/{urllib.parse.quote(self.cfg.device_id, safe='')}/register"
        payload = {
            "name": self.cfg.device_name,
            "ip": self.local_ip(),
            "rtp_port": 5004,
            "ptz_port": 6000,
            "device_type": "orange_pi_zero_camera",
            "video_transport": "srt",
        }
        try:
            status, data = self.json_request("POST", url, payload)
            if status != 200:
                self.log(f"REGISTER ERROR HTTP {status}: {data}")
                return False
            self.srt_port = int(data.get("srt_ingest_port") or 0)
            self.srt_latency_ms = int(data.get("srt_latency_ms") or self.cfg.srt_latency_ms)
            if not (1 <= self.srt_port <= 65535):
                self.log("REGISTER ERROR: server did not return srt_ingest_port")
                return False
            self.last_register = time.monotonic()
            self.log(f"REGISTERED {self.cfg.device_id}; SRT {self.server_host}:{self.srt_port}; latency={self.srt_latency_ms}ms")
            return True
        except Exception as exc:
            self.log(f"REGISTER ERROR: {exc}")
            return False

    def input_args(self) -> list[str]:
        c = self.cfg
        mode = c.input_mode.lower().strip()
        if mode == "rtsp":
            return ["-rtsp_transport", "tcp", "-i", c.input_url, "-map", "0:v:0", "-an", "-c:v", "copy", "-bsf:v", "dump_extra=freq=keyframe"]
        if mode == "v4l2_h264":
            return ["-f", "v4l2", "-input_format", "h264", "-video_size", f"{c.width}x{c.height}", "-framerate", str(c.fps), "-i", c.video_device, "-an", "-c:v", "copy"]
        if mode == "v4l2_encode":
            enc = c.encoder or "libx264"
            args = ["-f", "v4l2", "-video_size", f"{c.width}x{c.height}", "-framerate", str(c.fps), "-i", c.video_device, "-an", "-c:v", enc]
            if enc == "libx264":
                args += ["-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "baseline", "-bf", "0"]
            args += ["-b:v", f"{c.bitrate_kbps}k", "-maxrate", f"{c.bitrate_kbps}k", "-bufsize", f"{max(c.bitrate_kbps * 2, 500)}k", "-g", str(max(c.fps, 1)), "-pix_fmt", "yuv420p"]
            return args
        if mode == "test":
            return ["-re", "-f", "lavfi", "-i", f"testsrc2=size={c.width}x{c.height}:rate={c.fps}", "-an", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "baseline", "-bf", "0", "-g", str(max(c.fps, 1)), "-b:v", f"{c.bitrate_kbps}k", "-pix_fmt", "yuv420p"]
        raise ValueError(f"Unknown input_mode: {c.input_mode}")

    def ffmpeg_command(self) -> list[str]:
        target = f"srt://{self.server_host}:{self.srt_port}?mode=caller&transtype=live&latency={self.srt_latency_ms * 1000}&pkt_size=1316"
        return [self.cfg.ffmpeg, "-hide_banner", "-loglevel", "warning"] + self.input_args() + ["-muxdelay", "0", "-muxpreload", "0", "-f", "mpegts", target]

    def start_ffmpeg(self) -> None:
        cmd = self.ffmpeg_command()
        self.log("FFMPEG START: " + " ".join(cmd))
        self.proc = subprocess.Popen(cmd)
        self.restart_count += 1

    def stop_ffmpeg(self) -> None:
        if not self.proc:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def send_telemetry(self) -> None:
        url = f"{self.cfg.server_url.rstrip('/')}/api/devices/{urllib.parse.quote(self.cfg.device_id, safe='')}/telemetry"
        running = self.proc is not None and self.proc.poll() is None
        payload = {
            "fps": self.cfg.fps if running else 0,
            "bitrate_bps": self.cfg.bitrate_kbps * 1000 if running else 0,
            "dropped_frames": 0,
            "uptime_ms": int((time.monotonic() - self.start_monotonic) * 1000),
            "link_mbps": 100,
        }
        try:
            status, data = self.json_request("POST", url, payload)
            if status == 404:
                self.log("TELEMETRY: device not registered; registering again")
                self.register()
            elif status >= 400:
                self.log(f"TELEMETRY ERROR HTTP {status}: {data}")
        except Exception as exc:
            self.log(f"TELEMETRY ERROR: {exc}")

    def run(self) -> int:
        if not self.server_host:
            self.log("Invalid server_url")
            return 2
        while not self.stop and not self.register():
            time.sleep(self.cfg.reconnect_delay_sec)
        if self.stop:
            return 0
        self.start_ffmpeg()
        next_telemetry = 0.0
        try:
            while not self.stop:
                now = time.monotonic()
                if now >= next_telemetry:
                    self.send_telemetry()
                    next_telemetry = now + self.cfg.telemetry_period_sec
                if self.proc and self.proc.poll() is not None:
                    code = self.proc.returncode
                    self.log(f"FFMPEG EXIT {code}; restart after {self.cfg.reconnect_delay_sec}s")
                    self.stop_ffmpeg()
                    time.sleep(self.cfg.reconnect_delay_sec)
                    if not self.stop:
                        if time.monotonic() - self.last_register > 30:
                            self.register()
                        self.start_ffmpeg()
                time.sleep(0.2)
        finally:
            self.stop_ffmpeg()
        return 0


def main() -> int:
    cfg_path = Path(os.environ.get("ORANGE_PI_CAMERA_CONFIG", str(DEFAULT_CONFIG)))
    if not cfg_path.exists():
        print(f"Config not found: {cfg_path}", file=sys.stderr)
        print(f"Copy {BASE_DIR / 'config.example.json'} to {cfg_path} and edit it.", file=sys.stderr)
        return 2
    app = CameraStreamer(Config.load(cfg_path))

    def handle_signal(_sig, _frame):
        app.stop = True

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
