#!/usr/bin/env python3
"""Bridge ROS 2 drive, actuator, brush and auxiliary motor commands to ESP32."""

from __future__ import annotations

import json
import math
import threading
import time
from typing import Optional

import rclpy
from geometry_msgs.msg import Twist
from rclpy.node import Node
from std_msgs.msg import (
    Float32,
    Float32MultiArray,
    Int8,
    Int8MultiArray,
    Int64MultiArray,
    String,
)
from std_srvs.srv import SetBool, Trigger

try:
    import serial
except ImportError:
    serial = None  # type: ignore[assignment]


AUX_MOTOR_SEQUENCE_MAGIC = 0xA5000000
AUX_MOTOR_VALUE_MASK = 0x7FF


class Esp32TrackBridgeNode(Node):
    """Send ROS commands and receive ESP32 track/Hall/RC/aux telemetry."""

    def __init__(self) -> None:
        super().__init__('esp32_track_bridge_node')

        self.declare_parameter('dry_run', True)
        self.declare_parameter('serial_port', '/dev/esp32drive')
        self.declare_parameter('baud_rate', 115200)
        self.declare_parameter('track_width_m', 0.60)
        self.declare_parameter('max_track_speed_mps', 0.50)
        self.declare_parameter('command_timeout_sec', 0.50)
        self.declare_parameter('aux_command_timeout_sec', 0.50)
        self.declare_parameter('send_rate_hz', 20.0)
        self.declare_parameter('auto_arm', False)

        self.dry_run = bool(self.get_parameter('dry_run').value)
        self.serial_port = str(self.get_parameter('serial_port').value)
        self.baud_rate = int(self.get_parameter('baud_rate').value)
        self.track_width_m = float(self.get_parameter('track_width_m').value)
        self.max_track_speed_mps = float(
            self.get_parameter('max_track_speed_mps').value
        )
        self.command_timeout_sec = float(
            self.get_parameter('command_timeout_sec').value
        )
        self.aux_command_timeout_sec = float(
            self.get_parameter('aux_command_timeout_sec').value
        )
        send_rate_hz = float(self.get_parameter('send_rate_hz').value)
        self.auto_arm = bool(self.get_parameter('auto_arm').value)

        for name, value in (
            ('track_width_m', self.track_width_m),
            ('max_track_speed_mps', self.max_track_speed_mps),
            ('command_timeout_sec', self.command_timeout_sec),
            ('aux_command_timeout_sec', self.aux_command_timeout_sec),
            ('send_rate_hz', send_rate_hz),
        ):
            if value <= 0.0:
                raise ValueError(f'{name} must be greater than zero')

        self._lock = threading.RLock()
        self._serial = None
        self._reader_thread: Optional[threading.Thread] = None
        self._reader_stop = threading.Event()
        self._sequence = 0
        self._requested_left = 0
        self._requested_right = 0
        self._requested_actuator = 0
        self._requested_brush = 0
        self._requested_aux_motor = 0  # -1000..1000
        self._last_cmd_time = 0.0
        self._last_actuator_cmd_time = 0.0
        self._last_brush_cmd_time = 0.0
        self._last_aux_motor_cmd_time = 0.0
        self._armed_requested = False
        self._last_telemetry: dict = {}
        self._connected = False

        self.status_publisher = self.create_publisher(
            String, '/drive/esp32_status', 10
        )
        self.tick_publisher = self.create_publisher(
            Int64MultiArray, '/drive/hall_ticks', 20
        )
        self.pulse_rate_publisher = self.create_publisher(
            Float32MultiArray, '/drive/hall_pps', 20
        )
        self.direction_publisher = self.create_publisher(
            Int8MultiArray, '/drive/direction', 20
        )
        self.actuator_state_publisher = self.create_publisher(
            Int8, '/actuator/state', 20
        )
        self.brush_state_publisher = self.create_publisher(
            Float32MultiArray, '/brush/state', 20
        )
        self.aux_motor_state_publisher = self.create_publisher(
            Float32MultiArray, '/aux_motor/state', 20
        )

        self.create_subscription(Twist, '/cmd_vel', self._cmd_vel_callback, 20)
        self.create_subscription(
            Int8, '/actuator/command', self._actuator_command_callback, 20
        )
        self.create_subscription(
            Float32, '/brush/command', self._brush_command_callback, 20
        )
        self.create_subscription(
            Float32, '/aux_motor/command', self._aux_motor_command_callback, 20
        )
        self.create_service(SetBool, '/drive/arm', self._arm_service)
        self.create_service(Trigger, '/drive/estop', self._estop_service)
        self.create_service(Trigger, '/drive/reconnect', self._reconnect_service)
        self.create_timer(1.0 / send_rate_hz, self._send_tick)
        self.create_timer(1.0, self._publish_status)

        self._open_serial()
        if self.auto_arm:
            self.get_logger().warning(
                'auto_arm is enabled; verify lifted tracks and emergency stop'
            )
            self._armed_requested = True
            self._send_arm(True)

        self.get_logger().info(
            'ESP32 bridge started in %s mode; port=%s, baud=%d; '
            'aux topics=/actuator/command,/brush/command,/aux_motor/command'
            % (
                'DRY-RUN' if self.dry_run else 'HARDWARE',
                self.serial_port,
                self.baud_rate,
            )
        )

    @staticmethod
    def _checksum(body: str) -> int:
        value = 0
        for byte in body.encode('ascii', errors='strict'):
            value ^= byte
        return value

    def _next_sequence(self) -> int:
        with self._lock:
            self._sequence = (self._sequence + 1) & 0x0FFFFFFF
            return self._sequence

    @staticmethod
    def _aux_motor_sequence(command: int) -> int:
        command = max(-1000, min(1000, int(command)))
        return AUX_MOTOR_SEQUENCE_MAGIC | ((command + 1000) & AUX_MOTOR_VALUE_MASK)

    def _write_body(self, body: str) -> bool:
        frame = f'{body}*{self._checksum(body):02X}\n'.encode('ascii')
        if self.dry_run:
            return True
        with self._lock:
            connection = self._serial
        if connection is None:
            return False
        try:
            connection.write(frame)
            return True
        except Exception as exc:
            self.get_logger().error(f'ESP32 serial write failed: {exc}')
            self._mark_disconnected()
            return False

    def _open_serial(self) -> None:
        self._close_serial()
        if self.dry_run:
            self._connected = True
            return
        if serial is None:
            self.get_logger().error(
                'python3-serial is not installed; ESP32 bridge remains offline'
            )
            return
        try:
            connection = serial.Serial(
                self.serial_port,
                self.baud_rate,
                timeout=0.10,
                write_timeout=0.20,
            )
        except Exception as exc:
            self.get_logger().error(
                f'Cannot open ESP32 serial port {self.serial_port}: {exc}'
            )
            return
        with self._lock:
            self._serial = connection
            self._connected = True
        self._reader_stop.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name='esp32-track-reader',
            daemon=True,
        )
        self._reader_thread.start()

    def _close_serial(self) -> None:
        self._reader_stop.set()
        with self._lock:
            connection = self._serial
            self._serial = None
            self._connected = False
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
        thread = self._reader_thread
        self._reader_thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=0.5)

    def _mark_disconnected(self) -> None:
        with self._lock:
            self._connected = False
            connection = self._serial
            self._serial = None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def _reader_loop(self) -> None:
        while not self._reader_stop.is_set():
            with self._lock:
                connection = self._serial
            if connection is None:
                return
            try:
                raw = connection.readline()
            except Exception as exc:
                self.get_logger().error(f'ESP32 serial read failed: {exc}')
                self._mark_disconnected()
                return
            if not raw:
                continue
            try:
                line = raw.decode('ascii', errors='strict').strip()
                self._process_line(line)
            except Exception as exc:
                self.get_logger().warning(f'Bad ESP32 frame: {exc}')

    def _process_line(self, line: str) -> None:
        if not line:
            return
        if '*' not in line:
            self.get_logger().info(f'ESP32: {line}')
            return
        body, checksum_text = line.rsplit('*', 1)
        if len(checksum_text) != 2:
            raise ValueError('invalid checksum length')
        if int(checksum_text, 16) != self._checksum(body):
            raise ValueError('checksum mismatch')

        fields = body.split(',')
        if not fields:
            return
        if fields[0] == 'TEL':
            self._handle_telemetry(fields)
        elif fields[0] == 'AUXTEL':
            self._handle_aux_motor_telemetry(fields)
        elif fields[0] == 'ACK':
            with self._lock:
                self._last_telemetry['last_ack'] = fields[1:]
                self._last_telemetry['last_ack_time'] = time.time()
        elif fields[0] == 'BOOT':
            self.get_logger().info(f'ESP32 boot: {body}')

    def _handle_aux_motor_telemetry(self, fields: list[str]) -> None:
        if len(fields) < 10:
            raise ValueError(f'AUXTEL expected 10 fields, got {len(fields)}')
        telemetry = {
            'aux_motor_millis': int(fields[1]),
            'aux_motor_rc_us': int(fields[2]),
            'aux_motor_rc_valid': bool(int(fields[3])),
            'aux_motor_ros_command': int(fields[4]),
            'aux_motor_target': int(fields[5]),
            'aux_motor_actual': int(fields[6]),
            'aux_motor_direction': int(fields[7]),
            'aux_motor_throttle_mv': int(fields[8]),
            'aux_motor_mcp_ready': bool(int(fields[9])),
            'aux_motor_received_at': time.time(),
        }
        with self._lock:
            self._last_telemetry.update(telemetry)
            self._connected = True
        state = Float32MultiArray()
        state.data = [
            float(telemetry['aux_motor_ros_command']) / 1000.0,
            float(telemetry['aux_motor_target']) / 1000.0,
            float(telemetry['aux_motor_actual']) / 1000.0,
            float(telemetry['aux_motor_direction']),
            float(telemetry['aux_motor_throttle_mv']) / 1000.0,
            1.0 if telemetry['aux_motor_mcp_ready'] else 0.0,
            float(telemetry['aux_motor_rc_us']),
            1.0 if telemetry['aux_motor_rc_valid'] else 0.0,
        ]
        self.aux_motor_state_publisher.publish(state)

    def _handle_telemetry(self, fields: list[str]) -> None:
        if len(fields) < 13:
            raise ValueError(f'TEL expected at least 13 fields, got {len(fields)}')
        telemetry = {
            'millis': int(fields[1]),
            'armed': bool(int(fields[2])),
            'estop_ok': bool(int(fields[3])),
            'target_left': int(fields[4]),
            'target_right': int(fields[5]),
            'actual_left': int(fields[6]),
            'actual_right': int(fields[7]),
            'ticks_left': int(fields[8]),
            'ticks_right': int(fields[9]),
            'pps_left': float(fields[10]),
            'pps_right': float(fields[11]),
            'watchdog': bool(int(fields[12])),
            'received_at': time.time(),
        }
        if len(fields) >= 19:
            telemetry.update({
                'control_mode': fields[13],
                'rc_ch1_us': int(fields[14]),
                'rc_ch2_us': int(fields[15]),
                'rc_mode_us': int(fields[16]),
                'rc_arm_us': int(fields[17]),
                'rc_valid': bool(int(fields[18])),
            })
        if len(fields) >= 20:
            telemetry['rc_input_mode'] = fields[19]
        if len(fields) >= 24:
            telemetry.update({
                'actuator_rc_us': int(fields[20]),
                'actuator_rc_valid': bool(int(fields[21])),
                'actuator_direction': int(fields[22]),
                'actuator_timeout': bool(int(fields[23])),
            })
        if len(fields) >= 30:
            telemetry.update({
                'brush_rc_us': int(fields[24]),
                'brush_rc_valid': bool(int(fields[25])),
                'brush_ros_command': int(fields[26]),
                'brush_throttle_mv': int(fields[27]),
                'brush_brake': bool(int(fields[28])),
                'brush_mcp_ready': bool(int(fields[29])),
            })
        with self._lock:
            # Preserve AUXTEL fields that arrive independently.
            aux_fields = {
                key: value for key, value in self._last_telemetry.items()
                if key.startswith('aux_motor_')
            }
            self._last_telemetry = telemetry
            self._last_telemetry.update(aux_fields)
            self._connected = True

        ticks = Int64MultiArray()
        ticks.data = [telemetry['ticks_left'], telemetry['ticks_right']]
        self.tick_publisher.publish(ticks)
        pps = Float32MultiArray()
        pps.data = [telemetry['pps_left'], telemetry['pps_right']]
        self.pulse_rate_publisher.publish(pps)
        direction = Int8MultiArray()
        direction.data = [self._sign(telemetry['actual_left']), self._sign(telemetry['actual_right'])]
        self.direction_publisher.publish(direction)

        if 'actuator_direction' in telemetry:
            actuator = Int8()
            actuator.data = int(telemetry['actuator_direction'])
            self.actuator_state_publisher.publish(actuator)
        if 'brush_throttle_mv' in telemetry:
            brush = Float32MultiArray()
            brush.data = [
                float(telemetry.get('brush_ros_command', 0)) / 1000.0,
                float(telemetry['brush_throttle_mv']) / 1000.0,
                1.0 if telemetry.get('brush_brake', True) else 0.0,
                1.0 if telemetry.get('brush_mcp_ready', False) else 0.0,
                float(telemetry.get('brush_rc_us', 0)),
                1.0 if telemetry.get('brush_rc_valid', False) else 0.0,
            ]
            self.brush_state_publisher.publish(brush)

    @staticmethod
    def _sign(value: int) -> int:
        return 1 if value > 0 else (-1 if value < 0 else 0)

    def _cmd_vel_callback(self, message: Twist) -> None:
        linear = float(message.linear.x)
        angular = float(message.angular.z)
        if not math.isfinite(linear) or not math.isfinite(angular):
            self.get_logger().error('Rejected non-finite /cmd_vel')
            return
        half_track = self.track_width_m / 2.0
        left_mps = linear - angular * half_track
        right_mps = linear + angular * half_track
        with self._lock:
            self._requested_left = self._speed_to_command(left_mps)
            self._requested_right = self._speed_to_command(right_mps)
            self._last_cmd_time = time.monotonic()

    def _actuator_command_callback(self, message: Int8) -> None:
        value = int(message.data)
        with self._lock:
            self._requested_actuator = 1 if value > 0 else (-1 if value < 0 else 0)
            self._last_actuator_cmd_time = time.monotonic()

    def _brush_command_callback(self, message: Float32) -> None:
        value = float(message.data)
        if not math.isfinite(value):
            self.get_logger().error('Rejected non-finite /brush/command')
            return
        value = max(0.0, min(1.0, value))
        with self._lock:
            self._requested_brush = int(round(value * 1000.0))
            self._last_brush_cmd_time = time.monotonic()

    def _aux_motor_command_callback(self, message: Float32) -> None:
        value = float(message.data)
        if not math.isfinite(value):
            self.get_logger().error('Rejected non-finite /aux_motor/command')
            return
        value = max(-1.0, min(1.0, value))
        with self._lock:
            self._requested_aux_motor = int(round(value * 1000.0))
            self._last_aux_motor_cmd_time = time.monotonic()

    def _speed_to_command(self, speed_mps: float) -> int:
        normalized = max(-1.0, min(1.0, speed_mps / self.max_track_speed_mps))
        return int(round(normalized * 1000.0))

    def _send_tick(self) -> None:
        now = time.monotonic()
        with self._lock:
            drive_timed_out = now - self._last_cmd_time > self.command_timeout_sec
            actuator_timed_out = self._last_actuator_cmd_time == 0.0 or now - self._last_actuator_cmd_time > self.aux_command_timeout_sec
            brush_timed_out = self._last_brush_cmd_time == 0.0 or now - self._last_brush_cmd_time > self.aux_command_timeout_sec
            aux_motor_timed_out = self._last_aux_motor_cmd_time == 0.0 or now - self._last_aux_motor_cmd_time > self.aux_command_timeout_sec
            left = 0 if drive_timed_out else self._requested_left
            right = 0 if drive_timed_out else self._requested_right
            actuator = 0 if actuator_timed_out else self._requested_actuator
            brush = 0 if brush_timed_out else self._requested_brush
            aux_motor = 0 if aux_motor_timed_out else self._requested_aux_motor
            arm_requested = self._armed_requested

        if not arm_requested:
            left = right = actuator = brush = aux_motor = 0

        self._write_body(f'DRV,{self._next_sequence()},{left},{right}')
        # Existing firmware consumes actuator/brush from AUX. The 40-pin extension
        # decodes aux_motor from the encoded AUX sequence, so older main.cpp remains compatible.
        aux_sequence = self._aux_motor_sequence(aux_motor)
        self._write_body(f'AUX,{aux_sequence},{actuator},{brush}')

    def _send_arm(self, value: bool) -> bool:
        return self._write_body(f'ARM,{self._next_sequence()},{1 if value else 0}')

    def _arm_service(self, request: SetBool.Request, response: SetBool.Response) -> SetBool.Response:
        with self._lock:
            self._requested_left = self._requested_right = 0
            self._requested_actuator = self._requested_brush = self._requested_aux_motor = 0
            now = time.monotonic()
            self._last_cmd_time = now
            self._last_actuator_cmd_time = now
            self._last_brush_cmd_time = now
            self._last_aux_motor_cmd_time = now
            self._armed_requested = bool(request.data)
        sent = self._send_arm(bool(request.data))
        response.success = sent
        response.message = ('ESP32 arm command sent' if request.data else 'ESP32 disarm command sent') if sent else 'ESP32 is not connected'
        return response

    def _estop_service(self, _request: Trigger.Request, response: Trigger.Response) -> Trigger.Response:
        with self._lock:
            self._armed_requested = False
            self._requested_left = self._requested_right = 0
            self._requested_actuator = self._requested_brush = self._requested_aux_motor = 0
        sent = self._write_body(f'STOP,{self._next_sequence()}')
        response.success = sent
        response.message = 'Emergency stop sent to ESP32' if sent else 'ESP32 is not connected'
        return response

    def _reconnect_service(self, _request: Trigger.Request, response: Trigger.Response) -> Trigger.Response:
        self._open_serial()
        response.success = self._connected
        response.message = f'Connected to {self.serial_port}' if self._connected else f'Cannot connect to {self.serial_port}'
        return response

    def _publish_status(self) -> None:
        with self._lock:
            payload = {
                'connected': self._connected,
                'dry_run': self.dry_run,
                'serial_port': self.serial_port,
                'board': 'ESP32_WROOM_40PIN',
                'arm_requested': self._armed_requested,
                'requested_left': self._requested_left,
                'requested_right': self._requested_right,
                'requested_actuator': self._requested_actuator,
                'requested_brush': self._requested_brush,
                'requested_aux_motor': self._requested_aux_motor,
                'telemetry': dict(self._last_telemetry),
            }
        message = String()
        message.data = json.dumps(payload, ensure_ascii=False)
        self.status_publisher.publish(message)

    def destroy_node(self) -> bool:
        try:
            self._armed_requested = False
            self._requested_actuator = self._requested_brush = self._requested_aux_motor = 0
            self._write_body(f'AUX,{self._aux_motor_sequence(0)},0,0')
            self._write_body(f'STOP,{self._next_sequence()}')
        except Exception:
            pass
        self._close_serial()
        return super().destroy_node()


def main(args: Optional[list[str]] = None) -> None:
    rclpy.init(args=args)
    node = Esp32TrackBridgeNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
