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
            ptz_url = _first_text(caps_root, "XAddr")
            if not ptz_url:
                raise RuntimeError("PTZ XAddr not found in GetCapabilities")

            media_url = ""
            for el in caps_root.iter():
                if el.tag.rsplit("}", 1)[-1] == "Media":
                    for child in el.iter():
                        if child.tag.rsplit("}", 1)[-1] == "XAddr" and child.text:
                            media_url = child.text.strip()
                            break
                if media_url:
                    break
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
