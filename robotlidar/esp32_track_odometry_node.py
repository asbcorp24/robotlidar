#!/usr/bin/env python3
"""Publish differential-drive odometry from signed ESP32 Hall tick counters."""

from __future__ import annotations

import math
import threading
import time
from typing import Optional

import rclpy
from nav_msgs.msg import Odometry
from rclpy.node import Node
from std_msgs.msg import Int64MultiArray


class Esp32TrackOdometryNode(Node):
    """Convert cumulative signed Hall ticks from ESP32 into /wheel/odom."""

    def __init__(self) -> None:
        super().__init__('esp32_track_odometry_node')

        self.declare_parameter('ticks_topic', '/drive/hall_ticks')
        self.declare_parameter('pulses_per_motor_revolution', 6.0)
        self.declare_parameter('gear_ratio', 30.0)
        self.declare_parameter('drive_sprocket_circumference_m', 0.40)
        self.declare_parameter('track_width_m', 0.60)
        self.declare_parameter('odom_frame', 'odom')
        self.declare_parameter('base_frame', 'base_link')

        ticks_topic = str(self.get_parameter('ticks_topic').value)
        pulses_per_revolution = float(
            self.get_parameter('pulses_per_motor_revolution').value
        )
        gear_ratio = float(self.get_parameter('gear_ratio').value)
        sprocket_circumference = float(
            self.get_parameter('drive_sprocket_circumference_m').value
        )
        self.track_width_m = float(self.get_parameter('track_width_m').value)
        self.odom_frame = str(self.get_parameter('odom_frame').value)
        self.base_frame = str(self.get_parameter('base_frame').value)

        for name, value in (
            ('pulses_per_motor_revolution', pulses_per_revolution),
            ('gear_ratio', gear_ratio),
            ('drive_sprocket_circumference_m', sprocket_circumference),
            ('track_width_m', self.track_width_m),
        ):
            if value <= 0.0:
                raise ValueError(f'{name} must be greater than zero')

        self.meters_per_tick = (
            sprocket_circumference / (pulses_per_revolution * gear_ratio)
        )
        self._lock = threading.Lock()
        self._last_left_ticks: Optional[int] = None
        self._last_right_ticks: Optional[int] = None
        self._last_time: Optional[float] = None
        self.x = 0.0
        self.y = 0.0
        self.yaw = 0.0

        self.odom_publisher = self.create_publisher(Odometry, '/wheel/odom', 20)
        self.create_subscription(
            Int64MultiArray,
            ticks_topic,
            self._ticks_callback,
            50,
        )

        self.get_logger().info(
            'ESP32 Hall odometry listening on %s; %.9f m/tick'
            % (ticks_topic, self.meters_per_tick)
        )

    def _ticks_callback(self, message: Int64MultiArray) -> None:
        if len(message.data) < 2:
            self.get_logger().warning(
                'Hall tick message must contain [left_ticks, right_ticks]'
            )
            return

        left_ticks = int(message.data[0])
        right_ticks = int(message.data[1])
        now_monotonic = time.monotonic()

        with self._lock:
            if (
                self._last_left_ticks is None
                or self._last_right_ticks is None
                or self._last_time is None
            ):
                self._last_left_ticks = left_ticks
                self._last_right_ticks = right_ticks
                self._last_time = now_monotonic
                return

            delta_left_ticks = left_ticks - self._last_left_ticks
            delta_right_ticks = right_ticks - self._last_right_ticks
            dt = now_monotonic - self._last_time
            self._last_left_ticks = left_ticks
            self._last_right_ticks = right_ticks
            self._last_time = now_monotonic

            # ESP32 reset or integer discontinuity: rebase instead of producing
            # a large false movement.
            if abs(delta_left_ticks) > 1_000_000 or abs(delta_right_ticks) > 1_000_000:
                self.get_logger().warning('Hall tick counters rebased after a jump')
                return
            if dt <= 0.0:
                return

            delta_left = delta_left_ticks * self.meters_per_tick
            delta_right = delta_right_ticks * self.meters_per_tick
            delta_distance = (delta_right + delta_left) / 2.0
            delta_yaw = (delta_right - delta_left) / self.track_width_m
            midpoint_yaw = self.yaw + delta_yaw / 2.0

            self.x += delta_distance * math.cos(midpoint_yaw)
            self.y += delta_distance * math.sin(midpoint_yaw)
            self.yaw = math.atan2(
                math.sin(self.yaw + delta_yaw),
                math.cos(self.yaw + delta_yaw),
            )

            x = self.x
            y = self.y
            yaw = self.yaw

        linear_velocity = delta_distance / dt
        angular_velocity = delta_yaw / dt
        quaternion_z = math.sin(yaw / 2.0)
        quaternion_w = math.cos(yaw / 2.0)

        odom = Odometry()
        odom.header.stamp = self.get_clock().now().to_msg()
        odom.header.frame_id = self.odom_frame
        odom.child_frame_id = self.base_frame
        odom.pose.pose.position.x = x
        odom.pose.pose.position.y = y
        odom.pose.pose.orientation.z = quaternion_z
        odom.pose.pose.orientation.w = quaternion_w
        odom.twist.twist.linear.x = linear_velocity
        odom.twist.twist.angular.z = angular_velocity

        odom.pose.covariance = [
            0.05, 0.0, 0.0, 0.0, 0.0, 0.0,
            0.0, 0.05, 0.0, 0.0, 0.0, 0.0,
            0.0, 0.0, 9999.0, 0.0, 0.0, 0.0,
            0.0, 0.0, 0.0, 9999.0, 0.0, 0.0,
            0.0, 0.0, 0.0, 0.0, 9999.0, 0.0,
            0.0, 0.0, 0.0, 0.0, 0.0, 0.20,
        ]
        odom.twist.covariance = [
            0.10, 0.0, 0.0, 0.0, 0.0, 0.0,
            0.0, 9999.0, 0.0, 0.0, 0.0, 0.0,
            0.0, 0.0, 9999.0, 0.0, 0.0, 0.0,
            0.0, 0.0, 0.0, 9999.0, 0.0, 0.0,
            0.0, 0.0, 0.0, 0.0, 9999.0, 0.0,
            0.0, 0.0, 0.0, 0.0, 0.0, 0.30,
        ]
        self.odom_publisher.publish(odom)


def main(args: Optional[list[str]] = None) -> None:
    rclpy.init(args=args)
    node = Esp32TrackOdometryNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
