#!/usr/bin/env python3
"""RTSP H.264 passthrough and device registration for Raspberry Pi.

Video relay and remote control are independent. When video is enabled the
Raspberry requests the reliable SRT ingest from the central server and sends
H.264 as MPEG-TS with stream copy (no decode/encode). Old servers remain
compatible through an automatic RTP fallback.
"""
from __future__ import annotations

import json
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
        self._configured_active_camera: Optional[int] = None
        self._video_port: Optional[int] = None
        self._srt_port: Optional[int] = None
        self._transport = 'rtp'
        self._registered = False
        self._last_error = ''
        self._last_register_at = 0.0
        self._last_telemetry_at = 0.0
        self._last_gps_sent_at = 0.0
        self._restart_count = 0
        self._started_at = time.monotonic()
        self._video_requested = False
        self._progress_at = 0.0
        self._progress_value = -1
        self._process_started_at = 0.0
        self._camera_failures = 0
        self._next_camera_probe_at = 0.0
        self._retry_at = 0.0
        self._retry_step = 0
        self._last_video_ok_at = 0.0
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
            requested_camera = cfg['active_camera']
            # Settings retain the startup choice; failover changes the runtime
            # camera. Unrelated settings must not switch back to a failed source.
            sources_unchanged = all(
                cfg[key] == self._config.get(key)
                for key in ('rtsp_url', 'camera1_url', 'camera2_url')
            )
            if (requested_camera == self._configured_active_camera
                    and sources_unchanged):
                cfg['active_camera'] = self._config['active_camera']
            self._configured_active_camera = requested_camera
            changed = cfg != self._config
            self._config = cfg
            if changed:
                self._camera_failures = 0
                self._retry_at = 0.0
                self._retry_step = 0
                self._next_camera_probe_at = 0.0
                self._registered = False
                self._video_port = None
                self._srt_port = None
                self._transport = 'rtp'
        if changed:
            self._log('DEVICE: configuration changed; re-registering')
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
                'control_enabled': bool(cfg.get('remote_control_enabled')),
                'device_id': cfg.get('device_id') or '',
                'server_url': cfg.get('server_url') or '',
                'rtsp_configured': bool(cfg.get('rtsp_url')),
                'camera1_url': cfg.get('camera1_url') or '',
                'camera2_url': cfg.get('camera2_url') or '',
                'camera1_name': cfg.get('camera1_name') or 'Camera 1',
                'camera2_name': cfg.get('camera2_name') or 'Camera 2',
                'active_camera': int(cfg.get('active_camera') or 1),
                'registered': self._registered,
                'video_transport': self._transport,
                'video_ingest_port': self._video_port,
                'srt_ingest_port': self._srt_port,
                'ffmpeg_running': running,
                'ffmpeg_pid': process.pid if running else None,
                'restart_count': self._restart_count,
                'video_progress_age_sec': round(time.monotonic() - self._progress_at, 1) if self._progress_at else None,
                'camera_failures': self._camera_failures,
                'retry_in_sec': round(max(0.0, self._retry_at - time.monotonic()), 1),
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
            'remote_control_enabled': bool(settings.get('camera_remote_control_enabled', False)),
            'device_id': str(settings.get('camera_device_id') or self.default_device_id()).strip(),
            'rtsp_url': str(settings.get('camera_rtsp_url') or '').strip(),
            'camera1_url': str(settings.get('camera1_rtsp_url') or settings.get('camera_rtsp_url') or '').strip(),
            'camera2_url': str(settings.get('camera2_rtsp_url') or '').strip(),
            'camera1_name': str(settings.get('camera1_name') or 'Camera 1').strip() or 'Camera 1',
            'camera2_name': str(settings.get('camera2_name') or 'Camera 2').strip() or 'Camera 2',
            'active_camera': 2 if int(settings.get('camera_active_camera') or 1) == 2 else 1,
            'server_url': str(settings.get('camera_server_url') or '').strip().rstrip('/'),
            'ffmpeg': str(settings.get('camera_ffmpeg') or 'ffmpeg').strip() or 'ffmpeg',
            'control_port': int(settings.get('camera_control_port') or 6000),
            'reported_fps': int(settings.get('camera_reported_fps') or 25),
            'reported_bitrate_bps': int(settings.get('camera_reported_bitrate_bps') or 2_000_000),
            'srt_latency_ms': max(80, min(2000, int(settings.get('camera_srt_latency_ms') or 200))),
            'tapo_talkback_enabled': bool(settings.get('camera_tapo_talkback_enabled', False)),
            'auto_failover_enabled': bool(settings.get('camera_auto_failover_enabled', False)),
            'primary_camera': 2 if int(settings.get('camera_primary_camera') or 1) == 2 else 1,
            'failover_after_failures': max(1, int(settings.get('camera_failover_after_failures') or 3)),
            'failover_probe_interval_sec': max(2.0, float(settings.get('camera_failover_probe_interval_sec') or 10.0)),
            'return_to_primary': bool(settings.get('camera_return_to_primary', True)),
            'video_watchdog_enabled': bool(settings.get('camera_video_watchdog_enabled', True)),
            'video_watchdog_timeout_sec': max(3.0, float(settings.get('camera_video_watchdog_timeout_sec') or 8.0)),
            'video_watchdog_startup_grace_sec': max(3.0, float(settings.get('camera_video_watchdog_startup_grace_sec') or 12.0)),
        }

    def _run(self) -> None:
        while not self._stop.is_set():
            cfg = self._snapshot_config()
            active = bool(cfg.get('enabled') or cfg.get('remote_control_enabled') or cfg.get('tapo_talkback_enabled'))
            if not active:
                self._stop_ffmpeg()
                self._sleep(1.0)
                continue
            if not cfg.get('server_url') or not cfg.get('device_id'):
                self._set_error('Server URL and device ID are required')
                self._stop_ffmpeg()
                self._sleep(1.0)
                continue
            if cfg.get('enabled') and not self._active_rtsp_url(cfg):
                self._set_error('RTSP URL is required while video relay is enabled')
                self._stop_ffmpeg()
                self._sleep(1.0)
                continue

            try:
                if not self._registered or self._video_port is None:
                    self._register(cfg)
                if cfg.get('enabled') and self._video_requested:
                    self._maintain_video(cfg)
                else:
                    self._stop_ffmpeg()
                if time.time() - self._last_telemetry_at >= 1.0:
                    self._send_telemetry(cfg)
            except Exception as exc:
                self._set_error(str(exc))
                self._log(f'DEVICE: {exc}')
                with self._lock:
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
        requested_transport = 'srt' if cfg.get('enabled') else 'rtp'
        payload = {
            'name': f'Raspberry Pi RobotLiDAR ({cfg["device_id"]})',
            'ip': local_ip,
            'rtp_port': 5004,
            'ptz_port': cfg['control_port'],
            'device_type': 'raspberry_robotlidar',
            'video_transport': requested_transport,
            'talkback': bool(cfg.get('tapo_talkback_enabled')),
        }
        data = self._json_request(
            'POST',
            f'{cfg["server_url"]}/api/devices/{urllib.parse.quote(cfg["device_id"], safe="")}/register',
            payload,
        )
        rtp_port = int(data.get('video_ingest_port') or 0)
        if not 1 <= rtp_port <= 65535:
            raise RuntimeError('Central server did not return video_ingest_port')

        transport = str(data.get('video_transport') or 'rtp').strip().lower()
        srt_port = int(data.get('srt_ingest_port') or 0)
        if requested_transport == 'srt' and transport == 'srt' and not 1 <= srt_port <= 65535:
            raise RuntimeError('Central server selected SRT but did not return srt_ingest_port')
        if transport not in ('rtp', 'srt'):
            transport = 'rtp'

        with self._lock:
            self._video_port = rtp_port
            self._srt_port = srt_port if transport == 'srt' else None
            self._transport = transport
            self._registered = True
            self._last_register_at = time.time()
            self._last_error = ''

        if transport == 'srt':
            self._log(
                f'DEVICE: registered {cfg["device_id"]}; control UDP {cfg["control_port"]}; '
                f'SRT {srt_port} -> server RTP {rtp_port}'
            )
        else:
            self._log(
                f'DEVICE: registered {cfg["device_id"]}; control UDP {cfg["control_port"]}; '
                f'legacy RTP {rtp_port}'
            )

    def _ensure_ffmpeg(self, cfg: dict[str, Any]) -> None:
        with self._lock:
            process = self._process
            rtp_port = self._video_port
            srt_port = self._srt_port
            transport = self._transport
        if process is not None and process.poll() is None:
            return
        if rtp_port is None:
            return
        server_host = urllib.parse.urlparse(cfg['server_url']).hostname
        if not server_host:
            raise RuntimeError('Invalid central server URL')

        common = [
            cfg['ffmpeg'],
            '-hide_banner', '-loglevel', 'warning',
            '-fflags', '+genpts+discardcorrupt',
            '-use_wallclock_as_timestamps', '1',
            '-rtsp_transport', 'tcp',
            '-i', self._active_rtsp_url(cfg),
            '-map', '0:v:0', '-an',
            '-c:v', 'copy',
            '-progress', 'pipe:1', '-nostats',
            '-bsf:v', 'dump_extra=freq=keyframe',
        ]

        if transport == 'srt':
            if srt_port is None:
                raise RuntimeError('SRT selected without ingest port')
            latency_us = int(cfg['srt_latency_ms']) * 1000
            target = (
                f'srt://{server_host}:{srt_port}?mode=caller&transtype=live&'
                f'latency={latency_us}&pkt_size=1316'
            )
            command = common + [
                '-mpegts_flags', '+resend_headers',
                '-avoid_negative_ts', 'make_non_negative',
                '-muxdelay', '0',
                '-f', 'mpegts', target,
            ]
            self._log(
                f'CAMERA: Camera {cfg["active_camera"]} {self._active_camera_name(cfg)} -> '
                f'RTSP/TCP -> H264 copy -> SRT/MPEG-TS {server_host}:{srt_port} '
                f'latency={cfg["srt_latency_ms"]}ms'
            )
        else:
            target = f'rtp://{server_host}:{rtp_port}?pkt_size=1200'
            command = common + ['-f', 'rtp', target]
            self._log(f'CAMERA: starting legacy RTSP passthrough -> RTP {server_host}:{rtp_port}')

        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        with self._lock:
            self._process = process
            self._process_started_at = time.monotonic()
            self._progress_at = 0.0
            self._progress_value = -1
            self._restart_count += 1
        threading.Thread(target=self._read_ffmpeg_progress, args=(process,), daemon=True).start()
        threading.Thread(
            target=self._read_ffmpeg_stderr,
            args=(process,),
            name='robotlidar-camera-ffmpeg-log',
            daemon=True,
        ).start()

    def _read_ffmpeg_progress(self, process: subprocess.Popen) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            line = line.strip()
            if not line.startswith(('out_time_us=', 'out_time_ms=')):
                continue
            try:
                value = int(line.partition('=')[2])
            except ValueError:
                continue
            with self._lock:
                if self._process is process and value > self._progress_value:
                    self._progress_value = value
                    self._progress_at = time.monotonic()

    def _maintain_video(self, cfg: dict[str, Any]) -> None:
        now = time.monotonic()
        with self._lock:
            process = self._process
            started = self._process_started_at
            progress_at = self._progress_at
            retry_at = self._retry_at
        if process is not None and process.poll() is None:
            grace = cfg['video_watchdog_startup_grace_sec']
            timeout = cfg['video_watchdog_timeout_sec']
            if (cfg['video_watchdog_enabled'] and now - started > grace
                    and now - (progress_at or started) > timeout):
                self._log('CAMERA: FFmpeg video progress stalled; restarting')
                self._stop_ffmpeg()
                self._record_camera_failure(cfg)
                return
            if progress_at and now - progress_at < timeout and now - started > grace:
                with self._lock:
                    self._camera_failures = 0
                    self._retry_step = 0
                    self._retry_at = 0.0
                    self._last_video_ok_at = now
                self._probe_primary(cfg, now)
            return
        if process is not None:
            self._log(f'CAMERA: FFmpeg exited with code {process.returncode}')
            self._stop_ffmpeg()
            self._record_camera_failure(cfg)
            return
        if now < retry_at:
            return
        self._ensure_ffmpeg(self._snapshot_config())

    def _record_camera_failure(self, cfg: dict[str, Any]) -> None:
        with self._lock:
            self._camera_failures += 1
            failures = self._camera_failures
            active = int(self._config.get('active_camera') or 1)
        if cfg['auto_failover_enabled'] and failures >= cfg['failover_after_failures']:
            other = 1 if active == 2 else 2
            url = self._camera_rtsp_url(cfg, other)
            if url and self._rtsp_available(cfg, url, timeout_sec=2.0):
                self._switch_camera_runtime(other, 'FAILOVER')
                return
        delays = (2.0, 5.0, 10.0, 30.0)
        with self._lock:
            delay = delays[min(self._retry_step, len(delays) - 1)]
            self._retry_step = min(self._retry_step + 1, len(delays) - 1)
            # Probing a reserve can block; start the full delay after it returns.
            self._retry_at = time.monotonic() + delay
        self._log(f'CAMERA: restart backoff {delay:.0f}s')

    def _switch_camera_runtime(self, camera: int, reason: str) -> None:
        with self._lock:
            self._config['active_camera'] = camera
            self._camera_failures = 0
            self._retry_step = 0
            self._retry_at = 0.0
            self._next_camera_probe_at = time.monotonic() + self._config['failover_probe_interval_sec']
        self._log(f'CAMERA: {reason} -> Camera {camera}')
        self._stop_ffmpeg()
        self._wake.set()

    def _probe_primary(self, cfg: dict[str, Any], now: float) -> None:
        if not (cfg['auto_failover_enabled'] and cfg['return_to_primary']):
            return
        primary = cfg['primary_camera']
        with self._lock:
            if int(self._config.get('active_camera') or 1) == primary or now < self._next_camera_probe_at:
                return
            self._next_camera_probe_at = now + cfg['failover_probe_interval_sec']
        url = self._camera_rtsp_url(cfg, primary)
        if url and self._rtsp_available(cfg, url, timeout_sec=2.0):
            self._switch_camera_runtime(primary, 'FAILBACK')

    def set_video_requested(self, enabled: bool) -> tuple[bool, str]:
        enabled = bool(enabled)
        with self._lock:
            changed = self._video_requested != enabled
            self._video_requested = enabled
        if changed:
            self._log('CAMERA: viewer demand -> ' + ('ON' if enabled else 'OFF'))
            if not enabled:
                self._stop_ffmpeg()
            self._wake.set()
        return True, 'video demand enabled' if enabled else 'video demand disabled'

    def _camera_rtsp_url(self, cfg: dict[str, Any], camera: int) -> str:
        if int(camera) == 2:
            return str(cfg.get('camera2_url') or '').strip()
        return str(cfg.get('camera1_url') or cfg.get('rtsp_url') or '').strip()

    def _rtsp_available(self, cfg: dict[str, Any], url: str, timeout_sec: float = 4.0) -> bool:
        if not url:
            return False
        command = [
            str(cfg.get('ffmpeg') or 'ffmpeg'),
            '-hide_banner', '-loglevel', 'error',
            '-rtsp_transport', 'tcp',
            '-timeout', str(int(timeout_sec * 1_000_000)),
            '-i', url,
            '-map', '0:v:0',
            '-t', '0.15',
            '-c:v', 'copy',
            '-f', 'null', '-',
        ]
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=timeout_sec + 1.5,
                text=True,
            )
            if result.returncode == 0:
                return True
            detail = (result.stderr or '').strip().replace('\n', ' ')
            if len(detail) > 240:
                detail = detail[-240:]
            self._log('CAMERA: probe failed: ' + (detail or 'ffmpeg returned an error'))
        except subprocess.TimeoutExpired:
            self._log(f'CAMERA: probe timeout after {timeout_sec:.1f}s')
        except Exception as exc:
            self._log(f'CAMERA: probe error: {exc}')
        return False

    def select_camera(self, camera: int) -> tuple[bool, str]:
        camera = 2 if int(camera) == 2 else 1
        with self._lock:
            cfg = dict(self._config)
            current = int(cfg.get('active_camera') or 1)
        url = self._camera_rtsp_url(cfg, camera)
        if camera == 2 and not url:
            return False, 'Camera 2 RTSP URL is empty'
        if current == camera:
            return True, f'Camera {camera} already active'

        self._log(f'CAMERA: probe before switch -> Camera {camera}')
        if not self._rtsp_available(cfg, url):
            self._log(f'CAMERA: Camera {camera} unavailable; keeping Camera {current}')
            return False, f'Camera {camera} is unavailable; Camera {current} remains active'

        with self._lock:
            self._config['active_camera'] = camera
            self._camera_failures = 0
            self._retry_at = 0.0
            self._retry_step = 0
        self._log(f'CAMERA: switch request -> Camera {camera}')
        self._stop_ffmpeg()
        self._wake.set()
        return True, f'Camera {camera} selected'

    @staticmethod
    def _active_rtsp_url(cfg: dict[str, Any]) -> str:
        if int(cfg.get('active_camera') or 1) == 2 and str(cfg.get('camera2_url') or '').strip():
            return str(cfg.get('camera2_url') or '').strip()
        return str(cfg.get('camera1_url') or cfg.get('rtsp_url') or '').strip()

    @staticmethod
    def _active_camera_name(cfg: dict[str, Any]) -> str:
        if int(cfg.get('active_camera') or 1) == 2:
            return str(cfg.get('camera2_name') or 'Camera 2')
        return str(cfg.get('camera1_name') or 'Camera 1')

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
        video_ok = bool(cfg.get('enabled')) and process is not None and process.poll() is None
        payload = {
            'fps': cfg['reported_fps'] if video_ok else 0,
            'bitrate_bps': cfg['reported_bitrate_bps'] if video_ok else 0,
            'dropped_frames': 0,
            'uptime_ms': int((time.monotonic() - self._started_at) * 1000),
            'link_mbps': self._ethernet_speed_mbps(),
            'active_camera': int(cfg.get('active_camera') or 1),
        }

        now = time.time()
        if now - self._last_gps_sent_at >= 60.0:
            gps = self._read_gps_state()
            if gps and gps.get('valid'):
                payload.update({
                    'gps_valid': True,
                    'gps_latitude': gps['latitude'],
                    'gps_longitude': gps['longitude'],
                    'gps_updated_ms': int(float(gps.get('received_at') or now) * 1000),
                })
            else:
                payload['gps_valid'] = False

        try:
            self._json_request(
                'POST',
                f'{cfg["server_url"]}/api/devices/{urllib.parse.quote(cfg["device_id"], safe="")}/telemetry',
                payload,
            )
            with self._lock:
                self._last_telemetry_at = now
                if 'gps_valid' in payload:
                    self._last_gps_sent_at = now
                self._last_error = ''
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                with self._lock:
                    self._registered = False
                    self._video_port = None
                    self._srt_port = None
                self._stop_ffmpeg()
                return
            raise

    @staticmethod
    def _read_gps_state() -> Optional[dict[str, Any]]:
        data_dir = Path(os.environ.get('ROBOTLIDAR_DATA_DIR', '~/robotlidar_data')).expanduser()
        path = data_dir / 'state' / 'last_position.json'
        try:
            snap = json.loads(path.read_text(encoding='utf-8'))
            gps = snap.get('gps') if isinstance(snap, dict) else None
            if not isinstance(gps, dict) or not gps.get('valid'):
                return None
            lat = float(gps.get('latitude'))
            lon = float(gps.get('longitude'))
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                return None
            received_at = float(gps.get('received_at') or 0.0)
            if received_at <= 0 or time.time() - received_at > 120.0:
                return None
            return {
                'valid': True,
                'latitude': lat,
                'longitude': lon,
                'received_at': received_at,
            }
        except Exception:
            return None

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
