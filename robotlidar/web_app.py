#!/usr/bin/env python3
"""Local offline web control panel for RobotLidar."""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

import rclpy
import uvicorn
import yaml
from ament_index_python.packages import get_package_share_directory
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from pydantic import BaseModel, Field
from rclpy.node import Node
from sensor_msgs.msg import Imu, LaserScan
from std_msgs.msg import Bool, String
from std_srvs.srv import Trigger

try:
    from PIL import Image
except ImportError:
    Image = None  # type: ignore[assignment]

HOST = os.environ.get('ROBOTLIDAR_WEB_HOST', '0.0.0.0')
PORT = int(os.environ.get('ROBOTLIDAR_WEB_PORT', '8080'))
SERIAL_PORT = os.environ.get('ROBOTLIDAR_SERIAL_PORT', '/dev/ttyUSB0')
DATA_DIR = Path(os.environ.get('ROBOTLIDAR_DATA_DIR', '~/robotlidar_data')).expanduser()
MAPS_DIR = DATA_DIR / 'maps'
ROUTES_DIR = DATA_DIR / 'routes'
CONFIG_DIR = DATA_DIR / 'config'
SETTINGS_FILE = CONFIG_DIR / 'web_settings.json'
ROUTE_FILE = ROUTES_DIR / 'cleaning_route.yaml'
MAP_NAME_RE = re.compile(r'^[A-Za-zА-Яа-яЁё0-9_-]{1,64}$')


class DriveRequest(BaseModel):
    action: str


class MapSaveRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    set_default: bool = False


class MapSelectionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class NavigationRequest(BaseModel):
    map_name: Optional[str] = Field(default=None, max_length=64)


class SettingsRequest(BaseModel):
    auto_start: bool
    startup_mode: str


