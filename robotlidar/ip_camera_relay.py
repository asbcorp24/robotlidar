#!/usr/bin/env python3
"""RTSP H.264 passthrough from an IP camera to the central RobotLiDAR Go server.

The Raspberry Pi never decodes/re-encodes video: FFmpeg reads RTSP and sends the
existing H.264 elementary stream as RTP/UDP using ``-c:v copy``.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional


class IpCameraRelayManager:
    def __init__(self, log_callback=None) -> None:
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._process: Optional[subprocess.Popen] = None
        self._config: dict[str, Any] = {}
        self._video_port: Optional[int] = None
        self._registered = False
        self._last_error = ''
        self._last_register_at = 0.0
        self._last_telemetry_at = 0.0
        self._restart_count = 0
        self._started_at = time.monotonic()
        self._log_callback = log_callback

    def start(self, settings: dict[str, Any]) -> None:
        self.configure(settings)
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name='robotlidar-ip-camera-relay', daemon=True
            )
            self._thread.start()

    def configure(self, settings: dict[str, Any]) -> None:
        cfg = self._normalize(settings)
        with self._lock:
            changed = cfg != self._config
            self._config = cfg
            if changed:
                self._registered = False
                self._video_port = None
        if changed:
            self._log('CAMERA: configuration changed; restarting relay')
            self._stop_ffmpeg()
            self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._stop_ffmpeg()

    def status(self) -> dict[str, Any]:
        with self._lock:
            process = self._process
            cfg = dict(self._config)
            running = process is not None and process.poll() is None
            return {
                'enabled': bool(cfg.get('enabled')),
                'device_id': cfg.get('device_id') or '',
                'server_url': cfg.get('server_url') or '',
                'rtsp_configured': bool(cfg.get('rtsp_url')),
                'registered': self._registered,
                'video_ingest_port': self._video_port,
                'ffmpeg_running': running,
                'ffmpeg_pid': process.pid if running else None,
                'restart_count': self._restart_count,
                'last_error': self._last_error,
                'last_register_age_sec': round(time.time() - self._last_register_at, 1)
                if self._last_register_at else None,
                'last_telemetry_age_sec': round(time.time() - self._last_telemetry_at, 1)
                if self._last_telemetry_at else None,
            }

    @staticmethod
    def default_device_id() -> str:
        host = socket.gethostname().strip().upper().replace('_', '-').replace(' ', '-')
        host = ''.join(ch for ch in host if ch.isalnum() or ch == '-')[:32] or 'RASPBERRYPI'
        return f'TRACTOR-RPI-{host}'

    def _normalize(self, settings: dict[str, Any]) -> dict[str, Any]:
        return {
            'enabled': bool(settings.get('camera_enabled', False)),
            'device_id': str(settings.get('camera_device_id') or self.default_device_id()).strip(),
            'rtsp_url': str(settings.get('camera_rtsp_url') or '').strip(),
            'server_url': str(settings.get('camera_server_url') or '').strip().rstrip('/'),
            'ffmpeg': str(settings.get('camera_ffmpeg') or 'ffmpeg').strip() or 'ffmpeg',
            'control_port': int(settings.get('camera_control_port') or 6000),
            'reported_fps': int(settings.get('camera_reported_fps') or 25),
            'reported_bitrate_bps': int(settings.get('camera_reported_bitrate_bps') or 2_000_000),
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            cfg = self._snapshot_config()
            if not cfg.get('enabled'):
                self._stop_ffmpeg()
                self._sleep(1.0)
                continue
            if not cfg.get('rtsp_url') or not cfg.get('server_url') or not cfg.get('device_id'):
                self._set_error('RTSP URL, server URL and device ID are required')
                self._stop_ffmpeg()
                self._sleep(1.0)
                continue

            try:
                if not self._registered or self._video_port is None:
                    self._register(cfg)
                self._ensure_ffmpeg(cfg)
                if time.time() - self._last_telemetry_at >= 1.0:
                    self._send_telemetry(cfg)
            except Exception as exc:
                self._set_error(str(exc))
                self._log(f'CAMERA: {exc}')
                self._registered = False
                self._stop_ffmpeg()
                self._sleep(2.0)
                continue
            self._sleep(0.5)

    def _sleep(self, seconds: float) -> None:
        self._wake.wait(seconds)
        self._wake.clear()

    def _snapshot_config(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._config)

    def _register(self, cfg: dict[str, Any]) -> None:
        server_host = urllib.parse.urlparse(cfg['server_url']).hostname
        if not server_host:
            raise RuntimeError('Invalid central server URL')
        local_ip = self._local_ip_for(server_host)
        payload = {
            'name': f'Raspberry Pi + IP camera ({cfg["device_id"]})',
            'ip': local_ip,
            'rtp_port': 5004,
            'ptz_port': cfg['control_port'],
        }
        data = self._json_request(
            'POST',
            f'{cfg["server_url"]}/api/devices/{urllib.parse.quote(cfg["device_id"], safe="")}/register',
            payload,
        )
        port = int(data.get('video_ingest_port') or 0)
        if not 1 <= port <= 65535:
            raise RuntimeError('Central server did not return video_ingest_port')
        with self._lock:
            self._video_port = port
            self._registered = True
            self._last_register_at = time.time()
            self._last_error = ''
        self._log(f'CAMERA: registered {cfg["device_id"]}; RTP ingest UDP {port}')

    def _ensure_ffmpeg(self, cfg: dict[str, Any]) -> None:
        with self._lock:
            process = self._process
            port = self._video_port
        if process is not None and process.poll() is None:
            return
        if port is None:
            return
        server_host = urllib.parse.urlparse(cfg['server_url']).hostname
        if not server_host:
            raise RuntimeError('Invalid central server URL')
        target = f'rtp://{server_host}:{port}?pkt_size=1200'
        command = [
            cfg['ffmpeg'],
            '-hide_banner', '-loglevel', 'warning',
            '-fflags', 'nobuffer',
            '-rtsp_transport', 'tcp',
            '-i', cfg['rtsp_url'],
            '-map', '0:v:0', '-an',
            '-c:v', 'copy',
            '-bsf:v', 'dump_extra=freq=keyframe',
            '-f', 'rtp', target,
        ]
        self._log(f'CAMERA: starting RTSP passthrough -> {server_host}:{port}')
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        with self._lock:
            self._process = process
            self._restart_count += 1
        threading.Thread(
            target=self._read_ffmpeg_stderr,
            args=(process,),
            name='robotlidar-camera-ffmpeg-log',
            daemon=True,
        ).start()

    def _read_ffmpeg_stderr(self, process: subprocess.Popen) -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            line = line.strip()
            if line:
                self._log('CAMERA/FFMPEG: ' + line)

    def _stop_ffmpeg(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
        except ProcessLookupError:
            pass

    def _send_telemetry(self, cfg: dict[str, Any]) -> None:
        with self._lock:
            process = self._process
        video_ok = process is not None and process.poll() is None
        payload = {
            'fps': cfg['reported_fps'] if video_ok else 0,
            'bitrate_bps': cfg['reported_bitrate_bps'] if video_ok else 0,
            'dropped_frames': 0,
            'uptime_ms': int((time.monotonic() - self._started_at) * 1000),
            'link_mbps': self._ethernet_speed_mbps(),
        }
        try:
            self._json_request(
                'POST',
                f'{cfg["server_url"]}/api/devices/{urllib.parse.quote(cfg["device_id"], safe="")}/telemetry',
                payload,
            )
            with self._lock:
                self._last_telemetry_at = time.time()
                self._last_error = ''
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                with self._lock:
                    self._registered = False
                    self._video_port = None
                self._stop_ffmpeg()
                return
            raise

    @staticmethod
    def _local_ip_for(host: str) -> str:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect((host, 9))
            return str(sock.getsockname()[0])
        finally:
            sock.close()

    @staticmethod
    def _ethernet_speed_mbps() -> int:
        for iface in ('eth0', 'end0', 'enp1s0'):
            path = Path('/sys/class/net') / iface / 'speed'
            try:
                value = int(path.read_text().strip())
                if value > 0:
                    return value
            except Exception:
                continue
        return 0

    @staticmethod
    def _json_request(method: str, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode('utf-8')
        request = urllib.request.Request(
            url,
            data=body,
            method=method,
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(request, timeout=4.0) as response:
            raw = response.read()
        return json.loads(raw.decode('utf-8')) if raw else {}

    def _set_error(self, text: str) -> None:
        with self._lock:
            self._last_error = text

    def _log(self, text: str) -> None:
        callback = self._log_callback
        if callback:
            try:
                callback(text)
                return
            except Exception:
                pass
        print(text, flush=True)
