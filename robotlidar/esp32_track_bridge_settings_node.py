#!/usr/bin/env python3
"""ESP32 track bridge with persistent runtime configuration transport."""
from __future__ import annotations
import json,time
from collections import deque
from typing import Optional
import rclpy
from std_msgs.msg import String
from robotlidar.esp32_track_bridge_node import Esp32TrackBridgeNode
CFG_MAGIC=0xC6000000;CFG_GET=CFG_MAGIC;CFG_SET=CFG_MAGIC|0x00400000;CFG_RESET=CFG_MAGIC|0x00800000
CFG_KEYS={'us_enabled':1,'us_warn_mm':2,'us_stop_mm':3,'us_emergency_mm':4,'us_clear_mm':5,'us_danger_samples':6,'us_clear_samples':7,'us_sample_ms':8,'hall_enabled':16,'hall_left_inverted':17,'hall_right_inverted':18,'hall_ppr':19,'wheel_circ_mm':20,'track_width_mm':21,'rc_deadband_us':22,'rc1_min_us':23,'rc1_center_us':24,'rc1_max_us':25,'rc2_min_us':26,'rc2_center_us':27,'rc2_max_us':28,'rc3_min_us':29,'rc3_center_us':30,'rc3_max_us':31,'rc4_min_us':32,'rc4_center_us':33,'rc4_max_us':34,'rc5_min_us':35,'rc5_center_us':36,'rc5_max_us':37,'rc6_min_us':38,'rc6_center_us':39,'rc6_max_us':40,'throttle_idle_mv':41,'throttle_max_mv':42,'reverse_brake_ms':43,'reverse_settle_ms':44,'ramp_step':45,'actuator_timeout_ms':46,'actuator_guard_ms':47,'actuator_reversed':48,'brush_idle_mv':49,'brush_max_mv':50,'brush_stop_us':51,'brush_brake_active_high':52,'aux_idle_mv':53,'aux_max_mv':54,'aux_reverse_guard_ms':55,'aux_ramp_step':56,'aux_reverse_active_high':57,'rc_timeout_ms':58,'ros_aux_timeout_ms':59,'track_reverse_active_high':60}
BOOL_KEYS={'us_enabled','hall_enabled','hall_left_inverted','hall_right_inverted','actuator_reversed','brush_brake_active_high','aux_reverse_active_high','track_reverse_active_high'}
class Esp32TrackBridgeSettingsNode(Esp32TrackBridgeNode):
 def __init__(self):
  self._config_state={};self._config_publisher=None;self._config_queue=deque();self._config_pause_until=0.0;super().__init__();self._config_publisher=self.create_publisher(String,'/esp32/config/state',10);self.create_subscription(String,'/esp32/config/request',self._config_request_callback,10);self.create_timer(0.12,self._config_tick);self._queue_config_sequence(CFG_GET);self.get_logger().info('ESP32 persistent settings transport V2 enabled')
 def _process_line(self,line):
  if line and '*' in line:
   body,cs=line.rsplit('*',1)
   try: valid=len(cs)==2 and int(cs,16)==self._checksum(body)
   except ValueError: valid=False
   if valid and body.startswith('CFG,'): self._handle_config_frame(body.split(','));return
  super()._process_line(line)
 def _handle_config_frame(self,fields):
  if len(fields)<4:return
  state={'connected':True,'millis':int(fields[1]),'version':int(fields[2])}
  for item in fields[3:]:
   if '=' not in item:continue
   key,raw=item.split('=',1)
   try:value=int(raw)
   except ValueError:value=raw
   if key in BOOL_KEYS:value=bool(int(value))
   state[key]=value
  self._config_state=state
  if self._config_publisher is not None:
   m=String();m.data=json.dumps(state,ensure_ascii=False);self._config_publisher.publish(m)
 @staticmethod
 def _normalize_config_value(key,value): return 1 if key in BOOL_KEYS and bool(value) else (0 if key in BOOL_KEYS else max(0,min(65535,int(value))))
 def _queue_config_sequence(self,sequence): self._config_queue.append(int(sequence)&0xFFFFFFFF)
 def _config_tick(self):
  if not self._config_queue:return
  sequence=self._config_queue.popleft();self._config_pause_until=time.monotonic()+0.05
  if not self._write_body(f'PING,{sequence}'):self.get_logger().warning('ESP32 config packet was not sent')
 def _send_tick(self):
  if time.monotonic()<self._config_pause_until:return
  super()._send_tick()
 def _encoded_set_sequence(self,key,value):
  kid=CFG_KEYS.get(key)
  if kid is None:raise ValueError(f'unknown ESP32 setting: {key}')
  v=self._normalize_config_value(key,value);return CFG_SET|((kid&0x3F)<<16)|(v&0xFFFF)
 def _config_request_callback(self,message):
  try:r=json.loads(message.data)
  except Exception as exc:self.get_logger().warning(f'Bad /esp32/config/request JSON: {exc}');return
  if not isinstance(r,dict):return
  op=str(r.get('op','get')).lower()
  try:
   if op=='get':self._queue_config_sequence(CFG_GET)
   elif op=='reset':self._queue_config_sequence(CFG_RESET);self._queue_config_sequence(CFG_GET)
   elif op=='set':
    values=r.get('values') or {}
    if not isinstance(values,dict):raise ValueError('values must be an object')
    for key,value in values.items():self._queue_config_sequence(self._encoded_set_sequence(str(key),value))
    self._queue_config_sequence(CFG_GET)
   else:raise ValueError(f'unknown op: {op}')
  except Exception as exc:self.get_logger().error(f'ESP32 config request failed: {exc}')
def main(args:Optional[list[str]]=None):
 rclpy.init(args=args);node=Esp32TrackBridgeSettingsNode()
 try:rclpy.spin(node)
 except KeyboardInterrupt:pass
 finally:node.destroy_node();rclpy.shutdown()
if __name__=='__main__':main()
