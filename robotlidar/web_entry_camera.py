#!/usr/bin/env python3
"""RobotLidar Raspberry web entry with IP-camera RTSP relay support."""
from __future__ import annotations

from typing import Optional

import uvicorn
from fastapi import HTTPException
from pydantic import BaseModel, Field

from robotlidar import web_entry_settings as base
from robotlidar.ip_camera_relay import IpCameraRelayManager

app = base.app
web_app = base.web_app


class CameraSettingsRequest(BaseModel):
    enabled: bool = False
    device_id: str = Field(min_length=1, max_length=64)
    rtsp_url: str = Field(default='', max_length=2048)
    server_url: str = Field(default='', max_length=512)


camera_relay = IpCameraRelayManager(log_callback=web_app.process_manager._append_log)
camera_relay.start(web_app.settings.snapshot())


@app.get('/api/camera/status')
def api_camera_status() -> dict:
    return {'ok': True, 'camera': camera_relay.status()}


@app.post('/api/camera/settings')
def api_camera_settings(request: CameraSettingsRequest) -> dict:
    device_id = request.device_id.strip()
    rtsp_url = request.rtsp_url.strip()
    server_url = request.server_url.strip().rstrip('/')

    if request.enabled and not rtsp_url:
        raise HTTPException(status_code=400, detail='Укажите RTSP URL камеры')
    if request.enabled and not server_url:
        raise HTTPException(status_code=400, detail='Укажите адрес центрального сервера')
    if rtsp_url and not rtsp_url.lower().startswith('rtsp://'):
        raise HTTPException(status_code=400, detail='RTSP URL должен начинаться с rtsp://')
    if server_url and not server_url.lower().startswith(('http://', 'https://')):
        raise HTTPException(status_code=400, detail='Адрес сервера должен начинаться с http:// или https://')

    settings = web_app.settings.update(
        camera_enabled=bool(request.enabled),
        camera_device_id=device_id,
        camera_rtsp_url=rtsp_url,
        camera_server_url=server_url,
    )
    camera_relay.configure(settings)
    return {'ok': True, 'settings': settings, 'camera': camera_relay.status()}


@app.post('/api/camera/restart')
def api_camera_restart() -> dict:
    settings = web_app.settings.snapshot()
    camera_relay.configure({**settings, 'camera_enabled': False})
    camera_relay.configure(settings)
    return {'ok': True, 'camera': camera_relay.status()}


def main(args: Optional[list[str]] = None) -> None:
    del args
    uvicorn.run(
        app,
        host=web_app.HOST,
        port=web_app.PORT,
        log_level='info',
    )


if __name__ == '__main__':
    main()
