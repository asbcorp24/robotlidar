#!/usr/bin/env python3
"""Differential-drive odometry from one Hall pulse channel per track."""

from __future__ import annotations

import math
import threading
import time
from typing import Optional

import rclpy
from geometry_msgs.msg import TransformStamped
from nav_msgs.msg import Odometry
from rclpy.node import Node
from std_msgs.msg import Empty, Int8MultiArray
from tf2_ros import TransformBroadcaster

try:
    from gpiozero import DigitalInputDevice
except ImportError:
    DigitalInputDevice = None  # type: ignore[assignment]


class HallOdometryNode(Node):
    """Publish odometry using signed Hall pulses from left and right tracks."""

    def __init__(self) -> None:
        super().__init__('hall_odometry_node')

        self.declare_parameter('dry_run', True)
        self.declare_parameter('left_hall_pin', 5)
        self.declare_parameter('right_hall_pin', 6)
        self.declare_parameter('hall_pull_up', True)
        self.declare_parameter('left_hall_inverted', False)
        self.declare_parameter('right_hall_inverted', False)
        self.declare_parameter('bounce_time_sec', 0.002)

        self.declare_parameter('pulses_per_motor_revolution', 6.0)
        self.declare_parameter('gear_ratio', 30.0)
        self.declare_parameter('drive_sprocket_circumference_m', 0.40)
        self.declare_parameter('track_width_m', 0.60)

        self.declare_parameter('publish_rate_hz', 30.0)
        self.declare_parameter('odom_frame', 'odom')
        self.declare_parameter('base_frame', 'base_link')
        self.declare_parameter('publish_tf', True)

        self.dry_run = bool(self.get_parameter('dry_run').value)
        self.left_hall_pin = int(self.get_parameter('left_hall_pin').value)
        self.right_hall_pin = int(self.get_parameter('right_hall_pin').value)
        self.hall_pull_up = bool(self.get_parameter('hall_pull_up').value)
        self.left_hall_inverted = bool(
            self.get_parameter('left_hall_inverted').value
        )
        self.right_hall_inverted = bool(
            self.get_parameter('right_hall_inverted').value
        )
        self.bounce_time_sec = float(
            self.get_parameter('bounce_time_sec').value
        )

        pulses_per_revolution = float(
            self.get_parameter('pulses_per_motor_revolution').value
        )
        gear_ratio = float(self.get_parameter('gear_ratio').value)
        sprocket_circumference = float(
            self.get_parameter('drive_sprocket_circumference_m').value
        )
        self.track_width_m = float(self.get_parameter('track_width_m').value)
        publish_rate_hz = float(self.get_parameter('publish_rate_hz').value)

        self.odom_frame = str(self.get_parameter('odom_frame').value)
        self.base_frame = str(self.get_parameter('base_frame').value)
        self.publish_tf = bool(self.get_parameter('publish_tf').value)

        for name, value in (
            ('pulses_per_motor_revolution', pulses_per_revolution),
            ('gear_ratio', gear_ratio),
            ('drive_sprocket_circumference_m', sprocket_circumference),
            ('track_width_m', self.track_width_m),
            ('publish_rate_hz', publish_rate_hz),
        ):
            if value <= 0.0:
                raise ValueError(f'{name} must be greater than zero')

        if self.bounce_time_sec < 0.0:
            raise ValueError('bounce_time_sec cannot be negative')

        self.meters_per_pulse = (
            sprocket_circumference / (pulses_per_revolution * gear_ratio)
        )

        self._lock = threading.Lock()
        self._left_total_pulses = 0
        self._right_total_pulses = 0
        self._left_last_pulses = 0
        self._right_last_pulses = 0
        self._left_direction = 0
        self._right_direction = 0

        self.x = 0.0
        self.y = 0.0
        self.yaw = 0.0
        self._last_publish_time = time.monotonic()

        self.left_input = None
        self.right_input = None
        self._configure_gpio()

        self.odom_publisher = self.create_publisher(Odometry, '/wheel/odom', 20)
        self.tf_broadcaster = TransformBroadcaster(self)

        self.create_subscription(
            Int8MultiArray,
            '/drive/direction',
            self._direction_callback,
            20,
        )

        # One message on either topic represents one Hall edge. These topics
        # make it possible to test the math without physical GPIO.
        self.create_subscription(
            Empty, '/hall/left_pulse', self._left_test_pulse_callback, 50
        )
        self.create_subscription(
            Empty, '/hall/right_pulse', self._right_test_pulse_callback, 50
        )

        self.create_timer(1.0 / publish_rate_hz, self._publish_odometry)

        self.get_logger().info(
            'Hall odometry started in %s mode; %.9f m/pulse'
            % (
                'DRY-RUN' if self.dry_run else 'HARDWARE',
                self.meters_per_pulse,
            )
        )

    def _configure_gpio(self) -> None:
        if self.dry_run:
            return

        if DigitalInputDevice is None:
            self.get_logger().error(
                'gpiozero is unavailable; switching Hall inputs to dry-run mode'
            )
            self.dry_run = True
            return

        try:
            self.left_input = DigitalInputDevice(
                pin=self.left_hall_pin,
                pull_up=self.hall_pull_up,
                active_state=None,
                bounce_time=self.bounce_time_sec,
            )
            self.right_input = DigitalInputDevice(
                pin=self.right_hall_pin,
                pull_up=self.hall_pull_up,
                active_state=None,
                bounce_time=self.bounce_time_sec,
            )
            self.left_input.when_activated = self._left_gpio_pulse_callback
            self.right_input.when_activated = self._right_gpio_pulse_callback
        except Exception:
            self._close_inputs()
            self.dry_run = True
            self.get_logger().exception(
                'Hall GPIO initialization failed; switching to dry-run mode'
            )

    def _direction_callback(self, message: Int8MultiArray) -> None:
        if len(message.data) < 2:
            self.get_logger().warning(
                '/drive/direction must contain [left, right]'
            )
            return

        with self._lock:
            self._left_direction = self._normalize_direction(message.data[0])
            self._right_direction = self._normalize_direction(message.data[1])

    @staticmethod
    def _normalize_direction(value: int) -> int:
        if value > 0:
            return 1
        if value < 0:
            return -1
        return 0

    def _left_gpio_pulse_callback(self) -> None:
        self._register_left_pulse()

    def _right_gpio_pulse_callback(self) -> None:
        self._register_right_pulse()

    def _left_test_pulse_callback(self, _message: Empty) -> None:
        self._register_left_pulse()

    def _right_test_pulse_callback(self, _message: Empty) -> None:
        self._register_right_pulse()

    def _register_left_pulse(self) -> None:
        with self._lock:
            direction = self._left_direction
            if self.left_hall_inverted:
                direction = -direction
            self._left_total_pulses += direction

    def _register_right_pulse(self) -> None:
        with self._lock:
            direction = self._right_direction
            if self.right_hall_inverted:
                direction = -direction
            self._right_total_pulses += direction

    def _publish_odometry(self) -> None:
        now_monotonic = time.monotonic()
        dt = now_monotonic - self._last_publish_time
        if dt <= 0.0:
            return
        self._last_publish_time = now_monotonic

        with self._lock:
            left_total = self._left_total_pulses
            right_total = self._right_total_pulses

        delta_left_pulses = left_total - self._left_last_pulses
        delta_right_pulses = right_total - self._right_last_pulses
        self._left_last_pulses = left_total
        self._right_last_pulses = right_total

        delta_left = delta_left_pulses * self.meters_per_pulse
        delta_right = delta_right_pulses * self.meters_per_pulse

        delta_distance = (delta_right + delta_left) / 2.0
        delta_yaw = (delta_right - delta_left) / self.track_width_m
        midpoint_yaw = self.yaw + delta_yaw / 2.0

        self.x += delta_distance * math.cos(midpoint_yaw)
        self.y += delta_distance * math.sin(midpoint_yaw)
        self.yaw = math.atan2(
            math.sin(self.yaw + delta_yaw),
            math.cos(self.yaw + delta_yaw),
        )

        linear_velocity = delta_distance / dt
        angular_velocity = delta_yaw / dt

        stamp = self.get_clock().now().to_msg()
        quaternion_z = math.sin(self.yaw / 2.0)
        quaternion_w = math.cos(self.yaw / 2.0)

        odom = Odometry()
        odom.header.stamp = stamp
        odom.header.frame_id = self.odom_frame
        odom.child_frame_id = self.base_frame
        odom.pose.pose.position.x = self.x
        odom.pose.pose.position.y = self.y
        odom.pose.pose.orientation.z = quaternion_z
        odom.pose.pose.orientation.w = quaternion_w
        odom.twist.twist.linear.x = linear_velocity
        odom.twist.twist.angular.z = angular_velocity

        # Tracked vehicles slip, so lateral and yaw uncertainty must not be
        # presented as unrealistically small to sensor fusion.
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

        if self.publish_tf:
            transform = TransformStamped()
            transform.header.stamp = stamp
            transform.header.frame_id = self.odom_frame
            transform.child_frame_id = self.base_frame
            transform.transform.translation.x = self.x
            transform.transform.translation.y = self.y
            transform.transform.rotation.z = quaternion_z
            transform.transform.rotation.w = quaternion_w
            self.tf_broadcaster.sendTransform(transform)

    def _close_inputs(self) -> None:
        for device in (self.left_input, self.right_input):
            if device is not None:
                try:
                    device.close()
                except Exception:
                    pass
        self.left_input = None
        self.right_input = None

    def destroy_node(self) -> bool:
        self._close_inputs()
        return super().destroy_node()


def main(args: Optional[list[str]] = None) -> None:
    rclpy.init(args=args)
    node = HallOdometryNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
