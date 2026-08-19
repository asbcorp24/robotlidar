#!/usr/bin/env python3
"""Reliable web entry point with WebSocket ROS telemetry.

This wrapper keeps the FastAPI control API from ``web_app``, serves static files
reliably in ROS 2 symlink installs, and adds live WebSocket streams for lidar,
IMU, odometry and optional GPS assistance.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import threading
import time
from pathlib import Path
from typing import Optional

import uvicorn
from ament_index_python.packages import get_package_share_directory
from fastapi import HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Imu, LaserScan, NavSatFix, NavSatStatus
from std_msgs.msg import String

from robotlidar import web_app


def _find_static_directory() -> Path:
    candidates: list[Path] = []

    configured = os.environ.get('ROBOTLIDAR_STATIC_DIR')
    if configured:
        candidates.append(Path(configured).expanduser())

    try:
        candidates.append(
            Path(get_package_share_directory('robotlidar')) / 'web' / 'static'
        )
    except Exception:
        pass

    candidates.append(Path.cwd() / 'src' / 'robotlidar' / 'web' / 'static')

    module_path = Path(__file__).resolve()
    candidates.extend([
        module_path.parents[1] / 'web' / 'static',
        module_path.parents[2] / 'src' / 'robotlidar' / 'web' / 'static',
    ])

    required = (
        'index.html',
        'style.css',
        'app.js',
        'ws-client.js',
        'radar.html',
        'radar.css',
        'radar.js',
    )
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate.is_dir() and all(
            (candidate / name).is_file() for name in required
        ):
            return candidate

    checked = '\n'.join(f'  - {path}' for path in candidates)
    raise RuntimeError(
        'RobotLidar static directory was not found. Checked:\n' + checked
    )


STATIC_DIR = _find_static_directory()
web_app.static_dir = STATIC_DIR

# Remove routes that this wrapper serves explicitly. In particular /radar must
# not be handled by web_app.py, where a compatibility route may point at the
# main index page instead of the dedicated lidar/IMU screen.
web_app.app.routes[:] = [
    route
    for route in web_app.app.routes
    if getattr(route, 'path', None) not in ('/', '/static', '/radar', '/radar/')
]

_stream_lock = threading.RLock()
_stream_state = {
    'scan': {
        'points': [],
        'range_min': 0.0,
        'range_max': 0.0,
        'source_count': 0,
        'received_at': 0.0,
    },
    'imu': {
        'gyro': {'x': 0.0, 'y': 0.0, 'z': 0.0},
        'accel': {'x': 0.0, 'y': 0.0, 'z': 0.0},
        'tilt': {'roll_deg': 0.0, 'pitch_deg': 0.0},
        'received_at': 0.0,
    },
    'gps': {
        'online': False,
        'fix_valid': False,
        'latitude': None,
        'longitude': None,
        'altitude_m': None,
        'satellites_used': 0,
        'satellites_visible': 0,
        'hdop': None,
        'speed_mps': None,
        'course_deg': None,
        'assist_enabled': True,
        'assist_ready': False,
        'alignment_state': 'waiting_fix',
        'alignment_source': None,
        'heading_offset_deg': None,
        'local_x': None,
        'local_y': None,
        'reject_reason': 'waiting_for_data',
        'sentence_age_sec': None,
        'fix_age_sec': None,
        'received_at': 0.0,
    },
}


def _scan_stream_callback(message: LaserScan) -> None:
    ranges = list(message.ranges)
    step = max(1, math.ceil(len(ranges) / 360))
    points: list[list[float]] = []
    range_min = float(message.range_min)
    range_max = float(message.range_max)

    for index in range(0, len(ranges), step):
        distance = float(ranges[index])
        if not math.isfinite(distance):
            continue
        if distance < range_min or distance > range_max:
            continue
        angle = float(message.angle_min + index * message.angle_increment)
        points.append([round(angle, 5), round(distance, 3)])

    with _stream_lock:
        _stream_state['scan'] = {
            'points': points,
            'range_min': round(range_min, 3),
            'range_max': round(range_max, 3) if math.isfinite(range_max) else 20.0,
            'source_count': len(ranges),
            'received_at': time.time(),
        }


def _imu_stream_callback(message: Imu) -> None:
    ax = float(message.linear_acceleration.x)
    ay = float(message.linear_acceleration.y)
    az = float(message.linear_acceleration.z)
    roll = math.degrees(math.atan2(ay, az))
    pitch = math.degrees(math.atan2(-ax, math.sqrt(ay * ay + az * az)))

    with _stream_lock:
        _stream_state['imu'] = {
            'gyro': {
                'x': round(float(message.angular_velocity.x), 6),
                'y': round(float(message.angular_velocity.y), 6),
                'z': round(float(message.angular_velocity.z), 6),
            },
            'accel': {
                'x': round(ax, 5),
                'y': round(ay, 5),
                'z': round(az, 5),
            },
            'tilt': {
                'roll_deg': round(roll, 2),
                'pitch_deg': round(pitch, 2),
            },
            'received_at': time.time(),
        }


def _gps_fix_callback(message: NavSatFix) -> None:
    latitude = float(message.latitude)
    longitude = float(message.longitude)
    altitude = float(message.altitude)
    valid = (
        message.status.status >= NavSatStatus.STATUS_FIX
        and math.isfinite(latitude)
        and math.isfinite(longitude)
    )
    with _stream_lock:
        gps = dict(_stream_state['gps'])
        gps.update({
            'fix_valid': valid,
            'latitude': latitude if math.isfinite(latitude) else None,
            'longitude': longitude if math.isfinite(longitude) else None,
            'altitude_m': altitude if math.isfinite(altitude) else None,
            'received_at': time.time(),
        })
        _stream_state['gps'] = gps


def _gps_status_callback(message: String) -> None:
    try:
        parsed = json.loads(message.data)
    except (TypeError, ValueError, json.JSONDecodeError):
        return
    if not isinstance(parsed, dict):
        return
    with _stream_lock:
        gps = dict(_stream_state['gps'])
        gps.update(parsed)
        gps['received_at'] = time.time()
        _stream_state['gps'] = gps


def _gps_snapshot() -> dict:
    now = time.time()
    with _stream_lock:
        gps = dict(_stream_state['gps'])
    received_at = float(gps.get('received_at') or 0.0)
    age_sec = now - received_at if received_at else None
    gps['web_age_sec'] = round(age_sec, 3) if age_sec is not None else None
    gps['online'] = bool(
        gps.get('online')
        and age_sec is not None
        and age_sec < 3.0
    )
    if age_sec is None or age_sec >= 3.0:
        gps['fix_valid'] = False
        gps['assist_ready'] = False
    return gps


_original_bridge_status = web_app.bridge.status


def _bridge_status_with_gps() -> dict:
    payload = _original_bridge_status()
    gps = _gps_snapshot()
    sensors = dict(payload.get('sensors') or {})
    sensors['gps'] = {
        'online': bool(gps.get('online')),
        'age_sec': gps.get('web_age_sec'),
    }
    payload['sensors'] = sensors
    payload['gps'] = gps
    return payload


web_app.bridge.status = _bridge_status_with_gps  # type: ignore[method-assign]

_stream_subscriptions = [
    web_app.bridge.create_subscription(
        LaserScan, '/scan', _scan_stream_callback, qos_profile_sensor_data
    ),
    web_app.bridge.create_subscription(
        Imu, '/imu/data_raw', _imu_stream_callback, qos_profile_sensor_data
    ),
    web_app.bridge.create_subscription(
        NavSatFix, '/gps/fix', _gps_fix_callback, qos_profile_sensor_data
    ),
    web_app.bridge.create_subscription(String, '/gps/status', _gps_status_callback, 10),
]


def _status_payload() -> dict:
    return {
        'ok': True,
        'transport': 'websocket',
        'server_time': time.time(),
        'runtime': web_app.process_manager.status(),
        'ros': web_app.bridge.status(),
        'settings': web_app.settings.snapshot(),
        'route_file_exists': web_app.ROUTE_FILE.exists(),
    }


def _radar_payload() -> dict:
    payload = _status_payload()
    now = time.time()
    with _stream_lock:
        scan = _stream_state['scan']
        imu = _stream_state['imu']
        payload['scan'] = {
            'points': [list(point) for point in scan['points']],
            'range_min': scan['range_min'],
            'range_max': scan['range_max'],
            'source_count': scan['source_count'],
            'received_at': scan['received_at'],
            'age_sec': round(now - scan['received_at'], 3) if scan['received_at'] else None,
        }
        payload['imu'] = {
            'gyro': dict(imu['gyro']),
            'accel': dict(imu['accel']),
            'tilt': dict(imu['tilt']),
            'received_at': imu['received_at'],
            'age_sec': round(now - imu['received_at'], 3) if imu['received_at'] else None,
        }
        payload['gps'] = _gps_snapshot()
    return payload


@web_app.app.get('/', include_in_schema=False)
def index_page() -> HTMLResponse:
    html = (STATIC_DIR / 'index.html').read_text(encoding='utf-8')
    html = html.replace(
        '</head>',
        '''
  <style>
    .topbar-live-actions { display:flex; align-items:center; gap:.65rem; flex-wrap:wrap; justify-content:flex-end; }
    .radar-page-link { display:inline-flex; align-items:center; padding:.6rem .85rem; border-radius:999px; border:1px solid rgba(148,163,184,.32); color:inherit; text-decoration:none; font-weight:700; }
    .radar-page-link:hover { border-color:#38bdf8; }
  </style>
  <script src="/static/ws-client.js"></script>
</head>''',
    )
    connection = '<div class="connection" id="connectionBadge">Подключение…</div>'
    html = html.replace(
        connection,
        '<div class="topbar-live-actions">'
        '<a class="radar-page-link" href="/radar">Радар, IMU, GPS и RCWL</a>'
        + connection
        + '</div>',
    )
    html = html.replace('<b>MPU6050</b>', '<b>MPU6500</b>')
    return HTMLResponse(html)


@web_app.app.get('/radar', include_in_schema=False)
@web_app.app.get('/radar/', include_in_schema=False)
def radar_page() -> FileResponse:
    return FileResponse(STATIC_DIR / 'radar.html')


@web_app.app.get('/static/{filename}', include_in_schema=False)
def static_file(filename: str) -> FileResponse:
    allowed = {
        'style.css', 'app.js', 'ws-client.js', 'radar.css', 'radar.js'
    }
    if filename not in allowed:
        raise HTTPException(status_code=404, detail='Static file not found')
    path = STATIC_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f'Static file missing: {filename}')
    return FileResponse(path)


@web_app.app.get('/api/gps')
def api_gps() -> dict:
    return {'ok': True, 'gps': _gps_snapshot()}


@web_app.app.post('/api/gps/reset-assist')
async def api_gps_reset_assist() -> JSONResponse:
    try:
        result = await asyncio.to_thread(
            web_app.bridge.call_trigger, '/gps/reset_assist'
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return JSONResponse(result, status_code=200 if result['success'] else 409)


@web_app.app.websocket('/ws/status')
async def websocket_status(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(_status_payload())
            await asyncio.sleep(0.25)
    except (WebSocketDisconnect, RuntimeError):
        return


@web_app.app.websocket('/ws/radar')
async def websocket_radar(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            await websocket.send_json(_radar_payload())
            await asyncio.sleep(0.10)
    except (WebSocketDisconnect, RuntimeError):
        return


@web_app.app.get('/api/debug/static', include_in_schema=False)
def debug_static() -> dict:
    filenames = (
        'index.html', 'style.css', 'app.js', 'ws-client.js',
        'radar.html', 'radar.css', 'radar.js',
    )
    return {
        'static_dir': str(STATIC_DIR),
        'files': {
            name: {
                'exists': (STATIC_DIR / name).is_file(),
                'size': (STATIC_DIR / name).stat().st_size
                if (STATIC_DIR / name).is_file() else None,
            }
            for name in filenames
        },
        'websockets': ['/ws/status', '/ws/radar'],
        'pages': ['/', '/radar'],
        'gps_api': ['/api/gps', '/api/gps/reset-assist'],
    }


def main(args: Optional[list[str]] = None) -> None:
    del args
    uvicorn.run(
        web_app.app,
        host=web_app.HOST,
        port=web_app.PORT,
        log_level='info',
    )


if __name__ == '__main__':
    main()
