from __future__ import annotations

import asyncio
import struct
from typing import Dict, Set

import av
from aiortc import VideoStreamTrack


class BrowserVideoTrack(VideoStreamTrack):
    def __init__(self, relay):
        super().__init__()
        self.relay = relay
        self.queue = asyncio.Queue(maxsize=2)
        relay.subscribers.add(self)

    def push(self, frame):
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self.queue.put_nowait(frame)
        except asyncio.QueueFull:
            pass

    async def recv(self):
        frame = await self.queue.get()
        pts, time_base = await self.next_timestamp()
        frame.pts = pts
        frame.time_base = time_base
        return frame

    def stop(self):
        self.relay.subscribers.discard(self)
        super().stop()


class H264RtpRelay:
    def __init__(self, device_id: str):
        self.device_id = device_id
        self.codec = av.CodecContext.create("h264", "r")
        self.access_unit = bytearray()
        self.fu_buffer = None
        self.subscribers: Set[BrowserVideoTrack] = set()
        self.decoded_frames = 0
        self.decode_errors = 0

    def new_track(self):
        return BrowserVideoTrack(self)

    def feed(self, packet: bytes):
        parsed = self._payload(packet)
        if not parsed:
            return
        payload, marker = parsed
        if not payload:
            return
        nal_type = payload[0] & 0x1F
        if 1 <= nal_type <= 23:
            self.access_unit += b"\x00\x00\x00\x01" + payload
        elif nal_type == 24:
            pos = 1
            while pos + 2 <= len(payload):
                size = struct.unpack_from("!H", payload, pos)[0]
                pos += 2
                if size <= 0 or pos + size > len(payload):
                    break
                self.access_unit += b"\x00\x00\x00\x01" + payload[pos:pos + size]
                pos += size
        elif nal_type == 28 and len(payload) >= 2:
            fu_indicator, fu_header = payload[0], payload[1]
            start = bool(fu_header & 0x80)
            end = bool(fu_header & 0x40)
            nal_header = bytes([(fu_indicator & 0xE0) | (fu_header & 0x1F)])
            if start:
                self.fu_buffer = bytearray(b"\x00\x00\x00\x01" + nal_header + payload[2:])
            elif self.fu_buffer is not None:
                self.fu_buffer += payload[2:]
            if end and self.fu_buffer is not None:
                self.access_unit += self.fu_buffer
                self.fu_buffer = None
        if marker and self.access_unit:
            data = bytes(self.access_unit)
            self.access_unit.clear()
            self._decode(data)

    def _decode(self, data: bytes):
        try:
            frames = self.codec.decode(av.Packet(data))
        except Exception:
            self.decode_errors += 1
            return
        for frame in frames:
            self.decoded_frames += 1
            if frame.format.name != "yuv420p":
                frame = frame.reformat(format="yuv420p")
            for subscriber in list(self.subscribers):
                subscriber.push(frame)

    @staticmethod
    def _payload(packet: bytes):
        if len(packet) < 12 or packet[0] >> 6 != 2:
            return None
        b0, b1 = packet[0], packet[1]
        offset = 12 + (b0 & 0x0F) * 4
        if offset > len(packet):
            return None
        if b0 & 0x10:
            if offset + 4 > len(packet):
                return None
            words = struct.unpack_from("!H", packet, offset + 2)[0]
            offset += 4 + words * 4
        end = len(packet)
        if b0 & 0x20:
            pad = packet[-1]
            if not pad or pad > end - offset:
                return None
            end -= pad
        return packet[offset:end], bool(b1 & 0x80)


class RelayRegistry:
    def __init__(self):
        self.relays: Dict[str, H264RtpRelay] = {}

    def get(self, device_id: str):
        if device_id not in self.relays:
            self.relays[device_id] = H264RtpRelay(device_id)
        return self.relays[device_id]

    def feed(self, device_id: str, packet: bytes):
        self.get(device_id).feed(packet)

    def stats(self, device_id: str):
        relay = self.get(device_id)
        return {"decoded_frames": relay.decoded_frames, "decode_errors": relay.decode_errors, "viewers": len(relay.subscribers)}


relay_registry = RelayRegistry()
