from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import socket
import sqlite3
import struct
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
DB_PATH = BASE_DIR / "camera_hub.db"
RELAY_URL = os.getenv("WEBRTC_RELAY_URL", "http://127.0.0.1:8090").rstrip("/")

app = FastAPI(title="RobotLiDAR Camera Hub", version="0.5.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

OFFLINE_AFTER_SEC = 5.0
VIDEO_PORT_BASE = 10000
PTZ_MAGIC = 0x5354
PTZ_VERSION = 1
PTZ_TYPE = 1
FLAG_CENTER = 1 << 0
FLAG_REQUEST_IDR = 1 << 1


@dataclass
class Device:
    device_id: str
    name: str
    ip: str
    video_ingest_port: int
    ptz_port: int = 6000
    last_seen: float = field(default_factory=time.time)
    telemetry: Dict[str, Any] = field(default_factory=dict)

    @property
    def online(self) -> bool:
        return time.time() - self.last_seen <= OFFLINE_AFTER_SEC

    def public_json(self, alias: str | None = None, relay_status: dict | None = None) -> Dict[str, Any]:
        t = self.telemetry
        rs = relay_status or {}
        return {
            "id": self.device_id,
            "device_id": self.device_id,
            "name": alias or self.name or self.device_id,
            "online": self.online,
            "video_online": bool(rs.get("video_online", False)),
            "streamType": "webrtc-h264-passthrough",
            "streamUrl": f"/api/devices/{self.device_id}/webrtc",
            "pan": (t.get("pan_cdeg") or 0) / 100.0,
            "tilt": (t.get("tilt_cdeg") or 0) / 100.0,
            "fps": t.get("fps") or 0,
            "bitrateKbps": int((t.get("bitrate_bps") or 0) / 1000),
            "ethernet": f"{t.get('link_mbps')} Mbit/s" if t.get("link_mbps") else "—",
            "uptimeSec": int((t.get("uptime_ms") or 0) / 1000),
            "video_packets": int(rs.get("packets", 0)),
            "video_bytes": int(rs.get("bytes", 0)),
            "viewers": int(rs.get("viewers", 0)),
        }


class RegisterRequest(BaseModel):
    name: str = "Radxa ZERO 3E"
    ip: str
    rtp_port: int = Field(5004, ge=1, le=65535)
    ptz_port: int = Field(6000, ge=1, le=65535)


class TelemetryRequest(BaseModel):
    fps: int | None = None
    bitrate_bps: int | None = None
    dropped_frames: int | None = None
    uptime_ms: int | None = None
    pan_cdeg: int | None = None
    tilt_cdeg: int | None = None
    link_mbps: int | None = None


class PtzRequest(BaseModel):
    pan_cdeg: int = Field(0, ge=-9000, le=9000)
    tilt_cdeg: int = Field(0, ge=-4500, le=4500)
    speed_cdeg_s: int = Field(6000, ge=1, le=30000)
    request_idr: bool = False


class AuthRequest(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=4, max_length=128)


class AttachDeviceRequest(BaseModel):
    device_id: str = Field(min_length=3, max_length=128)
    alias: str | None = Field(default=None, max_length=128)


class WebRtcOffer(BaseModel):
    sdp: str
    type: str = "offer"


devices: Dict[str, Device] = {}
sessions: Dict[str, int] = {}
sequence = 0
udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS user_devices (
            user_id INTEGER NOT NULL,
            device_id TEXT NOT NULL,
            alias TEXT,
            created_at INTEGER NOT NULL,
            PRIMARY KEY(user_id, device_id),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_device_one_owner ON user_devices(device_id);
        """)


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"pbkdf2_sha256$200000${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, rounds, salt_hex, digest_hex = encoded.split("$", 3)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def current_user(authorization: str | None = Header(default=None)) -> sqlite3.Row:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    token = authorization.split(None, 1)[1]
    user_id = sessions.get(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Session expired")
    with db() as conn:
        row = conn.execute("SELECT id,username FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="User not found")
    return row


def owned_device(user_id: int, device_id: str) -> tuple[Device, str | None]:
    with db() as conn:
        row = conn.execute(
            "SELECT alias FROM user_devices WHERE user_id=? AND device_id=?",
            (user_id, device_id),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=403, detail="This tractor is not linked to your account")
    dev = devices.get(device_id)
    if not dev:
        raise HTTPException(status_code=409, detail="Tractor is linked but currently offline")
    return dev, row["alias"]


def relay_call(method: str, path: str, payload: dict | None = None, timeout: float = 2.0) -> dict:
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(RELAY_URL + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("error", str(exc))
        except Exception:
            detail = str(exc)
        raise RuntimeError(detail) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"WebRTC relay unavailable: {exc}") from exc


def relay_all_status() -> dict[str, dict]:
    try:
        data = relay_call("GET", "/api/streams", timeout=0.8)
        return data.get("streams", {})
    except RuntimeError:
        return {}


def allocate_video_port() -> int:
    used = {d.video_ingest_port for d in devices.values()}
    for status in relay_all_status().values():
        try:
            used.add(int(status.get("rtp_port", 0)))
        except (TypeError, ValueError):
            pass
    port = VIDEO_PORT_BASE
    while port in used:
        port += 1
    return port


def build_ptz_packet(pan=0, tilt=0, speed=6000, flags=0) -> bytes:
    global sequence
    sequence = (sequence + 1) & 0xFFFFFFFF
    return struct.pack("!HBBIhhHH", PTZ_MAGIC, PTZ_VERSION, PTZ_TYPE, sequence, pan, tilt, speed, flags)


@app.post("/api/auth/register")
def auth_register(req: AuthRequest):
    with db() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)",
                (req.username.strip(), hash_password(req.password), int(time.time())),
            )
            user_id = cur.lastrowid
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Username already exists")
    token = secrets.token_urlsafe(32)
    sessions[token] = int(user_id)
    return {"token": token, "user": {"id": user_id, "username": req.username.strip()}}


@app.post("/api/auth/login")
def auth_login(req: AuthRequest):
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username=? COLLATE NOCASE",
            (req.username.strip(),),
        ).fetchone()
    if not row or not verify_password(req.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid login or password")
    token = secrets.token_urlsafe(32)
    sessions[token] = int(row["id"])
    return {"token": token, "user": {"id": row["id"], "username": row["username"]}}


@app.get("/api/auth/me")
def auth_me(user=Depends(current_user)):
    return {"id": user["id"], "username": user["username"]}


@app.post("/api/auth/logout")
def auth_logout(authorization: str | None = Header(default=None)):
    if authorization and authorization.lower().startswith("bearer "):
        sessions.pop(authorization.split(None, 1)[1], None)
    return {"ok": True}


@app.get("/api/devices")
def list_devices(user=Depends(current_user)):
    with db() as conn:
        rows = conn.execute(
            "SELECT device_id,alias FROM user_devices WHERE user_id=? ORDER BY created_at",
            (user["id"],),
        ).fetchall()
    relay_status = relay_all_status()
    result = []
    for row in rows:
        dev = devices.get(row["device_id"])
        if dev:
            result.append(dev.public_json(row["alias"], relay_status.get(dev.device_id)))
        else:
            rs = relay_status.get(row["device_id"], {})
            result.append({
                "id": row["device_id"],
                "device_id": row["device_id"],
                "name": row["alias"] or row["device_id"],
                "online": False,
                "video_online": bool(rs.get("video_online", False)),
                "streamType": "webrtc-h264-passthrough",
                "streamUrl": f"/api/devices/{row['device_id']}/webrtc",
                "pan": 0,
                "tilt": 0,
                "fps": 0,
                "bitrateKbps": 0,
                "ethernet": "—",
                "uptimeSec": 0,
                "video_packets": int(rs.get("packets", 0)),
                "video_bytes": int(rs.get("bytes", 0)),
                "viewers": int(rs.get("viewers", 0)),
            })
    return {"devices": result, "relay_online": bool(relay_status) or _relay_health()}


def _relay_health() -> bool:
    try:
        relay_call("GET", "/health", timeout=0.4)
        return True
    except RuntimeError:
        return False


@app.get("/api/settings/devices")
def settings_devices(user=Depends(current_user)):
    with db() as conn:
        rows = conn.execute(
            "SELECT device_id,alias,created_at FROM user_devices WHERE user_id=? ORDER BY created_at",
            (user["id"],),
        ).fetchall()
    return {"devices": [dict(r) for r in rows]}


@app.post("/api/settings/devices")
def attach_device(req: AttachDeviceRequest, user=Depends(current_user)):
    device_id = req.device_id.strip()
    with db() as conn:
        owner = conn.execute("SELECT user_id FROM user_devices WHERE device_id=?", (device_id,)).fetchone()
        if owner and owner["user_id"] != user["id"]:
            raise HTTPException(status_code=409, detail="This tractor ID is already linked to another account")
        conn.execute(
            "INSERT INTO user_devices(user_id,device_id,alias,created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id,device_id) DO UPDATE SET alias=excluded.alias",
            (user["id"], device_id, req.alias.strip() if req.alias else None, int(time.time())),
        )
    return {"ok": True, "device_id": device_id}


@app.delete("/api/settings/devices/{device_id}")
def detach_device(device_id: str, user=Depends(current_user)):
    with db() as conn:
        conn.execute("DELETE FROM user_devices WHERE user_id=? AND device_id=?", (user["id"], device_id))
    return {"ok": True}


@app.post("/api/devices/{device_id}/register")
async def register_device(device_id: str, req: RegisterRequest):
    dev = devices.get(device_id)
    created = False
    if dev is None:
        dev = Device(device_id, req.name, req.ip, allocate_video_port(), req.ptz_port)
        devices[device_id] = dev
        created = True
    else:
        dev.name = req.name
        dev.ip = req.ip
        dev.ptz_port = req.ptz_port
        dev.last_seen = time.time()

    try:
        status = await asyncio.to_thread(
            relay_call,
            "POST",
            f"/api/streams/{device_id}",
            {"rtp_port": dev.video_ingest_port},
            2.0,
        )
    except RuntimeError as exc:
        if created:
            devices.pop(device_id, None)
        raise HTTPException(status_code=503, detail=str(exc))

    return {
        "ok": True,
        "device": asdict(dev),
        "video_ingest_port": dev.video_ingest_port,
        "relay": status,
    }


@app.post("/api/devices/{device_id}/telemetry")
def update_telemetry(device_id: str, req: TelemetryRequest):
    dev = devices.get(device_id)
    if not dev:
        raise HTTPException(status_code=404, detail="Device not registered")
    dev.last_seen = time.time()
    dev.telemetry.update({k: v for k, v in req.model_dump().items() if v is not None})
    return {"ok": True}


@app.get("/api/devices/{device_id}/video-status")
def video_status(device_id: str, user=Depends(current_user)):
    dev, _ = owned_device(user["id"], device_id)
    try:
        status = relay_call("GET", f"/api/streams/{device_id}/status")
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    return {
        "device_id": device_id,
        "rtp_port": dev.video_ingest_port,
        **status,
    }


@app.post("/api/devices/{device_id}/webrtc")
async def webrtc(device_id: str, offer: WebRtcOffer, user=Depends(current_user)):
    owned_device(user["id"], device_id)
    try:
        status = await asyncio.to_thread(relay_call, "GET", f"/api/streams/{device_id}/status", None, 1.0)
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not status.get("video_online"):
        raise HTTPException(status_code=409, detail="Video stream is offline")

    try:
        answer = await asyncio.to_thread(
            relay_call,
            "POST",
            f"/api/streams/{device_id}/webrtc",
            {"sdp": offer.sdp, "type": offer.type},
            8.0,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return answer


@app.post("/api/devices/{device_id}/ptz")
def ptz(device_id: str, req: PtzRequest, user=Depends(current_user)):
    dev, _ = owned_device(user["id"], device_id)
    flags = FLAG_REQUEST_IDR if req.request_idr else 0
    udp_sock.sendto(
        build_ptz_packet(req.pan_cdeg, req.tilt_cdeg, req.speed_cdeg_s, flags),
        (dev.ip, dev.ptz_port),
    )
    return {"ok": True}


@app.post("/api/devices/{device_id}/center")
def center(device_id: str, user=Depends(current_user)):
    dev, _ = owned_device(user["id"], device_id)
    udp_sock.sendto(build_ptz_packet(flags=FLAG_CENTER), (dev.ip, dev.ptz_port))
    return {"ok": True}


@app.post("/api/devices/{device_id}/request-idr")
def request_idr(device_id: str, user=Depends(current_user)):
    dev, _ = owned_device(user["id"], device_id)
    udp_sock.sendto(build_ptz_packet(flags=FLAG_REQUEST_IDR), (dev.ip, dev.ptz_port))
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@app.on_event("startup")
async def startup_task():
    init_db()
    if not await asyncio.to_thread(_relay_health):
        print(f"WARNING: Pion WebRTC relay is not reachable at {RELAY_URL}")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
