"""Exercise the real bridge callbacks with fake ROS imports and serial transport.

Run from the repository root: python -m unittest discover -s test -v
No ROS installation, serial device, network, or real-time sleeps are needed.
"""
import importlib
import sys
import threading
import types
import unittest
from collections import deque
from unittest.mock import Mock, patch


def load_bridges():
    """Stub only ROS dependencies during import; keep production methods intact."""
    modules = {}
    for name in ('rclpy', 'rclpy.node', 'geometry_msgs', 'geometry_msgs.msg',
                 'std_msgs', 'std_msgs.msg', 'std_srvs', 'std_srvs.srv'):
        modules[name] = types.ModuleType(name)
    modules['rclpy.node'].Node = type('Node', (), {})
    modules['geometry_msgs.msg'].Twist = type('Twist', (), {})
    for name in ('Bool', 'Float32', 'Float32MultiArray', 'Int8',
                 'Int8MultiArray', 'Int64MultiArray', 'String'):
        setattr(modules['std_msgs.msg'], name, type(name, (), {}))
    for name in ('SetBool', 'Trigger'):
        setattr(modules['std_srvs.srv'], name, type(name, (), {}))
    with patch.dict(sys.modules, modules):
        base = importlib.import_module('robotlidar.esp32_track_bridge_node')
        settings = importlib.import_module('robotlidar.esp32_track_bridge_settings_node')
    return base, settings


base, settings = load_bridges()


