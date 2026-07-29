#!/usr/bin/env python3
"""Record the tractor pose in the map frame as a reusable cleaning route."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import rclpy
import yaml
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.time import Time
from std_msgs.msg import Bool
from std_srvs.srv import Trigger
from tf2_ros import Buffer, TransformException, TransformListener


class RouteRecorderNode(Node):
    """Record map->base_link poses while the operator drives manually."""

    def __init__(self) -> None:
        super().__init__('route_recorder_node')

        self.declare_parameter(
            'route_file', '~/robotlidar_data/routes/cleaning_route.yaml'
        )
        self.declare_parameter('map_frame', 'map')
        self.declare_parameter('base_frame', 'base_link')
        self.declare_parameter('sample_rate_hz', 5.0)
        self.declare_parameter('minimum_distance_m', 0.30)
        self.declare_parameter('minimum_heading_rad', 0.20)
        self.declare_parameter('auto_save_on_stop', True)

        self.route_file = Path(
            str(self.get_parameter('route_file').value)
        ).expanduser()
        self.map_frame = str(self.get_parameter('map_frame').value)
        self.base_frame = str(self.get_parameter('base_frame').value)
        sample_rate_hz = float(self.get_parameter('sample_rate_hz').value)
        self.minimum_distance_m = float(
            self.get_parameter('minimum_distance_m').value
        )
        self.minimum_heading_rad = float(
            self.get_parameter('minimum_heading_rad').value
        )
        self.auto_save_on_stop = bool(
            self.get_parameter('auto_save_on_stop').value
        )

        if sample_rate_hz <= 0.0:
            raise ValueError('sample_rate_hz must be greater than zero')
        if self.minimum_distance_m < 0.0:
            raise ValueError('minimum_distance_m cannot be negative')
        if self.minimum_heading_rad < 0.0:
            raise ValueError('minimum_heading_rad cannot be negative')

        self.tf_buffer = Buffer(cache_time=Duration(seconds=30.0))
        self.tf_listener = TransformListener(self.tf_buffer, self)

        self.recording = False
        self.points: list[dict[str, float]] = []

        self.recording_publisher = self.create_publisher(
            Bool, '/route/recording', 10
        )
        self.create_service(Trigger, '/route/start_recording', self._start)
        self.create_service(Trigger, '/route/stop_recording', self._stop)
        self.create_service(Trigger, '/route/clear', self._clear)
        self.create_service(Trigger, '/route/save', self._save_service)
        self.create_timer(1.0 / sample_rate_hz, self._sample)

        self.get_logger().info(
            f'Route recorder ready; output: {self.route_file}'
        )

    def _start(
        self, _request: Trigger.Request, response: Trigger.Response
    ) -> Trigger.Response:
        if self.recording:
            response.success = False
            response.message = 'Route recording is already active'
            return response

        self.recording = True
        self._publish_recording_state()
        response.success = True
        response.message = (
            f'Recording started; existing points: {len(self.points)}'
        )
        self.get_logger().info(response.message)
        return response

    def _stop(
        self, _request: Trigger.Request, response: Trigger.Response
    ) -> Trigger.Response:
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

    def _clear(
        self, _request: Trigger.Request, response: Trigger.Response
    ) -> Trigger.Response:
        if self.recording:
            response.success = False
            response.message = 'Stop recording before clearing the route'
            return response

        self.points.clear()
        response.success = True
        response.message = 'Route cleared from memory'
        self.get_logger().info(response.message)
        return response

    def _save_service(
        self, _request: Trigger.Request, response: Trigger.Response
    ) -> Trigger.Response:
        try:
            self._save_route()
        except Exception as exc:
            response.success = False
            response.message = f'Route save failed: {exc}'
            self.get_logger().exception(response.message)
            return response

        response.success = True
        response.message = (
            f'Saved {len(self.points)} points to {self.route_file}'
        )
        self.get_logger().info(response.message)
        return response

    def _sample(self) -> None:
        if not self.recording:
            return

        try:
            transform = self.tf_buffer.lookup_transform(
                self.map_frame,
                self.base_frame,
                Time(),
                timeout=Duration(seconds=0.10),
            )
        except TransformException as exc:
            self.get_logger().warning(
                f'Cannot record route: {exc}',
                throttle_duration_sec=2.0,
            )
            return

        translation = transform.transform.translation
        rotation = transform.transform.rotation
        yaw = self._yaw_from_quaternion(
            rotation.x, rotation.y, rotation.z, rotation.w
        )

        point = {
            'x': float(translation.x),
            'y': float(translation.y),
            'yaw': float(yaw),
        }

        if self._should_store(point):
            self.points.append(point)
            if len(self.points) % 10 == 0:
                self.get_logger().info(
                    f'Route points recorded: {len(self.points)}'
                )

    def _should_store(self, point: dict[str, float]) -> bool:
        if not self.points:
            return True

        previous = self.points[-1]
        distance = math.hypot(
            point['x'] - previous['x'],
            point['y'] - previous['y'],
        )
        heading_change = abs(
            self._normalize_angle(point['yaw'] - previous['yaw'])
        )
        return (
            distance >= self.minimum_distance_m
            or heading_change >= self.minimum_heading_rad
        )

    def _save_route(self) -> None:
        if not self.points:
            raise RuntimeError('route contains no points')

        self.route_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            'format_version': 1,
            'frame_id': self.map_frame,
            'base_frame': self.base_frame,
            'point_count': len(self.points),
            'points': self.points,
        }
        temporary = self.route_file.with_suffix(self.route_file.suffix + '.tmp')
        temporary.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding='utf-8',
        )
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