class RosWebBridge(Node):
    DRIVE_VALUES = {
        'forward': (0.25, 0.0),
        'reverse': (-0.20, 0.0),
        'left': (0.0, 0.50),
        'right': (0.0, -0.50),
        'stop': (0.0, 0.0),
    }

    def __init__(self) -> None:
        super().__init__('robotlidar_web_bridge')
        self.cmd_publisher = self.create_publisher(Twist, '/cmd_vel', 20)
        self._state_lock = threading.RLock()
        self._clients_lock = threading.Lock()
        self._trigger_clients: dict[str, Any] = {}
        self._drive_action = 'stop'
        self._drive_deadline = 0.0
        self._stop_sent_after_timeout = True
        self.route_recording = False
        self.route_player_state = 'unknown'
        self.pose = {'x': 0.0, 'y': 0.0, 'yaw': 0.0}
        self.velocity = {'linear': 0.0, 'angular': 0.0}
        self.ultrasonic = {
            'distance_mm': 0,
            'distance_cm': 0.0,
            'valid': False,
            'near': False,
            'stop': False,
            'emergency': False,
        }
        self.last_seen = {
            'odom': 0.0,
            'imu': 0.0,
            'lidar': 0.0,
            'wheel_odom': 0.0,
            'ultrasonic': 0.0,
        }

        self.create_subscription(Bool, '/route/recording', self._route_recording_callback, 10)
        self.create_subscription(String, '/route/player_state', self._route_state_callback, 10)
        self.create_subscription(Odometry, '/odometry/filtered', self._odom_callback, 20)
        self.create_subscription(Odometry, '/wheel/odom', self._wheel_odom_callback, 20)
        self.create_subscription(Imu, '/imu/data_raw', self._imu_callback, 20)
        self.create_subscription(LaserScan, '/scan', self._scan_callback, 10)
        self.create_subscription(String, '/safety/ultrasonic_status', self._ultrasonic_callback, 20)
        self.create_timer(0.10, self._drive_timer)

    def set_drive(self, action: str, hold_sec: float = 0.45) -> None:
        if action not in self.DRIVE_VALUES:
            raise ValueError(f'unsupported drive action: {action}')
        with self._state_lock:
            self._drive_action = action
            self._drive_deadline = time.monotonic() + hold_sec if action != 'stop' else 0.0
            self._stop_sent_after_timeout = action == 'stop'
        self._publish_drive(action)

    def emergency_stop(self) -> None:
        self.set_drive('stop')
        for _ in range(2):
            time.sleep(0.03)
            self._publish_drive('stop')

    def call_trigger(self, service_name: str, timeout_sec: float = 4.0) -> dict:
        with self._clients_lock:
            client = self._trigger_clients.get(service_name)
            if client is None:
                client = self.create_client(Trigger, service_name)
                self._trigger_clients[service_name] = client
        if not client.wait_for_service(timeout_sec=min(timeout_sec, 2.0)):
            raise RuntimeError(f'ROS service unavailable: {service_name}')
        future = client.call_async(Trigger.Request())
        deadline = time.monotonic() + timeout_sec
        while not future.done():
            if time.monotonic() >= deadline:
                raise TimeoutError(f'ROS service timeout: {service_name}')
            time.sleep(0.02)
        response = future.result()
        if response is None:
            raise RuntimeError(f'ROS service failed: {service_name}')
        return {'success': bool(response.success), 'message': str(response.message)}

    def status(self) -> dict:
        now = time.monotonic()
        with self._state_lock:
            return {
                'drive_action': self._drive_action,
                'route_recording': self.route_recording,
                'route_player_state': self.route_player_state,
                'pose': dict(self.pose),
                'velocity': dict(self.velocity),
                'ultrasonic': dict(self.ultrasonic),
                'sensors': {
                    key: {
                        'online': timestamp > 0.0 and now - timestamp < 2.0,
                        'age_sec': round(now - timestamp, 2) if timestamp > 0.0 else None,
                    }
                    for key, timestamp in self.last_seen.items()
                },
            }

    def _drive_timer(self) -> None:
        with self._state_lock:
            action = self._drive_action
            expired = action != 'stop' and self._drive_deadline > 0.0 and time.monotonic() > self._drive_deadline
            if expired:
                self._drive_action = 'stop'
                self._drive_deadline = 0.0
                action = 'stop'
            if action == 'stop' and self._stop_sent_after_timeout:
                return
        self._publish_drive(action)
        if action == 'stop':
            with self._state_lock:
                self._stop_sent_after_timeout = True

    def _publish_drive(self, action: str) -> None:
        linear, angular = self.DRIVE_VALUES[action]
        message = Twist()
        message.linear.x = linear
        message.angular.z = angular
        self.cmd_publisher.publish(message)

    def _route_recording_callback(self, message: Bool) -> None:
        with self._state_lock:
            self.route_recording = bool(message.data)

    def _route_state_callback(self, message: String) -> None:
        with self._state_lock:
            self.route_player_state = str(message.data)

    def _ultrasonic_callback(self, message: String) -> None:
        try:
            data = json.loads(message.data)
            if not isinstance(data, dict):
                return
        except Exception:
            return
        with self._state_lock:
            self.ultrasonic = {
                'distance_mm': int(data.get('distance_mm', 0)),
                'distance_cm': float(data.get('distance_cm', 0.0)),
                'valid': bool(data.get('valid', False)),
                'near': bool(data.get('near', False)),
                'stop': bool(data.get('stop', False)),
                'emergency': bool(data.get('emergency', False)),
            }
            self.last_seen['ultrasonic'] = time.monotonic()

    def _odom_callback(self, message: Odometry) -> None:
        o = message.pose.pose.orientation
        yaw = math.atan2(2.0 * (o.w * o.z + o.x * o.y), 1.0 - 2.0 * (o.y * o.y + o.z * o.z))
        with self._state_lock:
            self.pose = {'x': round(float(message.pose.pose.position.x), 3), 'y': round(float(message.pose.pose.position.y), 3), 'yaw': round(float(yaw), 3)}
            self.velocity = {'linear': round(float(message.twist.twist.linear.x), 3), 'angular': round(float(message.twist.twist.angular.z), 3)}
            self.last_seen['odom'] = time.monotonic()

    def _wheel_odom_callback(self, _message: Odometry) -> None:
        with self._state_lock:
            self.last_seen['wheel_odom'] = time.monotonic()

    def _imu_callback(self, _message: Imu) -> None:
        with self._state_lock:
            self.last_seen['imu'] = time.monotonic()

    def _scan_callback(self, _message: LaserScan) -> None:
        with self._state_lock:
            self.last_seen['lidar'] = time.monotonic()


