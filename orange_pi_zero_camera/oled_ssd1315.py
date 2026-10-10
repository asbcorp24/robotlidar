#!/usr/bin/env python3
from __future__ import print_function

import fcntl
import glob
import json
import os
import socket
import subprocess
import time
try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse

CONFIG_PATH = os.environ.get("ORANGE_PI_CAMERA_CONFIG", "/etc/robotlidar/orange-pi-zero-camera.json")
I2C_SLAVE = 0x0703
WIDTH = 128
PAGES = 8

FONT = {
" ":(0,0,0,0,0),"-":(8,8,8,8,8),".":(0,96,96,0,0),"/":(32,16,8,4,2),":":(0,54,54,0,0),"?":(2,1,81,9,6),"_":(64,64,64,64,64),
"0":(62,81,73,69,62),"1":(0,66,127,64,0),"2":(66,97,81,73,70),"3":(33,65,69,75,49),"4":(24,20,18,127,16),"5":(39,69,69,69,57),"6":(60,74,73,73,48),"7":(1,113,9,5,3),"8":(54,73,73,73,54),"9":(6,73,73,41,30),
"A":(126,17,17,17,126),"B":(127,73,73,73,54),"C":(62,65,65,65,34),"D":(127,65,65,34,28),"E":(127,73,73,73,65),"F":(127,9,9,9,1),"G":(62,65,73,73,122),"H":(127,8,8,8,127),"I":(0,65,127,65,0),"J":(32,64,65,63,1),"K":(127,8,20,34,65),"L":(127,64,64,64,64),"M":(127,2,12,2,127),"N":(127,4,8,16,127),"O":(62,65,65,65,62),"P":(127,9,9,9,6),"Q":(62,65,81,33,94),"R":(127,9,25,41,70),"S":(70,73,73,73,49),"T":(1,1,127,1,1),"U":(63,64,64,64,63),"V":(31,32,64,32,31),"W":(63,64,56,64,63),"X":(99,20,8,20,99),"Y":(7,8,112,8,7),"Z":(97,81,73,69,67)
}

def log(msg):
    print("[SSD1315] " + str(msg), flush=True)

def load_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def sh(args, timeout=2):
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True, timeout=timeout)
        return p.returncode, p.stdout.strip()
    except Exception as e:
        return 1, str(e)

def service(name):
    c, o = sh(["systemctl", "is-active", name])
    return o.upper() if o else "DOWN"

def iface_ipv4(iface):
    c, o = sh(["ip", "-4", "-o", "addr", "show", "dev", iface, "scope", "global"])
    if c == 0 and o:
        p=o.split()
        if len(p)>=4:
            return p[3].split("/")[0]
    return ""

def network_state():
    eth_ip=""
    wifi_ip=""
    wifi_ssid=""
    wifi_conn=""
    ap=False

    c,o=sh(["ip","-4","-o","addr","show","scope","global"])
    if c==0:
        for line in o.splitlines():
            p=line.split()
            if len(p)<4:
                continue
            iface=p[1]
            ip=p[3].split("/")[0]
            if iface.startswith(("eth","en")) and not eth_ip:
                eth_ip=ip
            elif iface.startswith(("wl","wlan")) and not wifi_ip:
                wifi_ip=ip

    c,o=sh(["nmcli","-g","GENERAL.CONNECTION","device","show","wlan0"])
    if c==0:
        wifi_conn=o.strip()
        if wifi_conn in ("--","(null)"):
            wifi_conn=""
        if wifi_conn=="RobotLiDAR-Setup":
            ap=True
        elif wifi_conn:
            wifi_ssid=wifi_conn

    if ap:
        return {
            "mode":"AP",
            "eth_ip":eth_ip,
            "wifi_ip":wifi_ip or "10.42.0.1",
            "wifi_ssid":"RobotLiDAR-Setup",
            "web_ip":wifi_ip or "10.42.0.1",
        }
    if eth_ip:
        return {
            "mode":"ETH",
            "eth_ip":eth_ip,
            "wifi_ip":wifi_ip,
            "wifi_ssid":wifi_ssid,
            "web_ip":eth_ip,
        }
    if wifi_ip:
        return {
            "mode":"WIFI",
            "eth_ip":"",
            "wifi_ip":wifi_ip,
            "wifi_ssid":wifi_ssid,
            "web_ip":wifi_ip,
        }
    try:
        fallback=socket.gethostbyname(socket.gethostname())
    except Exception:
        fallback="0.0.0.0"
    return {
        "mode":"OFFLINE",
        "eth_ip":"",
        "wifi_ip":"",
        "wifi_ssid":"",
        "web_ip":fallback,
    }

