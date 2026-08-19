#!/usr/bin/env python3
"""Charging dock manager: save dock pose, monitor battery and navigate home."""
from __future__ import annotations
import json, math, os, time
from pathlib import Path
from typing import Optional
import rclpy
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionClient
from rclpy.duration import Duration
from rclpy.node import Node
from rclpy.time import Time
from std_msgs.msg import Bool, String
from std_srvs.srv import Trigger
from tf2_ros import Buffer, TransformException, TransformListener

class ChargingManagerNode(Node):
    def __init__(self):
        super().__init__('charging_manager_node')
        data_dir=Path(os.environ.get('ROBOTLIDAR_DATA_DIR','~/robotlidar_data')).expanduser()
        self.declare_parameter('dock_file',str(data_dir/'config'/'charging_dock.json'))
        self.declare_parameter('map_frame','map');self.declare_parameter('base_frame','base_link')
        self.declare_parameter('low_percent',25.0);self.declare_parameter('empty_voltage',20.0);self.declare_parameter('full_voltage',25.2)
        self.declare_parameter('low_samples',5);self.declare_parameter('rearm_percent',35.0)
        self.dock_file=Path(str(self.get_parameter('dock_file').value)).expanduser();self.map_frame=str(self.get_parameter('map_frame').value);self.base_frame=str(self.get_parameter('base_frame').value)
        self.low_percent=float(self.get_parameter('low_percent').value);self.empty_v=float(self.get_parameter('empty_voltage').value);self.full_v=float(self.get_parameter('full_voltage').value);self.low_samples=max(1,int(self.get_parameter('low_samples').value));self.rearm_percent=float(self.get_parameter('rearm_percent').value)
        self.tf_buffer=Buffer();self.tf_listener=TransformListener(self.tf_buffer,self);self.nav=ActionClient(self,NavigateToPose,'navigate_to_pose')
        self.status_pub=self.create_publisher(String,'/charging/status',10);self.low_pub=self.create_publisher(Bool,'/charging/low_battery',10)
        self.create_subscription(String,'/battery/status',self._battery,20);self.create_subscription(String,'/charging/config',self._config,10)
        self.create_service(Trigger,'/charging/set_dock_here',self._set_dock);self.create_service(Trigger,'/charging/go_home',self._go_home);self.create_service(Trigger,'/charging/cancel',self._cancel)
        self.battery={};self.low_count=0;self.low_latched=False;self.goal_handle=None;self.state='idle';self.dock=self._load_dock();self.create_timer(1.0,self._publish)
    def _load_dock(self):
        try:return json.loads(self.dock_file.read_text('utf-8')) if self.dock_file.exists() else None
        except Exception:return None
    @staticmethod
    def _yaw(q):return math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z))
    def _set_dock(self,_req,res):
        try:t=self.tf_buffer.lookup_transform(self.map_frame,self.base_frame,Time(),timeout=Duration(seconds=2.0))
        except TransformException as exc:res.success=False;res.message=f'Нет локализации map->base_link: {exc}';return res
        tr=t.transform.translation;q=t.transform.rotation;self.dock={'frame_id':self.map_frame,'x':float(tr.x),'y':float(tr.y),'yaw':self._yaw(q),'saved_at':time.time()}
        self.dock_file.parent.mkdir(parents=True,exist_ok=True);tmp=self.dock_file.with_suffix('.tmp');tmp.write_text(json.dumps(self.dock,ensure_ascii=False,indent=2),'utf-8');tmp.replace(self.dock_file)
        res.success=True;res.message=f"Точка зарядки сохранена: x={self.dock['x']:.2f}, y={self.dock['y']:.2f}";return res
    def _config(self,msg):
        try:d=json.loads(msg.data)
        except Exception:return
        for k,a in [('low_percent','low_percent'),('empty_voltage','empty_v'),('full_voltage','full_v'),('rearm_percent','rearm_percent')]:
            if k in d:setattr(self,a,float(d[k]))
        if 'low_samples' in d:self.low_samples=max(1,int(d['low_samples']))
    def _battery(self,msg):
        try:d=json.loads(msg.data)
        except Exception:return
        if not isinstance(d,dict):return
        v=float(d.get('voltage_v',0) or 0);online=bool(d.get('online',False));pct=None
        if online and self.full_v>self.empty_v:pct=max(0.0,min(100.0,(v-self.empty_v)*100.0/(self.full_v-self.empty_v)))
        d['percent']=round(pct,1) if pct is not None else None;self.battery=d
        if pct is None:self.low_count=0
        elif pct<=self.low_percent:self.low_count+=1
        else:self.low_count=0
        if not self.low_latched and self.low_count>=self.low_samples:self.low_latched=True;self.state='low_battery'
        elif self.low_latched and pct is not None and pct>=self.rearm_percent:self.low_latched=False;self.state='idle'
        b=Bool();b.data=self.low_latched;self.low_pub.publish(b)
    def _go_home(self,_req,res):
        if not self.dock:res.success=False;res.message='Точка зарядки не сохранена';return res
        if not self.nav.wait_for_server(timeout_sec=3.0):res.success=False;res.message='Nav2 navigate_to_pose недоступен';return res
        p=PoseStamped();p.header.frame_id=str(self.dock.get('frame_id','map'));p.header.stamp=self.get_clock().now().to_msg();p.pose.position.x=float(self.dock['x']);p.pose.position.y=float(self.dock['y']);yaw=float(self.dock.get('yaw',0));p.pose.orientation.z=math.sin(yaw/2);p.pose.orientation.w=math.cos(yaw/2)
        goal=NavigateToPose.Goal();goal.pose=p;f=self.nav.send_goal_async(goal);f.add_done_callback(self._goal_response);self.state='sending_to_dock';res.success=True;res.message='Команда возврата к зарядке отправлена';return res
    def _goal_response(self,f):
        try:g=f.result()
        except Exception:self.state='dock_error';return
        if not g.accepted:self.state='dock_rejected';return
        self.goal_handle=g;self.state='going_to_dock';r=g.get_result_async();r.add_done_callback(self._result)
    def _result(self,f):
        try:status=f.result().status;self.state='at_dock' if status==4 else f'dock_failed:{status}'
        except Exception:self.state='dock_error'
        self.goal_handle=None
    def _cancel(self,_req,res):
        if self.goal_handle:self.goal_handle.cancel_goal_async();self.goal_handle=None
        self.state='idle';res.success=True;res.message='Возврат к зарядке отменён';return res
    def _publish(self):
        m=String();m.data=json.dumps({'state':self.state,'low_battery':self.low_latched,'battery':self.battery,'dock':self.dock,'settings':{'low_percent':self.low_percent,'empty_voltage':self.empty_v,'full_voltage':self.full_v,'rearm_percent':self.rearm_percent,'low_samples':self.low_samples}},ensure_ascii=False);self.status_pub.publish(m)
def main(args:Optional[list[str]]=None):
    rclpy.init(args=args);n=ChargingManagerNode()
    try:rclpy.spin(n)
    except KeyboardInterrupt:pass
    finally:n.destroy_node();rclpy.shutdown()
if __name__=='__main__':main()
