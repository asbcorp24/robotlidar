from __future__ import annotations

import argparse
import json
import shutil
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

PTZ_MAGIC = 0x5354
PTZ_VERSION = 1
PTZ_TYPE = 1
DRIVE_TYPE = 2
BRUSH_TYPE = 3
FLAG_CENTER = 1 << 0
FLAG_REQUEST_IDR = 1 << 1


@dataclass
class Config:
    device_id: str = "camera-win-001"
    device_name: str = "Windows Camera Emulator"
    server_http: str = "http://127.0.0.1:8000"
    server_rtp_host: str = "127.0.0.1"
    server_rtp_port: int = 5004
    ptz_listen_port: int = 6000
    camera_name: str = "Integrated Camera"
    ffmpeg: str = "ffmpeg.exe"
    width: int = 1280
    height: int = 720
    fps: int = 30
    bitrate_kbps: int = 2000
    gop: int = 15
    preset: str = "ultrafast"
    telemetry_period_sec: float = 1.0

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class RadxaWindowsEmulator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.stop_event = threading.Event()
        self.video_proc: Optional[subprocess.Popen] = None
        self.start_time = time.monotonic()
        self.pan_cdeg = 0
        self.tilt_cdeg = 0
        self.track_left = 0
        self.track_right = 0
        self.brush_spin = 0
        self.brush_lift = 0
        self.video_restarts = 0
        self.session = requests.Session()
        self.video_ingest_port = cfg.server_rtp_port
        self.local_ip = self._get_local_ip()

    def _get_local_ip(self) -> str:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((self.cfg.server_rtp_host, 80))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            s.close()

    def register(self) -> bool:
        url = f"{self.cfg.server_http.rstrip('/')}/api/devices/{self.cfg.device_id}/register"
        payload = {
            "name": self.cfg.device_name,
            "ip": self.local_ip,
            "rtp_port": self.cfg.server_rtp_port,
            "ptz_port": self.cfg.ptz_listen_port,
        }
        try:
            r = self.session.post(url, json=payload, timeout=3)
            r.raise_for_status()
            data = r.json()
            self.video_ingest_port = int(data.get("video_ingest_port", self.cfg.server_rtp_port))
            print(f"[SERVER] registered {self.cfg.device_id} as {self.local_ip}")
            print(f"[SERVER] assigned RTP ingest UDP {self.video_ingest_port}")
            return True
        except Exception as e:
            print(f"[SERVER] register failed: {e}")
            return False

    def send_telemetry(self) -> None:
        url = f"{self.cfg.server_http.rstrip('/')}/api/devices/{self.cfg.device_id}/telemetry"
        uptime_ms = int((time.monotonic() - self.start_time) * 1000)
        video_ok = self.video_proc is not None and self.video_proc.poll() is None
        payload = {
            "fps": self.cfg.fps if video_ok else 0,
            "bitrate_bps": self.cfg.bitrate_kbps * 1000 if video_ok else 0,
            "dropped_frames": 0,
            "uptime_ms": uptime_ms,
            "pan_cdeg": self.pan_cdeg,
            "tilt_cdeg": self.tilt_cdeg,
            "link_mbps": 1000,
        }
        try:
            r = self.session.post(url, json=payload, timeout=2)
            if r.status_code == 404:
                self.register()
            else:
                r.raise_for_status()
        except Exception as e:
            print(f"[SERVER] telemetry failed: {e}")

    def ffmpeg_command(self) -> list[str]:
        target = f"rtp://{self.cfg.server_rtp_host}:{self.video_ingest_port}?pkt_size=1200"
        return [
            self.cfg.ffmpeg,
            "-hide_banner",
            "-loglevel", "warning",
            "-f", "dshow",
            "-rtbufsize", "256M",
            "-video_size", f"{self.cfg.width}x{self.cfg.height}",
            "-framerate", str(self.cfg.fps),
            "-i", f"video={self.cfg.camera_name}",
            "-an",
            "-c:v", "libx264",
            "-preset", self.cfg.preset,
            "-tune", "zerolatency",
            "-profile:v", "baseline",
            "-pix_fmt", "yuv420p",
            "-b:v", f"{self.cfg.bitrate_kbps}k",
            "-maxrate", f"{self.cfg.bitrate_kbps}k",
            "-bufsize", f"{max(2 * self.cfg.bitrate_kbps, 500)}k",
            "-g", str(self.cfg.gop),
            "-keyint_min", str(self.cfg.gop),
            "-bf", "0",
            "-sc_threshold", "0",
            "-bsf:v", "dump_extra=freq=keyframe",
            "-f", "rtp",
            target,
        ]

    def start_video(self) -> None:
        if self.video_proc and self.video_proc.poll() is None:
            return
        cmd = self.ffmpeg_command()
        print("[VIDEO] starting:")
        print("        " + " ".join(f'\"{x}\"' if " " in x else x for x in cmd))
        self.video_proc = subprocess.Popen(cmd)
        self.video_restarts += 1

    def stop_video(self) -> None:
        if not self.video_proc:
            return
        if self.video_proc.poll() is None:
            self.video_proc.terminate()
            try:
                self.video_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.video_proc.kill()
        self.video_proc = None

    def request_idr(self) -> None:
        print("[PTZ] REQUEST_IDR received; emulator will use the next scheduled IDR")

    def ptz_loop(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.cfg.ptz_listen_port))
        sock.settimeout(0.5)
        print(f"[CONTROL] listening UDP 0.0.0.0:{self.cfg.ptz_listen_port}")

        while not self.stop_event.is_set():
            try:
                data, peer = sock.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                break

            if len(data) < 16:
                continue
            try:
                magic, version, ptype, seq, value1, value2, extra1, extra2 = struct.unpack("!HBBIhhHH", data[:16])
            except struct.error:
                continue
            if magic != PTZ_MAGIC or version != PTZ_VERSION:
                continue

            if ptype == PTZ_TYPE:
                pan, tilt, speed, flags = value1, value2, extra1, extra2
                if flags & FLAG_CENTER:
                    self.pan_cdeg = 0
                    self.tilt_cdeg = 0
                else:
                    self.pan_cdeg = max(-9000, min(9000, pan))
                    self.tilt_cdeg = max(-4500, min(4500, tilt))
                if flags & FLAG_REQUEST_IDR:
                    self.request_idr()
                print(
                    f"[PTZ] from {peer[0]} seq={seq} "
                    f"pan={self.pan_cdeg / 100:.2f} tilt={self.tilt_cdeg / 100:.2f} "
                    f"speed={speed / 100:.2f} deg/s flags=0x{flags:04x}"
                )
            elif ptype == DRIVE_TYPE:
                self.track_left = max(-1000, min(1000, value1))
                self.track_right = max(-1000, min(1000, value2))
                print(
                    f"[DRIVE] from {peer[0]} seq={seq} "
                    f"left={self.track_left / 10:.1f}% right={self.track_right / 10:.1f}%"
                )
            elif ptype == BRUSH_TYPE:
                self.brush_spin = max(-1000, min(1000, value1))
                self.brush_lift = max(-1000, min(1000, value2))
                lift_name = "UP" if self.brush_lift > 0 else "DOWN" if self.brush_lift < 0 else "STOP"
                print(
                    f"[BRUSH] from {peer[0]} seq={seq} "
                    f"spin={self.brush_spin / 10:.1f}% lift={lift_name} ({self.brush_lift / 10:.1f}%)"
                )

        sock.close()

    def telemetry_loop(self) -> None:
        while not self.stop_event.wait(self.cfg.telemetry_period_sec):
            self.send_telemetry()

    def run(self) -> int:
        if shutil.which(self.cfg.ffmpeg) is None and not Path(self.cfg.ffmpeg).exists():
            print(f"FFmpeg not found: {self.cfg.ffmpeg}")
            print("Install FFmpeg and add it to PATH, or set the full path in config.json")
            return 2

        print(f"Device ID : {self.cfg.device_id}")
        print(f"Camera    : {self.cfg.camera_name}")
        print(f"Local IP  : {self.local_ip}")
        print(f"Server    : {self.cfg.server_http}")

        while not self.register():
            print("[SERVER] retry in 2 seconds...")
            time.sleep(2)

        print(f"RTP       : {self.cfg.server_rtp_host}:{self.video_ingest_port}")
        self.start_video()

        threads = [
            threading.Thread(target=self.ptz_loop, name="control", daemon=True),
            threading.Thread(target=self.telemetry_loop, name="telemetry", daemon=True),
        ]
        for t in threads:
            t.start()

        try:
            while not self.stop_event.wait(1.0):
                if self.video_proc and self.video_proc.poll() is not None:
                    code = self.video_proc.returncode
                    print(f"[VIDEO] FFmpeg exited with {code}; restarting")
                    time.sleep(1.0)
                    self.start_video()
        except KeyboardInterrupt:
            print("\nStopping emulator...")
        finally:
            self.stop_event.set()
            self.stop_video()
        return 0


def list_cameras(ffmpeg: str) -> int:
    cmd = [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"]
    print("Windows DirectShow devices:\n")
    try:
        subprocess.run(cmd, check=False)
        return 0
    except FileNotFoundError:
        print(f"FFmpeg not found: {ffmpeg}")
        return 2


def main() -> int:
    parser = argparse.ArgumentParser(description="Radxa ZERO 3E webcam emulator for Windows")
    parser.add_argument("--config", default="config.json", help="JSON config path")
    parser.add_argument("--list-cameras", action="store_true", help="List DirectShow cameras and exit")
    parser.add_argument("--ffmpeg", default="ffmpeg.exe", help="FFmpeg executable for --list-cameras")
    args = parser.parse_args()

    if args.list_cameras:
        return list_cameras(args.ffmpeg)

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        print(f"Config not found: {cfg_path}")
        print("Copy config.example.json to config.json and edit camera/server settings.")
        return 2

    return RadxaWindowsEmulator(Config.load(cfg_path)).run()


if __name__ == "__main__":
    raise SystemExit(main())
