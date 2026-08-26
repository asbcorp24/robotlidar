from __future__ import annotations

import argparse
import json
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
import websocket

CONTROL_MAGIC = 0x5354
CONTROL_VERSION = 1
PTZ_TYPE = 1
DRIVE_TYPE = 2
BRUSH_TYPE = 3
FLAG_CENTER = 1 << 0
FLAG_REQUEST_IDR = 1 << 1


@dataclass
class Config:
    device_id: str = "TRACTOR-WIN-0001"
    device_name: str = "Windows Tractor Emulator"
    server_http: str = "https://tele.xn----7sbbd7e6b.xn--p1ai"
    server_rtp_host: str = ""
    server_rtp_port: int = 5004
    ptz_listen_port: int = 6000
    video_transport: str = "srt"
    srt_latency_ms: int = 200
    control_websocket: bool = True
    control_udp_fallback: bool = True
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
        cfg = cls(**known)
        cfg.video_transport = str(cfg.video_transport or "srt").lower()
        if cfg.video_transport not in ("srt", "rtp"):
            cfg.video_transport = "srt"
        cfg.srt_latency_ms = max(80, min(2000, int(cfg.srt_latency_ms)))
        return cfg


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
        self.video_transport = cfg.video_transport
        self.video_ingest_port = cfg.server_rtp_port
        self.srt_ingest_port = 0
        self.srt_latency_ms = cfg.srt_latency_ms
        self.local_ip = self._get_local_ip()
        self._ws = None
        self._ws_lock = threading.RLock()
        self._ws_connected = False
        self._ws_reconnects = 0

    def _server_host(self) -> str:
        parsed = urllib.parse.urlparse(self.cfg.server_http)
        if parsed.hostname:
            return parsed.hostname
        return self.cfg.server_rtp_host or "127.0.0.1"

    def _get_local_ip(self) -> str:
        host = self._server_host()
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((host, 443 if self.cfg.server_http.lower().startswith("https://") else 80))
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
            "device_type": "windows_emulator",
            "video_transport": self.cfg.video_transport,
        }
        try:
            r = self.session.post(url, json=payload, timeout=5)
            r.raise_for_status()
            data = r.json()
            self.video_transport = str(data.get("video_transport", self.cfg.video_transport)).lower()
            self.video_ingest_port = int(data.get("video_ingest_port", self.cfg.server_rtp_port))
            self.srt_ingest_port = int(data.get("srt_ingest_port", 0) or 0)
            self.srt_latency_ms = int(data.get("srt_latency_ms", self.cfg.srt_latency_ms) or self.cfg.srt_latency_ms)
            print(f"[SERVER] registered {self.cfg.device_id} as {self.local_ip}")
            if self.video_transport == "srt" and self.srt_ingest_port:
                print(f"[SERVER] assigned SRT ingest UDP {self.srt_ingest_port}, latency {self.srt_latency_ms} ms")
            else:
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
            r = self.session.post(url, json=payload, timeout=3)
            if r.status_code == 404:
                self.register()
            else:
                r.raise_for_status()
        except Exception as e:
            print(f"[SERVER] telemetry failed: {e}")

    def ffmpeg_command(self) -> list[str]:
        common = [
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
        ]

        host = self._server_host()
        if self.video_transport == "srt" and self.srt_ingest_port > 0:
            latency_us = max(80, min(2000, int(self.srt_latency_ms))) * 1000
            target = (
                f"srt://{host}:{self.srt_ingest_port}?mode=caller&transtype=live&"
                f"latency={latency_us}&pkt_size=1316"
            )
            return common + [
                "-mpegts_flags", "+resend_headers",
                "-muxdelay", "0",
                "-f", "mpegts",
                target,
            ]

        rtp_host = self.cfg.server_rtp_host.strip() or host
        target = f"rtp://{rtp_host}:{self.video_ingest_port}?pkt_size=1200"
        return common + ["-f", "rtp", target]

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

    def _handle_control_packet(self, data: bytes, source: str) -> None:
        if len(data) != 16:
            print(f"[CONTROL] rejected {source}: packet size {len(data)}, expected 16")
            return
        try:
            magic, version, ptype, seq, value1, value2, extra1, extra2 = struct.unpack("!HBBIhhHH", data)
        except struct.error as exc:
            print(f"[CONTROL] rejected {source}: {exc}")
            return
        if magic != CONTROL_MAGIC or version != CONTROL_VERSION:
            print(f"[CONTROL] rejected {source}: bad header")
            return

        if ptype == PTZ_TYPE:
            pan, tilt, speed, flags = value1, value2, extra1, extra2
            if flags & FLAG_CENTER:
                self.pan_cdeg = 0
                self.tilt_cdeg = 0
            else:
                self.pan_cdeg = max(-18000, min(18000, pan))
                self.tilt_cdeg = max(-9000, min(9000, tilt))
            if flags & FLAG_REQUEST_IDR:
                self.request_idr()
            print(
                f"[PTZ] via {source} seq={seq} "
                f"pan={self.pan_cdeg / 100:.2f} tilt={self.tilt_cdeg / 100:.2f} "
                f"speed={speed / 100:.2f} deg/s flags=0x{flags:04x}"
            )
        elif ptype == DRIVE_TYPE:
            self.track_left = max(-1000, min(1000, value1))
            self.track_right = max(-1000, min(1000, value2))
            print(
                f"[DRIVE] via {source} seq={seq} "
                f"left={self.track_left / 10:.1f}% right={self.track_right / 10:.1f}%"
            )
        elif ptype == BRUSH_TYPE:
            self.brush_spin = max(-1000, min(1000, value1))
            self.brush_lift = max(-1000, min(1000, value2))
            lift_name = "UP" if self.brush_lift > 0 else "DOWN" if self.brush_lift < 0 else "STOP"
            print(
                f"[BRUSH] via {source} seq={seq} "
                f"spin={self.brush_spin / 10:.1f}% lift={lift_name} ({self.brush_lift / 10:.1f}%)"
            )
        else:
            print(f"[CONTROL] unknown packet type {ptype} via {source}")

    def _control_ws_url(self) -> str:
        parsed = urllib.parse.urlparse(self.cfg.server_http)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("server_http must start with http:// or https://")
        scheme = "wss" if parsed.scheme == "https" else "ws"
        base_path = parsed.path.rstrip("/")
        path = f"{base_path}/api/devices/{urllib.parse.quote(self.cfg.device_id, safe='')}/control-ws"
        return urllib.parse.urlunparse((scheme, parsed.netloc, path, "", "", ""))

    def websocket_loop(self) -> None:
        if not self.cfg.control_websocket:
            return
        while not self.stop_event.is_set():
            url = self._control_ws_url()
            try:
                ws = websocket.create_connection(url, timeout=8, enable_multithread=True)
                ws.settimeout(10)
                with self._ws_lock:
                    self._ws = ws
                    self._ws_connected = True
                    self._ws_reconnects += 1
                print(f"[CONTROL/WSS] connected {url}")
                while not self.stop_event.is_set():
                    try:
                        message = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        ws.ping("robotlidar-emulator")
                        continue
                    if message is None:
                        raise RuntimeError("server closed WebSocket")
                    if isinstance(message, str):
                        continue
                    self._handle_control_packet(bytes(message), "WSS")
            except Exception as exc:
                if not self.stop_event.is_set():
                    print(f"[CONTROL/WSS] disconnected: {exc}")
            finally:
                with self._ws_lock:
                    ws = self._ws
                    self._ws = None
                    self._ws_connected = False
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
            self.stop_event.wait(1.0)

    def udp_control_loop(self) -> None:
        if not self.cfg.control_udp_fallback:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.cfg.ptz_listen_port))
        sock.settimeout(0.5)
        print(f"[CONTROL/UDP] legacy fallback listening 0.0.0.0:{self.cfg.ptz_listen_port}")
        while not self.stop_event.is_set():
            try:
                data, peer = sock.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle_control_packet(data, f"UDP {peer[0]}")
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
        print(f"Video     : requested {self.cfg.video_transport.upper()}")
        print(f"Control   : WSS={'ON' if self.cfg.control_websocket else 'OFF'}, UDP fallback={'ON' if self.cfg.control_udp_fallback else 'OFF'}")

        while not self.register():
            print("[SERVER] retry in 2 seconds...")
            time.sleep(2)

        if self.video_transport == "srt" and self.srt_ingest_port:
            print(f"SRT       : {self._server_host()}:{self.srt_ingest_port} latency={self.srt_latency_ms}ms")
        else:
            print(f"RTP       : {self.cfg.server_rtp_host or self._server_host()}:{self.video_ingest_port}")
        self.start_video()

        threads = [
            threading.Thread(target=self.websocket_loop, name="control-wss", daemon=True),
            threading.Thread(target=self.udp_control_loop, name="control-udp", daemon=True),
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
            with self._ws_lock:
                ws = self._ws
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
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
    parser = argparse.ArgumentParser(description="RobotLiDAR tractor/camera emulator for Windows")
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