class Serial:
    def __init__(self):
        self.bodies = []
        self.on_write = None
        self.closed = False

    def write(self, raw):
        body, checksum = raw.decode('ascii').strip().rsplit('*', 1)
        assert int(checksum, 16) == base.Esp32TrackBridgeNode._checksum(body)
        self.bodies.append(body)
        if self.on_write:
            self.on_write(body)
        return len(raw)

    def close(self):
        self.closed = True


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        clock = types.SimpleNamespace(monotonic=lambda: self.now, time=lambda: 1000.0)
        for module in (base, settings):
            p = patch.object(module, 'time', clock)
            p.start()
            self.addCleanup(p.stop)
        self.node = settings.Esp32TrackBridgeSettingsNode.__new__(
            settings.Esp32TrackBridgeSettingsNode)
        n = self.node
        n._lock = threading.RLock()
        n._serial = self.serial = Serial()
        n._connected = True
        n.dry_run = False
        n._sequence = 0
        n._armed_requested = n._arm_confirmed = False
        n._arm_ack_event = threading.Event()
        n._pending_arm_sequence = n._pending_arm_result = None
        n._pending_arm_desired = False
        n._last_telemetry = {}
        n._last_ip_sequence = None
        n._reader_stop = threading.Event()
        n._reader_thread = None
        n._config_queue = deque()
        n._config_pending = None
        n._config_sent_at = 0.0
        n._config_attempts = 0
        n._config_pause_until = 0.0
        n._config_state = {}
        n._config_publisher = Mock()
        n._battery_publisher = Mock()
        n.get_logger = Mock(return_value=Mock())
        n.command_timeout_sec = n.aux_command_timeout_sec = 0.5
        for name in ('left', 'right', 'actuator', 'brush', 'aux_motor'):
            setattr(n, '_requested_' + name, 0)
        for name in ('cmd', 'actuator_cmd', 'brush_cmd', 'aux_motor_cmd'):
            setattr(n, '_last_' + name + '_time', self.now)
        for name in ('tick', 'pulse_rate', 'direction', 'actuator_state',
                     'brush_state', 'aux_motor_state'):
            setattr(n, name + '_publisher', Mock())

    def feed(self, body, corrupt=False):
        checksum = self.node._checksum(body) ^ int(corrupt)
        self.node._process_line(f'{body}*{checksum:02X}')

    def arm(self, enabled=True):
        return self.node._arm_service(types.SimpleNamespace(data=enabled),
                                      types.SimpleNamespace())

    def ack_on_write(self, status='OK', after=None):
        def respond(body):
            if body.startswith('ARM,'):
                seq = body.split(',')[1]
                self.feed(f'ACK,{seq},{status},0,0')
                if after:
                    after()
        self.serial.on_write = respond

    def telemetry_disarmed(self):
        self.feed('TEL,1000,0,1,0,0,0,0,0,0,0,0,0')

    def test_matching_ack_signals_pending_arm(self):
        self.node._pending_arm_sequence = 12
        self.feed('ACK,12,OK,0,0')
        self.assertTrue(self.node._arm_ack_event.is_set())
        self.assertEqual(self.node._pending_arm_result, 'OK')

    def test_other_ack_does_not_confirm_arm(self):
        self.node._pending_arm_sequence = 12
        for body in ('ACK,13,OK,0,0', 'ACK,invalid,OK,0,0'):
            self.feed(body)
        self.assertFalse(self.node._arm_ack_event.is_set())

    def test_corrupt_ack_is_rejected(self):
        self.node._pending_arm_sequence = 12
        with self.assertRaises(ValueError):
            self.feed('ACK,12,OK,0,0', corrupt=True)
        self.assertFalse(self.node._arm_ack_event.is_set())

    def test_missing_ack_status_cannot_confirm_arm(self):
        self.serial.on_write = lambda body: self.feed('ACK,' + body.split(',')[1])
        self.assertFalse(self.arm().success)
        self.assertFalse(self.node._arm_confirmed)

    def test_arm_succeeds_only_after_matching_ok(self):
        self.ack_on_write()
        self.assertTrue(self.arm().success)
        self.assertTrue(self.node._arm_confirmed)
        self.assertTrue(self.node._armed_requested)
        self.assertIsNone(self.node._pending_arm_sequence)

    def test_arm_ack_from_reader_thread(self):
        workers = []
        def respond(body):
            t = threading.Thread(target=self.feed,
                                 args=(f'ACK,{body.split(",")[1]},OK,0,0',))
            workers.append(t)
            t.start()
        self.serial.on_write = respond
        try:
            self.assertTrue(self.arm().success)
        finally:
            for t in workers:
                t.join(timeout=1)
                self.assertFalse(t.is_alive())

    def test_refused_arm_stays_disarmed(self):
        for status in ('REFUSED', 'NOT_ROS_MODE', 'PONG', 'CFG_OK'):
            with self.subTest(status=status):
                self.ack_on_write(status)
                self.assertFalse(self.arm().success)
                self.assertFalse(self.node._arm_confirmed)

    def test_arm_timeout_and_late_ack_stay_disarmed(self):
        self.node._arm_ack_event.wait = Mock(return_value=False)
        self.assertFalse(self.arm().success)
        self.feed('ACK,1,OK,0,0')
        self.assertFalse(self.node._arm_confirmed)
        self.assertIsNone(self.node._pending_arm_sequence)
        self.node._arm_ack_event.wait.assert_called_once_with(timeout=1.5)

    def test_arm_with_no_connection(self):
        self.node._serial = None
        self.node._connected = False
        self.assertFalse(self.arm().success)

    def test_dry_run_does_not_claim_hardware_arm(self):
        self.node.dry_run = True
        self.assertFalse(self.arm().success)
        self.assertEqual(self.serial.bodies, [])

    def test_serial_write_failure_does_not_arm(self):
        self.serial.write = Mock(side_effect=OSError('USB lost'))
        self.assertFalse(self.arm().success)
        self.assertFalse(self.node._connected)
        self.assertTrue(self.serial.closed)

    def test_disconnect_while_waiting_for_arm(self):
        self.serial.on_write = lambda body: self.node._mark_disconnected()
        self.assertFalse(self.arm().success)
        self.assertFalse(self.node._arm_confirmed)

    def test_disarm_requires_ack_and_clears_permission(self):
        self.node._arm_confirmed = self.node._armed_requested = True
        self.ack_on_write()
        self.assertTrue(self.arm(False).success)
        self.assertFalse(self.node._arm_confirmed)
        self.assertEqual(self.serial.bodies, ['ARM,1,0'])

    def test_disarm_ack_followed_by_disarmed_telemetry_is_success(self):
        self.ack_on_write(after=self.telemetry_disarmed)
        self.assertTrue(self.arm(False).success)
        self.assertFalse(self.node._arm_confirmed)

    def test_disarmed_telemetry_before_arm_ack_is_old_state(self):
        def respond(body):
            self.telemetry_disarmed()
            self.feed(f'ACK,{body.split(",")[1]},OK,0,0')
        self.serial.on_write = respond
        self.assertTrue(self.arm().success)

    def test_overlapping_arm_request_is_rejected_without_replacing_pending(self):
        self.node._pending_arm_sequence = 99
        self.assertFalse(self.arm().success)
        self.assertEqual(self.node._pending_arm_sequence, 99)
        self.assertEqual(self.serial.bodies, [])

    def test_disconnect_and_reconnect_cannot_resurrect_old_ack(self):
        def respond(body):
            self.feed(f'ACK,{body.split(",")[1]},OK,0,0')
            self.node._mark_disconnected()
            self.node._serial = Serial()
            self.node._connected = True
        self.serial.on_write = respond
        self.assertFalse(self.arm().success)
        self.assertFalse(self.node._arm_confirmed)

    def test_nonzero_commands_blocked_until_confirmed(self):
        self.node._armed_requested = True
        self.node._requested_left = 700
        self.node._requested_brush = 800
        self.node._send_tick()
        self.assertEqual(self.serial.bodies[0], 'DRV,1,0,0')
        self.assertTrue(self.serial.bodies[1].endswith(',0,0'))

    def test_confirmed_arm_allows_fresh_commands(self):
        self.ack_on_write()
        self.assertTrue(self.arm().success)
        self.serial.on_write = None
        self.node._requested_left = 700
        self.node._send_tick()
        self.assertEqual(self.serial.bodies[-2], 'DRV,2,700,0')

    def test_disarmed_telemetry_revokes_arm(self):
        self.node._arm_confirmed = self.node._armed_requested = True
        self.telemetry_disarmed()
        self.assertFalse(self.node._arm_confirmed)

    def test_disarmed_telemetry_after_ack_cannot_be_overridden(self):
        self.ack_on_write(after=self.telemetry_disarmed)
        self.assertFalse(self.arm().success)
        self.assertFalse(self.node._arm_confirmed)

    def test_estop_during_pending_arm_cannot_be_overridden_by_ack(self):
        def respond(body):
            if body.startswith('ARM,'):
                self.node._estop_service(None, types.SimpleNamespace())
                self.feed(f'ACK,{body.split(",")[1]},OK,0,0')
        self.serial.on_write = respond
        self.assertFalse(self.arm().success)
        self.assertFalse(self.node._arm_confirmed)
        self.assertTrue(any(b.startswith('STOP,') for b in self.serial.bodies))

    def test_close_serial_revokes_arm(self):
        self.node._arm_confirmed = True
        self.node._close_serial()
        self.assertFalse(self.node._arm_confirmed)
        self.assertTrue(self.serial.closed)

    def test_matching_config_ack_completes_request(self):
        self.node._queue_config_sequence(settings.CFG_GET)
        self.node._config_tick()
        self.feed(f'ACK,{settings.CFG_GET},CFG_OK,0,0')
        self.now += 1
        self.node._config_tick()
        self.assertEqual(len(self.serial.bodies), 1)
        self.assertIsNone(self.node._config_pending)

    def test_other_ack_does_not_stop_config_retries(self):
        self.node._queue_config_sequence(settings.CFG_GET)
        self.node._config_tick()
        self.feed('ACK,9,CFG_OK,0,0')
        self.now += 0.61
        self.node._config_tick()
        self.assertEqual(self.serial.bodies, [f'PING,{settings.CFG_GET}'] * 2)

    def test_lost_config_ack_retries_identical_set_then_completes(self):
        sequence = self.node._encoded_set_sequence('us_stop_mm', 550)
        self.node._queue_config_sequence(sequence)
        self.node._config_tick()
        self.now += 0.61
        self.node._config_tick()
        self.assertEqual(self.serial.bodies, [f'PING,{sequence}'] * 2)
        self.feed(f'ACK,{sequence},CFG_OK,0,0')
        self.now += 0.61
        self.node._config_tick()
        self.assertEqual(len(self.serial.bodies), 2)

    def test_config_timeout_advances_to_next_request(self):
        self.node._queue_config_sequence(settings.CFG_RESET)
        self.node._queue_config_sequence(settings.CFG_GET)
        for _ in range(5):
            self.node._config_tick()
            self.now += 0.61
        self.node._config_tick()
        self.assertEqual(self.serial.bodies,
                         [f'PING,{settings.CFG_RESET}'] * 4 + [f'PING,{settings.CFG_GET}'])

    def test_config_ack_without_status_does_not_complete_request(self):
        self.node._queue_config_sequence(settings.CFG_GET)
        self.node._config_tick()
        self.feed(f'ACK,{settings.CFG_GET}')
        self.assertEqual(self.node._config_pending, settings.CFG_GET)

    def test_corrupt_config_ack_does_not_stop_retries(self):
        self.node._queue_config_sequence(settings.CFG_GET)
        self.node._config_tick()
        with self.assertRaises(ValueError):
            self.feed(f'ACK,{settings.CFG_GET},CFG_OK,0,0', corrupt=True)
        self.assertEqual(self.node._config_pending, settings.CFG_GET)

    def test_config_retry_interval_and_four_attempt_limit(self):
        self.node._queue_config_sequence(settings.CFG_GET)
        self.node._config_tick()
        self.now += 0.59
        self.node._config_tick()
        self.assertEqual(len(self.serial.bodies), 1)
        for _ in range(5):
            self.now += 0.61
            self.node._config_tick()
        self.assertEqual(len(self.serial.bodies), 4)
        self.assertIsNone(self.node._config_pending)
        self.node.get_logger().error.assert_called_once()

    def test_write_failure_retries_after_connection_returns(self):
        self.node._serial = None
        self.node._queue_config_sequence(settings.CFG_GET)
        self.node._config_tick()
        self.assertEqual(self.node._config_attempts, 1)
        self.node._serial = self.serial
        self.now += 0.61
        self.node._config_tick()
        self.assertEqual(self.serial.bodies, [f'PING,{settings.CFG_GET}'])
        self.feed(f'ACK,{settings.CFG_GET},CFG_OK,0,0')
        self.assertIsNone(self.node._config_pending)

    def test_rejected_config_advances_queue(self):
        self.node._queue_config_sequence(settings.CFG_GET)
        self.node._queue_config_sequence(settings.CFG_RESET)
        self.node._config_tick()
        self.feed(f'ACK,{settings.CFG_GET},CFG_UNKNOWN_KEY,0,0')
        self.node._config_tick()
        self.assertEqual(self.serial.bodies[-1], f'PING,{settings.CFG_RESET}')

    def test_duplicate_config_ack_does_not_complete_next_request(self):
        self.node._queue_config_sequence(settings.CFG_GET)
        self.node._queue_config_sequence(settings.CFG_RESET)
        self.node._config_tick()
        self.feed(f'ACK,{settings.CFG_GET},CFG_OK,0,0')
        self.node._config_tick()
        self.feed(f'ACK,{settings.CFG_GET},CFG_OK,0,0')
        self.assertEqual(self.node._config_pending, settings.CFG_RESET)

    def test_config_does_not_suppress_drive_heartbeat(self):
        self.node._queue_config_sequence(settings.CFG_GET)
        self.node._config_tick()
        self.node._send_tick()
        self.assertTrue(any(b.startswith('DRV,') for b in self.serial.bodies))
        self.assertTrue(any(b.startswith('AUX,') for b in self.serial.bodies))

    def test_set_and_reset_requests_queue_readback(self):
        self.node._config_request_callback(types.SimpleNamespace(
            data='{"op":"set","values":{"us_stop_mm":550}}'))
        sequence = settings.CFG_SET | (3 << 16) | 550
        self.assertEqual(list(self.node._config_queue), [sequence, settings.CFG_GET])
        self.node._config_queue.clear()
        self.node._config_request_callback(types.SimpleNamespace(data='{"op":"reset"}'))
        self.assertEqual(list(self.node._config_queue), [settings.CFG_RESET, settings.CFG_GET])


if __name__ == '__main__':
    unittest.main()
