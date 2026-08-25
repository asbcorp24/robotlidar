#!/usr/bin/env python3
"""RobotLidar Raspberry web entry with RTSP relay and remote control."""
from __future__ import annotations

import uuid
from typing import Optional

import uvicorn
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from robotlidar import web_entry_settings as base
from robotlidar.ip_camera_relay import IpCameraRelayManager
from robotlidar.remote_control_gateway import RemoteControlGateway

app = base.app
web_app = base.web_app


class CameraSettingsRequest(BaseModel):
    enabled: bool = False
    remote_control_enabled: bool = False
    device_id: str = Field(min_length=1, max_length=64)
    rtsp_url: str = Field(default='', max_length=2048)
    server_url: str = Field(default='', max_length=512)
    control_port: int = Field(default=6000, ge=1, le=65535)
    onvif_url: str = Field(default='', max_length=1024)
    onvif_username: str = Field(default='', max_length=128)
    onvif_password: str = Field(default='', max_length=256)
    onvif_profile_token: str = Field(default='Profile_1', max_length=256)


class CameraEnabledRequest(BaseModel):
    enabled: bool


class RemoteControlEnabledRequest(BaseModel):
    enabled: bool


camera_relay = IpCameraRelayManager(log_callback=web_app.process_manager._append_log)
remote_control = RemoteControlGateway(
    web_app.bridge,
    arm_callback=base._set_esp32_arm,
    log_callback=web_app.process_manager._append_log,
)
_initial_settings = web_app.settings.snapshot()
if not _initial_settings.get('camera_device_id'):
    _initial_settings = web_app.settings.update(
        camera_device_id=IpCameraRelayManager.default_device_id()
    )
camera_relay.start(_initial_settings)
remote_control.start(_initial_settings)


def _new_device_id() -> str:
    return f'TRACTOR-RPI-{uuid.uuid4().hex[:12].upper()}'


def _validate_common(server_url: str, rtsp_url: str, onvif_url: str) -> None:
    if rtsp_url and not rtsp_url.lower().startswith('rtsp://'):
        raise HTTPException(status_code=400, detail='RTSP URL должен начинаться с rtsp://')
    if server_url and not server_url.lower().startswith(('http://', 'https://')):
        raise HTTPException(status_code=400, detail='Адрес сервера должен начинаться с http:// или https://')
    if onvif_url and not onvif_url.lower().startswith(('http://', 'https://')):
        raise HTTPException(status_code=400, detail='ONVIF URL должен начинаться с http:// или https://')


@app.get('/api/camera/status')
def api_camera_status() -> dict:
    return {
        'ok': True,
        'camera': camera_relay.status(),
        'control': remote_control.status(),
    }


@app.post('/api/camera/settings')
def api_camera_settings(request: CameraSettingsRequest) -> dict:
    device_id = request.device_id.strip().upper()
    rtsp_url = request.rtsp_url.strip()
    server_url = request.server_url.strip().rstrip('/')
    onvif_url = request.onvif_url.strip()
    _validate_common(server_url, rtsp_url, onvif_url)

    if request.enabled and not rtsp_url:
        raise HTTPException(status_code=400, detail='Укажите RTSP URL камеры')
    if (request.enabled or request.remote_control_enabled) and not server_url:
        raise HTTPException(status_code=400, detail='Укажите адрес центрального сервера')

    settings = web_app.settings.update(
        camera_enabled=bool(request.enabled),
        camera_remote_control_enabled=bool(request.remote_control_enabled),
        camera_device_id=device_id,
        camera_rtsp_url=rtsp_url,
        camera_server_url=server_url,
        camera_control_port=int(request.control_port),
        camera_onvif_url=onvif_url,
        camera_onvif_username=request.onvif_username.strip(),
        camera_onvif_password=request.onvif_password,
        camera_onvif_profile_token=request.onvif_profile_token.strip() or 'Profile_1',
    )
    camera_relay.configure(settings)
    remote_control.configure(settings)
    return {
        'ok': True,
        'settings': settings,
        'camera': camera_relay.status(),
        'control': remote_control.status(),
    }


