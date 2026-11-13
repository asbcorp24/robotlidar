"""Regression tests for camera selection, backoff and the progress watchdog."""
import unittest
from unittest.mock import Mock, patch

from robotlidar.ip_camera_relay import IpCameraRelayManager


class CameraReviewTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.clock = patch('robotlidar.ip_camera_relay.time.monotonic',
                           side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.relay = IpCameraRelayManager(log_callback=lambda text: None)
        self.settings = {
            'camera_enabled': True,
            'camera1_rtsp_url': 'rtsp://primary/stream',
            'camera2_rtsp_url': 'rtsp://reserve/stream',
            'camera_auto_failover_enabled': True,
            'camera_failover_after_failures': 1,
        }
        self.relay.configure(self.settings)

    def test_remote_control_toggle_preserves_failed_over_camera(self):
        with patch.object(self.relay, '_rtsp_available', return_value=True):
            self.relay._record_camera_failure(self.relay._snapshot_config())
        self.assertEqual(self.relay.status()['active_camera'], 2)
        self.relay.configure({**self.settings, 'camera_remote_control_enabled': True})
        self.assertEqual(self.relay.status()['active_camera'], 2,
                         'Changing control settings must not restore the failed camera')

    def test_full_backoff_remains_after_slow_failed_probe(self):
        def slow_probe(*args, **kwargs):
            self.now += 3.5
            return False
        with patch.object(self.relay, '_rtsp_available', side_effect=slow_probe):
            self.relay._record_camera_failure(self.relay._snapshot_config())
        self.assertGreaterEqual(self.relay._retry_at - self.now, 2.0,
                                'The reserve probe must not consume the restart delay')

    def test_explicit_startup_choice_change_selects_requested_camera(self):
        self.relay.configure({**self.settings, 'camera_active_camera': 2})
        self.assertEqual(self.relay.status()['active_camera'], 2)

    def test_changed_source_url_applies_configured_camera(self):
        self.relay._switch_camera_runtime(2, 'TEST')
        self.relay.configure({**self.settings,
                              'camera1_rtsp_url': 'rtsp://replacement/stream'})
        self.assertEqual(self.relay.status()['active_camera'], 1)

    def test_reapplying_unchanged_settings_keeps_failover_and_process(self):
        self.relay._switch_camera_runtime(2, 'TEST')
        process = self.live_process()
        self.relay.configure(self.settings)
        self.assertEqual(self.relay.status()['active_camera'], 2)
        self.assertIs(self.relay._process, process)
        process.terminate.assert_not_called()

    def live_process(self, started=80.0, progress=90.0):
        process = Mock()
        process.poll.return_value = None
        self.relay._process = process
        self.relay._process_started_at = started
        self.relay._progress_at = progress
        return process

    def test_stalled_video_triggers_restart_and_failure_tracking(self):
        self.live_process()
        with patch.object(self.relay, '_stop_ffmpeg') as stop, \
                patch.object(self.relay, '_record_camera_failure') as fail:
            self.relay._maintain_video(self.relay._snapshot_config())
        stop.assert_called_once()
        fail.assert_called_once()

    def test_startup_grace_prevents_premature_restart(self):
        self.live_process(started=95.0, progress=0.0)
        with patch.object(self.relay, '_stop_ffmpeg') as stop:
            self.relay._maintain_video(self.relay._snapshot_config())
        stop.assert_not_called()

    def test_disabled_watchdog_does_not_restart_stalled_video(self):
        self.live_process()
        cfg = self.relay._snapshot_config()
        cfg['video_watchdog_enabled'] = False
        with patch.object(self.relay, '_stop_ffmpeg') as stop:
            self.relay._maintain_video(cfg)
        stop.assert_not_called()

    def test_duplicate_progress_does_not_refresh_watchdog(self):
        process = self.live_process()
        def progress():
            yield 'out_time_us=25\n'
            self.now += 10
            yield 'out_time_ms=25\n'
        process.stdout = progress()
        self.relay._read_ffmpeg_progress(process)
        self.assertEqual(self.relay._progress_at, 100.0)

    def test_old_process_progress_cannot_refresh_new_process_watchdog(self):
        self.live_process()
        old = Mock(stdout=iter(['out_time_us=999\n']))
        self.relay._read_ffmpeg_progress(old)
        self.assertEqual(self.relay._progress_at, 90.0)

    def test_video_restart_waits_until_backoff_deadline(self):
        self.relay._retry_at = 102.0
        with patch.object(self.relay, '_ensure_ffmpeg') as start:
            self.relay._maintain_video(self.relay._snapshot_config())
            start.assert_not_called()
            self.now = 102.0
            self.relay._maintain_video(self.relay._snapshot_config())
            start.assert_called_once()


if __name__ == '__main__':
    unittest.main()
