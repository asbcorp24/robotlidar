#!/usr/bin/env python3
from __future__ import annotations

import math
import os
import select
import termios
import threading
import time
from typing import Callable, Optional


def _coord(raw: str, hemi: str) -> Optional[float]:
    try:
        value = float(raw)
    except Exception:
        return None
    degrees = int(value // 100)
    minutes = value - degrees * 100
    result = degrees + minutes / 60.0
    hemi = (hemi or "").upper()
    if hemi in ("S", "W"):
        result = -result
    if not math.isfinite(result):
        return None
    return result


class OptionalGPS:
    """Tiny NMEA reader for optional NEO-6M-class UART GPS receivers."""

    def __init__(self, port: str, baud: int = 9600, log: Optional[Callable[[str], None]] = None):
        self.port = str(port or "").strip()
        self.baud = int(baud or 9600)
        self.log = log or (lambda _msg: None)
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._fix: Optional[dict] = None
        self._last_uart_at = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="optional-gps", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self, max_age_sec: float = 120.0) -> Optional[dict]:
        with self._lock:
            fix = dict(self._fix) if self._fix else None
        if not fix or not fix.get("valid"):
            return None
        age = time.time() - float(fix.get("received_at") or 0.0)
        if age < 0 or age > max_age_sec:
            return None
        return fix

    def _speed_const(self):
        return {
            4800: termios.B4800,
            9600: termios.B9600,
            19200: termios.B19200,
            38400: termios.B38400,
            57600: termios.B57600,
            115200: termios.B115200,
        }.get(self.baud, termios.B9600)

    def _open(self) -> int:
        fd = os.open(self.port, os.O_RDONLY | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(fd)
        speed = self._speed_const()
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
        attrs[3] = 0
        attrs[4] = speed
        attrs[5] = speed
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        return fd

    def _run(self) -> None:
        while not self._stop.is_set():
            fd = None
            try:
                fd = self._open()
                self.log("GPS: UART {} @ {} baud".format(self.port, self.baud))
                buf = bytearray()
                while not self._stop.is_set():
                    ready, _, _ = select.select([fd], [], [], 1.0)
                    if not ready:
                        continue
                    chunk = os.read(fd, 512)
                    if not chunk:
                        time.sleep(0.1)
                        continue
                    self._last_uart_at = time.time()
                    buf.extend(chunk)
                    while b"\n" in buf:
                        raw, _, rest = buf.partition(b"\n")
                        buf = bytearray(rest)
                        self._parse(raw.decode("ascii", errors="ignore").strip())
            except Exception as exc:
                if not self._stop.is_set():
                    self.log("GPS: {} unavailable: {}".format(self.port, exc))
                    self._stop.wait(5.0)
            finally:
                if fd is not None:
                    try:
                        os.close(fd)
                    except Exception:
                        pass

    def _parse(self, line: str) -> None:
        if not line.startswith("$"):
            return
        body = line.split("*", 1)[0]
        parts = body.split(",")
        kind = parts[0][-3:] if parts else ""

        if kind == "GGA" and len(parts) >= 10:
            try:
                quality = int(parts[6] or "0")
                satellites = int(parts[7] or "0")
            except Exception:
                quality, satellites = 0, 0
            lat = _coord(parts[2], parts[3])
            lon = _coord(parts[4], parts[5])
            if quality <= 0 or lat is None or lon is None:
                return
            altitude = None
            try:
                altitude = float(parts[9])
            except Exception:
                pass
            self._store(lat, lon, satellites=satellites, altitude_m=altitude)

        elif kind == "RMC" and len(parts) >= 10:
            if (parts[2] or "").upper() != "A":
                return
            lat = _coord(parts[3], parts[4])
            lon = _coord(parts[5], parts[6])
            if lat is None or lon is None:
                return
            self._store(lat, lon)

    def _store(self, lat: float, lon: float, satellites: Optional[int] = None,
               altitude_m: Optional[float] = None) -> None:
        now = time.time()
        with self._lock:
            previous = self._fix or {}
            self._fix = {
                "valid": True,
                "latitude": float(lat),
                "longitude": float(lon),
                "satellites": satellites if satellites is not None else previous.get("satellites"),
                "altitude_m": altitude_m if altitude_m is not None else previous.get("altitude_m"),
                "received_at": now,
            }
