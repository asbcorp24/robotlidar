#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from onvif_discovery import discover as discover_onvif, diagnose_ptz, ws_security

CONFIG_PATH = Path(os.environ.get("ORANGE_PI_CAMERA_CONFIG", "/etc/robotlidar/orange-pi-zero-camera.json"))
LISTEN_HOST = os.environ.get("ORANGE_PI_WEB_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("ORANGE_PI_WEB_PORT", "8088"))
STREAM_SERVICE = "orange-pi-zero-camera.service"

RTSP_PORTS = (554, 8554, 10554)
ONVIF_PORTS = (80, 8000, 8080, 8899)
RTSP_PATHS = (
    "/",
    "/stream1",
    "/Streaming/Channels/101",
    "/h264Preview_01_main",
    "/cam/realmonitor?channel=1&subtype=0",
    "/live/ch00_0",
    "/11",
    "/camera",
)


def run_cmd(args: list[str], timeout: float = 12.0) -> tuple[int, str]:
    try:
        cp = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
        return cp.returncode, cp.stdout.strip()
    except Exception as exc:
        return 1, str(exc)


def load_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(data: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(CONFIG_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_PATH)


def ethernet_info() -> list[dict[str, str]]:
    code, out = run_cmd(["ip", "-4", "-o", "addr", "show", "scope", "global"], 5)
    if code != 0:
        return []
    result: list[dict[str, str]] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        iface, cidr = parts[1], parts[3]
        if iface == "lo" or iface.startswith(("wl", "wlan")):
            continue
        result.append({"interface": iface, "address": cidr})
    return result


def service_state(name: str) -> dict[str, Any]:
    code, active = run_cmd(["systemctl", "is-active", name], 4)
    _code2, enabled = run_cmd(["systemctl", "is-enabled", name], 4)
    return {"active": code == 0 and active == "active", "state": active or "unknown", "enabled": enabled == "enabled"}


def restart_streamer() -> tuple[bool, str]:
    code, out = run_cmd(["systemctl", "restart", STREAM_SERVICE], 15)
    return code == 0, out


def streamer_log(kind: str = "all", lines: int = 80) -> dict[str, Any]:
    lines = max(20, min(120, int(lines)))
    code, out = run_cmd([
        "journalctl", "-u", STREAM_SERVICE,
        "-n", str(lines), "--no-pager", "-o", "short-iso"
    ], 5)
    if code != 0:
        return {"ok": False, "kind": kind, "lines": [], "detail": out or "journalctl error"}

    raw = out.splitlines()
    if kind == "video":
        words = ("FFMPEG", "SRT", "RTSP", "REGISTER", "CAMERA SWITCH", "STREAM")
        raw = [line for line in raw if any(word in line.upper() for word in words)]
    elif kind == "ptz":
        words = ("ONVIF", "PTZ", "CONTROL/WSS", "CONTROL/PTZ")
        raw = [line for line in raw if any(word in line.upper() for word in words)]

    # Keep the response small even if a journal line is unexpectedly huge.
    raw = [line[-1000:] for line in raw[-80:]]
    return {"ok": True, "kind": kind, "lines": raw}


def tcp_open(ip: str, port: int, timeout: float = 0.18) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except OSError:
        return False


def rtsp_probe(ip: str, port: int, path: str, timeout: float = 0.45) -> tuple[bool, str]:
    url = "rtsp://{}:{}{}".format(ip, port, path)
    request = (
        "OPTIONS {} RTSP/1.0\r\n"
        "CSeq: 1\r\n"
        "User-Agent: RobotLiDAR-Scanner/1.0\r\n\r\n"
    ).format(url).encode("ascii", "ignore")
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(request)
            data = s.recv(1024).decode("latin1", "ignore")
        first = data.splitlines()[0] if data else ""
        return first.startswith("RTSP/"), first
    except OSError:
        return False, ""


def onvif_probe(ip: str, port: int, timeout: float = 0.4) -> bool:
    request = (
        "GET /onvif/device_service HTTP/1.0\r\n"
        "Host: {}\r\n"
        "Connection: close\r\n\r\n"
    ).format(ip).encode("ascii", "ignore")
    try:
        with socket.create_connection((ip, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(request)
            data = s.recv(512).decode("latin1", "ignore")
        return data.startswith("HTTP/")
    except OSError:
        return False


def scan_one_host(ip: str) -> dict[str, Any] | None:
    rtsp_results: list[dict[str, Any]] = []
    onvif_ports: list[int] = []

    for port in RTSP_PORTS:
        if not tcp_open(ip, port):
            continue
        matched = False
        for path in RTSP_PATHS:
            ok, response = rtsp_probe(ip, port, path)
            if ok:
                rtsp_results.append({
                    "port": port,
                    "path": path,
                    "url": "rtsp://{}:{}{}".format(ip, port, path),
                    "response": response,
                })
                matched = True
                # One valid RTSP endpoint on this port is enough for discovery.
                break
        if not matched:
            rtsp_results.append({
                "port": port,
                "path": "",
                "url": "rtsp://{}:{}/".format(ip, port),
                "response": "TCP open; RTSP path/auth not identified",
            })

    for port in ONVIF_PORTS:
        if tcp_open(ip, port) and onvif_probe(ip, port):
            onvif_ports.append(port)

    if not rtsp_results and not onvif_ports:
        return None

    try:
        name = socket.gethostbyaddr(ip)[0]
    except OSError:
        name = ""

    return {"ip": ip, "hostname": name, "rtsp": rtsp_results, "onvif_ports": onvif_ports}


def scan_network() -> dict[str, Any]:
    links = ethernet_info()
    if not links:
        return {"network": "", "devices": [], "detail": "Нет проводного IPv4 интерфейса"}

    iface = links[0]["interface"]
    cidr = links[0]["address"]
    local = ipaddress.ip_interface(cidr)
    network = local.network
    # On large corporate networks do not scan thousands of addresses. Scan the local /24 segment.
    if network.num_addresses > 256:
        network = ipaddress.ip_network("{}/24".format(local.ip), strict=False)

    hosts = [str(ip) for ip in network.hosts() if ip != local.ip]
    devices: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=48) as pool:
        futures = [pool.submit(scan_one_host, ip) for ip in hosts]
        for future in concurrent.futures.as_completed(futures):
            try:
                item = future.result()
                if item:
                    devices.append(item)
            except Exception:
                pass

    devices.sort(key=lambda x: tuple(int(p) for p in x["ip"].split(".")))
    return {"interface": iface, "network": str(network), "devices": devices}


PTZ_LOCK = threading.Lock()
PTZ_CACHE: dict[str, str] = {}


def active_rtsp_url(cfg: dict[str, Any]) -> str:
    active = 2 if int(cfg.get("active_camera") or 1) == 2 else 1
    if active == 2 and str(cfg.get("camera2_url") or "").strip():
        return str(cfg.get("camera2_url") or "").strip()
    return str(cfg.get("camera1_url") or cfg.get("input_url") or "").strip()


def preview_ffmpeg_cmd(cfg: dict[str, Any]) -> list[str]:
    ffmpeg = str(cfg.get("ffmpeg") or "/usr/local/bin/ffmpeg")
    url = active_rtsp_url(cfg)
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp", "-i", url,
        "-an", "-vf", "fps=3,scale=640:-2",
        "-q:v", "7", "-f", "mjpeg", "pipe:1"
    ]


def onvif_runtime(cfg: dict[str, Any]) -> tuple[str, str]:
    key = active_rtsp_url(cfg)
    with PTZ_LOCK:
        if PTZ_CACHE.get("key") == key and PTZ_CACHE.get("url") and PTZ_CACHE.get("token"):
            return PTZ_CACHE["url"], PTZ_CACHE["token"]
    result = discover_onvif(
        key,
        username=str(cfg.get("onvif_username") or ""),
        password=str(cfg.get("onvif_password") or ""),
        explicit_device_url=str(cfg.get("onvif_device_url") or ""),
    )
    with PTZ_LOCK:
        PTZ_CACHE["key"] = key
        PTZ_CACHE["url"] = result.ptz_url
        PTZ_CACHE["token"] = result.profile_token
    return result.ptz_url, result.profile_token


def onvif_post(cfg: dict[str, Any], body: str, timeout: float = 2.5) -> None:
    url, _token = onvif_runtime(cfg)
    username = str(cfg.get("onvif_username") or "")
    password = str(cfg.get("onvif_password") or "")
    security = ws_security(username, password) if username else ""
    envelope = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd" xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl" xmlns:tt="http://www.onvif.org/ver10/schema">
<s:Header>{security}</s:Header><s:Body>{body}</s:Body></s:Envelope>'''
    req = urllib.request.Request(url, data=envelope.encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/soap+xml; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            response.read(256)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(1000).decode("utf-8", "ignore").replace("\n", " ").strip()
        except Exception:
            pass
        reason = detail or str(exc.reason)
        if "ServiceNotSupported" in reason or "Service Not Supported" in reason:
            reason = "ServiceNotSupported"
        elif "ActionNotSupported" in reason or "Action Not Supported" in reason:
            reason = "ActionNotSupported"
        elif len(reason) > 220:
            reason = reason[:220] + "..."
        raise RuntimeError("HTTP {}: {}".format(exc.code, reason)) from exc


def local_ptz(cfg: dict[str, Any], direction: str, speed: float) -> str:
    if not bool(cfg.get("ptz_enabled", True)):
        raise RuntimeError("PTZ отключён в настройках")
    url, token_raw = onvif_runtime(cfg)
    _ = url
    token = escape(token_raw)
    speed = max(0.05, min(1.0, float(speed)))
    if direction == "home":
        if PTZ_CACHE.get("home_supported") == "no":
            raise RuntimeError("Home не поддерживается этой камерой")
        body = f'''<tptz:GotoHomePosition><tptz:ProfileToken>{token}</tptz:ProfileToken><tptz:Speed><tt:PanTilt x="{speed:.3f}" y="{speed:.3f}"/></tptz:Speed></tptz:GotoHomePosition>'''
        try:
            onvif_post(cfg, body)
            with PTZ_LOCK:
                PTZ_CACHE["home_supported"] = "yes"
            return "Home"
        except Exception as exc:
            if "ServiceNotSupported" in str(exc) or "ActionNotSupported" in str(exc):
                with PTZ_LOCK:
                    PTZ_CACHE["home_supported"] = "no"
                raise RuntimeError("Home не поддерживается этой камерой") from exc
            raise

    vx = 0.0
    vy = 0.0
    if direction == "left":
        vx = -speed
    elif direction == "right":
        vx = speed
    elif direction == "up":
        vy = speed
    elif direction == "down":
        vy = -speed
    else:
        raise RuntimeError("Неизвестное направление")

    move = f'''<tptz:ContinuousMove><tptz:ProfileToken>{token}</tptz:ProfileToken><tptz:Velocity><tt:PanTilt x="{vx:.3f}" y="{vy:.3f}"/></tptz:Velocity></tptz:ContinuousMove>'''
    stop = f'''<tptz:Stop><tptz:ProfileToken>{token}</tptz:ProfileToken><tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>'''
    onvif_post(cfg, move)
    time.sleep(0.22)
    onvif_post(cfg, stop)
    return "{} {:.2f}".format(direction, speed)


CAMERA_HTML = r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RobotLiDAR · Локальная камера</title>
<style>
:root{font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#15202b;background:#eef2f6}*{box-sizing:border-box}body{margin:0}.wrap{max-width:900px;margin:auto;padding:18px}.top{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px}.card{background:#fff;border-radius:14px;padding:16px;box-shadow:0 4px 20px #0000000c}.viewer{background:#0c1117;border-radius:12px;overflow:hidden;display:flex;align-items:center;justify-content:center;min-height:260px}.viewer img{display:block;width:100%;max-width:640px;height:auto}.controls{display:grid;grid-template-columns:70px 70px 70px;grid-template-rows:58px 58px 58px;gap:7px;justify-content:center;margin:18px 0}.controls button{font-size:24px;border:0;border-radius:10px;background:#e8eef6;cursor:pointer}.controls .home{font-size:18px;background:#1769e0;color:#fff}.up{grid-column:2}.left{grid-column:1;grid-row:2}.home{grid-column:2;grid-row:2}.right{grid-column:3;grid-row:2}.down{grid-column:2;grid-row:3}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.status{font-size:13px;color:#687684}.log{background:#111820;color:#d8e2ec;border-radius:10px;padding:10px;height:140px;overflow:auto;white-space:pre-wrap;font:12px/1.4 ui-monospace,Consolas,monospace}.btn{border:0;border-radius:8px;padding:9px 13px;background:#e8eef6;cursor:pointer;text-decoration:none;color:#213044;font-weight:600}@media(max-width:600px){.wrap{padding:10px}.top{align-items:flex-start;flex-direction:column}}
</style></head><body><div class="wrap">
<div class="top"><div><h2 style="margin:0">Локальная камера + PTZ</h2><div id="state" class="status">проверка...</div></div><a class="btn" href="/">← Настройки</a></div>
<div class="card">
<div id="disabled" style="display:none;padding:35px;text-align:center">Локальный просмотр отключён в настройках.</div>
<div id="enabled">
<div class="viewer"><img id="cam" alt="Camera preview"></div>
<div class="controls">
<button class="up" onclick="ptz('up')">▲</button>
<button class="left" onclick="ptz('left')">◀</button>
<button id="homeBtn" class="home" onclick="ptz('home')">●</button>
<button class="right" onclick="ptz('right')">▶</button>
<button class="down" onclick="ptz('down')">▼</button>
</div>
<div class="row"><label>Скорость PTZ <input id="speed" type="range" min="10" max="100" value="35"></label><span id="speedText">35%</span><button class="btn" onclick="reloadPreview()">Обновить видео</button></div>
<h3>PTZ журнал</h3><div id="ptzlog" class="log"></div>
</div></div></div>
<script>
const $=id=>document.getElementById(id);function addLog(s){const b=$('ptzlog');b.textContent=new Date().toLocaleTimeString()+' '+s+'\n'+b.textContent}
$('speed').oninput=()=>{$('speedText').textContent=$('speed').value+'%'};
async function init(){const r=await fetch('/api/status');const d=await r.json();const c=d.config||{};const en=c.local_preview_enabled!==false;$('enabled').style.display=en?'block':'none';$('disabled').style.display=en?'none':'block';$('state').textContent=(c.camera1_name||'Camera 1')+' · local preview '+(en?'ON':'OFF')+(c.ptz_enabled===false?' · PTZ OFF':' · PTZ ON');if(en)reloadPreview()}
function reloadPreview(){const img=$('cam');img.src='/api/preview.mjpg?t='+Date.now()}
async function ptz(dir){try{const speed=Number($('speed').value)/100;const r=await fetch('/api/local-ptz',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({direction:dir,speed})});const d=await r.json();if(!r.ok)throw new Error(d.detail||'PTZ error');addLog('OK '+(d.message||dir))}catch(e){addLog('ERR '+e.message);if(dir==='home'&&String(e.message).includes('не поддерживается')){const b=$('homeBtn');if(b){b.disabled=true;b.title='Home не поддерживается камерой';b.style.opacity='.45'}}}}
init();
</script></body></html>'''



HTML = r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RobotLiDAR · Orange Pi One Camera</title>
<style>
:root{font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#15202b;background:#eef2f6}*{box-sizing:border-box}body{margin:0}.wrap{max-width:1100px;margin:auto;padding:20px}.top{display:flex;justify-content:space-between;align-items:center;gap:15px;margin-bottom:18px}.brand h1{margin:0;font-size:24px}.muted{color:#687684;font-size:13px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.card{background:#fff;border-radius:14px;padding:18px;box-shadow:0 4px 20px #0000000c}.card h2{margin:0 0 14px;font-size:18px}.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}label{display:block;font-size:13px;color:#53606d;margin:10px 0 5px}input,select{width:100%;padding:10px 11px;border:1px solid #ccd5df;border-radius:8px;font-size:14px;background:#fff}button{border:0;border-radius:8px;padding:10px 14px;font-weight:600;cursor:pointer}.primary{background:#1769e0;color:#fff}.secondary{background:#e8eef6;color:#213044}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.status{display:inline-flex;align-items:center;gap:7px;padding:7px 10px;border-radius:20px;background:#eef2f6;font-size:13px}.dot{width:9px;height:9px;border-radius:50%;background:#9aa6b2}.dot.ok{background:#20a66a}.net{padding:11px;border:1px solid #dde4eb;border-radius:9px;background:#f9fbfd}.msg{margin-top:10px;white-space:pre-wrap;font-size:13px}.oktxt{color:#168252}.errtxt{color:#b62f2f}.scanTable{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}.scanTable th,.scanTable td{text-align:left;padding:9px;border-bottom:1px solid #e4e9ef;vertical-align:top}.scanTable th{color:#53606d}.pill{display:inline-block;background:#eef2f6;border-radius:12px;padding:3px 7px;margin:2px}.url{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;word-break:break-all}.logbox{margin:12px 0 0;background:#111820;color:#d8e2ec;border-radius:10px;padding:12px;height:260px;overflow:auto;font:12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;word-break:break-word}.logmeta{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.logmeta label{margin:0}.logmeta input{width:auto}@media(max-width:760px){.grid,.row{grid-template-columns:1fr}.top{align-items:flex-start;flex-direction:column}.scanTable{display:block;overflow:auto}}</style></head>
<body><div class="wrap">
<div class="top"><div class="brand"><h1>Orange Pi One · Camera + PTZ</h1><div class="muted">Локальная настройка RobotLiDAR через Ethernet</div></div><div id="svc" class="status"><span class="dot"></span><span>проверка...</span></div></div>
<div class="grid">
<section class="card"><h2>Ethernet</h2><div class="net"><strong>Проводное подключение</strong><div id="ethernet" class="muted" style="margin-top:6px">Определение адреса...</div></div><p class="muted">IP выдаётся вашей проводной сетью/DHCP либо задаётся средствами ОС Orange Pi.</p></section>
<section class="card"><h2>Сервер и устройство</h2><label>Device ID</label><input id="device_id"><label>Название</label><input id="device_name"><label>Адрес центрального сервера</label><input id="server_url"><label><input id="stream_enabled" type="checkbox" style="width:auto"> Транслировать видео на центральный сервер</label><label><input id="local_preview_enabled" type="checkbox" style="width:auto"> Разрешить локальный просмотр камеры</label><p class="muted">Обе функции независимы: можно отдельно включать SRT на сервер и локальный просмотр. ONVIF/PTZ работают независимо от них.</p><div class="row"><div><label>SRT latency, мс</label><input id="srt_latency_ms" type="number"></div><div><label>Telemetry, сек</label><input id="telemetry_period_sec" type="number" step="0.5"></div></div></section>
<section class="card"><h2>Камеры H.264</h2><label>Источник</label><select id="input_mode"><option value="rtsp">RTSP H.264 (copy)</option><option value="v4l2_h264">USB H.264</option><option value="v4l2_encode">USB + encode</option><option value="test">Тестовая картинка</option></select><div class="row"><div><label>Имя Camera 1</label><input id="camera1_name" placeholder="Передняя"></div><div><label>Имя Camera 2</label><input id="camera2_name" placeholder="Задняя"></div></div><label>RTSP Camera 1</label><input id="camera1_url" placeholder="rtsp://192.168.1.149:554/stream1"><label>RTSP Camera 2</label><input id="camera2_url" placeholder="rtsp://192.168.1.150:554/stream1"><label>Активная камера при запуске</label><select id="active_camera"><option value="1">Camera 1</option><option value="2">Camera 2</option></select><input id="input_url" type="hidden"><label>V4L2 устройство</label><input id="video_device"><div class="row"><div><label>Ширина</label><input id="width" type="number"></div><div><label>Высота</label><input id="height" type="number"></div><div><label>FPS</label><input id="fps" type="number"></div><div><label>Битрейт, kbps</label><input id="bitrate_kbps" type="number"></div></div><label>Encoder</label><input id="encoder"><p class="muted">Через SRT передаётся только одна выбранная камера. Переключение Camera 1 / Camera 2 приходит с центрального сервера без второго SRT-потока.</p></section>
<section class="card"><h2>ONVIF / PTZ</h2><label><input id="ptz_enabled" type="checkbox" style="width:auto"> PTZ включён</label><label><input id="onvif_auto_discovery" type="checkbox" style="width:auto"> Автоопределение ONVIF</label><label>ONVIF Device URL (необязательно)</label><input id="onvif_device_url"><label>ONVIF PTZ URL (необязательно)</label><input id="onvif_url"><div class="row"><div><label>Логин камеры</label><input id="onvif_username"></div><div><label>Пароль камеры</label><input id="onvif_password" type="password" placeholder="Оставьте пустым, чтобы не менять"></div></div><label>Profile Token (необязательно)</label><input id="onvif_profile_token"><div class="actions"><button class="secondary" onclick="probeOnvif()">Проверить возможности ONVIF</button></div><pre id="onvifDiag" class="logbox" style="height:220px">Диагностика ещё не запускалась.</pre></section>
</div>
<section class="card" style="margin-top:16px"><h2>Поиск RTSP / ONVIF камер</h2><p class="muted">Сканируется локальный проводной сегмент. Проверяются RTSP-порты 554, 8554, 10554 и типовые ONVIF HTTP-порты. Сканируйте только сеть, которой вы управляете или имеете разрешение проверять.</p><div class="actions"><button id="scanBtn" class="primary" onclick="scanCameras()">Сканировать сеть</button></div><div id="scanMsg" class="msg"></div><div id="scanResults"></div></section>
<section class="card" style="margin-top:16px"><h2>Журнал трансляции / ONVIF / PTZ</h2><div class="logmeta"><button class="secondary" onclick="setLogKind('all')">Все</button><button class="secondary" onclick="setLogKind('video')">Видео / SRT</button><button class="secondary" onclick="setLogKind('ptz')">ONVIF / PTZ</button><button class="secondary" onclick="loadLog()">Обновить</button><label><input id="logAuto" type="checkbox" style="width:auto" checked> авто 3 сек</label><span id="logInfo" class="muted"></span></div><pre id="streamLog" class="logbox">Загрузка журнала...</pre><p class="muted">Показываются только последние строки systemd-журнала сервиса трансляции. Отдельный лог-файл не создаётся, поэтому лишней записи на SD-карту нет.</p></section>
<section class="card" style="margin-top:16px"><h2>Применение</h2><div class="actions"><button class="secondary" onclick="location.href='/camera'">Локальная камера / PTZ</button><button class="primary" onclick="saveConfig(true)">Сохранить и перезапустить</button><button class="secondary" onclick="saveConfig(false)">Только сохранить</button><button class="secondary" onclick="restartService()">Перезапустить трансляцию</button></div><div id="saveMsg" class="msg"></div></section>
</div><script>
const $=id=>document.getElementById(id);let cfg={};
async function api(url,opt={}){const r=await fetch(url,opt);let d={};try{d=await r.json()}catch{}if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);return d}
function setValue(k,v){const e=$(k);if(!e)return;if(e.type==='checkbox')e.checked=!!v;else e.value=v??''}
async function load(){try{const d=await api('/api/status');cfg=d.config||{};if(!Object.prototype.hasOwnProperty.call(cfg,'stream_enabled'))cfg.stream_enabled=true;if(!Object.prototype.hasOwnProperty.call(cfg,'local_preview_enabled'))cfg.local_preview_enabled=true;Object.entries(cfg).forEach(([k,v])=>setValue(k,v));$('ethernet').textContent=(d.ethernet||[]).map(x=>`${x.interface}: ${x.address}`).join(' · ')||'Проводной IPv4 адрес не определён';const s=d.streamer||{};$('svc').innerHTML=`<span class="dot ${s.active?'ok':''}"></span><span>${cfg.stream_enabled===false?'SRT выключен · PTZ доступен':'трансляция: '+(s.state||'unknown')}</span>`}catch(e){$('saveMsg').className='msg errtxt';$('saveMsg').textContent=e.message}}
function collect(){const keys=['device_id','device_name','server_url','input_mode','input_url','camera1_name','camera1_url','camera2_name','camera2_url','video_device','encoder','onvif_device_url','onvif_url','onvif_username','onvif_profile_token'];const nums=['width','height','fps','bitrate_kbps','srt_latency_ms','telemetry_period_sec','active_camera'];const out={...cfg};keys.forEach(k=>out[k]=$(k).value);nums.forEach(k=>out[k]=Number($(k).value));if(!out.camera1_url)out.camera1_url=out.input_url;out.input_url=out.camera1_url;out.stream_enabled=$('stream_enabled').checked;out.local_preview_enabled=$('local_preview_enabled').checked;out.ptz_enabled=$('ptz_enabled').checked;out.onvif_auto_discovery=$('onvif_auto_discovery').checked;const p=$('onvif_password').value;if(p)out.onvif_password=p;return out}
async function saveConfig(restart){const m=$('saveMsg');m.className='msg';m.textContent='Сохранение...';try{const d=await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:collect(),restart})});m.className='msg oktxt';m.textContent=d.message||'Сохранено';$('onvif_password').value='';await load()}catch(e){m.className='msg errtxt';m.textContent=e.message}}
async function restartService(){try{const d=await api('/api/restart',{method:'POST'});$('saveMsg').className='msg oktxt';$('saveMsg').textContent=d.message||'Перезапущено';setTimeout(load,800)}catch(e){$('saveMsg').className='msg errtxt';$('saveMsg').textContent=e.message}}
function useCamera(slot,url,ip,onvifPort){$('input_mode').value='rtsp';$('camera'+slot+'_url').value=url;if(slot===1)$('input_url').value=url;if(onvifPort&&slot===1){$('onvif_auto_discovery').checked=true;$('onvif_device_url').value=`http://${ip}:${onvifPort}/onvif/device_service`}$('scanMsg').className='msg oktxt';$('scanMsg').textContent=`Камера добавлена как Camera ${slot}. Сохраните настройки.`;window.scrollTo({top:$('camera'+slot+'_url').getBoundingClientRect().top+window.scrollY-100,behavior:'smooth'})}
function renderScan(d){const devs=d.devices||[];if(!devs.length){$('scanResults').innerHTML='';$('scanMsg').className='msg';$('scanMsg').textContent=`Сканирование ${d.network||''} завершено. RTSP/ONVIF устройств не найдено.`;return}let h='<table class="scanTable"><thead><tr><th>IP</th><th>RTSP</th><th>ONVIF</th><th></th></tr></thead><tbody>';for(const x of devs){const r=(x.rtsp||[]);const o=(x.onvif_ports||[]);const rt=r.length?r.map(v=>`<div><span class="pill">:${v.port}</span> <span class="url">${v.url}</span><br><span class="muted">${v.response||''}</span></div>`).join(''):'—';const ov=o.length?o.map(p=>`<span class="pill">:${p}</span>`).join(' '):'—';let btn='';if(r.length){const u=JSON.stringify(r[0].url),ip=JSON.stringify(x.ip),op=o.length?o[0]:0;btn=`<button class="secondary" onclick='useCamera(1,${u},${ip},${op})'>В Camera 1</button> <button class="secondary" onclick='useCamera(2,${u},${ip},0)'>В Camera 2</button>`}h+=`<tr><td><strong>${x.ip}</strong>${x.hostname?`<div class="muted">${x.hostname}</div>`:''}</td><td>${rt}</td><td>${ov}</td><td>${btn}</td></tr>`}h+='</tbody></table>';$('scanResults').innerHTML=h;$('scanMsg').className='msg oktxt';$('scanMsg').textContent=`Найдено устройств: ${devs.length}. Сеть: ${d.network||''}`}
async function scanCameras(){const b=$('scanBtn'),m=$('scanMsg');b.disabled=true;b.textContent='Сканирование...';m.className='msg';m.textContent='Проверяю локальную сеть. Это может занять несколько секунд...';$('scanResults').innerHTML='';try{const d=await api('/api/camera-scan',{method:'POST'});renderScan(d)}catch(e){m.className='msg errtxt';m.textContent=e.message}finally{b.disabled=false;b.textContent='Сканировать сеть'}}

