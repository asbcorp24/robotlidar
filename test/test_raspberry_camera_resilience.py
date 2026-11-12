"""Offline tests for Raspberry camera resilience (no ROS or camera required)."""
import unittest
from unittest.mock import patch

from robotlidar.ip_camera_relay import IpCameraRelayManager


class CameraResilienceTests(unittest.TestCase):
    def setUp(self):
        self.relay = IpCameraRelayManager()
        self.relay.configure({
            'camera_enabled': True,
            'camera1_rtsp_url': 'rtsp://127.0.0.1/one',
            'camera2_rtsp_url': 'rtsp://127.0.0.1/two',
            'camera_auto_failover_enabled': True,
            'camera_failover_after_failures': 2,
            'camera_primary_camera': 1,
            'camera_return_to_primary': True,
        })

    def test_defaults_are_safe(self):
        cfg = self.relay._snapshot_config()
        self.assertEqual(cfg['active_camera'], 1)
        self.assertTrue(cfg['video_watchdog_enabled'])
        self.assertEqual(cfg['failover_after_failures'], 2)

    def test_backoff_is_bounded(self):
        cfg = self.relay._snapshot_config()
        with patch.object(self.relay, '_rtsp_available', return_value=False):
            for _ in range(8):
                self.relay._record_camera_failure(cfg)
        self.assertEqual(self.relay._retry_step, 3)
        self.assertGreater(self.relay._retry_at, 0)

    def test_failover_after_threshold(self):
        cfg = self.relay._snapshot_config()
        with patch.object(self.relay, '_rtsp_available', return_value=True):
            self.relay._record_camera_failure(cfg)
            self.assertEqual(self.relay.status()['active_camera'], 1)
            self.relay._record_camera_failure(cfg)
        self.assertEqual(self.relay.status()['active_camera'], 2)

    def test_failback(self):
        self.relay._switch_camera_runtime(2, 'TEST')
        cfg = self.relay._snapshot_config()
        with patch.object(self.relay, '_rtsp_available', return_value=True):
            self.relay._probe_primary(cfg, self.relay._next_camera_probe_at + 1)
        self.assertEqual(self.relay.status()['active_camera'], 1)

    def test_manual_switch_keeps_active_if_unavailable(self):
        with patch.object(self.relay, '_rtsp_available', return_value=False):
            ok, _ = self.relay.select_camera(2)
        self.assertFalse(ok)
        self.assertEqual(self.relay.status()['active_camera'], 1)


if __name__ == '__main__':
    unittest.main()
