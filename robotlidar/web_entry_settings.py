#!/usr/bin/env python3
"""RobotLidar web entry with ESP32 settings and RC-selected map autorun."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from std_msgs.msg import String
from std_srvs.srv import SetBool

from robotlidar import web_entry

web_app = web_entry.web_app
app = web_app.app
bridge = web_app.bridge
STATIC_DIR: Path = web_entry.STATIC_DIR


# The ESP32 serial bridge is permanently owned by web_stack.launch.py. Mapping and
# navigation use its topics and start only ESP32 odometry, never a second serial
# bridge and never the legacy GPIO drive backend.
def _start_mapping_with_external_esp32() -> None:
    web_app.process_manager._replace_process(
        [
            'ros2', 'launch', 'robotlidar', 'mapping.launch.py',
            f'serial_port:={web_app.SERIAL_PORT}',
            'use_esp32_drive:=true',
            'external_esp32_drive:=true',
        ],
        'mapping',
        None,
    )


def _start_navigation_with_external_esp32(map_path: Path) -> None:
    web_app.process_manager._replace_process(
        [
            'ros2', 'launch', 'robotlidar', 'navigation.launch.py',
            f'map:={map_path}',
            f'serial_port:={web_app.SERIAL_PORT}',
            'use_esp32_drive:=true',
            'external_esp32_drive:=true',
        ],
        'navigation',
        map_path.stem,
    )


web_app.process_manager.start_mapping = _start_mapping_with_external_esp32
web_app.process_manager.start_navigation = _start_navigation_with_external_esp32


_config_lock = threading.RLock()
_config_state = {'connected': False, 'received_at': None}
_config_publisher = bridge.create_publisher(String, '/esp32/config/request', 10)

_mode_lock = threading.RLock()
_esp32_mode: Optional[str] = None
_mode_generation = 0
_arm_client = bridge.create_client(SetBool, '/drive/arm')


class Esp32ConfigRequest(BaseModel):
    values: dict


def _config_state_callback(message: String) -> None:
    try:
        data = json.loads(message.data)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    data['received_at'] = time.time()
    with _config_lock:
        _config_state.clear()
        _config_state.update(data)


_config_subscription = bridge.create_subscription(
    String, '/esp32/config/state', _config_state_callback, 10
)


def _publish_config_request(payload: dict) -> None:
    message = String()
    message.data = json.dumps(payload, ensure_ascii=False)
    _config_publisher.publish(message)


def _current_mode_is_ros(generation: int) -> bool:
    with _mode_lock:
        return _mode_generation == generation and _esp32_mode == 'ROS'


def _set_esp32_arm(enabled: bool, timeout_sec: float = 8.0) -> tuple[bool, str]:
    if not _arm_client.wait_for_service(timeout_sec=timeout_sec):
        return False, '/drive/arm service unavailable'
    request = SetBool.Request()
    request.data = enabled
    future = _arm_client.call_async(request)
    deadline = time.monotonic() + timeout_sec
    while not future.done() and time.monotonic() < deadline:
        time.sleep(0.03)
    if not future.done():
        return False, '/drive/arm timeout'
    response = future.result()
    if response is None:
        return False, '/drive/arm failed'
    return bool(response.success), str(response.message)


def _wait_for_route_service(generation: int, timeout_sec: float = 35.0) -> bool:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline and _current_mode_is_ros(generation):
        try:
            result = bridge.call_trigger('/route/reload', timeout_sec=2.0)
            if result.get('success'):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _start_saved_route_for_ros_mode(generation: int) -> None:
    try:
        current = web_app.settings.snapshot()
        map_name = current.get('default_map')
        if not map_name:
            web_app.process_manager._append_log(
                'ESP32 MAP: start skipped: default map is not selected'
            )
            return
        map_path = web_app._map_yaml_path(str(map_name))
        if not map_path.exists():
            web_app.process_manager._append_log(
                f'ESP32 MAP: start skipped: map not found: {map_path}'
            )
            return
        if not web_app.ROUTE_FILE.exists():
            web_app.process_manager._append_log(
                f'ESP32 MAP: start skipped: saved route not found: {web_app.ROUTE_FILE}'
            )
            return
        if not _current_mode_is_ros(generation):
            return

        runtime = web_app.process_manager.status()
        if (
            runtime.get('mode') != 'navigation'
            or not runtime.get('process_running')
            or runtime.get('selected_map') != map_path.stem
        ):
            web_app.process_manager._append_log(
                f'ESP32 MAP: ROS mode selected, loading navigation map {map_path.stem}'
            )
            web_app.process_manager.start_navigation(map_path)

        if not _wait_for_route_service(generation):
            web_app.process_manager._append_log(
                'ESP32 MAP: route service did not become ready while ROS mode was selected'
            )
            return
        if not _current_mode_is_ros(generation):
            return

        armed, arm_message = _set_esp32_arm(True)
        web_app.process_manager._append_log(
            f'ESP32 MAP: ARM={armed}: {arm_message}'
        )
        if not armed or not _current_mode_is_ros(generation):
            return

        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and _current_mode_is_ros(generation):
            try:
                result = bridge.call_trigger('/route/play', timeout_sec=5.0)
            except Exception as exc:
                web_app.process_manager._append_log(
                    f'ESP32 MAP: waiting for Nav2 route start: {exc}'
                )
                time.sleep(0.7)
                continue
            if result.get('success'):
                web_app.process_manager._append_log(
                    'ESP32 MAP: saved route started by ESP32 ROS/map mode'
                )
                return
            message = str(result.get('message', 'route start rejected'))
            if 'already active' in message.lower():
                return
            web_app.process_manager._append_log(
                f'ESP32 MAP: route not ready yet: {message}'
            )
            time.sleep(0.7)

        web_app.process_manager._append_log('ESP32 MAP: route start timed out')
        _set_esp32_arm(False, timeout_sec=3.0)
    except Exception as exc:
        web_app.process_manager._append_log(f'ESP32 MAP: autorun failed: {exc}')
        try:
            _set_esp32_arm(False, timeout_sec=2.0)
        except Exception:
            pass


def _stop_saved_route_after_ros_mode() -> None:
    try:
        bridge.call_trigger('/route/cancel', timeout_sec=2.0)
    except Exception:
        pass
    try:
        _set_esp32_arm(False, timeout_sec=2.0)
    except Exception:
        pass
    web_app.process_manager._append_log(
        'ESP32 MAP: ROS/map mode left; route canceled and drive disarmed'
    )


def _esp32_status_callback(message: String) -> None:
    global _esp32_mode, _mode_generation
    try:
        payload = json.loads(message.data)
        telemetry = payload.get('telemetry') if isinstance(payload, dict) else None
        mode = str((telemetry or {}).get('control_mode') or '').upper()
    except Exception:
        return
    if mode not in {'ROS', 'RC', 'SAFE'}:
        return

    with _mode_lock:
        previous = _esp32_mode
        if mode == previous:
            return
        _esp32_mode = mode
        _mode_generation += 1
        generation = _mode_generation

    web_app.process_manager._append_log(
        f'ESP32 MODE: {previous or "UNKNOWN"} -> {mode}'
    )
    if mode == 'ROS':
        threading.Thread(
            target=_start_saved_route_for_ros_mode,
            args=(generation,),
            name='esp32-map-autorun',
            daemon=True,
        ).start()
    elif previous == 'ROS':
        threading.Thread(
            target=_stop_saved_route_after_ros_mode,
            name='esp32-map-stop',
            daemon=True,
        ).start()


_esp32_status_subscription = bridge.create_subscription(
    String, '/drive/esp32_status', _esp32_status_callback, 10
)


app.routes[:] = [r for r in app.routes if getattr(r, 'path', None) != '/']


@app.get('/', include_in_schema=False)
def index_page_with_settings() -> HTMLResponse:
    html = (STATIC_DIR / 'index.html').read_text(encoding='utf-8')
    html = html.replace(
        '</head>',
        '<style>.topbar-live-actions{display:flex;align-items:center;gap:.65rem;flex-wrap:wrap;justify-content:flex-end}'
        '.radar-page-link{display:inline-flex;align-items:center;padding:.6rem .85rem;border-radius:999px;'
        'border:1px solid rgba(148,163,184,.32);color:inherit;text-decoration:none;font-weight:700}'
        '.radar-page-link:hover{border-color:#38bdf8}</style><script src="/static/ws-client.js"></script></head>',
    )
    marker = '<div class="connection" id="connectionBadge">Подключение…</div>'
    html = html.replace(
        marker,
        '<div class="topbar-live-actions"><a class="radar-page-link" href="/radar">Радар, IMU и GPS</a>'
        '<a class="radar-page-link" href="/esp32-settings">Настройки ESP32</a>' + marker + '</div>',
    )
    return HTMLResponse(html)


@app.get('/esp32-settings', include_in_schema=False)
@app.get('/esp32-settings/', include_in_schema=False)
def esp32_settings_page() -> FileResponse:
    return FileResponse(STATIC_DIR / 'esp32-settings.html')


@app.get('/esp32-settings.css', include_in_schema=False)
def esp32_settings_css() -> FileResponse:
    return FileResponse(STATIC_DIR / 'esp32-settings.css', media_type='text/css')


@app.get('/esp32-settings.js', include_in_schema=False)
def esp32_settings_js() -> FileResponse:
    return FileResponse(STATIC_DIR / 'esp32-settings.js', media_type='application/javascript')


@app.get('/api/esp32/config')
def api_esp32_config() -> dict:
    with _config_lock:
        state = dict(_config_state)
    received_at = state.get('received_at')
    state['online'] = bool(received_at and time.time() - float(received_at) < 8.0)
    _publish_config_request({'op': 'get'})
    return {'ok': True, 'config': state}


@app.post('/api/esp32/config')
def api_set_esp32_config(request: Esp32ConfigRequest) -> dict:
    allowed = {
        'us_enabled', 'us_warn_mm', 'us_stop_mm', 'us_emergency_mm', 'us_clear_mm',
        'us_danger_samples', 'us_clear_samples', 'us_sample_ms', 'hall_enabled',
        'hall_left_inverted', 'hall_right_inverted', 'hall_ppr', 'wheel_circ_mm',
        'track_width_mm', 'rc_deadband_us', 'rc_timeout_ms', 'throttle_idle_mv',
        'throttle_max_mv', 'reverse_brake_ms', 'reverse_settle_ms', 'ramp_step',
        'track_reverse_active_high', 'actuator_timeout_ms', 'actuator_guard_ms',
        'actuator_reversed', 'brush_idle_mv', 'brush_max_mv', 'brush_stop_us',
        'brush_brake_active_high', 'aux_idle_mv', 'aux_max_mv',
        'aux_reverse_guard_ms', 'aux_ramp_step', 'aux_reverse_active_high',
        'ros_aux_timeout_ms',
    }
    for channel in range(1, 7):
        allowed.update({
            f'rc{channel}_min_us', f'rc{channel}_center_us', f'rc{channel}_max_us'
        })
    unknown = sorted(set(request.values) - allowed)
    if unknown:
        raise HTTPException(
            status_code=400,
            detail='Неизвестные параметры: ' + ', '.join(unknown),
        )
    _publish_config_request({'op': 'set', 'values': request.values})
    return {'ok': True, 'message': 'Настройки отправлены на ESP32 и сохраняются в NVS'}


@app.post('/api/esp32/config/reset')
def api_reset_esp32_config() -> dict:
    _publish_config_request({'op': 'reset'})
    return {'ok': True, 'message': 'Команда восстановления заводских настроек отправлена на ESP32'}


def main(args: Optional[list[str]] = None) -> None:
    del args
    uvicorn.run(app, host=web_app.HOST, port=web_app.PORT, log_level='info')


if __name__ == '__main__':
    main()