class LaunchProcessManager:
    def __init__(self, bridge: RosWebBridge) -> None:
        self.bridge = bridge
        self._lock = threading.RLock()
        self._process: Optional[subprocess.Popen[str]] = None
        self._mode = 'stopped'
        self._selected_map: Optional[str] = None
        self._logs: deque[str] = deque(maxlen=250)

    def start_mapping(self) -> None:
        self._replace_process(['ros2', 'launch', 'robotlidar', 'mapping.launch.py', f'serial_port:={SERIAL_PORT}'], 'mapping', None)

    def start_navigation(self, map_path: Path) -> None:
        self._replace_process(['ros2', 'launch', 'robotlidar', 'navigation.launch.py', f'map:={map_path}', f'serial_port:={SERIAL_PORT}'], 'navigation', map_path.stem)

    def stop(self) -> None:
        self.bridge.emergency_stop()
        with self._lock:
            process = self._process
            self._process = None
            self._mode = 'stopped'
            self._selected_map = None
        if process is None or process.poll() is not None:
            return
        self._append_log('WEB: stopping ROS launch process')
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            process.wait(timeout=6.0)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            process.wait(timeout=2.0)
        except ProcessLookupError:
            pass

    def status(self) -> dict:
        with self._lock:
            process = self._process
            mode = self._mode
            selected_map = self._selected_map
        running = process is not None and process.poll() is None
        exit_code = None if process is None or running else process.returncode
        if process is not None and not running and mode != 'stopped':
            with self._lock:
                self._mode = 'error'
                mode = 'error'
        return {'mode': mode, 'process_running': running, 'pid': process.pid if running else None, 'exit_code': exit_code, 'selected_map': selected_map, 'logs': list(self._logs)[-80:]}

    def _replace_process(self, command: list[str], mode: str, selected_map: Optional[str]) -> None:
        self.stop()
        self._append_log('WEB: ' + ' '.join(command))
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, preexec_fn=os.setsid)
        except Exception:
            self._append_log('WEB: failed to start ROS launch process')
            raise
        with self._lock:
            self._process = process
            self._mode = mode
            self._selected_map = selected_map
        threading.Thread(target=self._read_output, args=(process,), name='robotlidar-launch-log', daemon=True).start()

    def _read_output(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            self._append_log(line.rstrip())
        self._append_log(f'WEB: ROS launch exited with code {process.wait()}')

    def _append_log(self, line: str) -> None:
        with self._lock:
            self._logs.append(f'[{time.strftime("%H:%M:%S")}] {line}')


class RuntimeSettings:
    DEFAULTS = {'default_map': None, 'auto_start': False, 'startup_mode': 'navigation'}

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.data = dict(self.DEFAULTS)
        self.load()

    def load(self) -> dict:
        with self._lock:
            if SETTINGS_FILE.exists():
                try:
                    loaded = json.loads(SETTINGS_FILE.read_text('utf-8'))
                    if isinstance(loaded, dict):
                        self.data.update(loaded)
                except Exception:
                    pass
            self._normalize()
            return dict(self.data)

    def save(self) -> None:
        with self._lock:
            self._normalize()
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            temporary = SETTINGS_FILE.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')
            temporary.replace(SETTINGS_FILE)

    def update(self, **values: Any) -> dict:
        with self._lock:
            self.data.update(values)
        self.save()
        return self.snapshot()

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.data)

    def _normalize(self) -> None:
        if self.data.get('startup_mode') not in ('stopped', 'mapping', 'navigation'):
            self.data['startup_mode'] = 'navigation'
        self.data['auto_start'] = bool(self.data.get('auto_start', False))
        default_map = self.data.get('default_map')
        self.data['default_map'] = str(default_map) if default_map else None


def _static_directory() -> Path:
    try:
        installed = Path(get_package_share_directory('robotlidar')) / 'web' / 'static'
        if installed.exists():
            return installed
    except Exception:
        pass
    return Path(__file__).resolve().parents[1] / 'web' / 'static'


def _safe_map_name(raw_name: str) -> str:
    name = raw_name.strip().replace(' ', '_')
    if not MAP_NAME_RE.fullmatch(name):
        raise ValueError('Имя карты: только буквы, цифры, подчёркивание и дефис')
    return name


def _map_yaml_path(name: str) -> Path:
    safe_name = _safe_map_name(name)
    path = (MAPS_DIR / f'{safe_name}.yaml').resolve()
    if path.parent != MAPS_DIR.resolve():
        raise ValueError('invalid map path')
    return path


