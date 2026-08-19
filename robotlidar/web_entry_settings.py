#!/usr/bin/env python3
"""RobotLidar web entry with ESP32 settings, map autorun and charging control."""
from __future__ import annotations
import json,threading,time
from pathlib import Path
from typing import Optional
import uvicorn
from fastapi import HTTPException
from fastapi.responses import FileResponse,HTMLResponse
from pydantic import BaseModel
from std_msgs.msg import String
from std_srvs.srv import SetBool
from robotlidar import web_entry

web_app=web_entry.web_app;app=web_app.app;bridge=web_app.bridge;STATIC_DIR:Path=web_entry.STATIC_DIR

def _start_mapping_with_external_esp32():
 web_app.process_manager._replace_process(['ros2','launch','robotlidar','mapping.launch.py',f'serial_port:={web_app.SERIAL_PORT}','use_esp32_drive:=true','external_esp32_drive:=true','start_gps:=false'],'mapping',None)
def _start_navigation_with_external_esp32(map_path:Path):
 web_app.process_manager._replace_process(['ros2','launch','robotlidar','navigation.launch.py',f'map:={map_path}',f'serial_port:={web_app.SERIAL_PORT}','use_esp32_drive:=true','external_esp32_drive:=true','start_gps:=false'],'navigation',map_path.stem)
web_app.process_manager.start_mapping=_start_mapping_with_external_esp32;web_app.process_manager.start_navigation=_start_navigation_with_external_esp32

_config_lock=threading.RLock();_config_state={'connected':False,'received_at':None};_config_publisher=bridge.create_publisher(String,'/esp32/config/request',10)
_mode_lock=threading.RLock();_esp32_mode:Optional[str]=None;_mode_generation=0;_arm_client=bridge.create_client(SetBool,'/drive/arm')
_charging_lock=threading.RLock();_charging_state={};_charging_return_started=False;_charging_config_pub=bridge.create_publisher(String,'/charging/config',10)

class Esp32ConfigRequest(BaseModel):values:dict
class ChargingConfigRequest(BaseModel):low_percent:float=25.0;empty_voltage:float=20.0;full_voltage:float=25.2;rearm_percent:float=35.0;low_samples:int=5

def _config_state_callback(message:String):
 try:data=json.loads(message.data)
 except Exception:return
 if not isinstance(data,dict):return
 data['received_at']=time.time()
 with _config_lock:_config_state.clear();_config_state.update(data)
bridge.create_subscription(String,'/esp32/config/state',_config_state_callback,10)
def _publish_config_request(payload):m=String();m.data=json.dumps(payload,ensure_ascii=False);_config_publisher.publish(m)
def _current_mode_is_ros(generation):
 with _mode_lock:return _mode_generation==generation and _esp32_mode=='ROS'
def _set_esp32_arm(enabled,timeout_sec=8.0):
 if not _arm_client.wait_for_service(timeout_sec=timeout_sec):return False,'/drive/arm service unavailable'
 req=SetBool.Request();req.data=enabled;f=_arm_client.call_async(req);deadline=time.monotonic()+timeout_sec
 while not f.done() and time.monotonic()<deadline:time.sleep(.03)
 if not f.done():return False,'/drive/arm timeout'
 r=f.result();return (bool(r.success),str(r.message)) if r else (False,'/drive/arm failed')
def _wait_for_route_service(generation,timeout_sec=35.0):
 deadline=time.monotonic()+timeout_sec
 while time.monotonic()<deadline and _current_mode_is_ros(generation):
  try:
   if bridge.call_trigger('/route/reload',timeout_sec=2.0).get('success'):return True
  except Exception:pass
  time.sleep(.5)
 return False

