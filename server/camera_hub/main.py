from __future__ import annotations

import asyncio
import socket
import struct
import time
from dataclasses import dataclass, asdict, field
from typing import Dict, Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

app = FastAPI(title="RobotLiDAR Camera Hub", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

OFFLINE_AFTER_SEC = 5.0
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
    rtp_port: int = 5004
    ptz_port: int = 6000
    last_seen: float = field(default_factory=time.time)
    telemetry: Dict[str, Any] = field(default_factory=dict)

    @property
    def online(self) -> bool:
        return (time.time() - self.last_seen) <= OFFLINE_AFTER_SEC

    def json(self) -> Dict[str, Any]:
        data = asdict(self)
        data["online"] = self.online
        data["last_seen_age_ms"] = int((time.time() - self.last_seen) * 1000)
        return data


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


devices: Dict[str, Device] = {}
ws_clients: set[WebSocket] = set()
sequence = 0
udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def build_ptz_packet(*, pan: int = 0, tilt: int = 0, speed: int = 6000, flags: int = 0) -> bytes:
    global sequence
    sequence = (sequence + 1) & 0xFFFFFFFF
    return struct.pack(
        "!HBBIhhHH",
        PTZ_MAGIC,
        PTZ_VERSION,
        PTZ_TYPE,
        sequence,
        pan,
        tilt,
        speed,
        flags,
    )


def get_device(device_id: str) -> Device:
    dev = devices.get(device_id)
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    return dev


async def broadcast_devices() -> None:
    if not ws_clients:
        return
    payload = {"type": "devices", "devices": [d.json() for d in devices.values()]}
    dead: list[WebSocket] = []
    for ws in ws_clients:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        ws_clients.discard(ws)


@app.get("/api/devices")
async def list_devices():
    return {"devices": [d.json() for d in devices.values()]}


@app.post("/api/devices/{device_id}/register")
async def register_device(device_id: str, req: RegisterRequest):
    dev = devices.get(device_id)
    if dev is None:
        dev = Device(
            device_id=device_id,
            name=req.name,
            ip=req.ip,
            rtp_port=req.rtp_port,
            ptz_port=req.ptz_port,
        )
        devices[device_id] = dev
    else:
        dev.name = req.name
        dev.ip = req.ip
        dev.rtp_port = req.rtp_port
        dev.ptz_port = req.ptz_port
        dev.last_seen = time.time()
    await broadcast_devices()
    return {"ok": True, "device": dev.json()}


@app.post("/api/devices/{device_id}/telemetry")
async def update_telemetry(device_id: str, req: TelemetryRequest):
    dev = get_device(device_id)
    dev.last_seen = time.time()
    dev.telemetry.update({k: v for k, v in req.model_dump().items() if v is not None})
    await broadcast_devices()
    return {"ok": True}


@app.post("/api/devices/{device_id}/ptz")
async def ptz(device_id: str, req: PtzRequest):
    dev = get_device(device_id)
    flags = FLAG_REQUEST_IDR if req.request_idr else 0
    packet = build_ptz_packet(
        pan=req.pan_cdeg,
        tilt=req.tilt_cdeg,
        speed=req.speed_cdeg_s,
        flags=flags,
    )
    udp_sock.sendto(packet, (dev.ip, dev.ptz_port))
    return {"ok": True}


@app.post("/api/devices/{device_id}/center")
async def center(device_id: str):
    dev = get_device(device_id)
    udp_sock.sendto(build_ptz_packet(flags=FLAG_CENTER), (dev.ip, dev.ptz_port))
    return {"ok": True}


@app.post("/api/devices/{device_id}/request-idr")
async def request_idr(device_id: str):
    dev = get_device(device_id)
    udp_sock.sendto(build_ptz_packet(flags=FLAG_REQUEST_IDR), (dev.ip, dev.ptz_port))
    return {"ok": True}


@app.websocket("/ws/devices")
async def devices_ws(ws: WebSocket):
    await ws.accept()
    ws_clients.add(ws)
    await ws.send_json({"type": "devices", "devices": [d.json() for d in devices.values()]})
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_clients.discard(ws)


@app.on_event("startup")
async def startup_task():
    async def ticker():
        while True:
            await broadcast_devices()
            await asyncio.sleep(1.0)

    asyncio.create_task(ticker())
