#!/usr/bin/env python3
"""Seed AMCL from GPS-correlated recorded route points and expose localization readiness."""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Optional

import rclpy
import yaml
from geometry_msgs.msg import PoseWithCovarianceStamped
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import NavSatFix, NavSatStatus
from std_msgs.msg import Bool, String


class LocalizationBootstrapNode(Node):
    def __init__(self) -> None:
        super().__init__('localization_bootstrap_node')

        self.declare_parameter('route_file', '~/robotlidar_data/routes/cleaning_route.yaml')
        self.declare_parameter('gps_topic', '/gps/fix')
        self.declare_parameter('amcl_pose_topic', '/amcl_pose')
        self.declare_parameter('initialpose_topic', '/initialpose')
        self.declare_parameter('map_frame', 'map')
        self.declare_parameter('max_gps_sigma_m', 8.0)
        self.declare_parameter('max_gps_route_distance_m', 25.0)
        self.declare_parameter('initial_xy_sigma_floor_m', 3.0)
        self.declare_parameter('initial_yaw_sigma_rad', 1.57)
        self.declare_parameter('amcl_ready_sigma_m', 1.0)
        self.declare_parameter('amcl_ready_samples', 5)
        self.declare_parameter('republish_initialpose_sec', 2.0)

        self.route_file = Path(str(self.get_parameter('route_file').value)).expanduser()
        self.map_frame = str(self.get_parameter('map_frame').value)
        self.max_gps_sigma = float(self.get_parameter('max_gps_sigma_m').value)
        self.max_gps_route_distance = float(self.get_parameter('max_gps_route_distance_m').value)
        self.initial_xy_sigma_floor = float(self.get_parameter('initial_xy_sigma_floor_m').value)
        self.initial_yaw_sigma = float(self.get_parameter('initial_yaw_sigma_rad').value)
        self.amcl_ready_sigma = float(self.get_parameter('amcl_ready_sigma_m').value)
        self.amcl_ready_samples = max(1, int(self.get_parameter('amcl_ready_samples').value))
        self.republish_sec = max(0.5, float(self.get_parameter('republish_initialpose_sec').value))

        self.initialpose_pub = self.create_publisher(
            PoseWithCovarianceStamped,
            str(self.get_parameter('initialpose_topic').value),
            10,
        )
        self.ready_pub = self.create_publisher(Bool, '/localization/ready', 10)
        self.status_pub = self.create_publisher(String, '/localization/bootstrap_state', 10)
        self.create_subscription(
            NavSatFix,
            str(self.get_parameter('gps_topic').value),
            self._gps_callback,
            qos_profile_sensor_data,
        )
        self.create_subscription(
            PoseWithCovarianceStamped,
            str(self.get_parameter('amcl_pose_topic').value),
            self._amcl_callback,
            10,
        )

        self.route_points = self._load_gps_route_points()
        self.ready = False
        self._stable_samples = 0
        self._seed_point: Optional[dict] = None
        self._last_seed_publish = 0.0
        self._publish_ready(False)
        self._publish_status('waiting_gps', gps_route_points=len(self.route_points))
        self.get_logger().info(
            f'Localization bootstrap ready; GPS-tagged route points={len(self.route_points)}'
        )

    def _load_gps_route_points(self) -> list[dict]:
        if not self.route_file.exists():
            self.get_logger().warning(f'Route file not found: {self.route_file}')
            return []
        data = yaml.safe_load(self.route_file.read_text(encoding='utf-8')) or {}
        result = []
        for index, point in enumerate(data.get('points') or []):
            if not isinstance(point, dict):
                continue
            gps = point.get('gps')
            if not isinstance(gps, dict):
                continue
            try:
                lat = float(gps['latitude'])
                lon = float(gps['longitude'])
                x = float(point['x'])
                y = float(point['y'])
                yaw = float(point['yaw'])
            except (KeyError, TypeError, ValueError):
                continue
            if all(math.isfinite(v) for v in (lat, lon, x, y, yaw)):
                result.append({'index': index, 'lat': lat, 'lon': lon, 'x': x, 'y': y, 'yaw': yaw})
        return result

    def _gps_callback(self, message: NavSatFix) -> None:
        if self.ready or not self.route_points:
            return
        if message.status.status < NavSatStatus.STATUS_FIX:
            self._publish_status('waiting_gps_fix')
            return
        lat = float(message.latitude)
        lon = float(message.longitude)
        if not math.isfinite(lat) or not math.isfinite(lon):
            return

        covariance_x = max(0.0, float(message.position_covariance[0]))
        covariance_y = max(0.0, float(message.position_covariance[4]))
        sigma = math.sqrt(max(covariance_x, covariance_y))
        if sigma > self.max_gps_sigma > 0.0:
            self._publish_status('gps_too_inaccurate', gps_sigma_m=round(sigma, 2))
            return

        seed, distance = min(
            ((point, self._haversine_m(lat, lon, point['lat'], point['lon'])) for point in self.route_points),
            key=lambda item: item[1],
        )
        if self.max_gps_route_distance > 0.0 and distance > self.max_gps_route_distance:
            self._publish_status('gps_far_from_route', distance_m=round(distance, 2))
            return

        self._seed_point = seed
        now = time.monotonic()
        if now - self._last_seed_publish < self.republish_sec:
            return
        self._last_seed_publish = now
        self._publish_initial_pose(seed, max(self.initial_xy_sigma_floor, sigma))
        self._publish_status(
            'amcl_refining',
            source_route_point=seed['index'] + 1,
            gps_distance_m=round(distance, 2),
            gps_sigma_m=round(sigma, 2),
        )

    def _publish_initial_pose(self, seed: dict, xy_sigma: float) -> None:
        msg = PoseWithCovarianceStamped()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = self.map_frame
        msg.pose.pose.position.x = seed['x']
        msg.pose.pose.position.y = seed['y']
        msg.pose.pose.orientation.z = math.sin(seed['yaw'] / 2.0)
        msg.pose.pose.orientation.w = math.cos(seed['yaw'] / 2.0)
        covariance = [0.0] * 36
        covariance[0] = xy_sigma * xy_sigma
        covariance[7] = xy_sigma * xy_sigma
        covariance[35] = self.initial_yaw_sigma * self.initial_yaw_sigma
        msg.pose.covariance = covariance
        self.initialpose_pub.publish(msg)
        self.get_logger().info(
            f'AMCL initial pose from GPS: route point {seed["index"] + 1}, '
            f'x={seed["x"]:.2f}, y={seed["y"]:.2f}, yaw={seed["yaw"]:.2f}, sigma={xy_sigma:.2f} m'
        )

    def _amcl_callback(self, message: PoseWithCovarianceStamped) -> None:
        if self.ready or self._seed_point is None:
            return
        covariance = message.pose.covariance
        sigma = math.sqrt(max(0.0, float(covariance[0]), float(covariance[7])))
        if sigma <= self.amcl_ready_sigma:
            self._stable_samples += 1
        else:
            self._stable_samples = 0
        if self._stable_samples < self.amcl_ready_samples:
            self._publish_status(
                'amcl_refining',
                amcl_sigma_m=round(sigma, 3),
                stable_samples=self._stable_samples,
                required_samples=self.amcl_ready_samples,
            )
            return

        self.ready = True
        pose = message.pose.pose
        self._publish_ready(True)
        self._publish_status(
            'ready',
            x=round(float(pose.position.x), 3),
            y=round(float(pose.position.y), 3),
            amcl_sigma_m=round(sigma, 3),
        )
        self.get_logger().info(
            f'AMCL localization READY: x={pose.position.x:.2f}, y={pose.position.y:.2f}, sigma={sigma:.3f} m'
        )

    def _publish_ready(self, value: bool) -> None:
        msg = Bool()
        msg.data = value
        self.ready_pub.publish(msg)

    def _publish_status(self, state: str, **extra) -> None:
        payload = {'state': state, 'ready': self.ready, **extra}
        msg = String()
        msg.data = json.dumps(payload, ensure_ascii=False)
        self.status_pub.publish(msg)

    @staticmethod
    def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        radius = 6371000.0
        p1 = math.radians(lat1)
        p2 = math.radians(lat2)
        dp = math.radians(lat2 - lat1)
        dl = math.radians(lon2 - lon1)
        a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
        return 2.0 * radius * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def main(args: Optional[list[str]] = None) -> None:
    rclpy.init(args=args)
    node = LocalizationBootstrapNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