def _read_map_info(path: Path, default_name: Optional[str]) -> dict:
    result = {'name': path.stem, 'yaml_path': str(path), 'default': path.stem == default_name, 'modified_at': path.stat().st_mtime, 'image_exists': False, 'resolution': None, 'origin': None}
    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        image_value = data.get('image')
        if image_value:
            image_path = Path(str(image_value))
            if not image_path.is_absolute():
                image_path = path.parent / image_path
            result['image_exists'] = image_path.exists()
        result['resolution'] = data.get('resolution')
        result['origin'] = data.get('origin')
    except Exception as exc:
        result['error'] = str(exc)
    return result


def _list_maps(settings: RuntimeSettings) -> list[dict]:
    MAPS_DIR.mkdir(parents=True, exist_ok=True)
    default_name = settings.snapshot().get('default_map')
    maps = [_read_map_info(path, default_name) for path in MAPS_DIR.glob('*.yaml') if path.is_file()]
    maps.sort(key=lambda item: item['modified_at'], reverse=True)
    return maps


def _map_image_path(map_yaml: Path) -> Path:
    data = yaml.safe_load(map_yaml.read_text(encoding='utf-8')) or {}
    image_value = data.get('image')
    if not image_value:
        raise FileNotFoundError('map YAML has no image')
    image_path = Path(str(image_value))
    if not image_path.is_absolute():
        image_path = map_yaml.parent / image_path
    image_path = image_path.resolve()
    if image_path.parent != MAPS_DIR.resolve():
        raise ValueError('map image is outside maps directory')
    if not image_path.exists():
        raise FileNotFoundError(image_path)
    return image_path


DATA_DIR.mkdir(parents=True, exist_ok=True)
MAPS_DIR.mkdir(parents=True, exist_ok=True)
ROUTES_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
rclpy.init(args=None)
bridge = RosWebBridge()
ros_thread = threading.Thread(target=rclpy.spin, args=(bridge,), name='robotlidar-ros-spin', daemon=True)
ros_thread.start()
settings = RuntimeSettings()
process_manager = LaunchProcessManager(bridge)
app = FastAPI(title='RobotLidar Control', version='0.2.0')
static_dir = _static_directory()
app.mount('/static', StaticFiles(directory=str(static_dir)), name='static')


@app.on_event('startup')
def startup_event() -> None:
    def delayed_start() -> None:
        time.sleep(3.0)
        current = settings.snapshot()
        if not current.get('auto_start'):
            return
        try:
            mode = current.get('startup_mode')
            if mode == 'mapping':
                process_manager.start_mapping()
            elif mode == 'navigation':
                map_name = current.get('default_map')
                if not map_name:
                    process_manager._append_log('WEB: auto-start skipped: default map is not selected')
                    return
                map_path = _map_yaml_path(map_name)
                if not map_path.exists():
                    process_manager._append_log(f'WEB: auto-start skipped: map not found: {map_path}')
                    return
                process_manager.start_navigation(map_path)
        except Exception as exc:
            process_manager._append_log(f'WEB: auto-start failed: {exc}')
    threading.Thread(target=delayed_start, daemon=True).start()


@app.on_event('shutdown')
def shutdown_event() -> None:
    process_manager.stop()
    bridge.destroy_node()
    if rclpy.ok():
        rclpy.shutdown()


@app.get('/')
def index() -> FileResponse:
    return FileResponse(static_dir / 'index.html')


@app.get('/radar')
def radar() -> FileResponse:
    return FileResponse(static_dir / 'index.html')


@app.get('/api/status')
def api_status() -> dict:
    return {
        'ok': True,
        'runtime': process_manager.status(),
        'ros': bridge.status(),
        'settings': settings.snapshot(),
        'route_file_exists': ROUTE_FILE.exists(),
        'maps_count': len(_list_maps(settings)),
        'data_dir': str(DATA_DIR),
    }


@app.get('/api/maps')
def api_maps() -> dict:
    return {'maps': _list_maps(settings), 'settings': settings.snapshot()}


