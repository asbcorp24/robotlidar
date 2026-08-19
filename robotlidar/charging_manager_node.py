#!/usr/bin/env python3
"""Charging dock manager: save dock pose, monitor battery and navigate home."""
from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Optional

import rclpy
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionClient
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.time import Time
from std_msgs.msg import Bool, String
from std_srvs.srv import Trigger
from tf2_ros import Buffer, TransformException, TransformListener


class ChargingManagerNode(Node):
    def __init__(self) -> None:
        super().__init__('charging_manager_node')
        data_dir = Path(os.environ.get('ROBOTLIDAR_DATA_DIR', '~/robotlidar_data')).expanduser()
        cfg_dir = data_dir / 'config'

        self.declare_parameter('dock_file', str(cfg_dir / 'charging_dock.json'))
        self.declare_parameter('settings_file', str(cfg_dir / 'charging_settings.json'))
        self.declare_parameter('map_frame', 'map')
        self.declare_parameter('base_frame', 'base_link')
        self.declare_parameter('low_percent', 25.0)
        self.declare_parameter('empty_voltage', 20.0)
        self.declare_parameter('full_voltage', 25.2)
        self.declare_parameter('low_samples', 5)
        self.declare_parameter('rearm_percent', 35.0)

        # INA228 current direction depends on the physical shunt orientation.
        # +1 means positive current is charging, -1 means negative current is charging.
        self.declare_parameter('charge_current_sign', 1)
        self.declare_parameter('charge_detect_current_a', 0.5)
        self.declare_parameter('charge_detect_voltage_rise_v', 0.15)
        self.declare_parameter('charge_detect_samples', 5)
        self.declare_parameter('charged_percent', 95.0)
        self.declare_parameter('charged_samples', 5)

        self.dock_file = Path(str(self.get_parameter('dock_file').value)).expanduser()
        self.settings_file = Path(str(self.get_parameter('settings_file').value)).expanduser()
        self.map_frame = str(self.get_parameter('map_frame').value)
        self.base_frame = str(self.get_parameter('base_frame').value)
        self.low_percent = float(self.get_parameter('low_percent').value)
        self.empty_v = float(self.get_parameter('empty_voltage').value)
        self.full_v = float(self.get_parameter('full_voltage').value)
        self.low_samples = max(1, int(self.get_parameter('low_samples').value))
        self.rearm_percent = float(self.get_parameter('rearm_percent').value)
        self.charge_current_sign = 1 if int(self.get_parameter('charge_current_sign').value) >= 0 else -1
        self.charge_detect_current_a = max(0.0, float(self.get_parameter('charge_detect_current_a').value))
        self.charge_detect_voltage_rise_v = max(0.0, float(self.get_parameter('charge_detect_voltage_rise_v').value))
        self.charge_detect_samples = max(1, int(self.get_parameter('charge_detect_samples').value))
        self.charged_percent = max(1.0, min(100.0, float(self.get_parameter('charged_percent').value)))
        self.charged_samples = max(1, int(self.get_parameter('charged_samples').value))
        self._load_settings()

        self.tf_buffer = Buffer()
        self.tf_listener = TransformListener(self.tf_buffer, self)
        self.nav = ActionClient(self, NavigateToPose, 'navigate_to_pose')
        self.status_pub = self.create_publisher(String, '/charging/status', 10)
        self.low_pub = self.create_publisher(Bool, '/charging/low_battery', 10)
        self.create_subscription(String, '/battery/status', self._battery, 20)
        self.create_subscription(String, '/charging/config', self._config, 10)
        self.create_service(Trigger, '/charging/set_dock_here', self._set_dock)
        self.create_service(Trigger, '/charging/go_home', self._go_home)
        self.create_service(Trigger, '/charging/cancel', self._cancel)

        self.battery: dict = {}
        self.low_count = 0
        self.low_latched = False
        self.goal_handle = None
        self.state = 'idle'
        self.dock = self._load_dock()

        self.arrival_voltage: Optional[float] = None
        self.arrived_at: Optional[float] = None
        self.charging_started_at: Optional[float] = None
        self.charge_detect_count = 0
        self.charged_count = 0
        self.create_timer(1.0, self._publish)

    def _load_dock(self):
        try:
            return json.loads(self.dock_file.read_text('utf-8')) if self.dock_file.exists() else None
        except Exception:
            return None

    def _load_settings(self) -> None:
        try:
            data = json.loads(self.settings_file.read_text('utf-8')) if self.settings_file.exists() else {}
        except Exception:
            data = {}
        self.low_percent = float(data.get('low_percent', self.low_percent))
        self.empty_v = float(data.get('empty_voltage', self.empty_v))
        self.full_v = float(data.get('full_voltage', self.full_v))
        self.rearm_percent = float(data.get('rearm_percent', self.rearm_percent))
        self.low_samples = max(1, int(data.get('low_samples', self.low_samples)))
        self.charge_current_sign = 1 if int(data.get('charge_current_sign', self.charge_current_sign)) >= 0 else -1
        self.charge_detect_current_a = max(0.0, float(data.get('charge_detect_current_a', self.charge_detect_current_a)))
        self.charge_detect_voltage_rise_v = max(0.0, float(data.get('charge_detect_voltage_rise_v', self.charge_detect_voltage_rise_v)))
        self.charge_detect_samples = max(1, int(data.get('charge_detect_samples', self.charge_detect_samples)))
        self.charged_percent = max(1.0, min(100.0, float(data.get('charged_percent', self.charged_percent))))
        self.charged_samples = max(1, int(data.get('charged_samples', self.charged_samples)))

    def _settings_dict(self) -> dict:
        return {
            'low_percent': self.low_percent,
            'empty_voltage': self.empty_v,
            'full_voltage': self.full_v,
            'rearm_percent': self.rearm_percent,
            'low_samples': self.low_samples,
            'charge_current_sign': self.charge_current_sign,
            'charge_detect_current_a': self.charge_detect_current_a,
            'charge_detect_voltage_rise_v': self.charge_detect_voltage_rise_v,
            'charge_detect_samples': self.charge_detect_samples,
            'charged_percent': self.charged_percent,
            'charged_samples': self.charged_samples,
        }

    def _save_settings(self) -> None:
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.settings_file.with_suffix('.tmp')
        tmp.write_text(json.dumps(self._settings_dict(), ensure_ascii=False, indent=2), 'utf-8')
        tmp.replace(self.settings_file)

    @staticmethod
    def _yaw(q) -> float:
        return math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))

    def _set_dock(self, _req, res):
        try:
            t = self.tf_buffer.lookup_transform(
                self.map_frame, self.base_frame, Time(), timeout=Duration(seconds=2.0)
            )
        except TransformException as exc:
            res.success = False
            res.message = f'Нет локализации map->base_link: {exc}'
            return res
        tr = t.transform.translation
        q = t.transform.rotation
        self.dock = {
            'frame_id': self.map_frame,
            'x': float(tr.x),
            'y': float(tr.y),
            'yaw': self._yaw(q),
            'saved_at': time.time(),
        }
        self.dock_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.dock_file.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.dock, ensure_ascii=False, indent=2), 'utf-8')
        tmp.replace(self.dock_file)
        res.success = True
        res.message = f"Точка зарядки сохранена: x={self.dock['x']:.2f}, y={self.dock['y']:.2f}"
        return res

    def _config(self, msg: String) -> None:
        try:
            d = json.loads(msg.data)
        except Exception:
            return
        if not isinstance(d, dict):
            return
        for key, attr in (
            ('low_percent', 'low_percent'),
            ('empty_voltage', 'empty_v'),
            ('full_voltage', 'full_v'),
            ('rearm_percent', 'rearm_percent'),
            ('charge_detect_current_a', 'charge_detect_current_a'),
            ('charge_detect_voltage_rise_v', 'charge_detect_voltage_rise_v'),
            ('charged_percent', 'charged_percent'),
        ):
            if key in d:
                setattr(self, attr, float(d[key]))
        if 'low_samples' in d:
            self.low_samples = max(1, int(d['low_samples']))
        if 'charge_detect_samples' in d:
            self.charge_detect_samples = max(1, int(d['charge_detect_samples']))
        if 'charged_samples' in d:
            self.charged_samples = max(1, int(d['charged_samples']))
        if 'charge_current_sign' in d:
            self.charge_current_sign = 1 if int(d['charge_current_sign']) >= 0 else -1
        if self.full_v <= self.empty_v:
            self.full_v = self.empty_v + 0.1
        self.low_percent = max(0.0, min(100.0, self.low_percent))
        self.rearm_percent = max(self.low_percent, min(100.0, self.rearm_percent))
        self.charged_percent = max(self.rearm_percent, min(100.0, self.charged_percent))
        self.charge_detect_current_a = max(0.0, self.charge_detect_current_a)
        self.charge_detect_voltage_rise_v = max(0.0, self.charge_detect_voltage_rise_v)
        self._save_settings()

    def _battery(self, msg: String) -> None:
        try:
            d = json.loads(msg.data)
        except Exception:
            return
        if not isinstance(d, dict):
            return

        v = float(d.get('voltage_v', 0) or 0)
        current = float(d.get('current_a', 0) or 0)
        online = bool(d.get('online', False))
        pct = None
        if online and self.full_v > self.empty_v:
            pct = max(0.0, min(100.0, (v - self.empty_v) * 100.0 / (self.full_v - self.empty_v)))
        charge_current = current * self.charge_current_sign
        voltage_rise = 0.0 if self.arrival_voltage is None else v - self.arrival_voltage

        d['percent'] = round(pct, 1) if pct is not None else None
        d['charge_current_a'] = round(charge_current, 3)
        d['voltage_rise_v'] = round(voltage_rise, 3)
        self.battery = d

        if pct is None:
            self.low_count = 0
        elif pct <= self.low_percent:
            self.low_count += 1
        else:
            self.low_count = 0

        if not self.low_latched and self.low_count >= self.low_samples:
            self.low_latched = True
            if self.state not in {'at_dock', 'charging', 'charged'}:
                self.state = 'low_battery'
        elif self.low_latched and pct is not None and pct >= self.rearm_percent:
            self.low_latched = False
            if self.state == 'low_battery':
                self.state = 'idle'

        # Charging is only inferred after Nav2 has actually reached the dock.
        if online and self.state == 'at_dock':
            current_detected = charge_current >= self.charge_detect_current_a > 0.0
            voltage_detected = voltage_rise >= self.charge_detect_voltage_rise_v > 0.0
            if current_detected or voltage_detected:
                self.charge_detect_count += 1
            else:
                self.charge_detect_count = 0
            if self.charge_detect_count >= self.charge_detect_samples:
                self.state = 'charging'
                self.charging_started_at = time.time()
                self.charged_count = 0
                self.get_logger().info(
                    f'Charging confirmed: Icharge={charge_current:.2f} A, dV={voltage_rise:.2f} V'
                )

        if online and self.state == 'charging' and pct is not None:
            if pct >= self.charged_percent:
                self.charged_count += 1
            else:
                self.charged_count = 0
            if self.charged_count >= self.charged_samples:
                self.state = 'charged'
                self.low_latched = False
                self.low_count = 0
                self.get_logger().info(f'Battery charged: {pct:.1f}%')

        # If voltage subsequently drops well below the charged/rearm threshold,
        # the next work cycle can again request a return to the dock.
        if self.state == 'charged' and pct is not None and pct < self.rearm_percent:
            self.state = 'idle'
            self.charging_started_at = None
            self.arrival_voltage = None
            self.arrived_at = None

        b = Bool()
        b.data = self.low_latched
        self.low_pub.publish(b)

    def _go_home(self, _req, res):
        if not self.dock:
            res.success = False
            res.message = 'Точка зарядки не сохранена'
            return res
        if not self.nav.wait_for_server(timeout_sec=3.0):
            res.success = False
            res.message = 'Nav2 navigate_to_pose недоступен'
            return res

        self.arrival_voltage = None
        self.arrived_at = None
        self.charging_started_at = None
        self.charge_detect_count = 0
        self.charged_count = 0

        p = PoseStamped()
        p.header.frame_id = str(self.dock.get('frame_id', 'map'))
        p.header.stamp = self.get_clock().now().to_msg()
        p.pose.position.x = float(self.dock['x'])
        p.pose.position.y = float(self.dock['y'])
        yaw = float(self.dock.get('yaw', 0))
        p.pose.orientation.z = math.sin(yaw / 2)
        p.pose.orientation.w = math.cos(yaw / 2)
        goal = NavigateToPose.Goal()
        goal.pose = p
        future = self.nav.send_goal_async(goal)
        future.add_done_callback(self._goal_response)
        self.state = 'sending_to_dock'
        res.success = True
        res.message = 'Команда возврата к зарядке отправлена'
        return res

    def _goal_response(self, future) -> None:
        try:
            goal = future.result()
        except Exception:
            self.state = 'dock_error'
            return
        if not goal.accepted:
            self.state = 'dock_rejected'
            return
        self.goal_handle = goal
        self.state = 'going_to_dock'
        result = goal.get_result_async()
        result.add_done_callback(self._result)

    def _result(self, future) -> None:
        try:
            status = future.result().status
            if status == 4:
                self.state = 'at_dock'
                self.arrived_at = time.time()
                v = self.battery.get('voltage_v')
                self.arrival_voltage = float(v) if v is not None else None
                self.charge_detect_count = 0
                self.charged_count = 0
                self.get_logger().info(
                    'Dock reached; waiting for INA228 charging confirmation'
                )
            else:
                self.state = f'dock_failed:{status}'
        except Exception:
            self.state = 'dock_error'
        self.goal_handle = None

    def _cancel(self, _req, res):
        if self.goal_handle:
            self.goal_handle.cancel_goal_async()
            self.goal_handle = None
        self.state = 'idle'
        self.arrival_voltage = None
        self.arrived_at = None
        self.charging_started_at = None
        self.charge_detect_count = 0
        self.charged_count = 0
        res.success = True
        res.message = 'Возврат к зарядке отменён'
        return res

    def _publish(self) -> None:
        message = String()
        message.data = json.dumps(
            {
                'state': self.state,
                'low_battery': self.low_latched,
                'battery': self.battery,
                'dock': self.dock,
                'arrived_at': self.arrived_at,
                'charging_started_at': self.charging_started_at,
                'charge_detect_count': self.charge_detect_count,
                'charged_count': self.charged_count,
                'settings': self._settings_dict(),
            },
            ensure_ascii=False,
        )
        self.status_pub.publish(message)


def main(args: Optional[list[str]] = None) -> None:
    rclpy.init(args=args)
    node = ChargingManagerNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
