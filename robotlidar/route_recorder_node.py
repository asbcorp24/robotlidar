#!/usr/bin/env python3
"""Record map poses plus tool/auxiliary states as a repeatable work route."""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Optional

import rclpy
import yaml
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from rclpy.time import Time
from sensor_msgs.msg import NavSatFix, NavSatStatus
from std_msgs.msg import Bool, Float32MultiArray, Int8
from std_srvs.srv import Trigger
from tf2_ros import Buffer, TransformException, TransformListener


class RouteRecorderNode(Node):
    """Record map->base_link poses and current implement states while driving manually."""

    def __init__(self) -> None:
        super().__init__('route_recorder_node')

        self.declare_parameter('route_file', '~/robotlidar_data/routes/cleaning_route.yaml')
        self.declare_parameter('map_frame', 'map')
        self.declare_parameter('base_frame', 'base_link')
        self.declare_parameter('sample_rate_hz', 5.0)
        self.declare_parameter('minimum_distance_m', 0.30)
        self.declare_parameter('minimum_heading_rad', 0.20)
        self.declare_parameter('auto_save_on_stop', True)
        self.declare_parameter('include_gps', True)
        self.declare_parameter('gps_topic', '/gps/fix')
        self.declare_parameter('gps_max_age_sec', 3.0)
        self.declare_parameter('record_auxiliary_actions', True)

        self.route_file = Path(str(self.get_parameter('route_file').value)).expanduser()
        self.map_frame = str(self.get_parameter('map_frame').value)
        self.base_frame = str(self.get_parameter('base_frame').value)
        sample_rate_hz = float(self.get_parameter('sample_rate_hz').value)
        self.minimum_distance_m = float(self.get_parameter('minimum_distance_m').value)
        self.minimum_heading_rad = float(self.get_parameter('minimum_heading_rad').value)
        self.auto_save_on_stop = bool(self.get_parameter('auto_save_on_stop').value)
        self.include_gps = bool(self.get_parameter('include_gps').value)
        self.gps_max_age_sec = float(self.get_parameter('gps_max_age_sec').value)
        self.record_auxiliary_actions = bool(self.get_parameter('record_auxiliary_actions').value)
        gps_topic = str(self.get_parameter('gps_topic').value)

        if sample_rate_hz <= 0.0:
            raise ValueError('sample_rate_hz must be greater than zero')
        if self.minimum_distance_m < 0.0:
            raise ValueError('minimum_distance_m cannot be negative')
        if self.minimum_heading_rad < 0.0:
            raise ValueError('minimum_heading_rad cannot be negative')
        if self.gps_max_age_sec <= 0.0:
            raise ValueError('gps_max_age_sec must be greater than zero')

        self.tf_buffer = Buffer(cache_time=Duration(seconds=30.0))
        self.tf_listener = TransformListener(self.tf_buffer, self)

        self.recording = False
        self.points: list[dict] = []
        self._latest_gps: Optional[dict[str, float]] = None
        self._latest_gps_monotonic = 0.0

        # Snapshot of the actual implement state observed from ESP32 telemetry.
        # These values are copied into every stored route point. A point is also
        # stored immediately when an action changes, even if the tractor is stationary.
        self._actions = {
            'actuator': 0,      # -1 down, 0 stop, +1 up
            'brush': 0.0,       # 0.0 .. 1.0 actual speed estimate
            'aux_motor': 0.0,   # -1.0 .. +1.0 actual signed command
        }

        self.recording_publisher = self.create_publisher(Bool, '/route/recording', 10)
        if self.include_gps:
            self.create_subscription(NavSatFix, gps_topic, self._gps_callback, qos_profile_sensor_data)

        if self.record_auxiliary_actions:
            self.create_subscription(Int8, '/actuator/state', self._actuator_state_callback, 20)
            self.create_subscription(Float32MultiArray, '/brush/state', self._brush_state_callback, 20)
            self.create_subscription(Float32MultiArray, '/aux_motor/state', self._aux_motor_state_callback, 20)

        self.create_service(Trigger, '/route/start_recording', self._start)
        self.create_service(Trigger, '/route/stop_recording', self._stop)
        self.create_service(Trigger, '/route/clear', self._clear)
        self.create_service(Trigger, '/route/save', self._save_service)
        self.create_timer(1.0 / sample_rate_hz, self._sample)

        self.get_logger().info(
            f'Route recorder ready; output: {self.route_file}; GPS={self.include_gps}; '
            f'aux-actions={self.record_auxiliary_actions}'
        )

    def _start(self, _request: Trigger.Request, response: Trigger.Response) -> Trigger.Response:
        if self.recording:
            response.success = False
            response.message = 'Route recording is already active'
            return response
        self.recording = True
        self._publish_recording_state()
        response.success = True
        response.message = f'Recording started; existing points: {len(self.points)}'
        self.get_logger().info(response.message)
        return response

    def _stop(self, _request: Trigger.Request, response: Trigger.Response) -> Trigger.Response:
        if not self.recording:
            response.success = False
            response.message = 'Route recording is not active'
            return response
        self.recording = False
        self._publish_recording_state()
        if self.auto_save_on_stop:
            try:
                self._save_route()
            except Exception as exc:
                response.success = False
                response.message = f'Recording stopped, save failed: {exc}'
                self.get_logger().exception(response.message)
                return response
        response.success = True
        response.message = f'Recording stopped; points: {len(self.points)}'
        self.get_logger().info(response.message)
        return response

    def _clear(self, _request: Trigger.Request, response: Trigger.Response) -> Trigger.Response:
        if self.recording:
            response.success = False
            response.message = 'Stop recording before clearing the route'
            return response
        self.points.clear()
        response.success = True
        response.message = 'Route cleared from memory'
        self.get_logger().info(response.message)
        return response

    def _save_service(self, _request: Trigger.Request, response: Trigger.Response) -> Trigger.Response:
        try:
            self._save_route()
        except Exception as exc:
            response.success = False
            response.message = f'Route save failed: {exc}'
            self.get_logger().exception(response.message)
            return response
        response.success = True
        response.message = f'Saved {len(self.points)} points to {self.route_file}'
        self.get_logger().info(response.message)
        return response

    def _gps_callback(self, message: NavSatFix) -> None:
        if message.status.status < NavSatStatus.STATUS_FIX:
            return
        latitude = float(message.latitude)
        longitude = float(message.longitude)
        if not math.isfinite(latitude) or not math.isfinite(longitude):
            return
        covariance_x = float(message.position_covariance[0])
        covariance_y = float(message.position_covariance[4])
        horizontal_variance = max(covariance_x, covariance_y, 0.0)
        self._latest_gps = {
            'latitude': latitude,
            'longitude': longitude,
            'altitude_m': float(message.altitude) if math.isfinite(float(message.altitude)) else 0.0,
            'horizontal_sigma_m': math.sqrt(horizontal_variance),
        }
        self._latest_gps_monotonic = time.monotonic()

    def _actuator_state_callback(self, message: Int8) -> None:
        value = int(message.data)
        self._actions['actuator'] = 1 if value > 0 else (-1 if value < 0 else 0)

    def _brush_state_callback(self, message: Float32MultiArray) -> None:
        data = list(message.data)
        if len(data) < 3:
            return
        throttle_v = max(0.0, float(data[1]))
        brake = bool(data[2] >= 0.5)
        if brake or throttle_v <= 0.05:
            speed = 0.0
        else:
            # Firmware uses about 1.0 V at minimum running command and 2.85 V at max.
            speed = (throttle_v - 1.0) / (2.85 - 1.0)
            speed = max(0.0, min(1.0, speed))
        self._actions['brush'] = round(speed, 3)

    def _aux_motor_state_callback(self, message: Float32MultiArray) -> None:
        data = list(message.data)
        if len(data) < 3:
            return
        # index 2 is the actual signed output after reverse guard/ramping.
        actual = max(-1.0, min(1.0, float(data[2])))
        self._actions['aux_motor'] = round(actual, 3)

    def _sample(self) -> None:
        if not self.recording:
            return
        try:
            transform = self.tf_buffer.lookup_transform(
                self.map_frame, self.base_frame, Time(), timeout=Duration(seconds=0.10)
            )
        except TransformException as exc:
            self.get_logger().warning(f'Cannot record route: {exc}', throttle_duration_sec=2.0)
            return

        translation = transform.transform.translation
        rotation = transform.transform.rotation
        yaw = self._yaw_from_quaternion(rotation.x, rotation.y, rotation.z, rotation.w)
        point: dict = {
            'x': float(translation.x),
            'y': float(translation.y),
            'yaw': float(yaw),
        }
        if self.record_auxiliary_actions:
            point['actions'] = dict(self._actions)

        gps_age = time.monotonic() - self._latest_gps_monotonic
        if (
            self.include_gps
            and self._latest_gps is not None
            and self._latest_gps_monotonic > 0.0
            and gps_age <= self.gps_max_age_sec
        ):
            point['gps'] = dict(self._latest_gps)

        if self._should_store(point):
            self.points.append(point)
            if len(self.points) % 10 == 0:
                self.get_logger().info(f'Route points recorded: {len(self.points)}')

    def _should_store(self, point: dict) -> bool:
        if not self.points:
            return True
        previous = self.points[-1]

        # Tool state transitions must be preserved at their current map position.
        if point.get('actions') != previous.get('actions'):
            return True

        distance = math.hypot(
            float(point['x']) - float(previous['x']),
            float(point['y']) - float(previous['y']),
        )
        heading_change = abs(self._normalize_angle(float(point['yaw']) - float(previous['yaw'])))
        return distance >= self.minimum_distance_m or heading_change >= self.minimum_heading_rad

    def _save_route(self) -> None:
        if not self.points:
            raise RuntimeError('route contains no points')
        gps_points = sum(1 for point in self.points if 'gps' in point)
        first_gps = next((point['gps'] for point in self.points if 'gps' in point), None)
        action_points = sum(1 for point in self.points if 'actions' in point)
        self.route_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            'format_version': 3,
            'frame_id': self.map_frame,
            'base_frame': self.base_frame,
            'point_count': len(self.points),
            'gps_point_count': gps_points,
            'gps_origin': first_gps,
            'action_point_count': action_points,
            'action_schema': {
                'actuator': '-1 down, 0 stop, +1 up',
                'brush': '0.0..1.0',
                'aux_motor': '-1.0..1.0',
            },
            'points': self.points,
        }
        temporary = self.route_file.with_suffix(self.route_file.suffix + '.tmp')
        temporary.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding='utf-8')
        temporary.replace(self.route_file)

    def _publish_recording_state(self) -> None:
        message = Bool()
        message.data = self.recording
        self.recording_publisher.publish(message)

    @staticmethod
    def _yaw_from_quaternion(x: float, y: float, z: float, w: float) -> float:
        sin_yaw = 2.0 * (w * z + x * y)
        cos_yaw = 1.0 - 2.0 * (y * y + z * z)
        return math.atan2(sin_yaw, cos_yaw)

    @staticmethod
    def _normalize_angle(angle: float) -> float:
        return math.atan2(math.sin(angle), math.cos(angle))


def main(args: Optional[list[str]] = None) -> None:
    rclpy.init(args=args)
    node = RouteRecorderNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
