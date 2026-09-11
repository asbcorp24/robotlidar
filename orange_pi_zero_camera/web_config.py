#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import ipaddress
import json
import os
import socket
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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


HTML = r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RobotLiDAR · Orange Pi One Camera</title>
<style>
:root{font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#15202b;background:#eef2f6}*{box-sizing:border-box}body{margin:0}.wrap{max-width:1100px;margin:auto;padding:20px}.top{display:flex;justify-content:space-between;align-items:center;gap:15px;margin-bottom:18px}.brand h1{margin:0;font-size:24px}.muted{color:#687684;font-size:13px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.card{background:#fff;border-radius:14px;padding:18px;box-shadow:0 4px 20px #0000000c}.card h2{margin:0 0 14px;font-size:18px}.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}label{display:block;font-size:13px;color:#53606d;margin:10px 0 5px}input,select{width:100%;padding:10px 11px;border:1px solid #ccd5df;border-radius:8px;font-size:14px;background:#fff}button{border:0;border-radius:8px;padding:10px 14px;font-weight:600;cursor:pointer}.primary{background:#1769e0;color:#fff}.secondary{background:#e8eef6;color:#213044}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.status{display:inline-flex;align-items:center;gap:7px;padding:7px 10px;border-radius:20px;background:#eef2f6;font-size:13px}.dot{width:9px;height:9px;border-radius:50%;background:#9aa6b2}.dot.ok{background:#20a66a}.net{padding:11px;border:1px solid #dde4eb;border-radius:9px;background:#f9fbfd}.msg{margin-top:10px;white-space:pre-wrap;font-size:13px}.oktxt{color:#168252}.errtxt{color:#b62f2f}.scanTable{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}.scanTable th,.scanTable td{text-align:left;padding:9px;border-bottom:1px solid #e4e9ef;vertical-align:top}.scanTable th{color:#53606d}.pill{display:inline-block;background:#eef2f6;border-radius:12px;padding:3px 7px;margin:2px}.url{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;word-break:break-all}@media(max-width:760px){.grid,.row{grid-template-columns:1fr}.top{align-items:flex-start;flex-direction:column}.scanTable{display:block;overflow:auto}}</style></head>
<body><div class="wrap">
<div class="top"><div class="brand"><h1>Orange Pi One · Camera + PTZ</h1><div class="muted">Локальная настройка RobotLiDAR через Ethernet</div></div><div id="svc" class="status"><span class="dot"></span><span>проверка...</span></div></div>
<div class="grid">
<section class="card"><h2>Ethernet</h2><div class="net"><strong>Проводное подключение</strong><div id="ethernet" class="muted" style="margin-top:6px">Определение адреса...</div></div><p class="muted">IP выдаётся вашей проводной сетью/DHCP либо задаётся средствами ОС Orange Pi.</p></section>
<section class="card"><h2>Сервер и устройство</h2><label>Device ID</label><input id="device_id"><label>Название</label><input id="device_name"><label>Адрес центрального сервера</label><input id="server_url"><div class="row"><div><label>SRT latency, мс</label><input id="srt_latency_ms" type="number"></div><div><label>Telemetry, сек</label><input id="telemetry_period_sec" type="number" step="0.5"></div></div></section>
<section class="card"><h2>Камера</h2><label>Источник</label><select id="input_mode"><option value="rtsp">RTSP H.264 (copy)</option><option value="v4l2_h264">USB H.264</option><option value="v4l2_encode">USB + encode</option><option value="test">Тестовая картинка</option></select><label>RTSP URL</label><input id="input_url"><label>V4L2 устройство</label><input id="video_device"><div class="row"><div><label>Ширина</label><input id="width" type="number"></div><div><label>Высота</label><input id="height" type="number"></div><div><label>FPS</label><input id="fps" type="number"></div><div><label>Битрейт, kbps</label><input id="bitrate_kbps" type="number"></div></div><label>Encoder</label><input id="encoder"></section>
<section class="card"><h2>ONVIF / PTZ</h2><label><input id="ptz_enabled" type="checkbox" style="width:auto"> PTZ включён</label><label><input id="onvif_auto_discovery" type="checkbox" style="width:auto"> Автоопределение ONVIF</label><label>ONVIF Device URL (необязательно)</label><input id="onvif_device_url"><label>ONVIF PTZ URL (необязательно)</label><input id="onvif_url"><div class="row"><div><label>Логин камеры</label><input id="onvif_username"></div><div><label>Пароль камеры</label><input id="onvif_password" type="password" placeholder="Оставьте пустым, чтобы не менять"></div></div><label>Profile Token (необязательно)</label><input id="onvif_profile_token"></section>
</div>
<section class="card" style="margin-top:16px"><h2>Поиск RTSP / ONVIF камер</h2><p class="muted">Сканируется локальный проводной сегмент. Проверяются RTSP-порты 554, 8554, 10554 и типовые ONVIF HTTP-порты. Сканируйте только сеть, которой вы управляете или имеете разрешение проверять.</p><div class="actions"><button id="scanBtn" class="primary" onclick="scanCameras()">Сканировать сеть</button></div><div id="scanMsg" class="msg"></div><div id="scanResults"></div></section>
<section class="card" style="margin-top:16px"><h2>Применение</h2><div class="actions"><button class="primary" onclick="saveConfig(true)">Сохранить и перезапустить</button><button class="secondary" onclick="saveConfig(false)">Только сохранить</button><button class="secondary" onclick="restartService()">Перезапустить трансляцию</button></div><div id="saveMsg" class="msg"></div></section>
</div><script>
const $=id=>document.getElementById(id);let cfg={};
async function api(url,opt={}){const r=await fetch(url,opt);let d={};try{d=await r.json()}catch{}if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);return d}
function setValue(k,v){const e=$(k);if(!e)return;if(e.type==='checkbox')e.checked=!!v;else e.value=v??''}
async function load(){try{const d=await api('/api/status');cfg=d.config||{};Object.entries(cfg).forEach(([k,v])=>setValue(k,v));$('ethernet').textContent=(d.ethernet||[]).map(x=>`${x.interface}: ${x.address}`).join(' · ')||'Проводной IPv4 адрес не определён';const s=d.streamer||{};$('svc').innerHTML=`<span class="dot ${s.active?'ok':''}"></span><span>трансляция: ${s.state||'unknown'}</span>`}catch(e){$('saveMsg').className='msg errtxt';$('saveMsg').textContent=e.message}}
function collect(){const keys=['device_id','device_name','server_url','input_mode','input_url','video_device','encoder','onvif_device_url','onvif_url','onvif_username','onvif_profile_token'];const nums=['width','height','fps','bitrate_kbps','srt_latency_ms','telemetry_period_sec'];const out={...cfg};keys.forEach(k=>out[k]=$(k).value);nums.forEach(k=>out[k]=Number($(k).value));out.ptz_enabled=$('ptz_enabled').checked;out.onvif_auto_discovery=$('onvif_auto_discovery').checked;const p=$('onvif_password').value;if(p)out.onvif_password=p;return out}
async function saveConfig(restart){const m=$('saveMsg');m.className='msg';m.textContent='Сохранение...';try{const d=await api('/api/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:collect(),restart})});m.className='msg oktxt';m.textContent=d.message||'Сохранено';$('onvif_password').value='';await load()}catch(e){m.className='msg errtxt';m.textContent=e.message}}
async function restartService(){try{const d=await api('/api/restart',{method:'POST'});$('saveMsg').className='msg oktxt';$('saveMsg').textContent=d.message||'Перезапущено';setTimeout(load,800)}catch(e){$('saveMsg').className='msg errtxt';$('saveMsg').textContent=e.message}}
function useCamera(url,ip,onvifPort){$('input_mode').value='rtsp';$('input_url').value=url;if(onvifPort){$('onvif_auto_discovery').checked=true;$('onvif_device_url').value=`http://${ip}:${onvifPort}/onvif/device_service`}$('scanMsg').className='msg oktxt';$('scanMsg').textContent='Камера подставлена в настройки. Укажите логин/пароль камеры при необходимости и нажмите «Сохранить и перезапустить».';window.scrollTo({top:$('input_url').getBoundingClientRect().top+window.scrollY-100,behavior:'smooth'})}
function renderScan(d){const devs=d.devices||[];if(!devs.length){$('scanResults').innerHTML='';$('scanMsg').className='msg';$('scanMsg').textContent=`Сканирование ${d.network||''} завершено. RTSP/ONVIF устройств не найдено.`;return}let h='<table class="scanTable"><thead><tr><th>IP</th><th>RTSP</th><th>ONVIF</th><th></th></tr></thead><tbody>';for(const x of devs){const r=(x.rtsp||[]);const o=(x.onvif_ports||[]);const rt=r.length?r.map(v=>`<div><span class="pill">:${v.port}</span> <span class="url">${v.url}</span><br><span class="muted">${v.response||''}</span></div>`).join(''):'—';const ov=o.length?o.map(p=>`<span class="pill">:${p}</span>`).join(' '):'—';let btn='';if(r.length){const u=JSON.stringify(r[0].url),ip=JSON.stringify(x.ip),op=o.length?o[0]:0;btn=`<button class="secondary" onclick='useCamera(${u},${ip},${op})'>Использовать</button>`}h+=`<tr><td><strong>${x.ip}</strong>${x.hostname?`<div class="muted">${x.hostname}</div>`:''}</td><td>${rt}</td><td>${ov}</td><td>${btn}</td></tr>`}h+='</tbody></table>';$('scanResults').innerHTML=h;$('scanMsg').className='msg oktxt';$('scanMsg').textContent=`Найдено устройств: ${devs.length}. Сеть: ${d.network||''}`}
async function scanCameras(){const b=$('scanBtn'),m=$('scanMsg');b.disabled=true;b.textContent='Сканирование...';m.className='msg';m.textContent='Проверяю локальную сеть. Это может занять несколько секунд...';$('scanResults').innerHTML='';try{const d=await api('/api/camera-scan',{method:'POST'});renderScan(d)}catch(e){m.className='msg errtxt';m.textContent=e.message}finally{b.disabled=false;b.textContent='Сканировать сеть'}}
load();
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    server_version = "RobotLiDAROrangePiWeb/1.2"

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
        if self.path == "/":
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/status":
            cfg = load_config()
            cfg["onvif_password"] = ""
            self.send_json(200, {"ok": True, "config": cfg, "ethernet": ethernet_info(), "streamer": service_state(STREAM_SERVICE)})
            return
        self.send_json(404, {"detail": "Not found"})

    def do_POST(self) -> None:
        try:
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