def _start_saved_route_for_ros_mode(generation):
 try:
  with _charging_lock:
   if bool(_charging_state.get('low_battery')):return
  current=web_app.settings.snapshot();map_name=current.get('default_map')
  if not map_name:return web_app.process_manager._append_log('ESP32 MAP: default map is not selected')
  map_path=web_app._map_yaml_path(str(map_name))
  if not map_path.exists() or not web_app.ROUTE_FILE.exists() or not _current_mode_is_ros(generation):return
  runtime=web_app.process_manager.status()
  if runtime.get('mode')!='navigation' or not runtime.get('process_running') or runtime.get('selected_map')!=map_path.stem:web_app.process_manager.start_navigation(map_path)
  if not _wait_for_route_service(generation):return
  armed,msg=_set_esp32_arm(True);web_app.process_manager._append_log(f'ESP32 MAP: ARM={armed}: {msg}')
  if not armed:return
  deadline=time.monotonic()+20
  while time.monotonic()<deadline and _current_mode_is_ros(generation):
   try:r=bridge.call_trigger('/route/play',timeout_sec=5.0)
   except Exception:time.sleep(.7);continue
   if r.get('success') or 'already active' in str(r.get('message','')).lower():return
   time.sleep(.7)
 except Exception as exc:web_app.process_manager._append_log(f'ESP32 MAP: autorun failed: {exc}')

def _stop_saved_route_after_ros_mode():
 try:bridge.call_trigger('/route/cancel',timeout_sec=2.0)
 except Exception:pass
 try:_set_esp32_arm(False,2.0)
 except Exception:pass
 web_app.process_manager._append_log('ESP32 MAP: mode left; route canceled and drive disarmed')

def _return_to_charge():
 global _charging_return_started
 try:
  with _mode_lock:mode=_esp32_mode
  if mode!='ROS':
   web_app.process_manager._append_log('CHARGE: low battery; waiting for ESP32 ROS/map mode')
   return
  current=web_app.settings.snapshot();map_name=current.get('default_map')
  if not map_name:return web_app.process_manager._append_log('CHARGE: default map is not selected')
  map_path=web_app._map_yaml_path(str(map_name));runtime=web_app.process_manager.status()
  if runtime.get('mode')!='navigation' or not runtime.get('process_running') or runtime.get('selected_map')!=map_path.stem:web_app.process_manager.start_navigation(map_path);time.sleep(5)
  try:bridge.call_trigger('/route/cancel',timeout_sec=2.0)
  except Exception:pass
  armed,msg=_set_esp32_arm(True);web_app.process_manager._append_log(f'CHARGE: ARM={armed}: {msg}')
  if not armed:return
  for _ in range(20):
   try:
    r=bridge.call_trigger('/charging/go_home',timeout_sec=3.0)
    if r.get('success'):web_app.process_manager._append_log('CHARGE: returning to charging point');return
   except Exception:pass
   time.sleep(.5)
  web_app.process_manager._append_log('CHARGE: could not start return to dock')
 finally:
  with _charging_lock:_charging_return_started=False

def _charging_status_callback(message:String):
 global _charging_state,_charging_return_started
 try:d=json.loads(message.data)
 except Exception:return
 if not isinstance(d,dict):return
 start=False
 with _charging_lock:
  _charging_state=d
  if d.get('low_battery') and d.get('state') not in {'going_to_dock','sending_to_dock','at_dock'} and not _charging_return_started:_charging_return_started=True;start=True
 if start:threading.Thread(target=_return_to_charge,name='low-battery-return',daemon=True).start()
bridge.create_subscription(String,'/charging/status',_charging_status_callback,10)

def _esp32_status_callback(message:String):
 global _esp32_mode,_mode_generation
 try:
  payload=json.loads(message.data);mode=str(((payload.get('telemetry') if isinstance(payload,dict) else {}) or {}).get('control_mode') or '').upper()
 except Exception:return
 if mode not in {'ROS','RC','SAFE'}:return
 with _mode_lock:
  previous=_esp32_mode
  if mode==previous:return
  _esp32_mode=mode;_mode_generation+=1;generation=_mode_generation
 web_app.process_manager._append_log(f'ESP32 MODE: {previous or "UNKNOWN"} -> {mode}')
 if mode=='ROS':threading.Thread(target=_start_saved_route_for_ros_mode,args=(generation,),daemon=True).start()
 elif previous=='ROS':threading.Thread(target=_stop_saved_route_after_ros_mode,daemon=True).start()
