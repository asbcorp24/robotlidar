#!/usr/bin/env python3
"""RobotLidar Raspberry web entry with IP-camera RTSP relay support."""
from __future__ import annotations

import uuid
from typing import Optional

import uvicorn
from fastapi import HTTPException
from fastapi.responses import HTMLResponse
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


class CameraEnabledRequest(BaseModel):
    enabled: bool


camera_relay = IpCameraRelayManager(log_callback=web_app.process_manager._append_log)
_initial_settings = web_app.settings.snapshot()
if not _initial_settings.get('camera_device_id'):
    _initial_settings = web_app.settings.update(
        camera_device_id=IpCameraRelayManager.default_device_id()
    )
camera_relay.start(_initial_settings)


def _new_device_id() -> str:
    return f'TRACTOR-RPI-{uuid.uuid4().hex[:12].upper()}'


@app.get('/api/camera/status')
def api_camera_status() -> dict:
    return {'ok': True, 'camera': camera_relay.status()}


@app.post('/api/camera/settings')
def api_camera_settings(request: CameraSettingsRequest) -> dict:
    device_id = request.device_id.strip().upper()
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


@app.post('/api/camera/generate-device-id')
def api_camera_generate_device_id() -> dict:
    settings = web_app.settings.update(camera_device_id=_new_device_id())
    camera_relay.configure(settings)
    return {
        'ok': True,
        'device_id': settings['camera_device_id'],
        'settings': settings,
        'camera': camera_relay.status(),
    }


@app.post('/api/camera/restart')
def api_camera_restart() -> dict:
    settings = web_app.settings.snapshot()
    camera_relay.configure({**settings, 'camera_enabled': False})
    camera_relay.configure(settings)
    return {'ok': True, 'camera': camera_relay.status()}


# web_entry_settings already registered /. Replace only that page so all its
# existing ESP32/map/charging behavior stays intact while a camera card is
# added to the same Raspberry Pi settings panel.
app.routes[:] = [route for route in app.routes if getattr(route, 'path', None) != '/']

_CAMERA_CARD = r'''
<section class="card settings-card" id="cameraSettingsCard">
  <div class="card-head compact">
    <div><div class="eyebrow">IP-камера</div><h2>RTSP → центральный сервер</h2></div>
    <strong id="cameraRelaySummary">выключено</strong>
  </div>
  <div class="notice">Raspberry Pi забирает готовый H.264 по RTSP и отправляет его на центральный RobotLiDAR server без перекодирования через FFmpeg <code>-c:v copy</code>.</div>
  <div class="camera-enable-box">
    <label class="switch-line camera-master-switch">
      <input type="checkbox" id="cameraEnabled"> <strong>Передавать RTSP на сервер</strong>
    </label>
    <span class="muted">Переключатель применяется сразу. При выключении FFmpeg останавливается.</span>
  </div>
  <div class="settings-row camera-settings-row">
    <label class="camera-device-label">ID трактора
      <div class="camera-id-row">
        <input id="cameraDeviceId" maxlength="64" placeholder="TRACTOR-RPI-001">
        <button type="button" class="ghost" id="generateCameraDeviceId">Сгенерировать ID</button>
      </div>
    </label>
    <label class="camera-wide">RTSP URL камеры
      <input id="cameraRtspUrl" autocomplete="off" placeholder="rtsp://user:password@192.168.1.60:554/stream">
    </label>
    <label class="camera-wide">Адрес центрального сервера
      <input id="cameraServerUrl" autocomplete="off" placeholder="http://192.168.1.100:8000">
    </label>
    <button class="primary" id="saveCameraSettings">Сохранить настройки камеры</button>
    <button class="ghost" id="restartCameraRelay">Перезапустить поток</button>
  </div>
  <div class="gps-grid camera-status-grid">
    <div><span>Передача</span><strong id="cameraTransferState">—</strong></div>
    <div><span>Регистрация</span><strong id="cameraRegistered">—</strong></div>
    <div><span>FFmpeg</span><strong id="cameraFfmpeg">—</strong></div>
    <div><span>RTP порт сервера</span><strong id="cameraRtpPort">—</strong></div>
    <div><span>Перезапуски</span><strong id="cameraRestartCount">0</strong></div>
  </div>
  <p class="muted" id="cameraRelayError">Для прямой передачи камера должна отдавать H.264.</p>
</section>
'''

