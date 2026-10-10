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

from onvif_discovery import discover as discover_onvif, diagnose_ptz, get_ptz_status, ws_security

CONFIG_PATH = Path(os.environ.get("ORANGE_PI_CAMERA_CONFIG", "/etc/robotlidar/orange-pi-zero-camera.json"))
LISTEN_HOST = os.environ.get("ORANGE_PI_WEB_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("ORANGE_PI_WEB_PORT", "8088"))
STREAM_SERVICE = "orange-pi-zero-camera.service"
SETUP_AP_CONNECTION = "RobotLiDAR-Setup"
WIFI_CONFIG_MARKER = Path("/run/robotlidar-wifi-configuring")

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


def ethernet_interfaces() -> list[str]:
    base = Path("/sys/class/net")
    if not base.exists():
        return []
    names: list[str] = []
    for item in base.iterdir():
        name = item.name
        if name == "lo" or name.startswith(("wl", "wlan")):
            continue
        if name.startswith(("eth", "en")):
            names.append(name)
    return sorted(names)


def _network_cfg(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    data = (cfg or load_config()).get("network")
    return data if isinstance(data, dict) else {}


def _nmcli_available() -> bool:
    code, _out = run_cmd(["nmcli", "--version"], 4)
    return code == 0


def _nm_connection_for_interface(iface: str, create: bool = False) -> tuple[str, str]:
    code, out = run_cmd(["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", iface], 5)
    connection = out.strip() if code == 0 else ""
    if connection and connection not in ("--", "(null)"):
        return connection, ""

    if not create:
        return "", out

    # A vendor Debian image can leave Ethernet unmanaged until NetworkManager takes it over.
    run_cmd(["nmcli", "device", "set", iface, "managed", "yes"], 5)
    connection = "RobotLiDAR-{}".format(iface)
    code, out = run_cmd([
        "nmcli", "connection", "add", "type", "ethernet",
        "ifname", iface, "con-name", connection,
    ], 12)
    if code != 0 and "already exists" not in out.lower():
        return "", out
    return connection, ""


def network_state(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or load_config()
    saved = _network_cfg(cfg)
    interfaces = ethernet_interfaces()
    iface = str(saved.get("interface") or "").strip()
    if iface not in interfaces:
        links = ethernet_info()
        iface = links[0]["interface"] if links else (interfaces[0] if interfaces else "")

    current_cidr = ""
    for item in ethernet_info():
        if item["interface"] == iface:
            current_cidr = item["address"]
            break

    result: dict[str, Any] = {
        "available": _nmcli_available(),
        "interfaces": interfaces,
        "interface": iface,
        "connection": "",
        "mode": str(saved.get("mode") or "dhcp"),
        "ip": str(saved.get("ip") or ""),
        "prefix": int(saved.get("prefix") or 24),
        "gateway": str(saved.get("gateway") or ""),
        "dns": str(saved.get("dns") or ""),
        "current_address": current_cidr,
    }
    if not result["available"] or not iface:
        return result

    connection, _detail = _nm_connection_for_interface(iface, False)
    result["connection"] = connection
    if not connection:
        return result

    _c, method = run_cmd(["nmcli", "-g", "ipv4.method", "connection", "show", connection], 5)
    _c, addresses = run_cmd(["nmcli", "-g", "ipv4.addresses", "connection", "show", connection], 5)
    _c, gateway = run_cmd(["nmcli", "-g", "ipv4.gateway", "connection", "show", connection], 5)
    _c, dns = run_cmd(["nmcli", "-g", "ipv4.dns", "connection", "show", connection], 5)

    method = method.strip().lower()
    if method:
        result["mode"] = "dhcp" if method in ("auto", "dhcp") else "static"
    if result["mode"] == "static" and addresses.strip():
        first = addresses.strip().splitlines()[0].split(",")[0]
        try:
            ipi = ipaddress.ip_interface(first)
            result["ip"] = str(ipi.ip)
            result["prefix"] = int(ipi.network.prefixlen)
        except ValueError:
            pass
    result["gateway"] = gateway.strip() or result["gateway"]
    result["dns"] = " ".join(x.strip() for x in dns.replace(",", "\n").splitlines() if x.strip()) or result["dns"]
    return result


def validate_network_settings(req: dict[str, Any]) -> dict[str, Any]:
    interfaces = ethernet_interfaces()
    iface = str(req.get("interface") or "").strip()
    if not iface:
        iface = interfaces[0] if interfaces else ""
    if iface not in interfaces:
        raise ValueError("Не найден Ethernet-интерфейс")

    mode = str(req.get("mode") or "dhcp").strip().lower()
    if mode not in ("dhcp", "static"):
        raise ValueError("Режим сети должен быть DHCP или static")

    result: dict[str, Any] = {
        "mode": mode,
        "interface": iface,
        "ip": "",
        "prefix": 24,
        "gateway": "",
        "dns": "",
    }
    if mode == "dhcp":
        return result

    ip_text = str(req.get("ip") or "").strip()
    prefix = int(req.get("prefix") or 24)
    gateway = str(req.get("gateway") or "").strip()
    dns_raw = str(req.get("dns") or "").replace(",", " ")
    dns_items = [x for x in dns_raw.split() if x]

    try:
        ipaddress.ip_address(ip_text)
    except ValueError as exc:
        raise ValueError("Некорректный статический IPv4 адрес") from exc
    if prefix < 1 or prefix > 32:
        raise ValueError("Префикс сети должен быть от 1 до 32")
    if gateway:
        try:
            ipaddress.ip_address(gateway)
        except ValueError as exc:
            raise ValueError("Некорректный шлюз") from exc
    for item in dns_items:
        try:
            ipaddress.ip_address(item)
        except ValueError as exc:
            raise ValueError("Некорректный DNS: {}".format(item)) from exc

    result.update({
        "ip": ip_text,
        "prefix": prefix,
        "gateway": gateway,
        "dns": " ".join(dns_items),
    })
    return result


def apply_network_settings(settings: dict[str, Any]) -> tuple[bool, str, str]:
    if not _nmcli_available():
        return False, "NetworkManager/nmcli не установлен", ""

    iface = str(settings["interface"])
    connection, detail = _nm_connection_for_interface(iface, True)
    if not connection:
        return False, detail or "Не удалось создать Ethernet-профиль NetworkManager", ""

    if settings["mode"] == "dhcp":
        args = [
            "nmcli", "connection", "modify", connection,
            "connection.interface-name", iface,
            "ipv4.method", "auto",
            "ipv4.addresses", "",
            "ipv4.gateway", "",
            "ipv4.dns", "",
            "ipv4.ignore-auto-dns", "no",
        ]
        new_url = ""
    else:
        args = [
            "nmcli", "connection", "modify", connection,
            "connection.interface-name", iface,
            "ipv4.method", "manual",
            "ipv4.addresses", "{}/{}".format(settings["ip"], settings["prefix"]),
            "ipv4.gateway", str(settings["gateway"]),
            "ipv4.dns", str(settings["dns"]),
            "ipv4.ignore-auto-dns", "yes" if settings["dns"] else "no",
        ]
        new_url = "http://{}:{}/".format(settings["ip"], LISTEN_PORT)

    code, out = run_cmd(args, 12)
    if code != 0:
        return False, out or "nmcli connection modify failed", ""

    # Reply to the browser first; bringing the profile up can immediately change the IP.
    def activate() -> None:
        time.sleep(1.5)
        run_cmd(["nmcli", "connection", "up", connection, "ifname", iface], 30)

    threading.Thread(target=activate, daemon=True).start()
    return True, "Сетевые настройки сохранены. Ethernet будет переподключён через несколько секунд.", new_url


def wifi_interfaces() -> list[str]:
    base = Path("/sys/class/net")
    if not base.exists():
        return []
    return sorted(
        item.name for item in base.iterdir()
        if item.name.startswith(("wl", "wlan"))
    )


def wifi_state() -> dict[str, Any]:
    interfaces = wifi_interfaces()
    iface = interfaces[0] if interfaces else ""
    result: dict[str, Any] = {
        "available": _nmcli_available() and bool(iface),
        "interfaces": interfaces,
        "interface": iface,
        "connected": False,
        "ssid": "",
        "address": "",
        "signal": 0,
        "connection": "",
        "setup_ap": False,
        "ethernet_metric": 100,
        "wifi_metric": 600,
    }
    if not result["available"]:
        return result

    code, out = run_cmd(["nmcli", "-g", "GENERAL.CONNECTION", "device", "show", iface], 5)
    if code == 0:
        conn = out.strip()
        if conn and conn not in ("--", "(null)"):
            result["connection"] = conn
            result["connected"] = True
            result["setup_ap"] = conn == SETUP_AP_CONNECTION

    code, out = run_cmd(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL", "device", "wifi", "list", "ifname", iface], 8)
    if code == 0:
        for line in out.splitlines():
            parts = line.split(":")
            if len(parts) >= 3 and parts[0].strip() == "*":
                result["ssid"] = parts[1].strip()
                try:
                    result["signal"] = int(parts[2])
                except ValueError:
                    pass
                break

    code, out = run_cmd(["ip", "-4", "-o", "addr", "show", "dev", iface, "scope", "global"], 5)
    if code == 0 and out:
        parts = out.split()
        if len(parts) >= 4:
            result["address"] = parts[3]

    if result["connection"]:
        _c, metric = run_cmd(["nmcli", "-g", "ipv4.route-metric", "connection", "show", result["connection"]], 5)
        try:
            result["wifi_metric"] = int(metric.strip()) if metric.strip() else 600
        except ValueError:
            pass

    for eth in ethernet_interfaces():
        conn, _detail = _nm_connection_for_interface(eth, False)
        if conn:
            _c, metric = run_cmd(["nmcli", "-g", "ipv4.route-metric", "connection", "show", conn], 5)
            try:
                result["ethernet_metric"] = int(metric.strip()) if metric.strip() else 100
            except ValueError:
                pass
            break
    return result


def wifi_scan(iface: str = "") -> list[dict[str, Any]]:
    interfaces = wifi_interfaces()
    iface = iface if iface in interfaces else (interfaces[0] if interfaces else "")
    if not iface:
        raise RuntimeError("Wi-Fi интерфейс не найден")
    if not _nmcli_available():
        raise RuntimeError("NetworkManager/nmcli не установлен")

    run_cmd(["nmcli", "radio", "wifi", "on"], 5)
    run_cmd(["nmcli", "device", "set", iface, "managed", "yes"], 5)
    run_cmd(["ip", "link", "set", iface, "up"], 5)
    run_cmd(["nmcli", "device", "wifi", "rescan", "ifname", iface], 12)
    time.sleep(1.0)

    code, out = run_cmd([
        "nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY,IN-USE",
        "device", "wifi", "list", "ifname", iface
    ], 12)
    if code != 0:
        raise RuntimeError(out or "Не удалось просканировать Wi-Fi")

    seen: dict[str, dict[str, Any]] = {}
    for line in out.splitlines():
        parts = line.split(":")
        if len(parts) < 4:
            continue
        ssid = parts[0].strip()
        if not ssid:
            continue
        try:
            signal = int(parts[1])
        except ValueError:
            signal = 0
        item = {
            "ssid": ssid,
            "signal": signal,
            "security": parts[2].strip(),
            "active": parts[3].strip() == "*",
        }
        old = seen.get(ssid)
        if old is None or signal > int(old.get("signal") or 0):
            seen[ssid] = item
    return sorted(seen.values(), key=lambda x: (-int(bool(x["active"])), -int(x["signal"]), x["ssid"].lower()))


def apply_route_metrics(ethernet_metric: int = 100, wifi_metric: int = 600) -> None:
    ethernet_metric = max(1, min(9999, int(ethernet_metric)))
    wifi_metric = max(1, min(9999, int(wifi_metric)))

    for eth in ethernet_interfaces():
        conn, _detail = _nm_connection_for_interface(eth, False)
        if conn:
            run_cmd(["nmcli", "connection", "modify", conn, "ipv4.route-metric", str(ethernet_metric)], 8)

    for wifi in wifi_interfaces():
        conn, _detail = _nm_connection_for_interface(wifi, False)
        if conn:
            run_cmd(["nmcli", "connection", "modify", conn, "ipv4.route-metric", str(wifi_metric)], 8)


def _wifi_connect_now(req: dict[str, Any]) -> tuple[bool, str]:
    if not _nmcli_available():
        return False, "NetworkManager/nmcli не установлен"

    interfaces = wifi_interfaces()
    iface = str(req.get("interface") or "").strip()
    if iface not in interfaces:
        iface = interfaces[0] if interfaces else ""
    if not iface:
        return False, "Wi-Fi интерфейс не найден"

    ssid = str(req.get("ssid") or "").strip()
    password = str(req.get("password") or "")
    if not ssid:
        return False, "SSID не задан"

    try:
        ethernet_metric = int(req.get("ethernet_metric") or 100)
        wifi_metric = int(req.get("wifi_metric") or 600)
    except (TypeError, ValueError):
        return False, "Некорректная метрика маршрута"

    # If the user opened this page through the temporary setup AP, wlan0 must
    # leave AP mode before it can associate with the selected Wi-Fi network.
    run_cmd(["nmcli", "connection", "down", SETUP_AP_CONNECTION], 10)
    run_cmd(["nmcli", "radio", "wifi", "on"], 5)
    run_cmd(["nmcli", "device", "set", iface, "managed", "yes"], 5)
    run_cmd(["ip", "link", "set", iface, "up"], 5)

    # Prefer an existing profile that actually belongs to the requested SSID.
    # NetworkManager often names duplicate profiles "SSID 1", "SSID 2", so
    # matching only connection NAME is not reliable.
    existing = ""
    code2, profiles = run_cmd(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], 8)
    if code2 == 0:
        for line in profiles.splitlines():
            if ":" not in line:
                continue
            name, typ = line.rsplit(":", 1)
            if typ not in ("802-11-wireless", "wifi") or name == SETUP_AP_CONNECTION:
                continue
            _c, profile_ssid = run_cmd(["nmcli", "-g", "802-11-wireless.ssid", "connection", "show", name], 5)
            if profile_ssid.strip() == ssid:
                existing = name
                break

    if existing:
        # The fallback AP deliberately disables Wi-Fi autoconnect. Re-enable
        # only the profile explicitly selected by the user.
        run_cmd(["nmcli", "connection", "modify", existing, "connection.autoconnect", "yes"], 8)
        if password:
            run_cmd(["nmcli", "connection", "modify", existing, "802-11-wireless-security.psk", password], 8)
        code, out = run_cmd(["nmcli", "connection", "up", existing, "ifname", iface], 35)
    else:
        args = ["nmcli", "device", "wifi", "connect", ssid, "ifname", iface]
        if password:
            args += ["password", password]
        code, out = run_cmd(args, 35)

    if code != 0:
        # Leave wlan0 free so the fallback watchdog can restore
        # RobotLiDAR-Setup immediately after the configuration marker is removed.
        run_cmd(["nmcli", "device", "disconnect", iface], 8)
        return False, out or "Не удалось подключиться к Wi-Fi"

    conn, _detail = _nm_connection_for_interface(iface, False)
    if conn:
        run_cmd(["nmcli", "connection", "modify", conn, "connection.autoconnect", "yes"], 8)

    apply_route_metrics(ethernet_metric, wifi_metric)
    return True, "Wi-Fi подключён. Ethernet будет основным при меньшей метрике."


def schedule_wifi_connect(req: dict[str, Any]) -> tuple[bool, str]:
    interfaces = wifi_interfaces()
    iface = str(req.get("interface") or "").strip()
    if iface not in interfaces:
        iface = interfaces[0] if interfaces else ""
    ssid = str(req.get("ssid") or "").strip()
    if not iface:
        return False, "Wi-Fi интерфейс не найден"
    if not ssid:
        return False, "SSID не задан"

    def worker() -> None:
        try:
            WIFI_CONFIG_MARKER.write_text(str(time.time()), encoding="ascii")
        except Exception:
            pass
        try:
            # Give the HTTP response time to reach a browser connected to the
            # temporary AP before wlan0 is switched into client mode.
            time.sleep(2.0)
            ok, message = _wifi_connect_now(req)
            print("WIFI CONFIG:", "OK" if ok else "ERROR", message, flush=True)
        finally:
            try:
                WIFI_CONFIG_MARKER.unlink()
            except Exception:
                pass

    threading.Thread(target=worker, name="wifi-config", daemon=True).start()
    return True, (
        "Настройки приняты. Через 2 секунды Orange Pi переключит wlan0 на сеть '{}'. "
        "Если подключение не получится, открытая точка RobotLiDAR-Setup появится снова автоматически."
    ).format(ssid)

def wifi_disconnect(req: dict[str, Any]) -> tuple[bool, str]:
    interfaces = wifi_interfaces()
    iface = str(req.get("interface") or "").strip()
    if iface not in interfaces:
        iface = interfaces[0] if interfaces else ""
    if not iface:
        return False, "Wi-Fi интерфейс не найден"
    code, out = run_cmd(["nmcli", "device", "disconnect", iface], 15)
    if code != 0 and "not active" not in out.lower() and "не актив" not in out.lower():
        return False, out or "Не удалось отключить Wi-Fi"
    return True, "Wi-Fi отключён"


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


def runtime_active_camera(cfg: dict[str, Any]) -> int:
    active = 2 if int(cfg.get("active_camera") or 1) == 2 else 1
    try:
        value = Path("/run/robotlidar-active-camera").read_text(encoding="ascii").strip()
        if value in ("1", "2"):
            active = int(value)
    except Exception:
        pass
    return active


def active_rtsp_url(cfg: dict[str, Any]) -> str:
    active = runtime_active_camera(cfg)
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


def save_software_home(cfg: dict[str, Any]) -> dict:
    status = get_ptz_status(
        active_rtsp_url(cfg),
        username=str(cfg.get("onvif_username") or ""),
        password=str(cfg.get("onvif_password") or ""),
        explicit_device_url=str(cfg.get("onvif_device_url") or ""),
    )
    pan = float(status["pan"])
    tilt = float(status["tilt"])
    if pan <= -0.999 and tilt <= -0.999:
        cfg["ptz_software_home_enabled"] = False
        save_config(cfg)
        raise RuntimeError("Камера возвращает фиктивные координаты PTZ (-1/-1); сохранение базовой позиции по GetStatus невозможно")
    cfg["ptz_software_home_pan"] = pan
    cfg["ptz_software_home_tilt"] = tilt
    cfg["ptz_software_home_enabled"] = True
    save_config(cfg)
    return {"pan": cfg["ptz_software_home_pan"], "tilt": cfg["ptz_software_home_tilt"]}


def return_to_software_home(cfg: dict[str, Any], speed: float = 0.30) -> dict:
    if not bool(cfg.get("ptz_software_home_enabled")):
        raise RuntimeError("Базовое положение ещё не сохранено")
    target_pan = float(cfg.get("ptz_software_home_pan"))
    target_tilt = float(cfg.get("ptz_software_home_tilt"))
    speed = max(0.10, min(0.60, float(speed)))
    tolerance = 0.035

    url, token_raw = onvif_runtime(cfg)
    _ = url
    token = escape(token_raw)
    username = str(cfg.get("onvif_username") or "")
    password = str(cfg.get("onvif_password") or "")
    explicit = str(cfg.get("onvif_device_url") or "")
    rtsp = active_rtsp_url(cfg)

    for _step in range(28):
        status = get_ptz_status(rtsp, username=username, password=password, explicit_device_url=explicit)
        pan = float(status["pan"])
        tilt = float(status["tilt"])
        ep = target_pan - pan
        et = target_tilt - tilt
        if abs(ep) <= tolerance and abs(et) <= tolerance:
            return {"ok": True, "pan": pan, "tilt": tilt, "target_pan": target_pan, "target_tilt": target_tilt}

        vx = 0.0 if abs(ep) <= tolerance else (speed if ep > 0 else -speed)
        vy = 0.0 if abs(et) <= tolerance else (speed if et > 0 else -speed)
        move = f'''<tptz:ContinuousMove><tptz:ProfileToken>{token}</tptz:ProfileToken><tptz:Velocity><tt:PanTilt x="{vx:.3f}" y="{vy:.3f}"/></tptz:Velocity></tptz:ContinuousMove>'''
        stop = f'''<tptz:Stop><tptz:ProfileToken>{token}</tptz:ProfileToken><tptz:PanTilt>true</tptz:PanTilt><tptz:Zoom>true</tptz:Zoom></tptz:Stop>'''
        onvif_post(cfg, move)
        time.sleep(0.12)
        onvif_post(cfg, stop)
        time.sleep(0.08)

    status = get_ptz_status(rtsp, username=username, password=password, explicit_device_url=explicit)
    raise RuntimeError(
        "Не удалось точно вернуться в базовое положение: pan={:.3f}, tilt={:.3f}".format(
            float(status["pan"]), float(status["tilt"])
        )
    )


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
<div class="row"><label>Скорость PTZ <input id="speed" type="range" min="10" max="100" value="35"></label><span id="speedText">35%</span><button class="btn" onclick="reloadPreview()">Обновить видео</button></div><div class="row" style="margin-top:10px"><button class="btn" onclick="saveHome()">Запомнить базовое положение</button><button class="btn" onclick="goHome()">Вернуться в базовое</button><span id="homeState" class="status"></span></div>
<h3>PTZ журнал</h3><div id="ptzlog" class="log"></div>
</div></div></div>
<script>
const $=id=>document.getElementById(id);function addLog(s){const b=$('ptzlog');b.textContent=new Date().toLocaleTimeString()+' '+s+'\n'+b.textContent}
$('speed').oninput=()=>{$('speedText').textContent=$('speed').value+'%'};
async function init(){const r=await fetch('/api/status');const d=await r.json();const c=d.config||{};const en=c.local_preview_enabled!==false;$('enabled').style.display=en?'block':'none';$('disabled').style.display=en?'none':'block';$('state').textContent=(c.camera1_name||'Camera 1')+' · local preview '+(en?'ON':'OFF')+(c.ptz_enabled===false?' · PTZ OFF':' · PTZ ON');if(en)reloadPreview()}
function reloadPreview(){const img=$('cam');img.src='/api/preview.mjpg?t='+Date.now()}
async function ptz(dir){try{const speed=Number($('speed').value)/100;const r=await fetch('/api/local-ptz',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({direction:dir,speed})});const d=await r.json();if(!r.ok)throw new Error(d.detail||'PTZ error');addLog('OK '+(d.message||dir))}catch(e){addLog('ERR '+e.message);if(dir==='home'&&String(e.message).includes('не поддерживается')){const b=$('homeBtn');if(b){b.disabled=true;b.title='Home не поддерживается камерой';b.style.opacity='.45'}}}}
async function saveHome(){try{const r=await fetch('/api/software-home/save',{method:'POST'});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Ошибка');$('homeState').textContent='База: '+Number(d.pan).toFixed(3)+' / '+Number(d.tilt).toFixed(3);addLog('BASE SAVED '+$('homeState').textContent)}catch(e){addLog('ERR '+e.message)}}
async function goHome(){try{$('homeState').textContent='Возврат...';const speed=Number($('speed').value)/100;const r=await fetch('/api/software-home/goto',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({speed})});const d=await r.json();if(!r.ok)throw new Error(d.detail||'Ошибка');$('homeState').textContent='В базовом положении';addLog('BASE OK pan='+Number(d.pan).toFixed(3)+' tilt='+Number(d.tilt).toFixed(3))}catch(e){$('homeState').textContent='Ошибка возврата';addLog('ERR '+e.message)}}
init();
</script></body></html>'''



HTML = r'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>RobotLiDAR · Orange Pi Zero Camera</title>
<style>
:root{font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#15202b;background:#eef2f6}*{box-sizing:border-box}body{margin:0}.wrap{max-width:1100px;margin:auto;padding:20px}.top{display:flex;justify-content:space-between;align-items:center;gap:15px;margin-bottom:18px}.brand h1{margin:0;font-size:24px}.muted{color:#687684;font-size:13px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.card{background:#fff;border-radius:14px;padding:18px;box-shadow:0 4px 20px #0000000c}.card h2{margin:0 0 14px;font-size:18px}.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}label{display:block;font-size:13px;color:#53606d;margin:10px 0 5px}input,select{width:100%;padding:10px 11px;border:1px solid #ccd5df;border-radius:8px;font-size:14px;background:#fff}button{border:0;border-radius:8px;padding:10px 14px;font-weight:600;cursor:pointer}.primary{background:#1769e0;color:#fff}.secondary{background:#e8eef6;color:#213044}.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}.status{display:inline-flex;align-items:center;gap:7px;padding:7px 10px;border-radius:20px;background:#eef2f6;font-size:13px}.dot{width:9px;height:9px;border-radius:50%;background:#9aa6b2}.dot.ok{background:#20a66a}.net{padding:11px;border:1px solid #dde4eb;border-radius:9px;background:#f9fbfd}.msg{margin-top:10px;white-space:pre-wrap;font-size:13px}.oktxt{color:#168252}.errtxt{color:#b62f2f}.scanTable{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}.scanTable th,.scanTable td{text-align:left;padding:9px;border-bottom:1px solid #e4e9ef;vertical-align:top}.scanTable th{color:#53606d}.pill{display:inline-block;background:#eef2f6;border-radius:12px;padding:3px 7px;margin:2px}.url{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;word-break:break-all}.logbox{margin:12px 0 0;background:#111820;color:#d8e2ec;border-radius:10px;padding:12px;height:260px;overflow:auto;font:12px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;word-break:break-word}.logmeta{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.logmeta label{margin:0}.logmeta input{width:auto}@media(max-width:760px){.grid,.row{grid-template-columns:1fr}.top{align-items:flex-start;flex-direction:column}.scanTable{display:block;overflow:auto}}</style></head>
<body><div class="wrap">
<div class="top"><div class="brand"><h1>Orange Pi Zero · Camera + PTZ</h1><div class="muted">Локальная настройка RobotLiDAR через Ethernet</div></div><div id="svc" class="status"><span class="dot"></span><span>проверка...</span></div></div>
<div class="grid">
<section class="card"><h2>Ethernet / IP</h2><div class="net"><strong>Текущее подключение</strong><div id="ethernet" class="muted" style="margin-top:6px">Определение адреса...</div></div><div class="row"><div><label>Интерфейс</label><select id="network_interface"></select></div><div><label>Режим IPv4</label><select id="network_mode" onchange="toggleNetworkFields()"><option value="dhcp">DHCP (автоматически)</option><option value="static">Статический IP</option></select></div></div><div id="staticNetwork"><div class="row"><div><label>IP адрес</label><input id="network_ip" placeholder="192.168.1.75"></div><div><label>Префикс</label><input id="network_prefix" type="number" min="1" max="32" value="24"></div></div><div class="row"><div><label>Шлюз</label><input id="network_gateway" placeholder="192.168.1.1"></div><div><label>DNS</label><input id="network_dns" placeholder="8.8.8.8 1.1.1.1"></div></div></div><div class="actions"><button class="primary" onclick="applyNetwork()">Применить сеть</button></div><div id="networkMsg" class="msg"></div><p class="muted">DHCP повторно запрашивает адрес при появлении роутера. При смене на статический IP текущая страница отключится — откройте панель уже по новому адресу.</p></section>
<section class="card"><h2>Сервер и устройство</h2><label>Device ID</label><input id="device_id"><label>Название</label><input id="device_name"><label>Адрес центрального сервера</label><input id="server_url"><label><input id="stream_enabled" type="checkbox" style="width:auto"> Транслировать видео на центральный сервер</label><label><input id="local_preview_enabled" type="checkbox" style="width:auto"> Разрешить локальный просмотр камеры</label><p class="muted">Обе функции независимы: можно отдельно включать SRT на сервер и локальный просмотр. ONVIF/PTZ работают независимо от них.</p><div class="row"><div><label>SRT latency, мс</label><input id="srt_latency_ms" type="number"></div><div><label>Telemetry, сек</label><input id="telemetry_period_sec" type="number" step="0.5"></div></div></section>
<section class="card"><h2>Камеры H.264</h2><label>Источник</label><select id="input_mode"><option value="rtsp">RTSP H.264 (copy)</option><option value="v4l2_h264">USB H.264</option><option value="v4l2_encode">USB + encode</option><option value="test">Тестовая картинка</option></select><div class="row"><div><label>Имя Camera 1</label><input id="camera1_name" placeholder="Передняя"></div><div><label>Имя Camera 2</label><input id="camera2_name" placeholder="Задняя"></div></div><label>RTSP Camera 1</label><input id="camera1_url" placeholder="rtsp://192.168.1.149:554/stream1"><label>RTSP Camera 2</label><input id="camera2_url" placeholder="rtsp://192.168.1.150:554/stream1"><div class="row"><div><label>Активная камера при запуске</label><select id="active_camera"><option value="1">Camera 1</option><option value="2">Camera 2</option></select></div><div><label>Основная камера для автовозврата</label><select id="primary_camera"><option value="1">Camera 1</option><option value="2">Camera 2</option></select></div></div><label><input id="auto_failover_enabled" type="checkbox" style="width:auto"> Автоматически переключаться на вторую камеру при отказе</label><div class="row"><div><label>Ошибок до переключения</label><input id="failover_after_failures" type="number" min="1" max="20"></div><div><label>Проверка основной, сек</label><input id="failover_probe_interval_sec" type="number" min="3" max="300" step="1"></div></div><label><input id="return_to_primary" type="checkbox" style="width:auto"> Автоматически вернуться на основную камеру после восстановления</label><hr style="border:0;border-top:1px solid #e4e9ef;margin:16px 0"><label><input id="video_watchdog_enabled" type="checkbox" style="width:auto"> Watchdog зависшего RTSP-видео</label><div class="row"><div><label>Нет видеопрогресса, сек</label><input id="video_watchdog_timeout_sec" type="number" min="3" max="120" step="1"></div><div><label>Задержка после запуска, сек</label><input id="video_watchdog_startup_grace_sec" type="number" min="3" max="120" step="1"></div></div><p class="muted">Если RTSP-соединение формально осталось открытым, но FFmpeg перестал получать/передавать видеоданные, watchdog завершит зависший FFmpeg. После этого сработает обычный failover/backoff.</p><div id="cameraRuntime" class="net" style="margin-top:10px"><strong>Текущая камера:</strong> определение...</div><input id="input_url" type="hidden"><label>V4L2 устройство</label><input id="video_device"><div class="row"><div><label>Ширина</label><input id="width" type="number"></div><div><label>Высота</label><input id="height" type="number"></div><div><label>FPS</label><input id="fps" type="number"></div><div><label>Битрейт, kbps</label><input id="bitrate_kbps" type="number"></div></div><label>Encoder</label><input id="encoder"><p class="muted">В режиме failover после заданного числа подряд быстрых ошибок FFmpeg проверяется резервная камера. Если она доступна — поток переключается на неё. При включённом возврате основная камера периодически проверяется и после восстановления снова становится активной.</p></section>
<section class="card"><h2>ONVIF / PTZ</h2><label><input id="ptz_enabled" type="checkbox" style="width:auto"> PTZ включён</label><label><input id="onvif_auto_discovery" type="checkbox" style="width:auto"> Автоопределение ONVIF</label><label>ONVIF Device URL (необязательно)</label><input id="onvif_device_url"><label>ONVIF PTZ URL (необязательно)</label><input id="onvif_url"><div class="row"><div><label>Логин камеры</label><input id="onvif_username"></div><div><label>Пароль камеры</label><input id="onvif_password" type="password" placeholder="Оставьте пустым, чтобы не менять"></div></div><label>Profile Token (необязательно)</label><input id="onvif_profile_token"><div class="actions"><button class="secondary" onclick="probeOnvif()">Проверить возможности ONVIF</button></div><pre id="onvifDiag" class="logbox" style="height:220px">Диагностика ещё не запускалась.</pre></section>
</div>
<section class="card" style="margin-top:16px"><h2>Поиск RTSP / ONVIF камер</h2><p class="muted">Сканируется локальный проводной сегмент. Проверяются RTSP-порты 554, 8554, 10554 и типовые ONVIF HTTP-порты. Сканируйте только сеть, которой вы управляете или имеете разрешение проверять.</p><div class="actions"><button id="scanBtn" class="primary" onclick="scanCameras()">Сканировать сеть</button></div><div id="scanMsg" class="msg"></div><div id="scanResults"></div></section>
<section class="card" style="margin-top:16px"><h2>Журнал трансляции / ONVIF / PTZ</h2><div class="logmeta"><button class="secondary" onclick="setLogKind('all')">Все</button><button class="secondary" onclick="setLogKind('video')">Видео / SRT</button><button class="secondary" onclick="setLogKind('ptz')">ONVIF / PTZ</button><button class="secondary" onclick="loadLog()">Обновить</button><label><input id="logAuto" type="checkbox" style="width:auto" checked> авто 3 сек</label><span id="logInfo" class="muted"></span></div><pre id="streamLog" class="logbox">Загрузка журнала...</pre><p class="muted">Показываются только последние строки systemd-журнала сервиса трансляции. Отдельный лог-файл не создаётся, поэтому лишней записи на SD-карту нет.</p></section>
<section class="card" style="margin-top:16px"><h2>Применение</h2><div class="actions"><button class="secondary" onclick="location.href='/camera'">Локальная камера / PTZ</button><button class="primary" onclick="saveConfig(true)">Сохранить и перезапустить</button><button class="secondary" onclick="saveConfig(false)">Только сохранить</button><button class="secondary" onclick="restartService()">Перезапустить трансляцию</button></div><div id="saveMsg" class="msg"></div></section>
</div><script>
const $=id=>document.getElementById(id);let cfg={};
async function api(url,opt={}){const r=await fetch(url,opt);let d={};try{d=await r.json()}catch{}if(!r.ok)throw new Error(d.detail||`HTTP ${r.status}`);return d}
function setValue(k,v){const e=$(k);if(!e)return;if(e.type==='checkbox')e.checked=!!v;else e.value=v??''}
function toggleNetworkFields(){const box=$('staticNetwork');if(box)box.style.display=$('network_mode').value==='static'?'block':'none'}
async function load(){try{
 const d=await api('/api/status');cfg=d.config||{};
 if(!Object.prototype.hasOwnProperty.call(cfg,'stream_enabled'))cfg.stream_enabled=true;
 if(!Object.prototype.hasOwnProperty.call(cfg,'local_preview_enabled'))cfg.local_preview_enabled=true;
 if(!Object.prototype.hasOwnProperty.call(cfg,'auto_failover_enabled'))cfg.auto_failover_enabled=false;
 if(!Object.prototype.hasOwnProperty.call(cfg,'primary_camera'))cfg.primary_camera=1;
 if(!Object.prototype.hasOwnProperty.call(cfg,'failover_after_failures'))cfg.failover_after_failures=3;
 if(!Object.prototype.hasOwnProperty.call(cfg,'failover_probe_interval_sec'))cfg.failover_probe_interval_sec=10;
 if(!Object.prototype.hasOwnProperty.call(cfg,'return_to_primary'))cfg.return_to_primary=true;
 if(!Object.prototype.hasOwnProperty.call(cfg,'video_watchdog_enabled'))cfg.video_watchdog_enabled=true;
 if(!Object.prototype.hasOwnProperty.call(cfg,'video_watchdog_timeout_sec'))cfg.video_watchdog_timeout_sec=8;
 if(!Object.prototype.hasOwnProperty.call(cfg,'video_watchdog_startup_grace_sec'))cfg.video_watchdog_startup_grace_sec=12;
 Object.entries(cfg).forEach(([k,v])=>setValue(k,v));
 if(d.active_camera_runtime){const n=Number(d.active_camera_runtime);$('cameraRuntime').innerHTML='<strong>Текущая камера:</strong> Camera '+n+(n===Number(cfg.primary_camera||1)?' · основная':' · резервная / ручная')}
 $('ethernet').textContent=(d.ethernet||[]).map(x=>`${x.interface}: ${x.address}`).join(' · ')||'Проводной IPv4 адрес не определён';
 const n=d.network||{}, saved=cfg.network||{};
 const sel=$('network_interface');sel.innerHTML='';
 for(const name of (n.interfaces||[])){const o=document.createElement('option');o.value=name;o.textContent=name;sel.appendChild(o)}
 if(saved.interface||n.interface)sel.value=saved.interface||n.interface;
 $('network_mode').value=saved.mode||n.mode||'dhcp';
 $('network_ip').value=saved.ip||n.ip||'';
 $('network_prefix').value=saved.prefix||n.prefix||24;
 $('network_gateway').value=saved.gateway||n.gateway||'';
 $('network_dns').value=saved.dns||n.dns||'';
 toggleNetworkFields();
 if(!n.available){$('networkMsg').className='msg errtxt';$('networkMsg').textContent='NetworkManager/nmcli не найден: изменение IP из панели недоступно.'}
 const st=d.streamer||{};$('svc').innerHTML=`<span class="dot ${st.active?'ok':''}"></span><span>${cfg.stream_enabled===false?'SRT выключен · PTZ доступен':'трансляция: '+(st.state||'unknown')}</span>`
}catch(e){$('saveMsg').className='msg errtxt';$('saveMsg').textContent=e.message}}
async function applyNetwork(){const m=$('networkMsg');m.className='msg';m.textContent='Сохранение сети...';const network={mode:$('network_mode').value,interface:$('network_interface').value,ip:$('network_ip').value.trim(),prefix:Number($('network_prefix').value||24),gateway:$('network_gateway').value.trim(),dns:$('network_dns').value.trim()};try{const d=await api('/api/network',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(network)});cfg.network=network;m.className='msg oktxt';m.textContent=(d.message||'Сеть сохранена')+(d.new_url?' Новый адрес панели: '+d.new_url:'')}catch(e){m.className='msg errtxt';m.textContent=e.message}}
async function scanWifi(){const m=$('wifiMsg'),sel=$('wifi_ssid');m.className='msg';m.textContent='Сканирование Wi-Fi...';try{const d=await api('/api/wifi-scan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({interface:$('wifi_interface').value})});sel.innerHTML='';for(const x of (d.networks||[])){const o=document.createElement('option');o.value=x.ssid;o.textContent=x.ssid+' · '+x.signal+'%'+(x.security?' · '+x.security:'')+(x.active?' · подключено':'');sel.appendChild(o)}if(!(d.networks||[]).length){const o=document.createElement('option');o.value='';o.textContent='Сети не найдены';sel.appendChild(o)}m.className='msg oktxt';m.textContent='Найдено сетей: '+(d.networks||[]).length}catch(e){m.className='msg errtxt';m.textContent=e.message}}
async function connectWifi(){const m=$('wifiMsg');const manual=$('wifi_ssid_manual').value.trim();const body={interface:$('wifi_interface').value,ssid:manual||$('wifi_ssid').value,password:$('wifi_password').value,ethernet_metric:Number($('ethernet_metric').value||100),wifi_metric:Number($('wifi_metric').value||600)};if(!body.ssid){m.className='msg errtxt';m.textContent='Выберите Wi-Fi сеть';return}m.className='msg';m.textContent='Подключение...';try{const d=await api('/api/wifi-connect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});$('wifi_password').value='';m.className='msg oktxt';m.textContent=(d.message||'Подключение запланировано')+' После переключения подключите телефон/ноутбук к выбранной сети и откройте Orange Pi по её новому IP.'}catch(e){m.className='msg errtxt';m.textContent=e.message}}
async function disconnectWifi(){const m=$('wifiMsg');try{const d=await api('/api/wifi-disconnect',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({interface:$('wifi_interface').value})});m.className='msg oktxt';m.textContent=d.message||'Wi-Fi отключён';await load()}catch(e){m.className='msg errtxt';m.textContent=e.message}}
function collect(){const keys=['device_id','device_name','server_url','input_mode','input_url','camera1_name','camera1_url','camera2_name','camera2_url','video_device','encoder','onvif_device_url','onvif_url','onvif_username','onvif_profile_token'];const nums=['width','height','fps','bitrate_kbps','srt_latency_ms','telemetry_period_sec','active_camera','primary_camera','failover_after_failures','failover_probe_interval_sec','video_watchdog_timeout_sec','video_watchdog_startup_grace_sec'];const out={...cfg};keys.forEach(k=>out[k]=$(k).value);nums.forEach(k=>out[k]=Number($(k).value));if(!out.camera1_url)out.camera1_url=out.input_url;out.input_url=out.camera1_url;out.stream_enabled=$('stream_enabled').checked;out.local_preview_enabled=$('local_preview_enabled').checked;out.auto_failover_enabled=$('auto_failover_enabled').checked;out.return_to_primary=$('return_to_primary').checked;out.video_watchdog_enabled=$('video_watchdog_enabled').checked;out.ptz_enabled=$('ptz_enabled').checked;out.onvif_auto_discovery=$('onvif_auto_discovery').checked;const p=$('onvif_password').value;if(p)out.onvif_password=p;return out}
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
    server_version = "RobotLiDAROrangePiWeb/2.1"

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
            self.send_json(200, {"ok": True, "config": cfg, "active_camera_runtime": runtime_active_camera(cfg), "ethernet": ethernet_info(), "network": network_state(cfg), "streamer": service_state(STREAM_SERVICE)})
            return
        self.send_json(404, {"detail": "Not found"})

    def do_POST(self) -> None:
        try:
            if self.path == "/api/software-home/save":
                cfg = load_config()
                try:
                    result = save_software_home(cfg)
                    self.send_json(200, {"ok": True, **result})
                except Exception as exc:
                    self.send_json(500, {"detail": str(exc)})
                return
            if self.path == "/api/software-home/goto":
                req = self.read_json()
                cfg = load_config()
                try:
                    result = return_to_software_home(cfg, float(req.get("speed") or 0.30))
                    self.send_json(200, result)
                except Exception as exc:
                    self.send_json(500, {"detail": str(exc)})
                return
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
            if self.path in ("/api/wifi-scan", "/api/wifi-connect", "/api/wifi-disconnect"):
                self.send_json(410, {"detail": "Wi-Fi отключён. Устройство работает только по Ethernet."})
                return
            if self.path == "/api/network":
                req = self.read_json()
                try:
                    settings = validate_network_settings(req)
                except (ValueError, TypeError) as exc:
                    self.send_json(400, {"detail": str(exc)})
                    return
                cfg = load_config()
                cfg["network"] = settings
                save_config(cfg)
                ok, message, new_url = apply_network_settings(settings)
                if not ok:
                    self.send_json(500, {"detail": message})
                    return
                self.send_json(200, {"ok": True, "message": message, "new_url": new_url})
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
    print(f"Orange Pi Zero web config: http://{LISTEN_HOST}:{LISTEN_PORT}/", flush=True)
    ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
