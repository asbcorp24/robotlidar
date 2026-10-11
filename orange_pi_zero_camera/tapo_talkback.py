#!/usr/bin/env python3
from __future__ import annotations

import http.server
import json
import os
import queue
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


class TapoTalkbackError(RuntimeError):
    pass


def _host_from_config(cfg: Any) -> str:
    host = str(getattr(cfg, "tapo_host", "") or "").strip()
    if host:
        return host
    for raw in (
        str(getattr(cfg, "camera1_url", "") or "").strip(),
        str(getattr(cfg, "input_url", "") or "").strip(),
    ):
        if not raw:
            continue
        try:
            parsed = urllib.parse.urlparse(raw)
            if parsed.hostname:
                return parsed.hostname
        except Exception:
            pass
    return ""


class _PCMHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def do_GET(self) -> None:
        manager = getattr(self.server, "manager", None)
        if manager is None or self.path.split("?", 1)[0] != "/talk.wav":
            self.send_error(404)
            return

        generation = manager.generation
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()

        # PCM S16LE, mono, 8 kHz. Unknown/streaming RIFF length.
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF", 0xFFFFFFFF, b"WAVE", b"fmt ", 16,
            1, 1, 8000, 16000, 2, 16, b"data", 0xFFFFFFFF,
        )
        try:
            self.wfile.write(header)
            self.wfile.flush()
            while manager.active and manager.generation == generation:
                try:
                    chunk = manager.audio_queue.get(timeout=0.10)
                except queue.Empty:
                    # Keep FFmpeg/go2rtc alive while the network has a short gap.
                    chunk = b"\x00" * 1600
                if not chunk:
                    continue
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


