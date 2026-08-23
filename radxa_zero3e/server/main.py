from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import socket
import sqlite3
import struct
import time
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

app = FastAPI(title="RobotLiDAR Camera Hub", version="0.3.0")
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
    video_packets: int = 0
    video_bytes: int = 0
    video_last_seen: float = 0.0

    @property
    def online(self) -> bool:
        return (time.time() - self.last_seen) <= OFFLINE_AFTER_SEC

    @property
    def video_online(self) -> bool:
        return self.video_last_seen > 0 and (time.time() - self.video_last_seen) <= 3.0

    def public_json(self, alias: str | None = None) -> Dict[str, Any]:
        t = self.telemetry
        return {
            "id": self.device_id,
            "device_id": self.device_id,
            "name": alias or self.name or self.device_id,
            "online": self.online,
            "video_online": self.video_online,
            "streamType": "webrtc",
            "streamUrl": f"/api/devices/{self.device_id}/webrtc",
            "pan": (t.get("pan_cdeg") or 0) / 100.0,
            "tilt": (t.get("tilt_cdeg") or 0) / 100.0,
            "fps": t.get("fps") or 0,
            "bitrateKbps": int((t.get("bitrate_bps") or 0) / 1000),
            "ethernet": f"{t.get('link_mbps')} Mbit/s" if t.get("link_mbps") else "—",
            "uptimeSec": int((t.get("uptime_ms") or 0) / 1000),
            "video_packets": self.video_packets,
            "video_bytes": self.video_bytes,
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


devices: Dict[str, Device] = {}
sessions: Dict[str, int] = {}
sequence = 0
udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
rtp_transports: Dict[str, asyncio.DatagramTransport] = {}


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
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
            """
        )


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
        row = conn.execute("SELECT id, username FROM users WHERE id=?", (user_id,)).fetchone()
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


class RtpIngestProtocol(asyncio.DatagramProtocol):
    def __init__(self, device_id: str):
        self.device_id = device_id

    def datagram_received(self, data: bytes, addr):
        dev = devices.get(self.device_id)
        if dev:
            dev.video_packets += 1
            dev.video_bytes += len(data)
            dev.video_last_seen = time.time()


def allocate_video_port() -> int:
    used = {d.video_ingest_port for d in devices.values()}
    port = VIDEO_PORT_BASE
    while port in used:
        port += 1
    return port


async def ensure_rtp_listener(dev: Device) -> None:
    if dev.device_id in rtp_transports:
        return
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: RtpIngestProtocol(dev.device_id),
        local_addr=("0.0.0.0", dev.video_ingest_port),
    )
    rtp_transports[dev.device_id] = transport
    print(f"RTP ingest {dev.device_id}: UDP 0.0.0.0:{dev.video_ingest_port}")


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
        row = conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE", (req.username.strip(),)).fetchone()
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
            "SELECT device_id, alias FROM user_devices WHERE user_id=? ORDER BY created_at",
            (user["id"],),
        ).fetchall()
    result = []
    for row in rows:
        dev = devices.get(row["device_id"])
        if dev:
            result.append(dev.public_json(row["alias"]))
        else:
            result.append({
                "id": row["device_id"], "device_id": row["device_id"],
                "name": row["alias"] or row["device_id"], "online": False,
                "video_online": False, "streamType": "webrtc",
                "streamUrl": f"/api/devices/{row['device_id']}/webrtc",
                "pan": 0, "tilt": 0, "fps": 0, "bitrateKbps": 0,
                "ethernet": "—", "uptimeSec": 0,
            })
    return {"devices": result}


@app.get("/api/settings/devices")
def settings_devices(user=Depends(current_user)):
    with db() as conn:
        rows = conn.execute(
            "SELECT device_id, alias, created_at FROM user_devices WHERE user_id=? ORDER BY created_at",
            (user["id"],),
        ).fetchall()
    return {"devices": [dict(r) for r in rows]}


@app.post("/api/settings/devices")
def attach_device(req: AttachDeviceRequest, user=Depends(current_user)):
    device_id = req.device_id.strip()
    if not device_id:
        raise HTTPException(status_code=400, detail="Device ID is required")
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
    if dev is None:
        dev = Device(device_id, req.name, req.ip, allocate_video_port(), req.ptz_port)
        devices[device_id] = dev
    else:
        dev.name = req.name
        dev.ip = req.ip
        dev.ptz_port = req.ptz_port
        dev.last_seen = time.time()
    await ensure_rtp_listener(dev)
    return {"ok": True, "device": asdict(dev), "video_ingest_port": dev.video_ingest_port}


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
    return {"device_id": device_id, "video_online": dev.video_online, "video_packets": dev.video_packets, "video_bytes": dev.video_bytes}


@app.post("/api/devices/{device_id}/ptz")
def ptz(device_id: str, req: PtzRequest, user=Depends(current_user)):
    dev, _ = owned_device(user["id"], device_id)
    flags = FLAG_REQUEST_IDR if req.request_idr else 0
    udp_sock.sendto(build_ptz_packet(req.pan_cdeg, req.tilt_cdeg, req.speed_cdeg_s, flags), (dev.ip, dev.ptz_port))
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


@app.on_event("shutdown")
async def shutdown_task():
    for transport in rtp_transports.values():
        transport.close()
    rtp_transports.clear()


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
