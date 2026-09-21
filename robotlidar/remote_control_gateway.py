#!/usr/bin/env python3
"""Remote control gateway for central RobotLiDAR server -> Raspberry Pi.

Primary path is an outbound WebSocket opened by Raspberry Pi to the central
server, so remote control works through NAT without UDP port forwarding.
Legacy UDP :6000 remains available as a local/fallback transport.

The same fixed 16-byte binary packet is accepted from both transports:
  type 1 -> ONVIF AbsoluteMove for the IP camera
  type 2 -> ROS /cmd_vel for the ESP32 track bridge
  type 3 -> ROS /brush/command and /actuator/command
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from xml.sax.saxutils import escape

import websocket
from geometry_msgs.msg import Twist
from std_msgs.msg import Float32, Int8

CONTROL_MAGIC = 0x5354
CONTROL_VERSION = 1
TYPE_PTZ = 1
TYPE_DRIVE = 2
TYPE_BRUSH = 3
TYPE_CAMERA = 4
TYPE_STREAM = 5
FLAG_CENTER = 1 << 0


class RemoteControlGateway:
    def __init__(
        self,
        node,
        arm_callback: Optional[Callable[[bool, float], tuple[bool, str]]] = None,
        camera_callback: Optional[Callable[[int], tuple[bool, str]]] = None,
        stream_callback: Optional[Callable[[bool], tuple[bool, str]]] = None,
        log_callback=None,
    ) -> None:
        self._node = node
        self._arm_callback = arm_callback
        self._camera_callback = camera_callback
        self._stream_callback = stream_callback
        self._log_callback = log_callback
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._socket: Optional[socket.socket] = None
        self._ws: Any = None
        self._listener: Optional[threading.Thread] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._watchdog: Optional[threading.Thread] = None
        self._config: dict[str, Any] = {}
        self._last_drive_at = 0.0
        self._last_aux_at = 0.0
        self._last_packet_at = 0.0
        self._last_seq = 0
        self._last_error = ''
        self._drive = (0, 0)
        self._brush = (0, 0)
        self._pan_cdeg = 0
        self._tilt_cdeg = 0
        self._arm_in_progress = False
        self._last_arm_try = 0.0
        self._ws_connected = False
        self._ws_url = ''
        self._ws_reconnects = 0
        self._ptz_move_mode = 'auto'
        self._ptz_home_supported: Optional[bool] = None

        self._cmd_pub = node.create_publisher(Twist, '/cmd_vel', 20)
        self._brush_pub = node.create_publisher(Float32, '/brush/command', 20)
        self._actuator_pub = node.create_publisher(Int8, '/actuator/command', 20)

    def start(self, settings: dict[str, Any]) -> None:
        self.configure(settings)
        with self._lock:
            if self._listener and self._listener.is_alive():
                return
            self._listener = threading.Thread(target=self._listen_loop, name='remote-control-udp', daemon=True)
            self._ws_thread = threading.Thread(target=self._ws_loop, name='remote-control-wss', daemon=True)
            self._watchdog = threading.Thread(target=self._watchdog_loop, name='remote-control-watchdog', daemon=True)
            self._listener.start()
            self._ws_thread.start()
            self._watchdog.start()

    def configure(self, settings: dict[str, Any]) -> None:
        user_control_enabled = bool(settings.get('camera_remote_control_enabled', False))
        cfg = {
            'enabled': user_control_enabled,
            'transport_enabled': bool(user_control_enabled or settings.get('camera_enabled', False)),
            'port': int(settings.get('camera_control_port') or 6000),
            'server_url': str(settings.get('camera_server_url') or '').strip().rstrip('/'),
            'device_id': str(settings.get('camera_device_id') or '').strip(),
            'track_width_m': float(settings.get('camera_remote_track_width_m') or 0.60),
            'max_track_speed_mps': float(settings.get('camera_remote_max_track_speed_mps') or 0.50),
            'drive_watchdog_sec': float(settings.get('camera_remote_drive_watchdog_sec') or 0.45),
            'aux_watchdog_sec': float(settings.get('camera_remote_aux_watchdog_sec') or 0.55),
            'onvif_url': str(settings.get('camera_onvif_url') or '').strip(),
            'onvif_username': str(settings.get('camera_onvif_username') or '').strip(),
            'onvif_password': str(settings.get('camera_onvif_password') or ''),
            'onvif_profile_token': str(settings.get('camera_onvif_profile_token') or 'Profile_1').strip() or 'Profile_1',
        }
        if not 1 <= cfg['port'] <= 65535:
            cfg['port'] = 6000
        with self._lock:
            old_port = self._config.get('port')
            old_ws_key = (self._config.get('server_url'), self._config.get('device_id'), self._config.get('transport_enabled'))
            new_ws_key = (cfg.get('server_url'), cfg.get('device_id'), cfg.get('transport_enabled'))
            old_onvif = (self._config.get('onvif_url'), self._config.get('onvif_username'), self._config.get('onvif_profile_token'))
            new_onvif = (cfg.get('onvif_url'), cfg.get('onvif_username'), cfg.get('onvif_profile_token'))
            self._config = cfg
            if old_onvif != new_onvif:
                self._ptz_move_mode = 'auto'
                self._ptz_home_supported = None
        if old_port is not None and old_port != cfg['port']:
            self._close_socket()
        if old_ws_key != new_ws_key:
            self._close_ws()
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._close_socket()
        self._close_ws()
        self._safe_stop_outputs('gateway stop')

    def status(self) -> dict[str, Any]:
        with self._lock:
            cfg = dict(self._config)
            return {
                'enabled': bool(cfg.get('enabled')),
                'listen_port': cfg.get('port', 6000),
                'control_transport': 'websocket' if self._ws_connected else 'udp-fallback',
                'websocket_connected': self._ws_connected,
                'websocket_url': self._ws_url,
                'websocket_reconnects': self._ws_reconnects,
                'ros_mode_active': self._ros_mode_active(),
                'packet_age_sec': round(time.monotonic() - self._last_packet_at, 3) if self._last_packet_at else None,
                'last_seq': self._last_seq,
                'drive': {'left': self._drive[0], 'right': self._drive[1]},
                'brush': {'spin': self._brush[0], 'lift': self._brush[1]},
                'ptz': {'pan_cdeg': self._pan_cdeg, 'tilt_cdeg': self._tilt_cdeg},
                'camera_switch_supported': self._camera_callback is not None,
                'onvif_configured': bool(cfg.get('onvif_url')),
                'last_error': self._last_error,
            }

    def _ros_mode_active(self) -> bool:
        """Return True only while a ROS mapping/navigation launch is running."""
        try:
            from robotlidar import web_app
            state = web_app.process_manager.status()
            return bool(state.get('process_running')) and state.get('mode') in ('mapping', 'navigation')
        except Exception:
            return False

    def _ws_loop(self) -> None:
        while not self._stop.is_set():
            cfg = self._snapshot()
            if not cfg.get('transport_enabled') or not cfg.get('server_url') or not cfg.get('device_id'):
                self._set_ws_state(False, '')
                self._wake.wait(0.5)
                self._wake.clear()
                continue

            url = self._control_ws_url(str(cfg['server_url']), str(cfg['device_id']))
            self._set_ws_state(False, url)
            try:
                ws = websocket.create_connection(
                    url,
                    timeout=8,
                    enable_multithread=True,
                )
                ws.settimeout(10)
                with self._lock:
                    self._ws = ws
                    self._ws_connected = True
                    self._ws_url = url
                    self._ws_reconnects += 1
                    self._last_error = ''
                self._log(f'CONTROL/WSS: connected {url}')

                while not self._stop.is_set():
                    try:
                        message = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        ws.ping('robotlidar')
                        continue
                    if message is None:
                        raise RuntimeError('WebSocket closed by server')
                    if isinstance(message, str):
                        continue
                    data = bytes(message)
                    self._handle_packet(data)
            except Exception as exc:
                if not self._stop.is_set():
                    self._set_error(f'WSS: {exc}')
                    self._log(f'CONTROL/WSS: disconnected: {exc}')
            finally:
                self._close_ws()
                self._safe_stop_outputs('control WebSocket disconnected')

            self._wake.wait(1.0)
            self._wake.clear()

    @staticmethod
    def _control_ws_url(server_url: str, device_id: str) -> str:
        parsed = urllib.parse.urlparse(server_url)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            raise ValueError('Invalid central server URL for WebSocket')
        scheme = 'wss' if parsed.scheme == 'https' else 'ws'
        base_path = parsed.path.rstrip('/')
        path = f'{base_path}/api/devices/{urllib.parse.quote(device_id, safe="")}/control-ws'
        return urllib.parse.urlunparse((scheme, parsed.netloc, path, '', '', ''))

    def _set_ws_state(self, connected: bool, url: str) -> None:
        with self._lock:
            self._ws_connected = connected
            if url:
                self._ws_url = url

    def _close_ws(self) -> None:
        with self._lock:
            ws = self._ws
            self._ws = None
            self._ws_connected = False
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

    def _listen_loop(self) -> None:
        while not self._stop.is_set():
            cfg = self._snapshot()
            if not cfg.get('transport_enabled'):
                self._close_socket()
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            try:
                sock = self._ensure_socket(int(cfg['port']))
                sock.settimeout(0.5)
                data, _addr = sock.recvfrom(256)
            except socket.timeout:
                continue
            except OSError as exc:
                if self._stop.is_set():
                    break
                self._set_error(str(exc))
                time.sleep(0.5)
                continue
            try:
                self._handle_packet(data)
            except Exception as exc:
                self._set_error(str(exc))
                self._log(f'CONTROL: rejected packet: {exc}')

    def _ensure_socket(self, port: int) -> socket.socket:
        with self._lock:
            if self._socket is not None:
                return self._socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(('0.0.0.0', port))
            self._socket = sock
        self._log(f'CONTROL: legacy UDP listening 0.0.0.0:{port}')
        return sock

    def _close_socket(self) -> None:
        with self._lock:
            sock = self._socket
            self._socket = None
        if sock:
            try:
                sock.close()
            except OSError:
                pass

    def _handle_packet(self, data: bytes) -> None:
        if len(data) != 16:
            raise ValueError(f'packet size {len(data)}, expected 16')
        magic, version, packet_type, seq, value1, value2, speed, flags = struct.unpack('>HBBIhhHH', data)
        if magic != CONTROL_MAGIC or version != CONTROL_VERSION:
            raise ValueError('bad control header')
        now = time.monotonic()
        with self._lock:
            self._last_packet_at = now
            self._last_seq = int(seq)
            self._last_error = ''

        if packet_type == TYPE_DRIVE:
            left = max(-1000, min(1000, int(value1)))
            right = max(-1000, min(1000, int(value2)))
            with self._lock:
                self._last_drive_at = now
                self._drive = (left, right)
            if left or right:
                self._request_arm()
            self._publish_drive(left, right)
        elif packet_type == TYPE_BRUSH:
            spin = max(-1000, min(1000, int(value1)))
            lift = max(-1000, min(1000, int(value2)))
            with self._lock:
                self._last_aux_at = now
                self._brush = (spin, lift)
            if spin or lift:
                self._request_arm()
            self._publish_aux(spin, lift)
        elif packet_type == TYPE_PTZ:
            pan = int(value1)
            tilt = int(value2)
            center = bool(flags & FLAG_CENTER)
            if center:
                pan = 0
                tilt = 0
            with self._lock:
                prev_pan = self._pan_cdeg
                prev_tilt = self._tilt_cdeg
                self._pan_cdeg = pan
                self._tilt_cdeg = tilt
            self._log(f'CONTROL/PTZ: received seq={seq} pan={pan/100:.1f} tilt={tilt/100:.1f} speed={int(speed)/100:.1f}')
            threading.Thread(
                target=self._onvif_move,
                args=(pan, tilt, int(speed), pan - prev_pan, tilt - prev_tilt, center),
                daemon=True,
            ).start()
        elif packet_type == TYPE_CAMERA:
            camera = 2 if int(value1) == 2 else 1
            callback = self._camera_callback
            if callback is None:
                raise ValueError('camera switch callback is not configured')
            ok, message = callback(camera)
            if not ok:
                raise ValueError(message)
            self._log(f'CONTROL/CAMERA: {message}')
        elif packet_type == TYPE_STREAM:
            callback = self._stream_callback
            if callback is None:
                raise ValueError('stream demand callback is not configured')
            ok, message = callback(int(value1) != 0)
            if not ok:
                raise ValueError(message)
            self._log(f'CONTROL/STREAM: {message}')
        else:
            raise ValueError(f'unknown packet type {packet_type}')

    def _safe_stop_outputs(self, reason: str) -> None:
        with self._lock:
            had_drive = self._drive != (0, 0)
            had_aux = self._brush != (0, 0)
            self._drive = (0, 0)
            self._brush = (0, 0)
        if not self._ros_mode_active():
            if had_drive or had_aux:
                self._log(f'CONTROL: {reason}; STOP suppressed because ROS mode is not active')
            return
        self._publish_drive(0, 0)
        self._publish_aux(0, 0)
        if had_drive or had_aux:
            self._log(f'CONTROL: {reason} -> STOP (ROS mode)')

    def _request_arm(self) -> None:
        callback = self._arm_callback
        if callback is None:
            return
        with self._lock:
            now = time.monotonic()
            if self._arm_in_progress or now - self._last_arm_try < 1.0:
                return
            self._arm_in_progress = True
            self._last_arm_try = now

        def worker() -> None:
            try:
                ok, msg = callback(True, 2.0)
                if not ok:
                    self._log(f'CONTROL: ARM rejected: {msg}')
            except Exception as exc:
                self._log(f'CONTROL: ARM error: {exc}')
            finally:
                with self._lock:
                    self._arm_in_progress = False

        threading.Thread(target=worker, name='remote-control-arm', daemon=True).start()

    def _publish_drive(self, left: int, right: int) -> None:
        cfg = self._snapshot()
        max_speed = max(0.01, float(cfg.get('max_track_speed_mps', 0.50)))
        width = max(0.05, float(cfg.get('track_width_m', 0.60)))
        vl = max_speed * max(-1.0, min(1.0, left / 1000.0))
        vr = max_speed * max(-1.0, min(1.0, right / 1000.0))
        msg = Twist()
        msg.linear.x = (vl + vr) / 2.0
        msg.angular.z = (vr - vl) / width
        self._cmd_pub.publish(msg)

    def _publish_aux(self, spin: int, lift: int) -> None:
        brush = Float32()
        brush.data = min(1.0, abs(float(spin)) / 1000.0)
        actuator = Int8()
        actuator.data = 1 if lift > 0 else (-1 if lift < 0 else 0)
        self._brush_pub.publish(brush)
        self._actuator_pub.publish(actuator)

    def _watchdog_loop(self) -> None:
        drive_stopped = True
        aux_stopped = True
        while not self._stop.wait(0.05):
            cfg = self._snapshot()
            now = time.monotonic()
            ros_active = self._ros_mode_active()
            with self._lock:
                drive_age = now - self._last_drive_at if self._last_drive_at else 1e9
                aux_age = now - self._last_aux_at if self._last_aux_at else 1e9
                drive_nonzero = self._drive != (0, 0)
                aux_nonzero = self._brush != (0, 0)

            if drive_nonzero and drive_age > float(cfg.get('drive_watchdog_sec', 0.45)):
                if ros_active:
                    self._publish_drive(0, 0)
                with self._lock:
                    self._drive = (0, 0)
                if not drive_stopped:
                    if ros_active:
                        self._log('CONTROL: drive watchdog -> STOP (ROS mode)')
                    else:
                        self._log('CONTROL: drive watchdog expired; STOP suppressed outside ROS mode')
                drive_stopped = True
            elif drive_nonzero:
                drive_stopped = False

            if aux_nonzero and aux_age > float(cfg.get('aux_watchdog_sec', 0.55)):
                if ros_active:
                    self._publish_aux(0, 0)
                with self._lock:
                    self._brush = (0, 0)
                if not aux_stopped:
                    if ros_active:
                        self._log('CONTROL: aux watchdog -> STOP (ROS mode)')
                    else:
                        self._log('CONTROL: aux watchdog expired; STOP suppressed outside ROS mode')
                aux_stopped = True
            elif aux_nonzero:
                aux_stopped = False

    def _onvif_post(self, cfg: dict[str, Any], body: str, timeout: float = 2.5) -> None:
        url = str(cfg.get('onvif_url') or '')
        if not url:
            raise RuntimeError('ONVIF URL is not configured')
        username = str(cfg.get('onvif_username') or '')
        password = str(cfg.get('onvif_password') or '')
        security = self._ws_security(username, password) if username else ''
        envelope = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
<s:Header>{security}</s:Header><s:Body>{body}</s:Body></s:Envelope>'''
        req = urllib.request.Request(url, data=envelope.encode('utf-8'), method='POST',
                                     headers={'Content-Type': 'application/soap+xml; charset=utf-8'})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                response.read(256)
        except urllib.error.HTTPError as exc:
            detail = ''
            try:
                detail = exc.read(1000).decode('utf-8', 'ignore').replace('\n', ' ').strip()
            except Exception:
                pass
            reason = detail or str(exc.reason)
            if 'ServiceNotSupported' in reason or 'Service Not Supported' in reason:
                reason = 'ServiceNotSupported'
            elif 'ActionNotSupported' in reason or 'Action Not Supported' in reason:
                reason = 'ActionNotSupported'
            elif len(reason) > 220:
                reason = reason[:220] + '...'
            raise RuntimeError(f'HTTP {exc.code}: {reason}') from exc

    def local_ptz(self, direction: str, speed: float = 0.35) -> tuple[bool, str]:
        cfg = self._snapshot()
        if not str(cfg.get('onvif_url') or ''):
            return False, 'ONVIF URL не настроен'
        token = escape(str(cfg.get('onvif_profile_token') or 'Profile_1'))
        speed = max(0.05, min(1.0, float(speed)))
        direction = str(direction or '').lower()

        if direction == 'home':
            if self._ptz_home_supported is False:
                return False, 'Home не поддерживается этой камерой'
            body = f'''<tptz:GotoHomePosition><tptz:ProfileToken>{token}</tptz:ProfileToken><tptz:Speed><tt:PanTilt x="{speed:.3f}" y="{speed:.3f}"/></tptz:Speed></tptz:GotoHomePosition>'''
            try:
                self._onvif_post(cfg, body)
                self._ptz_home_supported = True
                return True, 'Home'
            except Exception as exc:
                if 'ServiceNotSupported' in str(exc) or 'ActionNotSupported' in str(exc):
                    self._ptz_home_supported = False
                    return False, 'Home не поддерживается этой камерой'
                return False, str(exc)

        vx = 0.0
        vy = 0.0
        if direction == 'left':
            vx = -speed
        elif direction == 'right':
            vx = speed
        elif direction == 'up':
            vy = speed
        elif direction == 'down':
            vy = -speed
        else:
            return False, 'Неизвестное направление PTZ'

        move = f'''<tptz:ContinuousMove><tptz:ProfileToken>{token}</tptz:ProfileToken><tptz:Velocity><tt:PanTilt x="{vx:.3f}" y="{vy:.3f}"/></tptz:Velocity></tptz:ContinuousMove>'''
        stop = f'''<tptz:Stop><tptz:ProfileToken>{token}</tptz:ProfileToken><tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>'''
        try:
            self._onvif_post(cfg, move)
            time.sleep(0.22)
            self._onvif_post(cfg, stop)
            self._ptz_move_mode = 'continuous'
            return True, f'{direction} {speed:.2f}'
        except Exception as exc:
            try:
                self._onvif_post(cfg, stop)
            except Exception:
                pass
            return False, str(exc)

    def _onvif_move(self, pan_cdeg: int, tilt_cdeg: int, speed_cdeg_s: int,
                    delta_pan_cdeg: int = 0, delta_tilt_cdeg: int = 0, center: bool = False) -> None:
        cfg = self._snapshot()
        if not str(cfg.get('onvif_url') or ''):
            self._log(f'CONTROL/PTZ: received pan={pan_cdeg/100:.1f} tilt={tilt_cdeg/100:.1f}; ONVIF URL is not configured')
            return
        token = escape(str(cfg.get('onvif_profile_token') or 'Profile_1'))
        speed = max(0.05, min(1.0, abs(speed_cdeg_s) / 9000.0 if speed_cdeg_s else 0.5))

        if center:
            ok, message = self.local_ptz('home', speed)
            if ok:
                self._log('CONTROL/PTZ: GotoHomePosition OK')
            else:
                self._log(f'CONTROL/PTZ: {message}')
            return

        if not (delta_pan_cdeg or delta_tilt_cdeg):
            return

        # Prefer the camera-compatible ContinuousMove path. It also works on
        # inexpensive ONVIF PTZ cameras that reject AbsoluteMove/RelativeMove.
        vx = speed if delta_pan_cdeg > 0 else (-speed if delta_pan_cdeg < 0 else 0.0)
        vy = speed if delta_tilt_cdeg > 0 else (-speed if delta_tilt_cdeg < 0 else 0.0)
        move = f'''<tptz:ContinuousMove><tptz:ProfileToken>{token}</tptz:ProfileToken><tptz:Velocity><tt:PanTilt x="{vx:.4f}" y="{vy:.4f}"/></tptz:Velocity></tptz:ContinuousMove>'''
        stop = f'''<tptz:Stop><tptz:ProfileToken>{token}</tptz:ProfileToken><tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>'''
        try:
            self._onvif_post(cfg, move)
            time.sleep(0.22)
            self._onvif_post(cfg, stop)
            self._ptz_move_mode = 'continuous'
            self._log(f'CONTROL/PTZ: ContinuousMove OK vx={vx:.2f} vy={vy:.2f}')
        except Exception as exc:
            self._set_error(f'ONVIF: {exc}')
            self._log(f'CONTROL/PTZ: ContinuousMove error: {exc}')
            try:
                self._onvif_post(cfg, stop)
            except Exception:
                pass

    @staticmethod
    def _ws_security(username: str, password: str) -> str:
        nonce = os.urandom(16)
        created = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
        digest = hashlib.sha1(nonce + created.encode('utf-8') + password.encode('utf-8')).digest()
        return f'''<wsse:Security s:mustUnderstand="1"><wsse:UsernameToken><wsse:Username>{escape(username)}</wsse:Username><wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{base64.b64encode(digest).decode()}</wsse:Password><wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</wsse:Nonce><wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>'''

    def _snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._config)

    def _set_error(self, text: str) -> None:
        with self._lock:
            self._last_error = text

    def _log(self, text: str) -> None:
        if self._log_callback:
            try:
                self._log_callback(text)
                return
            except Exception:
                pass
        print(text, flush=True)