@app.get('/api/maps/{name}/preview.png')
def api_map_preview(name: str):
    try:
        map_yaml = _map_yaml_path(name)
        image_path = _map_image_path(map_yaml)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if image_path.suffix.lower() == '.png':
        return FileResponse(image_path, media_type='image/png')
    if Image is None:
        raise HTTPException(status_code=503, detail='python3-pil is required for map previews')
    preview_dir = DATA_DIR / 'cache'
    preview_dir.mkdir(parents=True, exist_ok=True)
    preview_path = preview_dir / f'{map_yaml.stem}.png'
    if not preview_path.exists() or preview_path.stat().st_mtime < image_path.stat().st_mtime:
        with Image.open(image_path) as image:
            image.convert('L').save(preview_path, format='PNG')
    return FileResponse(preview_path, media_type='image/png')


@app.post('/api/drive')
def api_drive(request: DriveRequest) -> dict:
    if request.action not in RosWebBridge.DRIVE_VALUES:
        raise HTTPException(status_code=400, detail='Неизвестная команда движения')
    bridge.set_drive(request.action)
    return {'success': True, 'action': request.action}


@app.post('/api/mode/mapping')
def api_start_mapping() -> dict:
    try:
        process_manager.start_mapping()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {'success': True, 'mode': 'mapping'}


@app.post('/api/mode/navigation')
def api_start_navigation(request: NavigationRequest) -> dict:
    map_name = request.map_name or settings.snapshot().get('default_map')
    if not map_name:
        raise HTTPException(status_code=400, detail='Карта не выбрана')
    try:
        map_path = _map_yaml_path(map_name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not map_path.exists():
        raise HTTPException(status_code=404, detail='Файл карты не найден')
    try:
        process_manager.start_navigation(map_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {'success': True, 'mode': 'navigation', 'map': map_name}


@app.post('/api/mode/stop')
def api_stop_mode() -> dict:
    process_manager.stop()
    return {'success': True, 'mode': 'stopped'}


@app.post('/api/maps/save')
def api_save_map(request: MapSaveRequest) -> dict:
    if process_manager.status()['mode'] != 'mapping':
        raise HTTPException(status_code=409, detail='Сохранение карты доступно в режиме картографирования')
    try:
        name = _safe_map_name(request.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    output_base = MAPS_DIR / name
    try:
        result = subprocess.run(['ros2', 'run', 'nav2_map_server', 'map_saver_cli', '-f', str(output_base)], check=False, capture_output=True, text=True, timeout=25.0)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    process_manager._append_log('WEB MAP SAVE: ' + (result.stdout + result.stderr).strip())
    if result.returncode != 0:
        raise HTTPException(status_code=500, detail=(result.stdout + result.stderr).strip() or f'map_saver_cli returned {result.returncode}')
    yaml_path = output_base.with_suffix('.yaml')
    if not yaml_path.exists():
        raise HTTPException(status_code=500, detail='map_saver_cli завершился, но YAML карты не создан')
    if request.set_default:
        settings.update(default_map=name)
    return {'success': True, 'map': _read_map_info(yaml_path, settings.snapshot().get('default_map'))}


@app.post('/api/maps/default')
def api_set_default_map(request: MapSelectionRequest) -> dict:
    try:
        map_path = _map_yaml_path(request.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not map_path.exists():
        raise HTTPException(status_code=404, detail='Карта не найдена')
    return {'success': True, 'settings': settings.update(default_map=map_path.stem)}


@app.post('/api/settings')
def api_settings(request: SettingsRequest) -> dict:
    if request.startup_mode not in ('stopped', 'mapping', 'navigation'):
        raise HTTPException(status_code=400, detail='Неизвестный режим автозапуска')
    return {'success': True, 'settings': settings.update(auto_start=request.auto_start, startup_mode=request.startup_mode)}


ROUTE_SERVICES = {
    'start-recording': '/route/start_recording',
    'stop-recording': '/route/stop_recording',
    'clear': '/route/clear',
    'save': '/route/save',
    'play': '/route/play',
    'cancel': '/route/cancel',
    'reload': '/route/reload',
}


@app.post('/api/route/{operation}')
async def api_route_operation(operation: str) -> JSONResponse:
    service_name = ROUTE_SERVICES.get(operation)
    if service_name is None:
        raise HTTPException(status_code=404, detail='Неизвестная операция')
    try:
        result = await asyncio.to_thread(bridge.call_trigger, service_name)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return JSONResponse(result, status_code=200 if result['success'] else 409)


def main(args: Optional[list[str]] = None) -> None:
    del args
    uvicorn.run(app, host=HOST, port=PORT, log_level='info')


if __name__ == '__main__':
    main()
