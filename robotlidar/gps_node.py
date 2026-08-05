#!/usr/bin/env python3
"""Read a NMEA GPS receiver and publish optional low-weight odometry assistance.

The node is intentionally independent of gpsd and internet access. It reads the
NEO-6M (or another NMEA receiver) directly from a UART, publishes the standard
``/gps/fix`` topic and a JSON diagnostics topic, and can provide an additional
``/gps/odom`` position measurement for robot_localization.

GPS is never required for motion. Invalid, stale or implausible fixes are not
published as odometry, so Hall/IMU/LiDAR navigation continues indoors.
"""

from __future__ import annotations

import json
import math
import queue
import threading
import time
from dataclasses import dataclass
from typing import Optional

import rclpy
import serial
from nav_msgs.msg import Odometry
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import NavSatFix, NavSatStatus
from std_msgs.msg import Bool, String
from std_srvs.srv import Trigger


EARTH_RADIUS_M = 6378137.0
KNOT_TO_MPS = 0.514444


@dataclass
class WheelPose:
    x: float = 0.0
    y: float = 0.0
    yaw: float = 0.0
    received_monotonic: float = 0.0


class GpsNode(Node):
    """UART NMEA reader with quality gates and odometry-frame alignment."""

    def __init__(self) -> None:
        super().__init__('gps_node')

        self.declare_parameter('port', '/dev/ttyS0')
        self.declare_parameter('baudrate', 9600)
        self.declare_parameter('frame_id', 'gps_link')
        self.declare_parameter('odom_frame', 'odom')
        self.declare_parameter('wheel_odom_topic', '/wheel/odom')
        self.declare_parameter('fix_topic', '/gps/fix')
        self.declare_parameter('status_topic', '/gps/status')
        self.declare_parameter('assist_odom_topic', '/gps/odom')
        self.declare_parameter('assist_ready_topic', '/gps/assist_ready')
        self.declare_parameter('publish_assist_odom', True)
        self.declare_parameter('min_satellites', 4)
        self.declare_parameter('max_hdop', 4.0)
        self.declare_parameter('stale_timeout_sec', 3.0)
        self.declare_parameter('max_jump_m', 15.0)
        self.declare_parameter('max_plausible_speed_mps', 8.0)
        self.declare_parameter('position_sigma_floor_m', 2.5)
        self.declare_parameter('hdop_sigma_scale', 1.5)
        self.declare_parameter('alignment_distance_m', 5.0)
        self.declare_parameter('course_alignment_min_speed_mps', 0.8)
        self.declare_parameter('wheel_odom_max_age_sec', 2.0)
        self.declare_parameter('use_fixed_heading_offset', False)
        self.declare_parameter('fixed_heading_offset_deg', 0.0)
        self.declare_parameter('status_rate_hz', 2.0)
        self.declare_parameter('reconnect_interval_sec', 2.0)

        self.port = str(self.get_parameter('port').value)
        self.baudrate = int(self.get_parameter('baudrate').value)
        self.frame_id = str(self.get_parameter('frame_id').value)
        self.odom_frame = str(self.get_parameter('odom_frame').value)
        self.publish_assist_odom = bool(
            self.get_parameter('publish_assist_odom').value
        )
        self.min_satellites = int(self.get_parameter('min_satellites').value)
        self.max_hdop = float(self.get_parameter('max_hdop').value)
        self.stale_timeout_sec = float(
            self.get_parameter('stale_timeout_sec').value
        )
        self.max_jump_m = float(self.get_parameter('max_jump_m').value)
        self.max_plausible_speed_mps = float(
            self.get_parameter('max_plausible_speed_mps').value
        )
        self.position_sigma_floor_m = float(
            self.get_parameter('position_sigma_floor_m').value
        )
        self.hdop_sigma_scale = float(
            self.get_parameter('hdop_sigma_scale').value
        )
        self.alignment_distance_m = float(
            self.get_parameter('alignment_distance_m').value
        )
        self.course_alignment_min_speed_mps = float(
            self.get_parameter('course_alignment_min_speed_mps').value
        )
        self.wheel_odom_max_age_sec = float(
            self.get_parameter('wheel_odom_max_age_sec').value
        )
        self.use_fixed_heading_offset = bool(
            self.get_parameter('use_fixed_heading_offset').value
        )
        self.fixed_heading_offset_rad = math.radians(
            float(self.get_parameter('fixed_heading_offset_deg').value)
        )
        self.reconnect_interval_sec = float(
            self.get_parameter('reconnect_interval_sec').value
        )
        status_rate_hz = float(self.get_parameter('status_rate_hz').value)
        if status_rate_hz <= 0.0:
            raise ValueError('status_rate_hz must be greater than zero')

        fix_topic = str(self.get_parameter('fix_topic').value)
        status_topic = str(self.get_parameter('status_topic').value)
        assist_odom_topic = str(
            self.get_parameter('assist_odom_topic').value
        )
        assist_ready_topic = str(
            self.get_parameter('assist_ready_topic').value
        )
        wheel_odom_topic = str(
            self.get_parameter('wheel_odom_topic').value
        )

        self.fix_publisher = self.create_publisher(
            NavSatFix, fix_topic, qos_profile_sensor_data
        )
        self.status_publisher = self.create_publisher(String, status_topic, 10)
        self.assist_odom_publisher = self.create_publisher(
            Odometry, assist_odom_topic, qos_profile_sensor_data
        )
        self.assist_ready_publisher = self.create_publisher(
            Bool, assist_ready_topic, 10
        )
        self.create_subscription(
            Odometry,
            wheel_odom_topic,
            self._wheel_odom_callback,
            qos_profile_sensor_data,
        )
        self.create_service(Trigger, '/gps/reset_assist', self._reset_service)

        self._lock = threading.RLock()
        self._line_queue: queue.Queue[str] = queue.Queue(maxsize=500)
        self._stop_event = threading.Event()
        self._serial: Optional[serial.Serial] = None
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name='robotlidar-gps-uart',
            daemon=True,
        )

        self._connected = False
        self._connection_error: Optional[str] = None
        self._reconnect_count = 0
        self._checksum_errors = 0
        self._parse_errors = 0
        self._dropped_lines = 0
        self._sentences_received = 0
        self._last_sentence_monotonic = 0.0
        self._last_fix_monotonic = 0.0

        self._latitude: Optional[float] = None
        self._longitude: Optional[float] = None
        self._altitude_m: Optional[float] = None
        self._fix_quality = 0
        self._fix_type = 1
        self._satellites_used = 0
        self._satellites_visible = 0
        self._hdop: Optional[float] = None
        self._pdop: Optional[float] = None
        self._vdop: Optional[float] = None
        self._speed_mps: Optional[float] = None
        self._course_deg: Optional[float] = None
        self._rmc_valid: Optional[bool] = None
        self._utc_time: Optional[str] = None
        self._utc_date: Optional[str] = None
        self._reject_reason = 'waiting_for_fix'

        self._wheel_pose = WheelPose()
        self._origin_lat: Optional[float] = None
        self._origin_lon: Optional[float] = None
        self._origin_wheel_x = 0.0
        self._origin_wheel_y = 0.0
        self._origin_wheel_yaw = 0.0
        self._alignment_rotation_rad: Optional[float] = None
        self._alignment_source: Optional[str] = None
        self._alignment_state = 'disabled' if not self.publish_assist_odom else 'waiting_fix'
        self._local_x: Optional[float] = None
        self._local_y: Optional[float] = None
        self._last_accepted_lat: Optional[float] = None
        self._last_accepted_lon: Optional[float] = None
        self._last_accepted_monotonic = 0.0

        self.create_timer(0.05, self._process_lines)
        self.create_timer(1.0 / status_rate_hz, self._publish_status)
        self._reader_thread.start()

        self.get_logger().info(
            f'GPS UART: {self.port} at {self.baudrate} baud; '
            f'assist odometry: {self.publish_assist_odom}'
        )

    def _reader_loop(self) -> None:
        """Continuously reconnect and move complete NMEA lines into a queue."""
        while not self._stop_event.is_set():
            try:
                connection = serial.Serial(
                    port=self.port,
                    baudrate=self.baudrate,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=1.0,
                )
            except Exception as exc:
                with self._lock:
                    self._connected = False
                    self._connection_error = str(exc)
                self._stop_event.wait(self.reconnect_interval_sec)
                continue

            with self._lock:
                self._serial = connection
                self._connected = True
                self._connection_error = None
                self._reconnect_count += 1

            try:
                while not self._stop_event.is_set():
                    raw = connection.readline()
                    if not raw:
                        continue
                    line = raw.decode('ascii', errors='ignore').strip()
                    if not line:
                        continue
                    try:
                        self._line_queue.put_nowait(line)
                    except queue.Full:
                        try:
                            self._line_queue.get_nowait()
                        except queue.Empty:
                            pass
                        self._dropped_lines += 1
                        try:
                            self._line_queue.put_nowait(line)
                        except queue.Full:
                            pass
            except Exception as exc:
                with self._lock:
                    self._connection_error = str(exc)
            finally:
                try:
                    connection.close()
                except Exception:
                    pass
                with self._lock:
                    if self._serial is connection:
                        self._serial = None
                    self._connected = False

            self._stop_event.wait(self.reconnect_interval_sec)

    def _process_lines(self) -> None:
        for _ in range(100):
            try:
                line = self._line_queue.get_nowait()
            except queue.Empty:
                break
            try:
                self._parse_sentence(line)
            except Exception as exc:
                with self._lock:
                    self._parse_errors += 1
                    self._reject_reason = f'parse_error:{type(exc).__name__}'

    def _parse_sentence(self, line: str) -> None:
        if not line.startswith('$'):
            return
        payload = self._checked_payload(line)
        if payload is None:
            return

        fields = payload.split(',')
        sentence = fields[0][-3:].upper()
        now = time.monotonic()
        with self._lock:
            self._sentences_received += 1
            self._last_sentence_monotonic = now

        if sentence == 'GGA':
            self._parse_gga(fields, now)
        elif sentence == 'RMC':
            self._parse_rmc(fields)
        elif sentence == 'GSA':
            self._parse_gsa(fields)
        elif sentence == 'GSV':
            self._parse_gsv(fields)

    def _checked_payload(self, line: str) -> Optional[str]:
        body = line[1:]
        if '*' not in body:
            return body
        payload, checksum_text = body.split('*', 1)
        checksum_text = checksum_text[:2]
        checksum = 0
        for character in payload:
            checksum ^= ord(character)
        try:
            expected = int(checksum_text, 16)
        except ValueError:
            with self._lock:
                self._checksum_errors += 1
            return None
        if checksum != expected:
            with self._lock:
                self._checksum_errors += 1
            return None
        return payload

    def _parse_gga(self, fields: list[str], now: float) -> None:
        if len(fields) < 10:
            raise ValueError('short GGA sentence')

        latitude = self._parse_coordinate(fields[2], fields[3], latitude=True)
        longitude = self._parse_coordinate(fields[4], fields[5], latitude=False)
        fix_quality = self._to_int(fields[6], 0)
        satellites = self._to_int(fields[7], 0)
        hdop = self._to_float(fields[8])
        altitude = self._to_float(fields[9])

        with self._lock:
            self._utc_time = fields[1] or self._utc_time
            self._latitude = latitude
            self._longitude = longitude
            self._altitude_m = altitude
            self._fix_quality = fix_quality
            self._satellites_used = satellites
            if hdop is not None:
                self._hdop = hdop

        accepted, reason = self._fix_is_acceptable(latitude, longitude, now)
        with self._lock:
            self._reject_reason = reason
            if accepted:
                self._last_fix_monotonic = now

        self._publish_navsat_fix(accepted)
        if accepted and latitude is not None and longitude is not None:
            self._process_assist(latitude, longitude, now)

    def _parse_rmc(self, fields: list[str]) -> None:
        if len(fields) < 10:
            raise ValueError('short RMC sentence')
        speed_knots = self._to_float(fields[7])
        course_deg = self._to_float(fields[8])
        with self._lock:
            self._utc_time = fields[1] or self._utc_time
            self._rmc_valid = fields[2].upper() == 'A' if fields[2] else None
            self._speed_mps = (
                speed_knots * KNOT_TO_MPS if speed_knots is not None else None
            )
            self._course_deg = course_deg
            self._utc_date = fields[9] or self._utc_date

    def _parse_gsa(self, fields: list[str]) -> None:
        if len(fields) < 18:
            return
        with self._lock:
            self._fix_type = self._to_int(fields[2], self._fix_type)
            self._pdop = self._to_float(fields[15])
            gsa_hdop = self._to_float(fields[16])
            if gsa_hdop is not None:
                self._hdop = gsa_hdop
            self._vdop = self._to_float(fields[17])

    def _parse_gsv(self, fields: list[str]) -> None:
        if len(fields) < 4:
            return
        with self._lock:
            self._satellites_visible = self._to_int(
                fields[3], self._satellites_visible
            )

    def _fix_is_acceptable(
        self,
        latitude: Optional[float],
        longitude: Optional[float],
        now: float,
    ) -> tuple[bool, str]:
        with self._lock:
            quality = self._fix_quality
            satellites = self._satellites_used
            hdop = self._hdop
            previous_lat = self._last_accepted_lat
            previous_lon = self._last_accepted_lon
            previous_time = self._last_accepted_monotonic

        if latitude is None or longitude is None:
            return False, 'no_coordinates'
        if quality <= 0:
            return False, 'no_fix'
        if satellites < self.min_satellites:
            return False, f'few_satellites:{satellites}'
        if hdop is None:
            return False, 'no_hdop'
        if hdop > self.max_hdop:
            return False, f'hdop:{hdop:.2f}'

        if previous_lat is not None and previous_lon is not None:
            elapsed = max(0.0, now - previous_time)
            jump = self._distance_between(
                previous_lat, previous_lon, latitude, longitude
            )
            allowed = self.max_jump_m + self.max_plausible_speed_mps * elapsed
            if jump > allowed:
                return False, f'jump:{jump:.1f}m'

        with self._lock:
            self._last_accepted_lat = latitude
            self._last_accepted_lon = longitude
            self._last_accepted_monotonic = now
        return True, 'accepted'

    def _publish_navsat_fix(self, accepted: bool) -> None:
        with self._lock:
            latitude = self._latitude
            longitude = self._longitude
            altitude = self._altitude_m
            hdop = self._hdop

        message = NavSatFix()
        message.header.stamp = self.get_clock().now().to_msg()
        message.header.frame_id = self.frame_id
        message.status.service = NavSatStatus.SERVICE_GPS
        message.status.status = (
            NavSatStatus.STATUS_FIX if accepted else NavSatStatus.STATUS_NO_FIX
        )
        message.latitude = latitude if latitude is not None else math.nan
        message.longitude = longitude if longitude is not None else math.nan
        message.altitude = altitude if altitude is not None else math.nan

        sigma = self._position_sigma(hdop) if accepted else 100.0
        variance = sigma * sigma
        message.position_covariance = [
            variance, 0.0, 0.0,
            0.0, variance, 0.0,
            0.0, 0.0, variance * 4.0,
        ]
        message.position_covariance_type = (
            NavSatFix.COVARIANCE_TYPE_APPROXIMATED
        )
        self.fix_publisher.publish(message)

    def _process_assist(
        self, latitude: float, longitude: float, now: float
    ) -> None:
        if not self.publish_assist_odom:
            return

        with self._lock:
            wheel = WheelPose(
                self._wheel_pose.x,
                self._wheel_pose.y,
                self._wheel_pose.yaw,
                self._wheel_pose.received_monotonic,
            )
            speed_mps = self._speed_mps
            course_deg = self._course_deg

        wheel_fresh = (
            wheel.received_monotonic > 0.0
            and now - wheel.received_monotonic <= self.wheel_odom_max_age_sec
        )

        with self._lock:
            if self._origin_lat is None or self._origin_lon is None:
                self._origin_lat = latitude
                self._origin_lon = longitude
                self._origin_wheel_x = wheel.x if wheel_fresh else 0.0
                self._origin_wheel_y = wheel.y if wheel_fresh else 0.0
                self._origin_wheel_yaw = wheel.yaw if wheel_fresh else 0.0
                self._alignment_rotation_rad = None
                self._alignment_source = None
                self._alignment_state = 'waiting_motion'
                if self.use_fixed_heading_offset:
                    self._alignment_rotation_rad = self.fixed_heading_offset_rad
                    self._alignment_source = 'fixed_parameter'
                    self._alignment_state = 'aligned'

            origin_lat = self._origin_lat
            origin_lon = self._origin_lon
            origin_wheel_x = self._origin_wheel_x
            origin_wheel_y = self._origin_wheel_y
            rotation = self._alignment_rotation_rad

        east, north = self._local_enu(
            origin_lat, origin_lon, latitude, longitude
        )

        if rotation is None and wheel_fresh:
            rotation = self._try_course_alignment(
                wheel, speed_mps, course_deg
            )
            if rotation is None:
                rotation = self._try_displacement_alignment(wheel, east, north)

        with self._lock:
            rotation = self._alignment_rotation_rad
            if rotation is None:
                self._local_x = None
                self._local_y = None
                return

        cos_r = math.cos(rotation)
        sin_r = math.sin(rotation)
        local_x = origin_wheel_x + cos_r * east - sin_r * north
        local_y = origin_wheel_y + sin_r * east + cos_r * north

        with self._lock:
            self._local_x = local_x
            self._local_y = local_y
            hdop = self._hdop

        message = Odometry()
        message.header.stamp = self.get_clock().now().to_msg()
        message.header.frame_id = self.odom_frame
        message.child_frame_id = self.frame_id
        message.pose.pose.position.x = local_x
        message.pose.pose.position.y = local_y
        message.pose.pose.orientation.w = 1.0

        sigma = self._position_sigma(hdop)
        variance = sigma * sigma
        very_large = 1.0e6
        message.pose.covariance = [
            variance, 0.0, 0.0, 0.0, 0.0, 0.0,
            0.0, variance, 0.0, 0.0, 0.0, 0.0,
            0.0, 0.0, very_large, 0.0, 0.0, 0.0,
            0.0, 0.0, 0.0, very_large, 0.0, 0.0,
            0.0, 0.0, 0.0, 0.0, very_large, 0.0,
            0.0, 0.0, 0.0, 0.0, 0.0, very_large,
        ]
        self.assist_odom_publisher.publish(message)

    def _try_course_alignment(
        self,
        wheel: WheelPose,
        speed_mps: Optional[float],
        course_deg: Optional[float],
    ) -> Optional[float]:
        if (
            speed_mps is None
            or course_deg is None
            or speed_mps < self.course_alignment_min_speed_mps
        ):
            return None

        # NMEA course is clockwise from north. ENU angle is CCW from east.
        enu_heading = math.radians(90.0 - course_deg)
        rotation = self._normalize_angle(wheel.yaw - enu_heading)
        with self._lock:
            self._alignment_rotation_rad = rotation
            self._alignment_source = 'rmc_course'
            self._alignment_state = 'aligned'
        self.get_logger().info(
            f'GPS assist aligned from course; offset={math.degrees(rotation):.1f} deg'
        )
        return rotation

    def _try_displacement_alignment(
        self,
        wheel: WheelPose,
        east: float,
        north: float,
    ) -> Optional[float]:
        gps_distance = math.hypot(east, north)
        with self._lock:
            wheel_dx = wheel.x - self._origin_wheel_x
            wheel_dy = wheel.y - self._origin_wheel_y
        wheel_distance = math.hypot(wheel_dx, wheel_dy)

        if (
            gps_distance < self.alignment_distance_m
            or wheel_distance < self.alignment_distance_m
        ):
            return None

        ratio = wheel_distance / max(gps_distance, 0.001)
        if ratio < 0.45 or ratio > 2.2:
            with self._lock:
                self._alignment_state = 'waiting_consistent_motion'
            return None

        gps_heading = math.atan2(north, east)
        wheel_heading = math.atan2(wheel_dy, wheel_dx)
        rotation = self._normalize_angle(wheel_heading - gps_heading)
        with self._lock:
            self._alignment_rotation_rad = rotation
            self._alignment_source = 'displacement'
            self._alignment_state = 'aligned'
        self.get_logger().info(
            f'GPS assist aligned after {gps_distance:.1f} m; '
            f'offset={math.degrees(rotation):.1f} deg'
        )
        return rotation

    def _wheel_odom_callback(self, message: Odometry) -> None:
        orientation = message.pose.pose.orientation
        yaw = math.atan2(
            2.0 * (
                orientation.w * orientation.z
                + orientation.x * orientation.y
            ),
            1.0 - 2.0 * (
                orientation.y * orientation.y
                + orientation.z * orientation.z
            ),
        )
        with self._lock:
            self._wheel_pose = WheelPose(
                x=float(message.pose.pose.position.x),
                y=float(message.pose.pose.position.y),
                yaw=float(yaw),
                received_monotonic=time.monotonic(),
            )

    def _reset_service(
        self, _request: Trigger.Request, response: Trigger.Response
    ) -> Trigger.Response:
        self._reset_assist()
        response.success = True
        response.message = 'GPS assist origin and alignment were reset'
        return response

    def _reset_assist(self) -> None:
        with self._lock:
            self._origin_lat = None
            self._origin_lon = None
            self._alignment_rotation_rad = None
            self._alignment_source = None
            self._local_x = None
            self._local_y = None
            self._alignment_state = (
                'waiting_fix' if self.publish_assist_odom else 'disabled'
            )
            self._last_accepted_lat = None
            self._last_accepted_lon = None
            self._last_accepted_monotonic = 0.0

    def _publish_status(self) -> None:
        now = time.monotonic()
        with self._lock:
            sentence_age = (
                now - self._last_sentence_monotonic
                if self._last_sentence_monotonic > 0.0
                else None
            )
            fix_age = (
                now - self._last_fix_monotonic
                if self._last_fix_monotonic > 0.0
                else None
            )
            online = (
                self._connected
                and sentence_age is not None
                and sentence_age <= self.stale_timeout_sec
            )
            fix_valid = (
                fix_age is not None
                and fix_age <= self.stale_timeout_sec
                and self._reject_reason == 'accepted'
            )
            assist_ready = (
                fix_valid
                and self.publish_assist_odom
                and self._alignment_rotation_rad is not None
            )
            data = {
                'online': online,
                'port': self.port,
                'baudrate': self.baudrate,
                'connection_error': self._connection_error,
                'fix_valid': fix_valid,
                'fix_quality': self._fix_quality,
                'fix_type': self._fix_type,
                'latitude': self._latitude,
                'longitude': self._longitude,
                'altitude_m': self._altitude_m,
                'satellites_used': self._satellites_used,
                'satellites_visible': self._satellites_visible,
                'hdop': self._hdop,
                'pdop': self._pdop,
                'vdop': self._vdop,
                'speed_mps': self._speed_mps,
                'course_deg': self._course_deg,
                'rmc_valid': self._rmc_valid,
                'utc_time': self._utc_time,
                'utc_date': self._utc_date,
                'sentence_age_sec': round(sentence_age, 3)
                if sentence_age is not None
                else None,
                'fix_age_sec': round(fix_age, 3)
                if fix_age is not None
                else None,
                'reject_reason': self._reject_reason,
                'assist_enabled': self.publish_assist_odom,
                'assist_ready': assist_ready,
                'alignment_state': self._alignment_state,
                'alignment_source': self._alignment_source,
                'heading_offset_deg': (
                    round(math.degrees(self._alignment_rotation_rad), 2)
                    if self._alignment_rotation_rad is not None
                    else None
                ),
                'local_x': self._local_x,
                'local_y': self._local_y,
                'sentences_received': self._sentences_received,
                'checksum_errors': self._checksum_errors,
                'parse_errors': self._parse_errors,
                'dropped_lines': self._dropped_lines,
                'reconnect_count': self._reconnect_count,
            }

        status_message = String()
        status_message.data = json.dumps(
            data, ensure_ascii=False, separators=(',', ':')
        )
        self.status_publisher.publish(status_message)

        ready_message = Bool()
        ready_message.data = bool(data['assist_ready'])
        self.assist_ready_publisher.publish(ready_message)

    def _position_sigma(self, hdop: Optional[float]) -> float:
        if hdop is None or not math.isfinite(hdop):
            return max(self.position_sigma_floor_m, 10.0)
        return max(
            self.position_sigma_floor_m,
            hdop * self.hdop_sigma_scale,
        )

    @staticmethod
    def _parse_coordinate(
        raw: str, hemisphere: str, *, latitude: bool
    ) -> Optional[float]:
        if not raw or not hemisphere:
            return None
        degree_digits = 2 if latitude else 3
        if len(raw) <= degree_digits:
            return None
        degrees = float(raw[:degree_digits])
        minutes = float(raw[degree_digits:])
        result = degrees + minutes / 60.0
        if hemisphere.upper() in ('S', 'W'):
            result = -result
        return result

    @staticmethod
    def _to_float(raw: str) -> Optional[float]:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    @staticmethod
    def _to_int(raw: str, default: int) -> int:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _local_enu(
        origin_lat: float,
        origin_lon: float,
        latitude: float,
        longitude: float,
    ) -> tuple[float, float]:
        origin_lat_rad = math.radians(origin_lat)
        east = (
            math.radians(longitude - origin_lon)
            * EARTH_RADIUS_M
            * math.cos(origin_lat_rad)
        )
        north = math.radians(latitude - origin_lat) * EARTH_RADIUS_M
        return east, north

    @classmethod
    def _distance_between(
        cls,
        lat1: float,
        lon1: float,
        lat2: float,
        lon2: float,
    ) -> float:
        east, north = cls._local_enu(lat1, lon1, lat2, lon2)
        return math.hypot(east, north)

    @staticmethod
    def _normalize_angle(angle: float) -> float:
        return math.atan2(math.sin(angle), math.cos(angle))

    def destroy_node(self) -> bool:
        self._stop_event.set()
        with self._lock:
            connection = self._serial
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        if self._reader_thread.is_alive():
            self._reader_thread.join(timeout=2.0)
        return super().destroy_node()


def main(args: Optional[list[str]] = None) -> None:
    rclpy.init(args=args)
    node = GpsNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
