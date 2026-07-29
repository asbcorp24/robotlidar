#!/usr/bin/env python3
"""Discrete GPIO drive controller for a two-track vehicle."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Dict, Optional

import rclpy
from geometry_msgs.msg import Twist
from rclpy.node import Node
from std_msgs.msg import Int8MultiArray

try:
    from gpiozero import OutputDevice
except ImportError:
    OutputDevice = None  # type: ignore[assignment]


@dataclass(frozen=True)
class DriveState:
    left: int
    right: int

    def normalized(self) -> "DriveState":
        return DriveState(_sign(self.left), _sign(self.right))


def _sign(value: float, deadband: float = 0.0) -> int:
    if value > deadband:
        return 1
    if value < -deadband:
        return -1
    return 0


class MotorGpioNode(Node):
    """Convert /cmd_vel into four mutually interlocked GPIO outputs."""

    def __init__(self) -> None:
        super().__init__('motor_gpio_node')

        self.declare_parameter('dry_run', True)
        self.declare_parameter('active_high', True)
        self.declare_parameter('left_forward_pin', 17)
        self.declare_parameter('left_reverse_pin', 27)
        self.declare_parameter('right_forward_pin', 22)
        self.declare_parameter('right_reverse_pin', 23)
        self.declare_parameter('track_width_m', 0.60)
        self.declare_parameter('wheel_command_deadband', 0.05)
        self.declare_parameter('command_timeout_sec', 0.50)
        self.declare_parameter('reverse_pause_sec', 0.35)
        self.declare_parameter('control_rate_hz', 50.0)

        self.dry_run = bool(self.get_parameter('dry_run').value)
        self.active_high = bool(self.get_parameter('active_high').value)
        self.track_width_m = float(self.get_parameter('track_width_m').value)
        self.deadband = float(self.get_parameter('wheel_command_deadband').value)
        self.command_timeout_sec = float(
            self.get_parameter('command_timeout_sec').value
        )
        self.reverse_pause_sec = float(
            self.get_parameter('reverse_pause_sec').value
        )
        control_rate_hz = float(self.get_parameter('control_rate_hz').value)

        if self.track_width_m <= 0.0:
            raise ValueError('track_width_m must be greater than zero')
        if control_rate_hz <= 0.0:
            raise ValueError('control_rate_hz must be greater than zero')
        if self.command_timeout_sec <= 0.0:
            raise ValueError('command_timeout_sec must be greater than zero')
        if self.reverse_pause_sec < 0.0:
            raise ValueError('reverse_pause_sec cannot be negative')

        self.pin_numbers = {
            'left_forward': int(self.get_parameter('left_forward_pin').value),
            'left_reverse': int(self.get_parameter('left_reverse_pin').value),
            'right_forward': int(self.get_parameter('right_forward_pin').value),
            'right_reverse': int(self.get_parameter('right_reverse_pin').value),
        }

        self.outputs: Dict[str, OutputDevice] = {}
        self._configure_gpio()

        self.current_state = DriveState(0, 0)
        self.requested_state = DriveState(0, 0)
        self.pending_state: Optional[DriveState] = None
        self.pending_apply_time = 0.0
        self.last_cmd_time = time.monotonic()
        self.timed_out = True

        self.direction_publisher = self.create_publisher(
            Int8MultiArray, '/drive/direction', 10
        )
        self.create_subscription(Twist, '/cmd_vel', self._cmd_vel_callback, 10)
        self.create_timer(1.0 / control_rate_hz, self._control_tick)

        self._apply_state(DriveState(0, 0), force_publish=True)
        self.get_logger().info(
            'Motor GPIO node started in %s mode; BCM pins: %s'
            % ('DRY-RUN' if self.dry_run else 'HARDWARE', self.pin_numbers)
        )

    def _configure_gpio(self) -> None:
        if self.dry_run:
            return

        if OutputDevice is None:
            self.get_logger().error(
                'gpiozero is unavailable; switching to dry-run mode for safety'
            )
            self.dry_run = True
            return

        try:
            for name, pin in self.pin_numbers.items():
                self.outputs[name] = OutputDevice(
                    pin=pin,
                    active_high=self.active_high,
                    initial_value=False,
                )
        except Exception:
            self._close_outputs()
            self.dry_run = True
            self.get_logger().exception(
                'GPIO initialization failed; switching to dry-run mode'
            )

    def _cmd_vel_callback(self, message: Twist) -> None:
        linear = float(message.linear.x)
        angular = float(message.angular.z)

        if not math.isfinite(linear) or not math.isfinite(angular):
            self.get_logger().error('Rejected non-finite /cmd_vel command')
            self.requested_state = DriveState(0, 0)
            return

        half_track = self.track_width_m / 2.0
        left_target = linear - angular * half_track
        right_target = linear + angular * half_track

        self.requested_state = DriveState(
            _sign(left_target, self.deadband),
            _sign(right_target, self.deadband),
        )
        self.last_cmd_time = time.monotonic()
        self.timed_out = False

    def _control_tick(self) -> None:
        now = time.monotonic()

        if now - self.last_cmd_time > self.command_timeout_sec:
            if not self.timed_out:
                self.get_logger().warning('Command timeout: stopping both tracks')
            self.timed_out = True
            self.requested_state = DriveState(0, 0)
            self.pending_state = None

        if self.pending_state is not None:
            if now >= self.pending_apply_time:
                target = self.pending_state
                self.pending_state = None
                self._apply_state(target)
            return

        target = self.requested_state.normalized()
        if target == self.current_state:
            return

        if self._requires_reverse_pause(self.current_state, target):
            self._apply_state(DriveState(0, 0))
            self.pending_state = target
            self.pending_apply_time = now + self.reverse_pause_sec
            return

        self._apply_state(target)

    @staticmethod
    def _requires_reverse_pause(current: DriveState, target: DriveState) -> bool:
        left_reversing = current.left != 0 and target.left == -current.left
        right_reversing = current.right != 0 and target.right == -current.right
        return left_reversing or right_reversing

    def _apply_state(
        self, state: DriveState, *, force_publish: bool = False
    ) -> None:
        state = state.normalized()

        # First switch every output off. Forward and reverse of one track can
        # therefore never be active at the same time.
        if not self.dry_run:
            for output in self.outputs.values():
                output.off()

            if state.left > 0:
                self.outputs['left_forward'].on()
            elif state.left < 0:
                self.outputs['left_reverse'].on()

            if state.right > 0:
                self.outputs['right_forward'].on()
            elif state.right < 0:
                self.outputs['right_reverse'].on()

        changed = state != self.current_state
        self.current_state = state
        if changed or force_publish:
            direction = Int8MultiArray()
            direction.data = [state.left, state.right]
            self.direction_publisher.publish(direction)
            self.get_logger().info(
                f'Drive state: left={state.left}, right={state.right}'
            )

    def _close_outputs(self) -> None:
        for output in self.outputs.values():
            try:
                output.off()
                output.close()
            except Exception:
                pass
        self.outputs.clear()

    def destroy_node(self) -> bool:
        self.requested_state = DriveState(0, 0)
        self.pending_state = None
        self._apply_state(DriveState(0, 0), force_publish=True)
        self._close_outputs()
        return super().destroy_node()


def main(args: Optional[list[str]] = None) -> None:
    rclpy.init(args=args)
    node = MotorGpioNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