def host(url):
    try:
        return urlparse(str(url)).hostname or "?"
    except Exception:
        return "?"

def temp():
    for p in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            v=float(open(p).read().strip())
            if v>1000: v/=1000.0
            if -20<v<150: return "{:.0f}C".format(v)
        except Exception: pass
    return "N/A"

def memory():
    try:
        d={}
        for line in open("/proc/meminfo"):
            k,v=line.split(":",1); d[k]=int(v.strip().split()[0])
        total=d.get("MemTotal",0)//1024; avail=d.get("MemAvailable",d.get("MemFree",0))//1024
        return "{}/{}M".format(max(total-avail,0),total)
    except Exception: return "?"

def uptime():
    try:
        s=int(float(open("/proc/uptime").read().split()[0])); h=(s//3600)%24; m=(s//60)%60; d=s//86400
        return ("{}D {:02d}:{:02d}".format(d,h,m) if d else "{:02d}:{:02d}".format(h,m))
    except Exception: return "?"

def disk():
    try:
        st=os.statvfs("/"); total=st.f_blocks*st.f_frsize/(1024.0**3); free=st.f_bavail*st.f_frsize/(1024.0**3)
        return "{:.1f}/{:.1f}G".format(total-free,total)
    except Exception: return "?"

class SSD1315(object):
    def __init__(self, device, address=0x3C, flip=False):
        self.fd=os.open(device, os.O_RDWR); self.device=device; self.address=address
        fcntl.ioctl(self.fd, I2C_SLAVE, address)
        seg=0xA0 if flip else 0xA1; com=0xC0 if flip else 0xC8
        self.cmd(0xAE,0xD5,0x80,0xA8,0x3F,0xD3,0x00,0x40,0x8D,0x14,0x20,0x02,seg,com,0xDA,0x12,0x81,0x7F,0xD9,0xF1,0xDB,0x40,0xA4,0xA6,0xAF)
        time.sleep(0.05); self.clear()
    def close(self):
        try: os.close(self.fd)
        except Exception: pass
    def cmd(self,*vals):
        for v in vals: os.write(self.fd, bytes(bytearray([0x00,v])))
    def write_page(self,page,data):
        self.cmd(0xB0+page,0x00,0x10)
        for i in range(0,128,16): os.write(self.fd, bytes(bytearray([0x40]+list(data[i:i+16]))))
    def show(self,buf):
        for page in range(8): self.write_page(page,buf[page*128:(page+1)*128])
    def clear(self): self.show(bytearray(1024))

def render(lines):
    b=bytearray(1024)
    for row,line in enumerate(lines[:8]):
        x=0
        for ch in str(line).upper()[:21]:
            g=FONT.get(ch,FONT["?"])
            for c in g:
                if x<128: b[row*128+x]=c
                x+=1
            if x<128: b[row*128+x]=0
            x+=1
            if x>=128: break
    return b

def camera_host_port(url):
    try:
        p=urlparse(str(url))
        return p.hostname or "", int(p.port or 554)
    except Exception:
        return "",554

def camera_online(url, timeout=0.35):
    h,p=camera_host_port(url)
    if not h:
        return False
    try:
        s=socket.create_connection((h,p),timeout=timeout)
        s.close()
        return True
    except Exception:
        return False

def active_camera(cfg):
    try:
        n=int(open("/run/robotlidar-active-camera").read().strip())
        return 2 if n==2 else 1
    except Exception:
        return 2 if int(cfg.get("active_camera",1) or 1)==2 else 1

def pages(cfg,dev,addr):
    net=network_state()
    mode=str(cfg.get("input_mode","rtsp"))
    ac=active_camera(cfg)

    cam1_url=cfg.get("camera1_url","") or cfg.get("input_url","")
    cam2_url=cfg.get("camera2_url","")
    cam_url=cam2_url if ac==2 else cam1_url
    cam=host(cam_url) if mode=="rtsp" else cfg.get("video_device","/dev/video0")

    web_ip=net.get("web_ip") or "0.0.0.0"

    p1=[
        "NETWORK",
        "ETHERNET ONLY",
        "ETH "+(str(net.get("eth_ip")) if net.get("eth_ip") else "DOWN"),
        "WEB "+str(web_ip)+":8088",
        "STREAM "+service("orange-pi-zero-camera.service"),
        "SERVER "+host(cfg.get("server_url","")),
        "WIFI DISABLED",
        "",
    ]

    if mode=="rtsp":
        c1="ONLINE" if camera_online(cam1_url) else "OFFLINE"
        c2=("ONLINE" if camera_online(cam2_url) else "OFFLINE") if cam2_url else "N/A"
    else:
        c1="LOCAL"
        c2="N/A"

    failover=bool(cfg.get("auto_failover_enabled",False)) and ac != int(cfg.get("primary_camera",1) or 1)

    p2=[
        "VIDEO STATUS",
        "CAM1 "+c1,
        "CAM2 "+c2,
        "ACTIVE CAM {}".format(ac),
        "MODE "+("FAILOVER" if failover else "NORMAL"),
        "CAM "+str(cam),
        "PTZ "+("ON" if cfg.get("ptz_enabled",False) else "OFF"),
        "WATCH "+("ON" if cfg.get("video_watchdog_enabled",True) else "OFF"),
    ]

    try:
        load=os.getloadavg()[0]
    except Exception:
        load=0.0

    p3=[
        "SYSTEM",
        "UP "+uptime(),
        "LOAD {:.2f}".format(load),
        "RAM "+memory(),
        "DISK "+disk(),
        "TEMP "+temp(),
        "WEB "+service("orange-pi-zero-web.service"),
        "SSD1315 0X{:02X}".format(addr),
    ]
    return [p1,p2,p3]

def find(cfg):
    addr=int(str(cfg.get("display_i2c_address","0x3c")),0); bus=str(cfg.get("display_i2c_bus","0")); flip=bool(cfg.get("display_flip",False))
    candidates=["/dev/i2c-"+bus] if bus.lower()!="auto" else sorted(glob.glob("/dev/i2c-*"))
    last=None
    for dev in candidates:
        o=None
        try:
            o=SSD1315(dev,addr,flip); return o,dev,addr
        except Exception as e:
            last=e
            if o: o.close()
    raise RuntimeError("SSD1315 not found addr=0x{:02X} buses={} last={}".format(addr,candidates,last))

def main():
    while True:
        cfg=load_config()
        if not cfg.get("display_enabled",True): time.sleep(5); continue
        oled=None
        try:
            oled,dev,addr=find(cfg); log("ready {} 0x{:02X}".format(dev,addr)); n=0
            while True:
                cfg=load_config()
                if not cfg.get("display_enabled",True): oled.clear(); break
                ps=pages(cfg,dev,addr); oled.show(render(ps[n%len(ps)])); n+=1
                time.sleep(max(1.0,float(cfg.get("display_rotate_sec",4.0) or 4.0)))
        except KeyboardInterrupt:
            if oled: oled.clear()
            return
        except Exception as e:
            log("ERROR: "+str(e)); time.sleep(5)
        finally:
            if oled: oled.close()

if __name__=="__main__": main()
