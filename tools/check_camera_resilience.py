"""Optional local RTSP smoke test; requires FFmpeg and MediaMTX executables.

Uses two generated H.264 cameras and a localhost RTP receiver. Tests the real
relay's FFmpeg launch, progress, watchdog, failover and failback. No ROS,
physical cameras or central server is involved. All child processes are reaped.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from robotlidar.ip_camera_relay import IpCameraRelayManager


def unused_tcp_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def terminate(process):
    if process is not None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        process.wait(timeout=3)


def suspend(process, resume=False):
    """Suspend only the synthetic publisher created by this test."""
    if os.name == 'nt':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x0800, False, process.pid)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            nt = ctypes.WinDLL('ntdll')
            operation = nt.NtResumeProcess if resume else nt.NtSuspendProcess
            operation.argtypes = [ctypes.c_void_p]
            operation.restype = ctypes.c_long
            status = operation(handle)
            if status != 0:
                raise RuntimeError(f'Publisher suspend/resume failed: {status}')
        finally:
            kernel.CloseHandle(handle)
    else:
        os.kill(process.pid, signal.SIGCONT if resume else signal.SIGSTOP)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mediamtx', required=True)
    parser.add_argument('--ffmpeg', default='ffmpeg')
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    children, handles = [], []
    suspended = None
    relay = None
    receiver_stop = threading.Event()
    packets = [0]
    result = {'checks': [], 'transport': 'RTSP/TCP -> H264 copy -> RTP/localhost'}
    logs = []

    def log(message):
        logs.append(message)
        print(message, flush=True)

    def launch(command, name):
        handle = (args.output_dir / f'{name}.log').open('w', encoding='utf-8')
        handles.append(handle)
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=handle, stderr=subprocess.STDOUT,
                                   creationflags=0x08000000 if os.name == 'nt' else 0)
        children.append(process)
        return process

    def wait_for(predicate, description, timeout=20, maintain=False):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if maintain:
                relay._maintain_video(relay._snapshot_config())
            if predicate():
                log('PASS: ' + description)
                result['checks'].append(description)
                return
            time.sleep(0.1)
        raise AssertionError('Timed out: ' + description)

    def has_progress():
        with relay._lock:
            return bool(relay._progress_at and time.monotonic() - relay._progress_at < 2)

    try:
        with tempfile.TemporaryDirectory(prefix='robotlidar-rtsp-') as temporary, \
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sink:
            sink.bind(('127.0.0.1', 0))
            sink.settimeout(0.2)
            def receive():
                while not receiver_stop.is_set():
                    try:
                        data, _ = sink.recvfrom(65535)
                        if data:
                            packets[0] += 1
                    except socket.timeout:
                        continue
                    except OSError:
                        return
            receiver = threading.Thread(target=receive, daemon=True)
            receiver.start()
            port = unused_tcp_port()
            cfgfile = Path(temporary) / 'mediamtx.yml'
            cfgfile.write_text(
                f'logLevel: warn\nrtspAddress: 127.0.0.1:{port}\n'
                'rtspTransports: [tcp]\nrtmp: false\nhls: false\n'
                'webrtc: false\nsrt: false\napi: false\n'
                'readTimeout: 30s\nwriteTimeout: 5s\npaths:\n  all_others:\n',
                encoding='utf-8')
            server = launch([args.mediamtx, str(cfgfile)], 'mediamtx')
            def server_ready():
                if server.poll() is not None:
                    raise AssertionError('MediaMTX exited; inspect mediamtx.log')
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                        return True
                except OSError:
                    return False
            wait_for(server_ready, 'Local RTSP server started', timeout=10)
            urls = [f'rtsp://127.0.0.1:{port}/camera{i}' for i in (1, 2)]
            def publish(index, suffix=''):
                return launch([
                    args.ffmpeg, '-hide_banner', '-loglevel', 'warning', '-re',
                    '-f', 'lavfi', '-i', 'testsrc2=size=160x120:rate=10',
                    '-an', '-c:v', 'libx264', '-preset', 'ultrafast', '-tune',
                    'zerolatency', '-g', '10', '-f', 'rtsp', '-rtsp_transport',
                    'tcp', urls[index]], f'publisher-{index + 1}{suffix}')
            primary = publish(0)
            publish(1)
            relay = IpCameraRelayManager(log_callback=log)
            relay.configure({
                'camera_enabled': True, 'camera_ffmpeg': args.ffmpeg,
                'camera_server_url': 'http://127.0.0.1',
                'camera1_rtsp_url': urls[0], 'camera2_rtsp_url': urls[1],
                'camera_auto_failover_enabled': True,
                'camera_failover_after_failures': 1,
                'camera_failover_probe_interval_sec': 2,
                'camera_video_watchdog_timeout_sec': 3,
                'camera_video_watchdog_startup_grace_sec': 3,
            })
            relay._video_port = sink.getsockname()[1]
            cfg = relay._snapshot_config()
            wait_for(lambda: relay._rtsp_available(cfg, urls[0], timeout_sec=2),
                     'Primary RTSP exposes H264 stream', timeout=15)
            wait_for(lambda: relay._rtsp_available(cfg, urls[1], timeout_sec=2),
                     'Reserve RTSP exposes H264 stream', timeout=15)
            wait_for(lambda: has_progress() and packets[0] > 5,
                     'Primary relay produces progress and RTP packets', maintain=True)
            suspend(primary)
            suspended = primary
            wait_for(lambda: relay.status()['active_camera'] == 2,
                     'Frozen primary triggers watchdog and reserve switch', maintain=True)
            assert any('progress stalled' in message for message in logs)
            suspend(primary, resume=True)
            suspended = None
            terminate(primary)
            previous_packets = packets[0]
            wait_for(lambda: has_progress() and packets[0] > previous_packets + 5,
                     'Reserve relay produces progress and RTP packets', maintain=True)
            restored_primary = publish(0, '-restored')
            wait_for(lambda: relay.status()['active_camera'] == 1,
                     'Restored primary selected by failback', maintain=True)
            previous_packets = packets[0]
            wait_for(lambda: has_progress() and packets[0] > previous_packets + 5,
                     'Primary relay resumes progress and RTP packets', maintain=True)
            terminate(restored_primary)
            wait_for(lambda: relay.status()['active_camera'] == 2,
                     'Disconnected primary triggers reserve switch', maintain=True)
            previous_packets = packets[0]
            wait_for(lambda: has_progress() and packets[0] > previous_packets + 5,
                     'Reserve delivers RTP after primary disconnect', maintain=True)
            result['rtp_packets'] = packets[0]
            result['result'] = 'PASS'
    except Exception as exc:
        result['result'] = 'FAIL'
        result['error'] = str(exc)
        raise
    finally:
        receiver_stop.set()
        if suspended is not None:
            suspend(suspended, resume=True)
        if relay is not None:
            relay.stop()
        for process in reversed(children):
            terminate(process)
        for handle in handles:
            handle.close()
        (args.output_dir / 'result.json').write_text(
            json.dumps(result, indent=2), encoding='utf-8')
        (args.output_dir / 'relay.log').write_text('\n'.join(logs), encoding='utf-8')


if __name__ == '__main__':
    main()
