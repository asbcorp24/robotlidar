#!/usr/bin/env python3
"""Publish raw MPU6050 measurements as sensor_msgs/Imu.

The node intentionally leaves orientation unavailable because MPU6050 has no
magnetometer. robot_localization should fuse angular_velocity.z with wheel
odometry while SLAM/AMCL provides the global map correction.
"""

from __future__ import annotations

import math
import struct
import time
from typing import Optional

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import Imu
from std_msgs.msg import Float32

try:
    from smbus2 import SMBus
except ImportError:
    SMBus = None  # type: ignore[assignment]


GRAVITY = 9.80665


class MPU6050Node(Node):
    """Minimal offline MPU6050 I2C driver for Raspberry Pi."""

    REG_SMPLRT_DIV = 0x19
    REG_CONFIG = 0x1A
    REG_GYRO_CONFIG = 0x1B
    REG_ACCEL_CONFIG = 0x1C
    REG_ACCEL_XOUT_H = 0x3B
    REG_PWR_MGMT_1 = 0x6B
    REG_WHO_AM_I = 0x75

    def __init__(self) -> None:
        super().__init__('mpu6050_node')

        self.declare_parameter('dry_run', True)
        self.declare_parameter('i2c_bus', 1)
        self.declare_parameter('i2c_address', 0x68)
        self.declare_parameter('frame_id', 'imu_link')
        self.declare_parameter('publish_rate_hz', 50.0)
        self.declare_parameter('calibration_samples', 500)
        self.declare_parameter('calibration_sample_delay_sec', 0.01)
        self.declare_parameter('gyro_range_dps', 500)
        self.declare_parameter('accel_range_g', 4)
        self.declare_parameter('dlpf_cfg', 4)
        self.declare_parameter('axis_sign_x', 1.0)
        self.declare_parameter('axis_sign_y', 1.0)
        self.declare_parameter('axis_sign_z', 1.0)
        self.declare_parameter('angular_velocity_variance', 0.0025)
        self.declare_parameter('linear_acceleration_variance', 0.25)

        self.dry_run = bool(self.get_parameter('dry_run').value)
        self.i2c_bus_number = int(self.get_parameter('i2c_bus').value)
        self.address = int(self.get_parameter('i2c_address').value)
        self.frame_id = str(self.get_parameter('frame_id').value)
        self.publish_rate_hz = float(self.get_parameter('publish_rate_hz').value)
        self.calibration_samples = int(
            self.get_parameter('calibration_samples').value
        )
        self.calibration_delay = float(
            self.get_parameter('calibration_sample_delay_sec').value
        )
        self.gyro_range_dps = int(self.get_parameter('gyro_range_dps').value)
        self.accel_range_g = int(self.get_parameter('accel_range_g').value)
        self.dlpf_cfg = int(self.get_parameter('dlpf_cfg').value)
        self.axis_sign = (
            float(self.get_parameter('axis_sign_x').value),
            float(self.get_parameter('axis_sign_y').value),
            float(self.get_parameter('axis_sign_z').value),
        )
        self.angular_variance = float(
            self.get_parameter('angular_velocity_variance').value
        )
        self.accel_variance = float(
            self.get_parameter('linear_acceleration_variance').value
        )

        if self.publish_rate_hz <= 0.0:
            raise ValueError('publish_rate_hz must be greater than zero')
        if self.calibration_samples < 0:
            raise ValueError('calibration_samples cannot be negative')
        if self.calibration_delay < 0.0:
            raise ValueError('calibration_sample_delay_sec cannot be negative')
        if self.dlpf_cfg not in range(0, 8):
            raise ValueError('dlpf_cfg must be between 0 and 7')
        if any(sign not in (-1.0, 1.0) for sign in self.axis_sign):
            raise ValueError('axis_sign_x/y/z must be either 1.0 or -1.0')

        self.gyro_scale, gyro_bits = self._gyro_settings(self.gyro_range_dps)
        self.accel_scale, accel_bits = self._accel_settings(self.accel_range_g)

        self.bus = None
        self.gyro_bias = [0.0, 0.0, 0.0]

        self.imu_publisher = self.create_publisher(Imu, '/imu/data_raw', 30)
        self.temperature_publisher = self.create_publisher(
            Float32, '/imu/temperature', 10
        )

        if not self.dry_run:
            self._open_and_configure(gyro_bits, accel_bits)
            self._calibrate_gyro()

        self.create_timer(1.0 / self.publish_rate_hz, self._publish)

        self.get_logger().info(
            'MPU6050 started in %s mode at 0x%02X, %.1f Hz'
            % (
                'DRY-RUN' if self.dry_run else 'HARDWARE',
                self.address,
                self.publish_rate_hz,
            )
        )

    @staticmethod
    def _gyro_settings(range_dps: int) -> tuple[float, int]:
        table = {
            250: (131.0, 0),
            500: (65.5, 1),
            1000: (32.8, 2),
            2000: (16.4, 3),
        }
        try:
            return table[range_dps]
        except KeyError as exc:
            raise ValueError(
                'gyro_range_dps must be 250, 500, 1000 or 2000'
            ) from exc

    @staticmethod
    def _accel_settings(range_g: int) -> tuple[float, int]:
        table = {
            2: (16384.0, 0),
            4: (8192.0, 1),
            8: (4096.0, 2),
            16: (2048.0, 3),
        }
        try:
            return table[range_g]
        except KeyError as exc:
            raise ValueError('accel_range_g must be 2, 4, 8 or 16') from exc

    def _open_and_configure(self, gyro_bits: int, accel_bits: int) -> None:
        if SMBus is None:
            self.get_logger().error(
                'python3-smbus2 is unavailable; switching MPU6050 to dry-run'
            )
            self.dry_run = True
            return

        try:
            self.bus = SMBus(self.i2c_bus_number)
            who_am_i = self.bus.read_byte_data(self.address, self.REG_WHO_AM_I)
            if (who_am_i & 0x7E) != 0x68:
                raise RuntimeError(
                    f'unexpected WHO_AM_I 0x{who_am_i:02X}, expected MPU6050'
                )

            self.bus.write_byte_data(self.address, self.REG_PWR_MGMT_1, 0x01)
            time.sleep(0.10)
            self.bus.write_byte_data(self.address, self.REG_CONFIG, self.dlpf_cfg)
            self.bus.write_byte_data(
                self.address, self.REG_GYRO_CONFIG, gyro_bits << 3
            )
            self.bus.write_byte_data(
                self.address, self.REG_ACCEL_CONFIG, accel_bits << 3
            )

            divider = max(0, min(255, round(1000.0 / self.publish_rate_hz) - 1))
            self.bus.write_byte_data(self.address, self.REG_SMPLRT_DIV, divider)
        except Exception:
            self.get_logger().exception(
                'MPU6050 initialization failed; switching to dry-run'
            )
            self._close_bus()
            self.dry_run = True

    def _read_raw(self) -> tuple[int, int, int, int, int, int, int]:
        if self.bus is None:
            return (0, 0, int(self.accel_scale), 0, 0, 0, 0)

        block = self.bus.read_i2c_block_data(
            self.address, self.REG_ACCEL_XOUT_H, 14
        )
        if len(block) != 14:
            raise RuntimeError(f'MPU6050 returned {len(block)} bytes, expected 14')
        return struct.unpack('>hhhhhhh', bytes(block))

    def _calibrate_gyro(self) -> None:
        if self.dry_run or self.calibration_samples == 0:
            return

        self.get_logger().info(
            'Calibrating MPU6050 gyro: keep the tractor completely still'
        )
        sums = [0.0, 0.0, 0.0]
        accepted = 0

        for _ in range(self.calibration_samples):
            try:
                _, _, _, _, gx, gy, gz = self._read_raw()
            except Exception:
                self.get_logger().exception('MPU6050 calibration read failed')
                continue

            sums[0] += gx / self.gyro_scale
            sums[1] += gy / self.gyro_scale
            sums[2] += gz / self.gyro_scale
            accepted += 1
            if self.calibration_delay:
                time.sleep(self.calibration_delay)

        if accepted == 0:
            raise RuntimeError('MPU6050 calibration produced no valid samples')

        self.gyro_bias = [value / accepted for value in sums]
        self.get_logger().info(
            'Gyro bias [deg/s]: x=%.5f y=%.5f z=%.5f'
            % tuple(self.gyro_bias)
        )

    def _publish(self) -> None:
        try:
            ax_raw, ay_raw, az_raw, temp_raw, gx_raw, gy_raw, gz_raw = (
                self._read_raw()
            )
        except Exception:
            self.get_logger().error(
                'MPU6050 read failed; no IMU message published',
                throttle_duration_sec=2.0,
            )
            return

        accel = [
            ax_raw / self.accel_scale * GRAVITY * self.axis_sign[0],
            ay_raw / self.accel_scale * GRAVITY * self.axis_sign[1],
            az_raw / self.accel_scale * GRAVITY * self.axis_sign[2],
        ]
        gyro_deg = [
            gx_raw / self.gyro_scale - self.gyro_bias[0],
            gy_raw / self.gyro_scale - self.gyro_bias[1],
            gz_raw / self.gyro_scale - self.gyro_bias[2],
        ]
        gyro = [
            math.radians(gyro_deg[0]) * self.axis_sign[0],
            math.radians(gyro_deg[1]) * self.axis_sign[1],
            math.radians(gyro_deg[2]) * self.axis_sign[2],
        ]

        message = Imu()
        message.header.stamp = self.get_clock().now().to_msg()
        message.header.frame_id = self.frame_id
        message.orientation_covariance[0] = -1.0

        message.angular_velocity.x = gyro[0]
        message.angular_velocity.y = gyro[1]
        message.angular_velocity.z = gyro[2]
        message.angular_velocity_covariance = [
            self.angular_variance, 0.0, 0.0,
            0.0, self.angular_variance, 0.0,
            0.0, 0.0, self.angular_variance,
        ]

        message.linear_acceleration.x = accel[0]
        message.linear_acceleration.y = accel[1]
        message.linear_acceleration.z = accel[2]
        message.linear_acceleration_covariance = [
            self.accel_variance, 0.0, 0.0,
            0.0, self.accel_variance, 0.0,
            0.0, 0.0, self.accel_variance,
        ]
        self.imu_publisher.publish(message)

        temperature = Float32()
        temperature.data = temp_raw / 340.0 + 36.53
        self.temperature_publisher.publish(temperature)

    def _close_bus(self) -> None:
        if self.bus is not None:
            try:
                self.bus.close()
            except Exception:
                pass
        self.bus = None

    def destroy_node(self) -> bool:
        self._close_bus()
        return super().destroy_node()


def main(args: Optional[list[str]] = None) -> None:
    rclpy.init(args=args)
    node = MPU6050Node()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