@app.post('/api/camera/enabled')
def api_camera_enabled(request: CameraEnabledRequest) -> dict:
    settings = web_app.settings.snapshot()
    if request.enabled:
        if not str(settings.get('camera_rtsp_url') or '').strip():
            raise HTTPException(status_code=400, detail='Сначала укажите RTSP URL камеры')
        if not str(settings.get('camera_server_url') or '').strip():
            raise HTTPException(status_code=400, detail='Сначала укажите адрес центрального сервера')
    settings = web_app.settings.update(camera_enabled=bool(request.enabled))
    camera_relay.configure(settings)
    return {'ok': True, 'enabled': bool(request.enabled), 'camera': camera_relay.status()}


@app.post('/api/control/enabled')
def api_control_enabled(request: RemoteControlEnabledRequest) -> dict:
    settings = web_app.settings.snapshot()
    if request.enabled and not str(settings.get('camera_server_url') or '').strip():
        raise HTTPException(status_code=400, detail='Сначала укажите адрес центрального сервера')
    settings = web_app.settings.update(camera_remote_control_enabled=bool(request.enabled))
    camera_relay.configure(settings)
    remote_control.configure(settings)
    return {'ok': True, 'enabled': bool(request.enabled), 'control': remote_control.status()}


@app.post('/api/camera/generate-device-id')
def api_camera_generate_device_id() -> dict:
    settings = web_app.settings.update(camera_device_id=_new_device_id())
    camera_relay.configure(settings)
    remote_control.configure(settings)
    return {
        'ok': True,
        'device_id': settings['camera_device_id'],
        'settings': settings,
        'camera': camera_relay.status(),
        'control': remote_control.status(),
    }


@app.post('/api/camera/restart')
def api_camera_restart() -> dict:
    settings = web_app.settings.snapshot()
    camera_relay.configure({**settings, 'camera_enabled': False})
    camera_relay.configure(settings)
    return {'ok': True, 'camera': camera_relay.status()}


app.routes[:] = [route for route in app.routes if getattr(route, 'path', None) != '/']

_CAMERA_CARD = r'''
<section class="card settings-card" id="cameraSettingsCard">
  <div class="card-head compact">
    <div><div class="eyebrow">IP-камера и удалённое управление</div><h2>Raspberry Pi ↔ центральный сервер</h2></div>
    <strong id="cameraRelaySummary">выключено</strong>
  </div>
  <div class="notice">RTSP-видео и удалённое управление независимы. Можно отключить передачу видео, оставив управление гусеницами, щёткой и камерой с центрального сайта.</div>
  <div class="camera-enable-box">
    <label class="switch-line camera-master-switch"><input type="checkbox" id="cameraEnabled"> <strong>Передавать RTSP на сервер</strong></label>
    <label class="switch-line camera-master-switch"><input type="checkbox" id="remoteControlEnabled"> <strong>Разрешить управление с сайта</strong></label>
  </div>
  <div class="settings-row camera-settings-row">
    <label class="camera-device-label">Device ID
      <div class="camera-id-row"><input id="cameraDeviceId" maxlength="64" placeholder="TRACTOR-RPI-001"><button type="button" class="ghost" id="generateCameraDeviceId">Сгенерировать ID</button></div>
    </label>
    <label class="camera-wide">Адрес центрального сервера<input id="cameraServerUrl" autocomplete="off" placeholder="http://192.168.1.100:8000"></label>
    <label>UDP порт управления<input id="cameraControlPort" type="number" min="1" max="65535" value="6000"></label>
    <label class="camera-wide">RTSP URL камеры<input id="cameraRtspUrl" autocomplete="off" placeholder="rtsp://user:password@192.168.1.60:554/stream"></label>
  </div>
  <details class="camera-onvif"><summary><strong>ONVIF PTZ камеры</strong> — открыть настройки поворота</summary>
    <div class="settings-row camera-settings-row camera-onvif-grid">
      <label class="camera-wide">ONVIF PTZ URL<input id="cameraOnvifUrl" autocomplete="off" placeholder="http://192.168.1.60/onvif/ptz_service"></label>
      <label>ONVIF пользователь<input id="cameraOnvifUsername" autocomplete="off"></label>
      <label>ONVIF пароль<input id="cameraOnvifPassword" type="password" autocomplete="new-password"></label>
      <label>Profile token<input id="cameraOnvifProfileToken" value="Profile_1"></label>
    </div>
  </details>
  <div class="button-row camera-actions"><button class="primary" id="saveCameraSettings">Сохранить настройки</button><button class="ghost" id="restartCameraRelay">Перезапустить видеопоток</button></div>
  <div class="gps-grid camera-status-grid">
    <div><span>RTSP</span><strong id="cameraTransferState">—</strong></div>
    <div><span>Регистрация</span><strong id="cameraRegistered">—</strong></div>
    <div><span>FFmpeg</span><strong id="cameraFfmpeg">—</strong></div>
    <div><span>RTP порт</span><strong id="cameraRtpPort">—</strong></div>
    <div><span>Управление</span><strong id="remoteControlState">—</strong></div>
    <div><span>Последняя команда</span><strong id="remoteControlAge">—</strong></div>
    <div><span>Гусеницы L/R</span><strong id="remoteDriveState">0 / 0</strong></div>
    <div><span>Щётка / подъём</span><strong id="remoteBrushState">0 / 0</strong></div>
    <div><span>PTZ</span><strong id="remotePtzState">0° / 0°</strong></div>
  </div>
  <p class="muted" id="cameraRelayError">Для прямой передачи камера должна отдавать H.264.</p>
</section>
'''