_CAMERA_SCRIPT = r'''
<style>
.camera-settings-row{align-items:end}.camera-settings-row label{min-width:190px}.camera-settings-row .camera-wide{flex:1 1 440px}.camera-settings-row input{width:100%;box-sizing:border-box}.camera-status-grid{margin-top:1rem}.camera-enable-box{display:flex;gap:1rem;align-items:center;flex-wrap:wrap;margin:1rem 0}.camera-master-switch{font-size:1.05rem}.camera-device-label{flex:1 1 430px}.camera-id-row{display:flex;gap:.55rem;align-items:center}.camera-id-row input{flex:1}.camera-id-row button{white-space:nowrap}code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace}
</style>
<script>
(function(){
  const byId=(id)=>document.getElementById(id);
  let cameraRefreshBusy=false;
  let cameraToggleBusy=false;
  function cameraToast(message,type){ if(typeof toast==='function') toast(message,type); }
  async function cameraRequest(url,options={}){
    const response=await fetch(url,{headers:{'Content-Type':'application/json',...(options.headers||{})},...options});
    let data={}; try{data=await response.json();}catch(_){ }
    if(!response.ok) throw new Error(data.detail||data.message||`HTTP ${response.status}`);
    return data;
  }
  async function refreshCameraSettings(){
    if(cameraRefreshBusy)return;
    cameraRefreshBusy=true;
    try{
      const [statusData,allData]=await Promise.all([cameraRequest('/api/camera/status'),cameraRequest('/api/status')]);
      const c=statusData.camera||{},s=allData.settings||{};
      if(!cameraToggleBusy)byId('cameraEnabled').checked=!!s.camera_enabled;
      if(document.activeElement!==byId('cameraDeviceId'))byId('cameraDeviceId').value=s.camera_device_id||c.device_id||'';
      if(document.activeElement!==byId('cameraRtspUrl'))byId('cameraRtspUrl').value=s.camera_rtsp_url||'';
      if(document.activeElement!==byId('cameraServerUrl'))byId('cameraServerUrl').value=s.camera_server_url||'';
      byId('cameraTransferState').textContent=c.enabled?(c.ffmpeg_running?'ВКЛ / поток идёт':'ВКЛ / ожидание'):'ВЫКЛ';
      byId('cameraTransferState').className=c.enabled&&c.ffmpeg_running?'value-ok':(c.enabled?'value-warn':'');
      byId('cameraRegistered').textContent=c.registered?'OK':'нет';
      byId('cameraRegistered').className=c.registered?'value-ok':'value-warn';
      byId('cameraFfmpeg').textContent=c.ffmpeg_running?`работает PID ${c.ffmpeg_pid}`:'остановлен';
      byId('cameraFfmpeg').className=c.ffmpeg_running?'value-ok':'value-warn';
      byId('cameraRtpPort').textContent=c.video_ingest_port??'—';
      byId('cameraRestartCount').textContent=c.restart_count??0;
      byId('cameraRelaySummary').textContent=!c.enabled?'передача выключена':(c.ffmpeg_running?'поток активен':'ожидание потока');
      byId('cameraRelayError').textContent=c.last_error||'H.264 передаётся без decode/encode; нагрузка Raspberry Pi минимальна.';
    }catch(e){ byId('cameraRelayError').textContent='Камера: '+e.message; }
    finally{cameraRefreshBusy=false;}
  }
  byId('cameraEnabled').addEventListener('change',async()=>{
    const checkbox=byId('cameraEnabled');
    const wanted=checkbox.checked;
    cameraToggleBusy=true;
    checkbox.disabled=true;
    try{
      await cameraRequest('/api/camera/enabled',{method:'POST',body:JSON.stringify({enabled:wanted})});
      cameraToast(wanted?'Передача RTSP включена':'Передача RTSP выключена','success');
    }catch(e){
      checkbox.checked=!wanted;
      cameraToast(e.message,'error');
    }finally{
      checkbox.disabled=false;
      cameraToggleBusy=false;
      await refreshCameraSettings();
    }
  });
  byId('generateCameraDeviceId').addEventListener('click',async()=>{
    if(!confirm('Сгенерировать новый device_id? Если старый ID уже привязан на центральном сервере, новый ID нужно будет добавить в аккаунт.'))return;
    try{
      const data=await cameraRequest('/api/camera/generate-device-id',{method:'POST'});
      byId('cameraDeviceId').value=data.device_id||'';
      cameraToast(`Новый device_id: ${data.device_id}`,'success');
      await refreshCameraSettings();
    }catch(e){cameraToast(e.message,'error');}
  });
  byId('saveCameraSettings').addEventListener('click',async()=>{
    try{
      await cameraRequest('/api/camera/settings',{method:'POST',body:JSON.stringify({
        enabled:byId('cameraEnabled').checked,
        device_id:byId('cameraDeviceId').value.trim(),
        rtsp_url:byId('cameraRtspUrl').value.trim(),
        server_url:byId('cameraServerUrl').value.trim()
      })});
      cameraToast('Настройки камеры сохранены','success');
      await refreshCameraSettings();
    }catch(e){cameraToast(e.message,'error');}
  });
  byId('restartCameraRelay').addEventListener('click',async()=>{
    try{await cameraRequest('/api/camera/restart',{method:'POST'});cameraToast('Видеопоток перезапускается','success');setTimeout(refreshCameraSettings,500);}
    catch(e){cameraToast(e.message,'error');}
  });
  refreshCameraSettings();
  setInterval(refreshCameraSettings,2000);
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
    uvicorn.run(
        app,
        host=web_app.HOST,
        port=web_app.PORT,
        log_level='info',
    )


if __name__ == '__main__':
    main()
