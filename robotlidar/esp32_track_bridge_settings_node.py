#!/usr/bin/env python3
"""ESP32 track bridge with persistent runtime configuration transport."""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Optional

import rclpy
from std_msgs.msg import String

from robotlidar.esp32_track_bridge_node import Esp32TrackBridgeNode


CFG_MAGIC = 0xC6000000
CFG_GET = CFG_MAGIC
CFG_SET = CFG_MAGIC | 0x00400000
CFG_RESET = CFG_MAGIC | 0x00800000

CFG_KEYS = {
    'us_enabled': 1,
    'us_warn_mm': 2,
    'us_stop_mm': 3,
    'us_emergency_mm': 4,
    'us_clear_mm': 5,
    'us_danger_samples': 6,
    'us_clear_samples': 7,
    'us_sample_ms': 8,
    'hall_enabled': 16,
    'hall_left_inverted': 17,
    'hall_right_inverted': 18,
    'hall_ppr': 19,
    'wheel_circ_mm': 20,
    'track_width_mm': 21,
}


class Esp32TrackBridgeSettingsNode(Esp32TrackBridgeNode):
    def __init__(self) -> None:
        self._config_state: dict = {}
        self._config_publisher = None
        self._config_queue: deque[int] = deque()
        self._config_pause_until = 0.0
        super().__init__()
        self._config_publisher = self.create_publisher(String, '/esp32/config/state', 10)
        self.create_subscription(String, '/esp32/config/request', self._config_request_callback, 10)
        self.create_timer(0.12, self._config_tick)
        self._queue_config_sequence(CFG_GET)
        self.get_logger().info('ESP32 persistent settings transport enabled')

    def _process_line(self, line: str) -> None:
        if line and '*' in line:
            body, checksum_text = line.rsplit('*', 1)
            try:
                valid = len(checksum_text) == 2 and int(checksum_text, 16) == self._checksum(body)
            except ValueError:
                valid = False
            if valid and body.startswith('CFG,'):
                self._handle_config_frame(body.split(','))
                return
        super()._process_line(line)

    def _handle_config_frame(self, fields: list[str]) -> None:
        if len(fields) < 4:
            return
        state = {
            'connected': True,
            'millis': int(fields[1]),
            'version': int(fields[2]),
        }
        for item in fields[3:]:
            if '=' not in item:
                continue
            key, raw = item.split('=', 1)
            try:
                value = int(raw)
            except ValueError:
                value = raw
            if key in ('us_enabled', 'hall_enabled', 'hall_left_inverted', 'hall_right_inverted'):
                value = bool(int(value))
            state[key] = value
        self._config_state = state
        if self._config_publisher is not None:
            message = String()
            message.data = json.dumps(state, ensure_ascii=False)
            self._config_publisher.publish(message)

    @staticmethod
    def _normalize_config_value(key: str, value) -> int:
        if key in ('us_enabled', 'hall_enabled', 'hall_left_inverted', 'hall_right_inverted'):
            return 1 if bool(value) else 0
        return max(0, min(65535, int(value)))

    def _queue_config_sequence(self, sequence: int) -> None:
        self._config_queue.append(int(sequence) & 0xFFFFFFFF)

    def _config_tick(self) -> None:
        if not self._config_queue:
            return
        sequence = self._config_queue.popleft()
        # Give the ESP32 one short quiet interval so main.cpp leaves this PING
        # sequence visible to serialEventRun/settings_controller.cpp. 50 ms is
        # well below the 450 ms drive watchdog and normal drive traffic resumes
        # between configuration packets.
        self._config_pause_until = time.monotonic() + 0.05
        if not self._write_body(f'PING,{sequence}'):
            self.get_logger().warning('ESP32 config packet was not sent')

    def _send_tick(self) -> None:
        if time.monotonic() < self._config_pause_until:
            return
        super()._send_tick()

    def _encoded_set_sequence(self, key: str, value) -> int:
        key_id = CFG_KEYS.get(key)
        if key_id is None:
            raise ValueError(f'unknown ESP32 setting: {key}')
        encoded_value = self._normalize_config_value(key, value)
        return CFG_SET | ((key_id & 0x3F) << 16) | (encoded_value & 0xFFFF)

    def _config_request_callback(self, message: String) -> None:
        try:
            request = json.loads(message.data)
        except Exception as exc:
            self.get_logger().warning(f'Bad /esp32/config/request JSON: {exc}')
            return
        if not isinstance(request, dict):
            return
        op = str(request.get('op', 'get')).lower()
        try:
            if op == 'get':
                self._queue_config_sequence(CFG_GET)
            elif op == 'reset':
                self._queue_config_sequence(CFG_RESET)
                self._queue_config_sequence(CFG_GET)
            elif op == 'set':
                values = request.get('values') or {}
                if not isinstance(values, dict):
                    raise ValueError('values must be an object')
                for key, value in values.items():
                    self._queue_config_sequence(self._encoded_set_sequence(str(key), value))
                self._queue_config_sequence(CFG_GET)
            else:
                raise ValueError(f'unknown op: {op}')
        except Exception as exc:
            self.get_logger().error(f'ESP32 config request failed: {exc}')


def main(args: Optional[list[str]] = None) -> None:
    rclpy.init(args=args)
    node = Esp32TrackBridgeSettingsNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
