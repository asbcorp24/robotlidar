#!/usr/bin/env python3
"""Remote control gateway for central RobotLiDAR server -> Raspberry Pi.

Receives the same 16-byte UDP packets used by the Orange Pi gateway and routes:
  type 1 -> ONVIF AbsoluteMove for the IP camera
  type 2 -> ROS /cmd_vel for the ESP32 track bridge
  type 3 -> ROS /brush/command and /actuator/command

The ESP32 serial port is intentionally not opened here: esp32_track_bridge_node
already owns it and performs the watchdog/telemetry protocol.
"""
from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import threading
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from xml.sax.saxutils import escape

from geometry_msgs.msg import Twist
from std_msgs.msg import Float32, Int8

CONTROL_MAGIC = 0x5354
CONTROL_VERSION = 1
TYPE_PTZ = 1
TYPE_DRIVE = 2
TYPE_BRUSH = 3
FLAG_CENTER = 1 << 0


class RemoteControlGateway:
    def __init__(
        self,
        node,
        arm_callback: Optional[Callable[[bool, float], tuple[bool, str]]] = None,
        log_callback=None,
    ) -> None:
        self._node = node
        self._arm_callback = arm_callback
        self._log_callback = log_callback
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._socket: Optional[socket.socket] = None
        self._listener: Optional[threading.Thread] = None
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

        self._cmd_pub = node.create_publisher(Twist, '/cmd_vel', 20)
        self._brush_pub = node.create_publisher(Float32, '/brush/command', 20)
        self._actuator_pub = node.create_publisher(Int8, '/actuator/command', 20)

    def start(self, settings: dict[str, Any]) -> None:
        self.configure(settings)
        with self._lock:
            if self._listener and self._listener.is_alive():
                return
            self._listener = threading.Thread(target=self._listen_loop, name='remote-control-udp', daemon=True)
            self._watchdog = threading.Thread(target=self._watchdog_loop, name='remote-control-watchdog', daemon=True)
            self._listener.start()
            self._watchdog.start()

    def configure(self, settings: dict[str, Any]) -> None:
        cfg = {
            'enabled': bool(settings.get('camera_remote_control_enabled', True)),
            'port': int(settings.get('camera_control_port') or 6000),
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
            self._config = cfg
        if old_port is not None and old_port != cfg['port']:
            self._close_socket()
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._close_socket()
        self._publish_drive(0, 0)
        self._publish_aux(0, 0)

    def status(self) -> dict[str, Any]:
        with self._lock:
            cfg = dict(self._config)
            return {
                'enabled': bool(cfg.get('enabled')),
                'listen_port': cfg.get('port', 6000),
                'packet_age_sec': round(time.monotonic() - self._last_packet_at, 3) if self._last_packet_at else None,
                'last_seq': self._last_seq,
                'drive': {'left': self._drive[0], 'right': self._drive[1]},
                'brush': {'spin': self._brush[0], 'lift': self._brush[1]},
                'ptz': {'pan_cdeg': self._pan_cdeg, 'tilt_cdeg': self._tilt_cdeg},
                'onvif_configured': bool(cfg.get('onvif_url')),
                'last_error': self._last_error,
            }

    def _listen_loop(self) -> None:
        while not self._stop.is_set():
            cfg = self._snapshot()
            if not cfg.get('enabled'):
                self._close_socket()
                self._wake.wait(0.5); self._wake.clear()
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
        self._log(f'CONTROL: listening UDP 0.0.0.0:{port}')
        return sock

    def _close_socket(self) -> None:
        with self._lock:
            sock = self._socket
            self._socket = None
        if sock:
            try: sock.close()
            except OSError: pass

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
            if flags & FLAG_CENTER:
                pan = 0; tilt = 0
            with self._lock:
                self._pan_cdeg = pan
                self._tilt_cdeg = tilt
            threading.Thread(target=self._onvif_move, args=(pan, tilt, int(speed)), daemon=True).start()
        else:
            raise ValueError(f'unknown packet type {packet_type}')

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
        # Current physical brush controller is speed-only; negative is magnitude.
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
            with self._lock:
                drive_age = now - self._last_drive_at if self._last_drive_at else 1e9
                aux_age = now - self._last_aux_at if self._last_aux_at else 1e9
                drive_nonzero = self._drive != (0, 0)
                aux_nonzero = self._brush != (0, 0)
            if drive_nonzero and drive_age > float(cfg.get('drive_watchdog_sec', 0.45)):
                self._publish_drive(0, 0)
                with self._lock: self._drive = (0, 0)
                if not drive_stopped: self._log('CONTROL: drive watchdog -> STOP')
                drive_stopped = True
            elif drive_nonzero:
                drive_stopped = False
            if aux_nonzero and aux_age > float(cfg.get('aux_watchdog_sec', 0.55)):
                self._publish_aux(0, 0)
                with self._lock: self._brush = (0, 0)
                if not aux_stopped: self._log('CONTROL: aux watchdog -> STOP')
                aux_stopped = True
            elif aux_nonzero:
                aux_stopped = False

    def _onvif_move(self, pan_cdeg: int, tilt_cdeg: int, speed_cdeg_s: int) -> None:
        cfg = self._snapshot()
        url = str(cfg.get('onvif_url') or '')
        if not url:
            return
        # Generic ONVIF position space is normalized to [-1, 1].
        pan = max(-1.0, min(1.0, pan_cdeg / 18000.0))
        tilt = max(-1.0, min(1.0, tilt_cdeg / 9000.0))
        speed = max(0.05, min(1.0, abs(speed_cdeg_s) / 9000.0 if speed_cdeg_s else 0.5))
        username = str(cfg.get('onvif_username') or '')
        password = str(cfg.get('onvif_password') or '')
        token = escape(str(cfg.get('onvif_profile_token') or 'Profile_1'))
        security = self._ws_security(username, password) if username else ''
        envelope = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
<s:Header>{security}</s:Header><s:Body><tptz:AbsoluteMove><tptz:ProfileToken>{token}</tptz:ProfileToken><tptz:Position><tt:PanTilt x="{pan:.6f}" y="{tilt:.6f}" space="http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace"/></tptz:Position><tptz:Speed><tt:PanTilt x="{speed:.4f}" y="{speed:.4f}" space="http://www.onvif.org/ver10/tptz/PanTiltSpaces/GenericSpeedSpace"/></tptz:Speed></tptz:AbsoluteMove></s:Body></s:Envelope>'''
        req = urllib.request.Request(url, data=envelope.encode('utf-8'), method='POST', headers={'Content-Type': 'application/soap+xml; charset=utf-8'})
        try:
            with urllib.request.urlopen(req, timeout=2.0) as response:
                response.read(64)
            self._log(f'CONTROL/PTZ: pan={pan_cdeg/100:.1f} tilt={tilt_cdeg/100:.1f}')
        except Exception as exc:
            self._set_error(f'ONVIF: {exc}')
            self._log(f'CONTROL/PTZ: ONVIF error: {exc}')

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
