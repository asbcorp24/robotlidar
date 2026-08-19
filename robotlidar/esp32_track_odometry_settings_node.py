#!/usr/bin/env python3
"""Hall odometry that follows calibration stored on the ESP32."""

from __future__ import annotations

import json
from typing import Optional

import rclpy
from std_msgs.msg import String

from robotlidar.esp32_track_odometry_node import Esp32TrackOdometryNode


class Esp32TrackOdometrySettingsNode(Esp32TrackOdometryNode):
    def __init__(self) -> None:
        super().__init__()
        self.create_subscription(String, '/esp32/config/state', self._config_callback, 10)
        self.get_logger().info('Hall calibration can be updated from ESP32 NVS settings')

    def _config_callback(self, message: String) -> None:
        try:
            data = json.loads(message.data)
        except Exception:
            return
        if not isinstance(data, dict) or not data.get('hall_enabled', False):
            return
        try:
            pulses_per_rev = float(data.get('hall_ppr', 0))
            circumference_m = float(data.get('wheel_circ_mm', 0)) / 1000.0
            track_width_m = float(data.get('track_width_mm', 0)) / 1000.0
        except (TypeError, ValueError):
            return
        if pulses_per_rev <= 0 or circumference_m <= 0 or track_width_m <= 0:
            return
        with self._lock:
            new_meters_per_tick = circumference_m / pulses_per_rev
            changed = (
                abs(new_meters_per_tick - self.meters_per_tick) > 1e-12
                or abs(track_width_m - self.track_width_m) > 1e-9
            )
            self.meters_per_tick = new_meters_per_tick
            self.track_width_m = track_width_m
            if changed:
                # Rebase counters so a calibration edit never produces a fake jump.
                self._last_left_ticks = None
                self._last_right_ticks = None
                self._last_time = None
        if changed:
            self.get_logger().info(
                'Applied ESP32 Hall calibration: %.9f m/tick, track_width=%.3f m'
                % (self.meters_per_tick, self.track_width_m)
            )


def main(args: Optional[list[str]] = None) -> None:
    rclpy.init(args=args)
    node = Esp32TrackOdometrySettingsNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