class TapoTalkback:
    def __init__(self, cfg_provider: Callable[[], Any], log: Callable[[str], None]):
        self.cfg_provider = cfg_provider
        self.log = log
        self.lock = threading.RLock()
        self.audio_queue: "queue.Queue[bytes]" = queue.Queue(maxsize=16)
        self.active = False
        self.generation = 0
        self.http_server = None
        self.http_thread = None
        self.go2rtc_proc = None
        self.go2rtc_log_thread = None
        self.go2rtc_config_path = Path("/run/robotlidar-go2rtc.yaml")

    def _cfg(self) -> Any:
        return self.cfg_provider()

    def enabled(self) -> bool:
        cfg = self._cfg()
        return bool(getattr(cfg, "tapo_talkback_enabled", False))

    def _write_go2rtc_config(self, cfg: Any) -> None:
        host = _host_from_config(cfg)
        password = str(getattr(cfg, "tapo_cloud_password", "") or "")
        if not host:
            raise TapoTalkbackError("Tapo talkback: не задан IP камеры")
        if not password:
            raise TapoTalkbackError("Tapo talkback: не задан пароль Tapo Cloud")

        # go2rtc Tapo source: cloud password without username.
        source = "tapo://{}@{}".format(urllib.parse.quote(password, safe=""), host)
        ffmpeg_bin = str(
            getattr(cfg, "go2rtc_ffmpeg_bin", "/opt/ffmpeg-6.0.1-armhf-static/ffmpeg")
            or "/opt/ffmpeg-6.0.1-armhf-static/ffmpeg"
        )
        content = (
            'api:\n'
            '  listen: "127.0.0.1:1984"\n'
            'rtsp:\n'
            '  listen: "127.0.0.1:8554"\n'
            'webrtc:\n'
            '  listen: "127.0.0.1:8555"\n'
            'ffmpeg:\n'
            '  bin: "{}"\n'
            'streams:\n'
            '  robotlidar_tapo: "{}"\n'
        ).format(
            ffmpeg_bin.replace('"', '\\"'),
            source.replace('"', '\\"'),
        )
        self.go2rtc_config_path.write_text(content, encoding="utf-8")

    def _go2rtc_ready(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", 1984), timeout=0.3):
                return True
        except OSError:
            return False

    def _read_go2rtc_log(self, proc: subprocess.Popen) -> None:
        if proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                line = line.strip()
                if line:
                    self.log("GO2RTC: " + line)
        except Exception:
            pass

    def _ensure_go2rtc(self) -> None:
        cfg = self._cfg()
        binary = str(getattr(cfg, "go2rtc_bin", "/usr/local/bin/go2rtc") or "/usr/local/bin/go2rtc")
        if self.go2rtc_proc is not None and self.go2rtc_proc.poll() is None and self._go2rtc_ready():
            return
        if not os.path.isfile(binary) or not os.access(binary, os.X_OK):
            raise TapoTalkbackError("go2rtc не установлен: {}".format(binary))

        self._write_go2rtc_config(cfg)
        if self.go2rtc_proc is not None and self.go2rtc_proc.poll() is None:
            try:
                self.go2rtc_proc.terminate()
                self.go2rtc_proc.wait(timeout=2)
            except Exception:
                try:
                    self.go2rtc_proc.kill()
                except Exception:
                    pass

        self.go2rtc_proc = subprocess.Popen(
            [binary, "-config", str(self.go2rtc_config_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            bufsize=1,
        )
        self.go2rtc_log_thread = threading.Thread(
            target=self._read_go2rtc_log,
            args=(self.go2rtc_proc,),
            name="go2rtc-log",
            daemon=True,
        )
        self.go2rtc_log_thread.start()

        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if self.go2rtc_proc.poll() is not None:
                raise TapoTalkbackError("go2rtc завершился сразу после запуска")
            if self._go2rtc_ready():
                return
            time.sleep(0.15)
        raise TapoTalkbackError("go2rtc не открыл API 127.0.0.1:1984")

    def _ensure_http_source(self) -> None:
        if self.http_server is not None:
            return
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 1985), _PCMHandler)
        server.daemon_threads = True
        server.manager = self
        self.http_server = server
        self.http_thread = threading.Thread(
            target=server.serve_forever,
            name="tapo-talkback-pcm",
            daemon=True,
        )
        self.http_thread.start()

    def _stream_api(self, src: str) -> None:
        query = urllib.parse.urlencode({"dst": "robotlidar_tapo", "src": src})
        req = urllib.request.Request(
            "http://127.0.0.1:1984/api/streams?" + query,
            data=b"",
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status >= 300:
                    raise TapoTalkbackError("go2rtc API HTTP {}".format(resp.status))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace").strip()
            except Exception:
                detail = ""
            if detail:
                raise TapoTalkbackError("go2rtc API HTTP {}: {}".format(exc.code, detail))
            raise TapoTalkbackError("go2rtc API HTTP {}".format(exc.code))

    def start(self) -> None:
        with self.lock:
            if not self.enabled():
                raise TapoTalkbackError("Tapo talkback выключен в настройках")
            self._ensure_go2rtc()
            self._ensure_http_source()
            self._clear_queue()
            self.generation += 1
            self.active = True
            src = "ffmpeg:http://127.0.0.1:1985/talk.wav#audio=pcma"
            try:
                self._stream_api(src)
            except Exception:
                self.active = False
                raise
            self.log("TAPO TALKBACK: START")

    def feed(self, pcm_s16le: bytes, sample_rate: int = 8000) -> None:
        if not pcm_s16le:
            return
        if sample_rate != 8000:
            self.log("TAPO TALKBACK ignored unsupported rate {}".format(sample_rate))
            return
        if len(pcm_s16le) > 32768:
            pcm_s16le = pcm_s16le[:32768]
        if len(pcm_s16le) & 1:
            pcm_s16le = pcm_s16le[:-1]
        with self.lock:
            if not self.active:
                return
            try:
                self.audio_queue.put_nowait(bytes(pcm_s16le))
            except queue.Full:
                # Low latency is more important than preserving old speech.
                try:
                    self.audio_queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self.audio_queue.put_nowait(bytes(pcm_s16le))
                except queue.Full:
                    pass

    def stop(self) -> None:
        with self.lock:
            was_active = self.active
            self.active = False
            self.generation += 1
            self._clear_queue()
            if self._go2rtc_ready():
                try:
                    self._stream_api("")
                except Exception as exc:
                    self.log("TAPO TALKBACK stop warning: {}".format(exc))
            if was_active:
                self.log("TAPO TALKBACK: STOP")

    def close(self) -> None:
        self.stop()
        with self.lock:
            if self.http_server is not None:
                try:
                    self.http_server.shutdown()
                    self.http_server.server_close()
                except Exception:
                    pass
                self.http_server = None
            if self.go2rtc_proc is not None and self.go2rtc_proc.poll() is None:
                try:
                    self.go2rtc_proc.terminate()
                    self.go2rtc_proc.wait(timeout=2)
                except Exception:
                    try:
                        self.go2rtc_proc.kill()
                    except Exception:
                        pass
            self.go2rtc_proc = None

    def _clear_queue(self) -> None:
        while True:
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                return