async function probeOnvif(){const box=$('onvifDiag');box.textContent='Опрос камеры...';try{const d=await api('/api/onvif-diagnose',{method:'POST'});const lines=[];lines.push('PTZ URL: '+(d.ptz_url||'—'));lines.push('Profile: '+(d.profile_token||'—'));for(const k of ['GetStatus','GetConfigurations','GetConfigurationOptions','GetPresets']){const x=d[k]||{};lines.push('');lines.push(k+': '+(x.supported?'SUPPORTED':'NOT SUPPORTED'));if(x.error)lines.push('  error: '+x.error);if(x.pan_x!=null||x.tilt_y!=null)lines.push('  pan='+String(x.pan_x??'—')+' tilt='+String(x.tilt_y??'—'));if(x.zoom_x!=null)lines.push('  zoom='+x.zoom_x);if(x.move_status)lines.push('  move_status='+JSON.stringify(x.move_status));if(x.configurations)lines.push('  configs='+JSON.stringify(x.configurations));if(x.spaces)lines.push('  spaces='+JSON.stringify(x.spaces));if(x.presets)lines.push('  presets='+JSON.stringify(x.presets));}box.textContent=lines.join('\n')}catch(e){box.textContent='Ошибка: '+e.message}}

let logKind='all';
function setLogKind(k){logKind=k;loadLog()}
async function loadLog(){if(document.hidden)return;try{const d=await api('/api/log?kind='+encodeURIComponent(logKind));const box=$('streamLog');const stick=box.scrollTop+box.clientHeight>=box.scrollHeight-25;box.textContent=(d.lines||[]).join('\n')||'Нет строк для выбранного фильтра.';$('logInfo').textContent=(logKind==='all'?'все события':logKind==='video'?'видео / SRT':'ONVIF / PTZ')+' · '+(d.lines||[]).length+' строк';if(stick)box.scrollTop=box.scrollHeight}catch(e){$('streamLog').textContent='Ошибка журнала: '+e.message}}
load();loadLog();setInterval(()=>{if($('logAuto')?.checked&&!document.hidden)loadLog()},3000);
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    server_version = "RobotLiDAROrangePiWeb/1.9"

    def log_message(self, fmt: str, *args: Any) -> None:
        print("WEB:", fmt % args, flush=True)

    def send_json(self, status: int, obj: Any) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        length = min(int(self.headers.get("Content-Length", "0") or "0"), 1024 * 1024)
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")) if raw else {}

    def do_GET(self) -> None:
        if self.path == "/camera":
            body = CAMERA_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/preview.mjpg"):
            cfg = load_config()
            if not bool(cfg.get("local_preview_enabled", True)):
                self.send_json(403, {"detail": "Локальный просмотр отключён"})
                return
            url = active_rtsp_url(cfg)
            if not url:
                self.send_json(400, {"detail": "RTSP URL не задан"})
                return
            proc = None
            try:
                proc = subprocess.Popen(preview_ffmpeg_cmd(cfg), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self.send_response(200)
                self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
                self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                self.end_headers()
                buf = bytearray()
                while proc.poll() is None:
                    chunk = proc.stdout.read(4096) if proc.stdout else b""
                    if not chunk:
                        break
                    buf.extend(chunk)
                    while True:
                        a = buf.find(b"\xff\xd8")
                        b = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
                        if a < 0 or b < 0:
                            if len(buf) > 2 * 1024 * 1024:
                                del buf[:-65536]
                            break
                        frame = bytes(buf[a:b+2])
                        del buf[:b+2]
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode() + b"\r\n\r\n")
                        self.wfile.write(frame)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                pass
            finally:
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1)
                    except Exception:
                        proc.kill()
            return
        if self.path == "/":
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/log"):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            kind = str(params.get("kind", ["all"])[0]).lower()
            if kind not in ("all", "video", "ptz"):
                kind = "all"
            self.send_json(200, streamer_log(kind))
            return
        if self.path == "/api/status":
            cfg = load_config()
            cfg["onvif_password"] = ""
            self.send_json(200, {"ok": True, "config": cfg, "ethernet": ethernet_info(), "streamer": service_state(STREAM_SERVICE)})
            return
        self.send_json(404, {"detail": "Not found"})

    def do_POST(self) -> None:
        try:
            if self.path == "/api/onvif-diagnose":
                cfg = load_config()
                try:
                    result = diagnose_ptz(
                        active_rtsp_url(cfg),
                        username=str(cfg.get("onvif_username") or ""),
                        password=str(cfg.get("onvif_password") or ""),
                        explicit_device_url=str(cfg.get("onvif_device_url") or ""),
                    )
                    self.send_json(200, {"ok": True, **result})
                except Exception as exc:
                    self.send_json(500, {"detail": str(exc)})
                return
            if self.path == "/api/local-ptz":
                req = self.read_json()
                cfg = load_config()
                direction = str(req.get("direction") or "").lower()
                speed = float(req.get("speed") or 0.35)
                try:
                    message = local_ptz(cfg, direction, speed)
                    self.send_json(200, {"ok": True, "message": message})
                except Exception as exc:
                    self.send_json(500, {"detail": str(exc)})
                return
            if self.path == "/api/camera-scan":
                result = scan_network()
                result["ok"] = True
                self.send_json(200, result)
                return
            if self.path == "/api/config":
                req = self.read_json()
                new_cfg = req.get("config")
                if not isinstance(new_cfg, dict):
                    self.send_json(400, {"detail": "config object required"})
                    return
                old_cfg = load_config()
                if not new_cfg.get("onvif_password") and old_cfg.get("onvif_password"):
                    new_cfg["onvif_password"] = old_cfg["onvif_password"]
                device_id = str(new_cfg.get("device_id") or "").strip()
                server_url = str(new_cfg.get("server_url") or "").strip()
                if len(device_id) < 3:
                    self.send_json(400, {"detail": "Device ID слишком короткий"})
                    return
                if not server_url.startswith(("http://", "https://")):
                    self.send_json(400, {"detail": "Некорректный адрес сервера"})
                    return
                save_config(new_cfg)
                if bool(req.get("restart")):
                    ok, msg = restart_streamer()
                    if not ok:
                        self.send_json(500, {"detail": msg or "Не удалось перезапустить трансляцию"})
                        return
                    self.send_json(200, {"ok": True, "message": "Настройки сохранены, трансляция перезапущена"})
                else:
                    self.send_json(200, {"ok": True, "message": "Настройки сохранены"})
                return
            if self.path == "/api/restart":
                ok, msg = restart_streamer()
                if not ok:
                    self.send_json(500, {"detail": msg or "Не удалось перезапустить трансляцию"})
                    return
                self.send_json(200, {"ok": True, "message": "Трансляция перезапущена"})
                return
            self.send_json(404, {"detail": "Not found"})
        except Exception as exc:
            self.send_json(500, {"detail": str(exc)})


def main() -> None:
    print(f"Orange Pi One web config: http://{LISTEN_HOST}:{LISTEN_PORT}/", flush=True)
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
