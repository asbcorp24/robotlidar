#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import escape

import websocket
from onvif_discovery import discover as discover_onvif

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = BASE_DIR / "config.json"

CONTROL_MAGIC = 0x5354
CONTROL_VERSION = 1
TYPE_PTZ = 1
TYPE_DRIVE = 2
TYPE_BRUSH = 3
FLAG_CENTER = 1 << 0


@dataclass
class Config:
    device_id: str = "CAM-OPIZERO-001"
    device_name: str = "Orange Pi Zero Camera"
    server_url: str = "https://tele.xn----7sbbd7e6b.xn--p1ai"
    input_mode: str = "rtsp"
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

    ptz_enabled: bool = True
    onvif_auto_discovery: bool = True
    onvif_device_url: str = ""
    onvif_url: str = ""
    onvif_username: str = ""
    onvif_password: str = ""
    onvif_profile_token: str = ""

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class CameraStreamer:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.proc: Optional[subprocess.Popen] = None
        self.stop_event = threading.Event()
        self.start_monotonic = time.monotonic()
        self.srt_port = 0
        self.srt_latency_ms = cfg.srt_latency_ms
        self.server_host = urllib.parse.urlparse(cfg.server_url).hostname or ""
        self.last_register = 0.0
        self.restart_count = 0
        self.ws_thread: Optional[threading.Thread] = None
        self.ws = None
        self.ws_connected = False
        self.ws_url = ""
        self.last_seq = 0
        self.pan_cdeg = 0
        self.tilt_cdeg = 0
        self.onvif_url = cfg.onvif_url.strip()
        self.onvif_profile_token = cfg.onvif_profile_token.strip()
        self.onvif_device_url = cfg.onvif_device_url.strip()

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
            "device_type": "orange_pi_zero_camera_ptz" if self.cfg.ptz_enabled else "orange_pi_zero_camera",
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

    def discover_onvif_if_needed(self) -> None:
        if not self.cfg.ptz_enabled:
            return
        if self.onvif_url and self.onvif_profile_token:
            self.log(f"ONVIF manual config: PTZ={self.onvif_url}; profile={self.onvif_profile_token}")
            return
        if not self.cfg.onvif_auto_discovery:
            self.log("ONVIF auto-discovery disabled")
            return
        if self.cfg.input_mode.lower().strip() != "rtsp":
            self.log("ONVIF auto-discovery skipped: input_mode is not rtsp")
            return
        try:
            result = discover_onvif(
                self.cfg.input_url,
                username=self.cfg.onvif_username,
                password=self.cfg.onvif_password,
                explicit_device_url=self.onvif_device_url,
            )
            self.onvif_device_url = result.device_service_url
            self.onvif_url = result.ptz_url
            self.onvif_profile_token = result.profile_token
            self.log(f"ONVIF discovered: device={self.onvif_device_url}; PTZ={self.onvif_url}; profile={self.onvif_profile_token}")
        except Exception as exc:
            self.log(f"ONVIF discovery failed: {exc}")
            if self.cfg.onvif_url:
                self.onvif_url = self.cfg.onvif_url
            if self.cfg.onvif_profile_token:
                self.onvif_profile_token = self.cfg.onvif_profile_token
            if self.onvif_url:
                self.log("ONVIF: using manual PTZ URL fallback")

    def control_ws_url(self) -> str:
        parsed = urllib.parse.urlparse(self.cfg.server_url.rstrip("/"))
        scheme = "wss" if parsed.scheme == "https" else "ws"
        path = f"{parsed.path.rstrip('/')}/api/devices/{urllib.parse.quote(self.cfg.device_id, safe='')}/control-ws"
        return urllib.parse.urlunparse((scheme, parsed.netloc, path, "", "", ""))

    def start_control(self) -> None:
        if not self.cfg.ptz_enabled:
            self.log("PTZ disabled in config")
            return
        self.ws_thread = threading.Thread(target=self.control_loop, name="ptz-wss", daemon=True)
        self.ws_thread.start()

    def control_loop(self) -> None:
        self.ws_url = self.control_ws_url()
        while not self.stop_event.is_set():
            try:
                ws = websocket.create_connection(self.ws_url, timeout=8, enable_multithread=True)
                ws.settimeout(10)
                self.ws = ws
                self.ws_connected = True
                self.log(f"CONTROL/WSS connected: {self.ws_url}")
                while not self.stop_event.is_set():
                    try:
                        message = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        ws.ping("opizero-camera")
                        continue
                    if message is None:
                        raise RuntimeError("WebSocket closed by server")
                    if isinstance(message, str):
                        continue
                    self.handle_control_packet(bytes(message))
            except Exception as exc:
                if not self.stop_event.is_set():
                    self.log(f"CONTROL/WSS disconnected: {exc}")
            finally:
                self.ws_connected = False
                if self.ws is not None:
                    try:
                        self.ws.close()
                    except Exception:
                        pass
                    self.ws = None
            self.stop_event.wait(self.cfg.reconnect_delay_sec)

    def handle_control_packet(self, data: bytes) -> None:
        if len(data) != 16:
            self.log(f"CONTROL ignored: packet size {len(data)}")
            return
        magic, version, packet_type, seq, value1, value2, speed, flags = struct.unpack(">HBBIhhHH", data)
        if magic != CONTROL_MAGIC or version != CONTROL_VERSION:
            self.log("CONTROL ignored: bad header")
            return
        self.last_seq = int(seq)
        if packet_type == TYPE_PTZ:
            pan = int(value1)
            tilt = int(value2)
            if flags & FLAG_CENTER:
                pan = 0
                tilt = 0
            self.pan_cdeg = pan
            self.tilt_cdeg = tilt
            threading.Thread(target=self.onvif_move, args=(pan, tilt, int(speed)), daemon=True).start()
        elif packet_type in (TYPE_DRIVE, TYPE_BRUSH):
            self.log(f"CONTROL ignored type={packet_type}: camera-only device")
        else:
            self.log(f"CONTROL ignored unknown type={packet_type}")

    def onvif_move(self, pan_cdeg: int, tilt_cdeg: int, speed_cdeg_s: int) -> None:
        if not self.onvif_url or not self.onvif_profile_token:
            self.log(f"PTZ received pan={pan_cdeg/100:.1f} tilt={tilt_cdeg/100:.1f}; ONVIF PTZ/profile unavailable")
            return
        pan = max(-1.0, min(1.0, pan_cdeg / 18000.0))
        tilt = max(-1.0, min(1.0, tilt_cdeg / 9000.0))
        speed = max(0.05, min(1.0, abs(speed_cdeg_s) / 9000.0 if speed_cdeg_s else 0.5))
        token = escape(self.onvif_profile_token)
        security = self.ws_security(self.cfg.onvif_username, self.cfg.onvif_password) if self.cfg.onvif_username else ""
        envelope = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
