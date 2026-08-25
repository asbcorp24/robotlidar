#!/usr/bin/env python3
"""Simple H.264 RTSP IP-camera emulator for RobotLiDAR testing.

Runs a local RTSP server backed by GStreamer videotestsrc. It is intended to
exercise exactly the same Raspberry Pi RTSP -> FFmpeg copy -> central server
path that will later be used with a real outdoor IP camera.
"""
from __future__ import annotations

import argparse
import socket
import sys

try:
    import gi
    gi.require_version('Gst', '1.0')
    gi.require_version('GstRtspServer', '1.0')
    from gi.repository import GLib, Gst, GstRtspServer
except Exception as exc:  # pragma: no cover - only useful on target host
    print('Не найдены Python/GStreamer RTSP зависимости.', file=sys.stderr)
    print('Установите:', file=sys.stderr)
    print(
        'sudo apt install -y python3-gi gir1.2-gst-rtsp-server-1.0 '
        'gstreamer1.0-tools gstreamer1.0-plugins-base '
        'gstreamer1.0-plugins-good gstreamer1.0-plugins-ugly',
        file=sys.stderr,
    )
    print(f'Ошибка импорта: {exc}', file=sys.stderr)
    raise SystemExit(2)


def local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(('8.8.8.8', 80))
        return str(sock.getsockname()[0])
    except OSError:
        return '127.0.0.1'
    finally:
        sock.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='RobotLiDAR H.264 RTSP camera emulator')
    parser.add_argument('--host', default='0.0.0.0', help='RTSP listen address')
    parser.add_argument('--port', type=int, default=8554, help='RTSP TCP port')
    parser.add_argument('--path', default='/test', help='RTSP mount path')
    parser.add_argument('--width', type=int, default=1280)
    parser.add_argument('--height', type=int, default=720)
    parser.add_argument('--fps', type=int, default=25)
    parser.add_argument('--bitrate-kbps', type=int, default=2000)
    parser.add_argument(
        '--pattern',
        default='smpte',
        choices=['smpte', 'snow', 'black', 'white', 'red', 'green', 'blue', 'ball'],
        help='GStreamer videotestsrc pattern',
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not 1 <= args.port <= 65535:
        raise SystemExit('Некорректный RTSP port')
    if args.width < 160 or args.height < 120 or args.fps < 1:
        raise SystemExit('Некорректные параметры видео')

    path = '/' + args.path.strip('/')
    Gst.init(None)

    server = GstRtspServer.RTSPServer()
    server.set_address(args.host)
    server.set_service(str(args.port))

    factory = GstRtspServer.RTSPMediaFactory()
    factory.set_shared(True)
    # x264enc outputs H.264 directly. SPS/PPS are periodically repeated by
    # h264parse/rtph264pay so a client can join after the emulator has started.
    launch = (
        f'( videotestsrc is-live=true pattern={args.pattern} '
        f'! video/x-raw,width={args.width},height={args.height},framerate={args.fps}/1 '
        f'! videoconvert '
        f'! x264enc tune=zerolatency speed-preset=ultrafast bitrate={args.bitrate_kbps} '
        f'key-int-max={args.fps} bframes=0 byte-stream=true '
        f'! video/x-h264,profile=baseline '
        f'! h264parse config-interval=1 '
        f'! rtph264pay name=pay0 pt=96 config-interval=1 )'
    )
    factory.set_launch(launch)
    server.get_mount_points().add_factory(path, factory)

    attach_id = server.attach(None)
    if attach_id == 0:
        raise SystemExit('Не удалось запустить RTSP server')

    ip = local_ip()
    print('RobotLiDAR RTSP camera emulator запущен')
    print(f'  Video: H.264 Baseline {args.width}x{args.height}@{args.fps}, {args.bitrate_kbps} kbps')
    print(f'  Local: rtsp://127.0.0.1:{args.port}{path}')
    print(f'  LAN:   rtsp://{ip}:{args.port}{path}')
    print()
    print('Проверка:')
    print(f'  ffprobe -rtsp_transport tcp rtsp://127.0.0.1:{args.port}{path}')
    print('Остановка: Ctrl+C')

    loop = GLib.MainLoop()
    try:
        loop.run()
    except KeyboardInterrupt:
        print('\nRTSP emulator остановлен')


if __name__ == '__main__':
    main()
