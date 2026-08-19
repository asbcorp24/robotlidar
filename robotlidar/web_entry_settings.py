#!/usr/bin/env python3
"""RobotLidar web entry extended with an ESP32 persistent settings page."""
from __future__ import annotations
import json,threading,time
from pathlib import Path
from typing import Optional
import uvicorn
from fastapi import HTTPException
from fastapi.responses import FileResponse,HTMLResponse
from pydantic import BaseModel
from std_msgs.msg import String
from robotlidar import web_entry
app=web_entry.web_app.app;bridge=web_entry.web_app.bridge;STATIC_DIR:Path=web_entry.STATIC_DIR
_config_lock=threading.RLock();_config_state={'connected':False,'received_at':None};_config_publisher=bridge.create_publisher(String,'/esp32/config/request',10)
class Esp32ConfigRequest(BaseModel): values:dict
def _config_state_callback(message:String):
 try:data=json.loads(message.data)
 except Exception:return
 if not isinstance(data,dict):return
 data['received_at']=time.time()
 with _config_lock:_config_state.clear();_config_state.update(data)
_config_subscription=bridge.create_subscription(String,'/esp32/config/state',_config_state_callback,10)
def _publish_config_request(payload):m=String();m.data=json.dumps(payload,ensure_ascii=False);_config_publisher.publish(m)
app.routes[:]=[r for r in app.routes if getattr(r,'path',None)!='/']
@app.get('/',include_in_schema=False)
def index_page_with_settings():
 html=(STATIC_DIR/'index.html').read_text(encoding='utf-8');html=html.replace('</head>','''<style>.topbar-live-actions{display:flex;align-items:center;gap:.65rem;flex-wrap:wrap;justify-content:flex-end}.radar-page-link{display:inline-flex;align-items:center;padding:.6rem .85rem;border-radius:999px;border:1px solid rgba(148,163,184,.32);color:inherit;text-decoration:none;font-weight:700}.radar-page-link:hover{border-color:#38bdf8}</style><script src="/static/ws-client.js"></script></head>''');c='<div class="connection" id="connectionBadge">Подключение…</div>';html=html.replace(c,'<div class="topbar-live-actions"><a class="radar-page-link" href="/radar">Радар, IMU и GPS</a><a class="radar-page-link" href="/esp32-settings">Настройки ESP32</a>'+c+'</div>');return HTMLResponse(html)
@app.get('/esp32-settings',include_in_schema=False)
@app.get('/esp32-settings/',include_in_schema=False)
def esp32_settings_page():return FileResponse(STATIC_DIR/'esp32-settings.html')
@app.get('/esp32-settings.css',include_in_schema=False)
def esp32_settings_css():return FileResponse(STATIC_DIR/'esp32-settings.css',media_type='text/css')
@app.get('/esp32-settings.js',include_in_schema=False)
def esp32_settings_js():return FileResponse(STATIC_DIR/'esp32-settings.js',media_type='application/javascript')
@app.get('/api/esp32/config')
def api_esp32_config():
 with _config_lock:state=dict(_config_state)
 t=state.get('received_at');state['online']=bool(t and time.time()-float(t)<8.0);_publish_config_request({'op':'get'});return {'ok':True,'config':state}
@app.post('/api/esp32/config')
def api_set_esp32_config(request:Esp32ConfigRequest):
 allowed={'us_enabled','us_warn_mm','us_stop_mm','us_emergency_mm','us_clear_mm','us_danger_samples','us_clear_samples','us_sample_ms','hall_enabled','hall_left_inverted','hall_right_inverted','hall_ppr','wheel_circ_mm','track_width_mm','rc_deadband_us','rc_timeout_ms','throttle_idle_mv','throttle_max_mv','reverse_brake_ms','reverse_settle_ms','ramp_step','track_reverse_active_high','actuator_timeout_ms','actuator_guard_ms','actuator_reversed','brush_idle_mv','brush_max_mv','brush_stop_us','brush_brake_active_high','aux_idle_mv','aux_max_mv','aux_reverse_guard_ms','aux_ramp_step','aux_reverse_active_high','ros_aux_timeout_ms'}
 for ch in range(1,7):allowed.update({f'rc{ch}_min_us',f'rc{ch}_center_us',f'rc{ch}_max_us'})
 unknown=sorted(set(request.values)-allowed)
 if unknown:raise HTTPException(status_code=400,detail='Неизвестные параметры: '+', '.join(unknown))
 _publish_config_request({'op':'set','values':request.values});return {'ok':True,'message':'Настройки отправлены на ESP32 и сохраняются в NVS'}
@app.post('/api/esp32/config/reset')
def api_reset_esp32_config():_publish_config_request({'op':'reset'});return {'ok':True,'message':'Команда восстановления заводских настроек отправлена на ESP32'}
def main(args:Optional[list[str]]=None):
 del args;uvicorn.run(app,host=web_entry.web_app.HOST,port=web_entry.web_app.PORT,log_level='info')
if __name__=='__main__':main()
