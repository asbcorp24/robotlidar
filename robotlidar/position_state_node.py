#!/usr/bin/env python3
"""Persist latest robot position and a compact history in every control mode."""
from __future__ import annotations

import json
import math
import os
import time
from collections import deque
from pathlib import Path
from typing import Optional

import rclpy
from geometry_msgs.msg import PoseWithCovarianceStamped
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import NavSatFix, NavSatStatus
from std_msgs.msg import Bool, String


class PositionStateNode(Node):
    def __init__(self) -> None:
        super().__init__('position_state_node')
        data_dir = Path(os.environ.get('ROBOTLIDAR_DATA_DIR', '~/robotlidar_data')).expanduser()
        self.state_dir = data_dir / 'state'
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.last_file = self.state_dir / 'last_position.json'
        self.history_file = self.state_dir / 'position_history.jsonl'

        self.declare_parameter('save_interval_sec', 5.0)
        self.declare_parameter('history_interval_sec', 10.0)
        self.declare_parameter('history_max_records', 500)
        self.save_interval = max(1.0, float(self.get_parameter('save_interval_sec').value))
        self.history_interval = max(self.save_interval, float(self.get_parameter('history_interval_sec').value))
        self.history_max_records = max(20, int(self.get_parameter('history_max_records').value))

        self.gps: Optional[dict] = None
        self.gps_status: dict = {}
        self.odom: Optional[dict] = None
        self.map_pose: Optional[dict] = None
        self.amcl_sigma_m: Optional[float] = None
        self.localization_ready = False
        self._last_history_write = 0.0

        self.create_subscription(NavSatFix, '/gps/fix', self._gps_cb, qos_profile_sensor_data)
        self.create_subscription(String, '/gps/status', self._gps_status_cb, 10)
        self.create_subscription(Odometry, '/wheel/odom', self._odom_cb, 20)
        self.create_subscription(PoseWithCovarianceStamped, '/amcl_pose', self._amcl_cb, 10)
        self.create_subscription(Bool, '/localization/ready', self._ready_cb, 10)
        self.create_timer(self.save_interval, self._save)
        self.get_logger().info(f'Persistent position state: {self.last_file}')

    @staticmethod
    def _yaw(q) -> float:
        return math.atan2(2.0 * (q.w * q.z + q.x * q.y), 1.0 - 2.0 * (q.y * q.y + q.z * q.z))

    def _gps_cb(self, msg: NavSatFix) -> None:
        lat, lon = float(msg.latitude), float(msg.longitude)
        valid = msg.status.status >= NavSatStatus.STATUS_FIX and math.isfinite(lat) and math.isfinite(lon)
        sx = max(0.0, float(msg.position_covariance[0]))
        sy = max(0.0, float(msg.position_covariance[4]))
        self.gps = {
            'latitude': lat if math.isfinite(lat) else None,
            'longitude': lon if math.isfinite(lon) else None,
            'altitude_m': float(msg.altitude) if math.isfinite(float(msg.altitude)) else None,
            'sigma_m': math.sqrt(max(sx, sy)),
            'valid': valid,
            'received_at': time.time(),
        }

    def _gps_status_cb(self, msg: String) -> None:
        try:
            data = json.loads(msg.data)
        except Exception:
            return
        if isinstance(data, dict):
            self.gps_status = data

    def _odom_cb(self, msg: Odometry) -> None:
        p, q = msg.pose.pose.position, msg.pose.pose.orientation
        self.odom = {'x': float(p.x), 'y': float(p.y), 'yaw': self._yaw(q), 'received_at': time.time()}

    def _amcl_cb(self, msg: PoseWithCovarianceStamped) -> None:
        p, q = msg.pose.pose.position, msg.pose.pose.orientation
        cov = msg.pose.covariance
        self.amcl_sigma_m = math.sqrt(max(0.0, float(cov[0]), float(cov[7])))
        self.map_pose = {
            'x': float(p.x), 'y': float(p.y), 'yaw': self._yaw(q),
            'sigma_m': self.amcl_sigma_m, 'received_at': time.time(),
        }

    def _ready_cb(self, msg: Bool) -> None:
        self.localization_ready = bool(msg.data)

    def _snapshot(self) -> dict:
        source = 'LOCALIZATION_UNCERTAIN'
        if self.localization_ready and self.map_pose:
            source = 'AMCL_CONFIRMED'
        elif self.odom:
            source = 'ODOM_ONLY'
        elif self.gps and self.gps.get('valid'):
            source = 'GPS_ONLY'
        return {
            'timestamp': time.time(),
            'source': source,
            'localization_ready': self.localization_ready,
            'gps': self.gps,
            'gps_status': self.gps_status,
            'map_pose': self.map_pose,
            'odom_pose': self.odom,
        }

    def _save(self) -> None:
        snap = self._snapshot()
        tmp = self.last_file.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(self.last_file)
        now = time.monotonic()
        if now - self._last_history_write >= self.history_interval:
            self._last_history_write = now
            self._append_history(snap)

    def _append_history(self, snap: dict) -> None:
        records = deque(maxlen=self.history_max_records)
        if self.history_file.exists():
            try:
                with self.history_file.open('r', encoding='utf-8') as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            records.append(line)
            except Exception:
                records.clear()
        records.append(json.dumps(snap, ensure_ascii=False, separators=(',', ':')))
        tmp = self.history_file.with_suffix('.jsonl.tmp')
        tmp.write_text('\n'.join(records) + '\n', encoding='utf-8')
        tmp.replace(self.history_file)


def main(args: Optional[list[str]] = None) -> None:
    rclpy.init(args=args)
    node = PositionStateNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            node._save()
        except Exception:
            pass
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