_CAMERA_SCRIPT = r'''
<style>
.camera-settings-row{align-items:end}.camera-settings-row label{min-width:190px}.camera-settings-row .camera-wide{flex:1 1 440px}.camera-settings-row input{width:100%;box-sizing:border-box}.camera-status-grid{margin-top:1rem}.camera-enable-box{display:flex;gap:1.5rem;align-items:center;flex-wrap:wrap;margin:1rem 0}.camera-master-switch{font-size:1.02rem}.camera-device-label{flex:1 1 430px}.camera-id-row{display:flex;gap:.55rem;align-items:center}.camera-id-row input{flex:1}.camera-id-row button{white-space:nowrap}.camera-onvif{margin:.8rem 0;padding:.75rem;border:1px solid rgba(148,163,184,.25);border-radius:12px}.camera-onvif summary{cursor:pointer}.camera-onvif-grid{margin-top:.8rem}.camera-actions{margin-top:1rem}code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
</style>
<script>
(function(){
 const byId=(id)=>document.getElementById(id); let busy=false;
 function msg(t,k){if(typeof toast==='function')toast(t,k)}
 async function req(url,opt={}){const r=await fetch(url,{headers:{'Content-Type':'application/json',...(opt.headers||{})},...opt});let d={};try{d=await r.json()}catch(_){ }if(!r.ok)throw new Error(d.detail||d.message||`HTTP ${r.status}`);return d}
 function val(id){return byId(id).value.trim()}
 async function refresh(){if(busy)return;busy=true;try{const [d,a]=await Promise.all([req('/api/camera/status'),req('/api/status')]);const c=d.camera||{},q=d.control||{},s=a.settings||{};
  if(document.activeElement!==byId('cameraEnabled'))byId('cameraEnabled').checked=!!s.camera_enabled;
  if(document.activeElement!==byId('remoteControlEnabled'))byId('remoteControlEnabled').checked=!!s.camera_remote_control_enabled;
  const fields=[['cameraDeviceId','camera_device_id'],['cameraRtspUrl','camera_rtsp_url'],['cameraServerUrl','camera_server_url'],['cameraControlPort','camera_control_port'],['cameraOnvifUrl','camera_onvif_url'],['cameraOnvifUsername','camera_onvif_username'],['cameraOnvifPassword','camera_onvif_password'],['cameraOnvifProfileToken','camera_onvif_profile_token']];
  fields.forEach(([id,key])=>{if(document.activeElement!==byId(id)&&s[key]!=null)byId(id).value=s[key]});
  byId('cameraTransferState').textContent=c.enabled?(c.ffmpeg_running?'ВКЛ / поток идёт':'ВКЛ / ожидание'):'ВЫКЛ'; byId('cameraTransferState').className=c.enabled&&c.ffmpeg_running?'value-ok':(c.enabled?'value-warn':'');
  byId('cameraRegistered').textContent=c.registered?'OK':'нет'; byId('cameraRegistered').className=c.registered?'value-ok':'value-warn';
  byId('cameraFfmpeg').textContent=c.ffmpeg_running?`PID ${c.ffmpeg_pid}`:'остановлен'; byId('cameraRtpPort').textContent=c.video_ingest_port??'—';
  byId('remoteControlState').textContent=q.enabled?`UDP :${q.listen_port}`:'выключено'; byId('remoteControlState').className=q.enabled?'value-ok':'';
  byId('remoteControlAge').textContent=q.packet_age_sec==null?'—':`${q.packet_age_sec} с`;
  byId('remoteDriveState').textContent=`${q.drive?.left??0} / ${q.drive?.right??0}`; byId('remoteBrushState').textContent=`${q.brush?.spin??0} / ${q.brush?.lift??0}`;
  byId('remotePtzState').textContent=`${Number(q.ptz?.pan_cdeg||0)/100}° / ${Number(q.ptz?.tilt_cdeg||0)/100}°`;
  byId('cameraRelaySummary').textContent=c.registered?(c.ffmpeg_running?'онлайн / видео':'онлайн / управление'):'не зарегистрирован';
  byId('cameraRelayError').textContent=q.last_error||c.last_error||'Серверные DRIVE/BRUSH команды идут через ROS в существующий ESP32 bridge; PTZ — через ONVIF.';
 }catch(e){byId('cameraRelayError').textContent=e.message}finally{busy=false}}
 async function toggle(url,box,onText,offText){const wanted=box.checked;box.disabled=true;try{await req(url,{method:'POST',body:JSON.stringify({enabled:wanted})});msg(wanted?onText:offText,'success')}catch(e){box.checked=!wanted;msg(e.message,'error')}finally{box.disabled=false;refresh()}}
 byId('cameraEnabled').addEventListener('change',()=>toggle('/api/camera/enabled',byId('cameraEnabled'),'RTSP включён','RTSP выключен'));
 byId('remoteControlEnabled').addEventListener('change',()=>toggle('/api/control/enabled',byId('remoteControlEnabled'),'Удалённое управление включено','Удалённое управление выключено'));
 byId('generateCameraDeviceId').addEventListener('click',async()=>{if(!confirm('Создать новый device_id? Его потребуется привязать на центральном сервере.'))return;try{const d=await req('/api/camera/generate-device-id',{method:'POST'});byId('cameraDeviceId').value=d.device_id;msg(`Новый ID: ${d.device_id}`,'success');refresh()}catch(e){msg(e.message,'error')}});
 byId('saveCameraSettings').addEventListener('click',async()=>{try{await req('/api/camera/settings',{method:'POST',body:JSON.stringify({enabled:byId('cameraEnabled').checked,remote_control_enabled:byId('remoteControlEnabled').checked,device_id:val('cameraDeviceId'),rtsp_url:val('cameraRtspUrl'),server_url:val('cameraServerUrl'),control_port:Number(val('cameraControlPort')||6000),onvif_url:val('cameraOnvifUrl'),onvif_username:val('cameraOnvifUsername'),onvif_password:byId('cameraOnvifPassword').value,onvif_profile_token:val('cameraOnvifProfileToken')||'Profile_1'})});msg('Настройки сохранены','success');refresh()}catch(e){msg(e.message,'error')}});
 byId('restartCameraRelay').addEventListener('click',async()=>{try{await req('/api/camera/restart',{method:'POST'});msg('Видеопоток перезапускается','success')}catch(e){msg(e.message,'error')}setTimeout(refresh,500)});
 refresh();setInterval(refresh,1000);
})();
</script>
'''


@app.get('/', include_in_schema=False)
def index_page_with_camera() -> HTMLResponse:
    response = base.index_page_with_settings()
    html = response.body.decode('utf-8')
    marker = '<section class="card logs-card">'
    html = html.replace(marker, _CAMERA_CARD + marker, 1)
    html = html.replace('</body>', _CAMERA_SCRIPT + '</body>', 1)
    return HTMLResponse(html)


def main(args: Optional[list[str]] = None) -> None:
    del args
    uvicorn.run(app, host=web_app.HOST, port=web_app.PORT, log_level='info')


if __name__ == '__main__':
    main()
