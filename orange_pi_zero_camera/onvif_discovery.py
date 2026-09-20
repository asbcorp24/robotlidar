from __future__ import annotations

import base64
import hashlib
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape


@dataclass
class OnvifDiscoveryResult:
    device_service_url: str
    ptz_url: str
    profile_token: str


def ws_security(username: str, password: str) -> str:
    if not username:
        return ""
    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    digest = hashlib.sha1(nonce + created.encode("utf-8") + password.encode("utf-8")).digest()
    return f'''<wsse:Security s:mustUnderstand="1"><wsse:UsernameToken><wsse:Username>{escape(username)}</wsse:Username><wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{base64.b64encode(digest).decode()}</wsse:Password><wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</wsse:Nonce><wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>'''


def soap_request(url: str, body: str, username: str, password: str, timeout: float = 3.0) -> bytes:
    security = ws_security(username, password)
    envelope = f'''<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"><s:Header>{security}</s:Header><s:Body>{body}</s:Body></s:Envelope>'''
    req = urllib.request.Request(url, data=envelope.encode("utf-8"), method="POST", headers={"Content-Type": "application/soap+xml; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def _first_text(root: ET.Element, local_name: str) -> str:
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == local_name and el.text:
            value = el.text.strip()
            if value:
                return value
    return ""


def _find_profile_token(root: ET.Element) -> str:
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] in ("Profiles", "Profile"):
            token = el.attrib.get("token") or el.attrib.get("Token")
            if token:
                return token
    text = ET.tostring(root, encoding="unicode")
    m = re.search(r'\btoken=["\']([^"\']+)["\']', text)
    return m.group(1) if m else ""


def _device_candidates(input_url: str, explicit_device_url: str = "") -> list[str]:
    candidates: list[str] = []
    if explicit_device_url:
        candidates.append(explicit_device_url)
    parsed = urllib.parse.urlparse(input_url)
    host = parsed.hostname or ""
    if not host:
        return candidates
    ports = []
    if parsed.port and parsed.port not in (554, 8554):
        ports.append(parsed.port)
    ports += [80, 8080, 8000, 8899]
    seen = set()
    for port in ports:
        netloc = host if port == 80 else f"{host}:{port}"
        for path in ("/onvif/device_service", "/onvif/device_service/", "/onvif/device"):
            url = f"http://{netloc}{path}"
            if url not in seen:
                seen.add(url)
                candidates.append(url)
    return candidates


def discover(input_url: str, username: str = "", password: str = "", explicit_device_url: str = "") -> OnvifDiscoveryResult:
    last_error: Exception | None = None
    for device_url in _device_candidates(input_url, explicit_device_url):
        try:
            capabilities_body = '''<tds:GetCapabilities xmlns:tds="http://www.onvif.org/ver10/device/wsdl"><tds:Category>All</tds:Category></tds:GetCapabilities>'''
            caps_raw = soap_request(device_url, capabilities_body, username, password)
            caps_root = ET.fromstring(caps_raw)
            ptz_url = ""
            media_url = ""
            for el in caps_root.iter():
                local = el.tag.rsplit("}", 1)[-1]
                if local == "PTZ":
                    for child in el.iter():
                        if child.tag.rsplit("}", 1)[-1] == "XAddr" and child.text:
                            ptz_url = child.text.strip()
                            break
                elif local == "Media":
                    for child in el.iter():
                        if child.tag.rsplit("}", 1)[-1] == "XAddr" and child.text:
                            media_url = child.text.strip()
                            break
                if ptz_url and media_url:
                    break
            if not ptz_url:
                raise RuntimeError("PTZ XAddr not found in GetCapabilities")
            if not media_url:
                media_url = device_url

            profiles_body = '''<trt:GetProfiles xmlns:trt="http://www.onvif.org/ver10/media/wsdl"/>'''
            profiles_raw = soap_request(media_url, profiles_body, username, password)
            profile_root = ET.fromstring(profiles_raw)
            profile_token = _find_profile_token(profile_root)
            if not profile_token:
                raise RuntimeError("ProfileToken not found in GetProfiles")
            return OnvifDiscoveryResult(device_service_url=device_url, ptz_url=ptz_url, profile_token=profile_token)
        except Exception as exc:
            last_error = exc
    if last_error:
        raise RuntimeError(f"ONVIF auto-discovery failed: {last_error}") from last_error
    raise RuntimeError("ONVIF auto-discovery failed: no camera host/candidates")


def _local(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _xml_summary(raw: bytes) -> dict:
    root = ET.fromstring(raw)
    result: dict = {"root": _local(root)}
    fault = ""
    reason = ""
    for el in root.iter():
        name = _local(el)
        text = (el.text or "").strip()
        if name in ("Value", "Text") and text:
            if "Fault" in [_local(x) for x in list(root.iter())[:3]]:
                fault = fault or text
        if name == "Text" and text:
            reason = text
    if reason:
        result["reason"] = reason
    return result


def diagnose_ptz(input_url: str, username: str = "", password: str = "", explicit_device_url: str = "") -> dict:
    discovery = discover(input_url, username=username, password=password, explicit_device_url=explicit_device_url)
    ptz_url = discovery.ptz_url
    token = discovery.profile_token

    def call(name: str, body: str) -> dict:
        try:
            raw = soap_request(ptz_url, body, username, password, timeout=3.5)
            root = ET.fromstring(raw)
            out = {"supported": True, "http_ok": True}
            if name == "GetStatus":
                for el in root.iter():
                    lname = _local(el)
                    if lname == "PanTilt":
                        if "x" in el.attrib: out["pan_x"] = el.attrib.get("x")
                        if "y" in el.attrib: out["tilt_y"] = el.attrib.get("y")
                    elif lname == "Zoom" and "x" in el.attrib:
                        out["zoom_x"] = el.attrib.get("x")
                    elif lname == "MoveStatus":
                        values = {}
                        for child in el.iter():
                            if child is el:
                                continue
                            txt = (child.text or "").strip()
                            if txt:
                                values[_local(child)] = txt
                        if values:
                            out["move_status"] = values
            elif name == "GetConfigurations":
                configs = []
                for el in root.iter():
                    if _local(el) in ("PTZConfiguration", "Configurations"):
                        token_attr = el.attrib.get("token") or el.attrib.get("Token") or ""
                        if token_attr:
                            item = {"token": token_attr}
                            name_el = next((x for x in el.iter() if _local(x) == "Name" and (x.text or "").strip()), None)
                            if name_el is not None:
                                item["name"] = name_el.text.strip()
                            configs.append(item)
                out["configurations"] = configs
            elif name == "GetConfigurationOptions":
                spaces = []
                for el in root.iter():
                    lname = _local(el)
                    if lname.endswith("Space") or lname.endswith("Spaces"):
                        text = (el.text or "").strip()
                        if text and text.startswith("http"):
                            spaces.append(text)
                    if lname == "URI" and (el.text or "").strip():
                        spaces.append(el.text.strip())
                out["spaces"] = sorted(set(spaces))
            elif name == "GetPresets":
                presets = []
                for el in root.iter():
                    if _local(el) == "Preset":
                        item = {
                            "token": el.attrib.get("token") or el.attrib.get("Token") or ""
                        }
                        for child in el.iter():
                            if _local(child) == "Name" and (child.text or "").strip():
                                item["name"] = child.text.strip()
                                break
                        presets.append(item)
                out["presets"] = presets
            return out
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(1600).decode("utf-8", "ignore").replace("\n", " ").strip()
            except Exception:
                pass
            reason = "HTTP {}".format(exc.code)
            if "ServiceNotSupported" in detail or "Service Not Supported" in detail:
                reason = "ServiceNotSupported"
            elif "ActionNotSupported" in detail or "Action Not Supported" in detail:
                reason = "ActionNotSupported"
            return {"supported": False, "http_ok": False, "error": reason}
        except Exception as exc:
            return {"supported": False, "http_ok": False, "error": str(exc)[:220]}

    config_token = token
    configs_result = call(
        "GetConfigurations",
        '<tptz:GetConfigurations xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"/>'
    )
    if configs_result.get("configurations"):
        config_token = configs_result["configurations"][0].get("token") or token

    results = {
        "device_service_url": discovery.device_service_url,
        "ptz_url": ptz_url,
        "profile_token": token,
        "GetStatus": call(
            "GetStatus",
            '<tptz:GetStatus xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"><tptz:ProfileToken>{}</tptz:ProfileToken></tptz:GetStatus>'.format(escape(token))
        ),
        "GetConfigurations": configs_result,
        "GetConfigurationOptions": call(
            "GetConfigurationOptions",
            '<tptz:GetConfigurationOptions xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"><tptz:ConfigurationToken>{}</tptz:ConfigurationToken></tptz:GetConfigurationOptions>'.format(escape(config_token))
        ),
        "GetPresets": call(
            "GetPresets",
            '<tptz:GetPresets xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"><tptz:ProfileToken>{}</tptz:ProfileToken></tptz:GetPresets>'.format(escape(token))
        ),
    }
    return results