bridge.create_subscription(String,'/drive/esp32_status',_esp32_status_callback,10)

app.routes[:]=[r for r in app.routes if getattr(r,'path',None)!='/']
@app.get('/',include_in_schema=False)
def index_page_with_settings():
 html=(STATIC_DIR/'index.html').read_text('utf-8');html=html.replace('</head>','<style>.topbar-live-actions{display:flex;align-items:center;gap:.65rem;flex-wrap:wrap;justify-content:flex-end}.radar-page-link{display:inline-flex;align-items:center;padding:.6rem .85rem;border-radius:999px;border:1px solid rgba(148,163,184,.32);color:inherit;text-decoration:none;font-weight:700}</style><script src="/static/ws-client.js"></script></head>');marker='<div class="connection" id="connectionBadge">Подключение…</div>';html=html.replace(marker,'<div class="topbar-live-actions"><a class="radar-page-link" href="/radar">Радар, IMU и GPS</a><a class="radar-page-link" href="/esp32-settings">Настройки ESP32</a>'+marker+'</div>');return HTMLResponse(html)
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
 t=state.get('received_at');state['online']=bool(t and time.time()-float(t)<8);_publish_config_request({'op':'get'});return {'ok':True,'config':state}
@app.post('/api/esp32/config')
def api_set_esp32_config(request:Esp32ConfigRequest):
 allowed={'us_enabled','us_warn_mm','us_stop_mm','us_emergency_mm','us_clear_mm','us_danger_samples','us_clear_samples','us_sample_ms','hall_enabled','hall_left_inverted','hall_right_inverted','hall_ppr','wheel_circ_mm','track_width_mm','rc_deadband_us','rc_timeout_ms','throttle_idle_mv','throttle_max_mv','reverse_brake_ms','reverse_settle_ms','ramp_step','track_reverse_active_high','actuator_timeout_ms','actuator_guard_ms','actuator_reversed','brush_idle_mv','brush_max_mv','brush_stop_us','brush_brake_active_high','aux_idle_mv','aux_max_mv','aux_reverse_guard_ms','aux_ramp_step','aux_reverse_active_high','ros_aux_timeout_ms'}
 for ch in range(1,7):allowed.update({f'rc{ch}_min_us',f'rc{ch}_center_us',f'rc{ch}_max_us'})
 unknown=sorted(set(request.values)-allowed)
 if unknown:raise HTTPException(400,'Неизвестные параметры: '+', '.join(unknown))
 _publish_config_request({'op':'set','values':request.values});return {'ok':True}
@app.post('/api/esp32/config/reset')
def api_reset_esp32_config():_publish_config_request({'op':'reset'});return {'ok':True}
@app.get('/api/charging/status')
def charging_status():
 with _charging_lock:return {'ok':True,'charging':dict(_charging_state),'esp32_mode':_esp32_mode}
@app.post('/api/charging/config')
def charging_config(req:ChargingConfigRequest):
 if req.full_voltage<=req.empty_voltage:raise HTTPException(400,'Полное напряжение должно быть выше пустого')
 m=String();m.data=json.dumps(req.model_dump(),ensure_ascii=False);_charging_config_pub.publish(m);return {'ok':True,'settings':req.model_dump()}
@app.post('/api/charging/set-dock')
def charging_set_dock():
 if web_app.process_manager.status().get('mode')!='mapping':raise HTTPException(409,'Точку зарядки можно отметить только при картографировании')
 try:r=bridge.call_trigger('/charging/set_dock_here',timeout_sec=3.0)
 except Exception as exc:raise HTTPException(503,str(exc))
 if not r.get('success'):raise HTTPException(409,str(r.get('message')))
 return r
@app.post('/api/charging/go-home')
def charging_go_home():
 threading.Thread(target=_return_to_charge,daemon=True).start();return {'success':True,'message':'Возврат к зарядке запрошен'}

def main(args:Optional[list[str]]=None):del args;uvicorn.run(app,host=web_app.HOST,port=web_app.PORT,log_level='info')
if __name__=='__main__':main()