<s:Header>{security}</s:Header><s:Body><tptz:AbsoluteMove><tptz:ProfileToken>{token}</tptz:ProfileToken><tptz:Position><tt:PanTilt x="{pan:.6f}" y="{tilt:.6f}" space="http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace"/></tptz:Position><tptz:Speed><tt:PanTilt x="{speed:.4f}" y="{speed:.4f}" space="http://www.onvif.org/ver10/tptz/PanTiltSpaces/GenericSpeedSpace"/></tptz:Speed></tptz:AbsoluteMove></s:Body></s:Envelope>'''
        req = urllib.request.Request(self.onvif_url, data=envelope.encode("utf-8"), method="POST", headers={"Content-Type": "application/soap+xml; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=2.5) as response:
                response.read(64)
            self.log(f"CONTROL/PTZ seq={self.last_seq} pan={pan_cdeg/100:.1f} tilt={tilt_cdeg/100:.1f}")
        except Exception as exc:
            self.log(f"CONTROL/PTZ ONVIF error: {exc}")

    @staticmethod
    def ws_security(username: str, password: str) -> str:
        nonce = os.urandom(16)
        created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        digest = hashlib.sha1(nonce + created.encode("utf-8") + password.encode("utf-8")).digest()
        return f'''<wsse:Security s:mustUnderstand="1"><wsse:UsernameToken><wsse:Username>{escape(username)}</wsse:Username><wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{base64.b64encode(digest).decode()}</wsse:Password><wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</wsse:Nonce><wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>'''

    def send_telemetry(self) -> None:
        url = f"{self.cfg.server_url.rstrip('/')}/api/devices/{urllib.parse.quote(self.cfg.device_id, safe='')}/telemetry"
        running = self.proc is not None and self.proc.poll() is None
        payload = {
            "fps": self.cfg.fps if running else 0,
            "bitrate_bps": self.cfg.bitrate_kbps * 1000 if running else 0,
            "dropped_frames": 0,
            "uptime_ms": int((time.monotonic() - self.start_monotonic) * 1000),
            "pan_cdeg": self.pan_cdeg,
            "tilt_cdeg": self.tilt_cdeg,
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
        while not self.stop_event.is_set() and not self.register():
            self.stop_event.wait(self.cfg.reconnect_delay_sec)
        if self.stop_event.is_set():
            return 0
        self.discover_onvif_if_needed()
        self.start_ffmpeg()
        self.start_control()
        next_telemetry = 0.0
        try:
            while not self.stop_event.is_set():
                now = time.monotonic()
                if now >= next_telemetry:
                    self.send_telemetry()
                    next_telemetry = now + self.cfg.telemetry_period_sec
                if self.proc and self.proc.poll() is not None:
                    code = self.proc.returncode
                    self.log(f"FFMPEG EXIT {code}; restart after {self.cfg.reconnect_delay_sec}s")
                    self.stop_ffmpeg()
                    self.stop_event.wait(self.cfg.reconnect_delay_sec)
                    if not self.stop_event.is_set():
                        if time.monotonic() - self.last_register > 30:
                            self.register()
                        self.start_ffmpeg()
                self.stop_event.wait(0.2)
        finally:
            if self.ws is not None:
                try:
                    self.ws.close()
                except Exception:
                    pass
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
        app.stop_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
