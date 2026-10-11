#!/usr/bin/env python3
from __future__ import annotations

import os
import socket
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


def _get(cfg: dict[str, Any], key: str, default: Any = None) -> Any:
    value = cfg.get(key, default)
    return default if value is None else value


def _host_from_config(cfg: dict[str, Any]) -> str:
    host = str(_get(cfg, "camera_tapo_host", "") or "").strip()
    if host:
        return host
    for key in ("camera1_rtsp_url", "camera_rtsp_url"):
        raw = str(_get(cfg, key, "") or "").strip()
        if not raw:
            continue
        try:
            parsed = urllib.parse.urlparse(raw)
            if parsed.hostname:
                return parsed.hostname
        except Exception:
            pass
    return ""


class TapoTalkback:
    """Tapo two-way audio for Raspberry Pi.

    The browser records a complete phrase and the central server forwards it to
    the Raspberry through the already established device control WebSocket.
    Raspberry converts the clip locally and go2rtc feeds PCMA/8000 to Tapo.
    """

    def __init__(self, cfg_provider: Callable[[], dict[str, Any]], log: Callable[[str], None]):
        self.cfg_provider = cfg_provider
        self.log = log
        self.lock = threading.RLock()
        self.go2rtc_proc = None
        self.go2rtc_log_thread = None
        self.go2rtc_config_path = Path("/run/robotlidar-go2rtc-rpi.yaml")

    def _cfg(self) -> dict[str, Any]:
        return dict(self.cfg_provider() or {})

    def enabled(self) -> bool:
        return bool(_get(self._cfg(), "camera_tapo_talkback_enabled", False))

    def _write_go2rtc_config(self, cfg: dict[str, Any]) -> None:
        host = _host_from_config(cfg)
        password = str(_get(cfg, "camera_tapo_cloud_password", "") or "")
        if not host:
            raise TapoTalkbackError("Tapo talkback: не задан IP камеры")
        if not password:
            raise TapoTalkbackError("Tapo talkback: не задан пароль Tapo Cloud")

        source = "tapo://{}@{}".format(urllib.parse.quote(password, safe=""), host)
        ffmpeg_bin = str(_get(cfg, "camera_talkback_ffmpeg_bin", "ffmpeg") or "ffmpeg")
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
        try:
            os.chmod(self.go2rtc_config_path, 0o600)
        except Exception:
            pass

    @staticmethod
    def _api_ready() -> bool:
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
        binary = str(_get(cfg, "camera_go2rtc_bin", "/usr/local/bin/go2rtc") or "/usr/local/bin/go2rtc")
        if self.go2rtc_proc is not None and self.go2rtc_proc.poll() is None and self._api_ready():
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
            name="go2rtc-rpi-log",
            daemon=True,
        )
        self.go2rtc_log_thread.start()

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if self.go2rtc_proc.poll() is not None:
                raise TapoTalkbackError("go2rtc завершился сразу после запуска")
            if self._api_ready():
                return
            time.sleep(0.15)
        raise TapoTalkbackError("go2rtc не открыл API 127.0.0.1:1984")

    def _stream_api(self, src: str) -> None:
        query = urllib.parse.urlencode({"dst": "robotlidar_tapo", "src": src})
        req = urllib.request.Request(
            "http://127.0.0.1:1984/api/streams?" + query,
            data=b"",
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status >= 300:
                    raise TapoTalkbackError("go2rtc API HTTP {}".format(resp.status))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace").strip()
            except Exception:
                detail = ""
            raise TapoTalkbackError(
                "go2rtc API HTTP {}{}".format(exc.code, ": " + detail if detail else "")
            )

    def play_clip(self, data: bytes, extension: str = "webm") -> None:
        if not data:
            return
        extension = str(extension or "webm").lower().strip().lstrip(".")
        if extension not in ("webm", "ogg", "wav", "m4a", "mp4"):
            extension = "webm"

        stamp = int(time.time() * 1000)
        path = Path("/run/robotlidar-talkback-rpi-{}.{}".format(stamp, extension))
        padded_path = Path("/run/robotlidar-talkback-rpi-{}-padded.wav".format(stamp))

        with self.lock:
            if not self.enabled():
                raise TapoTalkbackError("Tapo talkback выключен в настройках")
            self._ensure_go2rtc()
            cfg = self._cfg()
            ffmpeg_bin = str(_get(cfg, "camera_talkback_ffmpeg_bin", "ffmpeg") or "ffmpeg")

            if self._api_ready():
                try:
                    self._stream_api("")
                except Exception:
                    pass

            path.write_bytes(data)
            cmd = [
                ffmpeg_bin,
                "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(path),
                "-af", "adelay=800|800,volume=2.0,alimiter=limit=0.98:attack=5:release=50,apad=pad_dur=0.8",
                "-ac", "1", "-ar", "8000",
                "-c:a", "pcm_s16le",
                str(padded_path),
            ]
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=30,
            )
            if result.returncode != 0 or not padded_path.exists():
                detail = (result.stderr or "").strip()
                raise TapoTalkbackError("FFmpeg clip prepare failed: {}".format(detail[-500:]))

            src = "ffmpeg:{}#audio=pcma/8000#input=file".format(padded_path)
            self.log("TAPO TALKBACK CLIP: PLAY {} bytes + 800ms/800ms padding + boosted volume".format(len(data)))
            self._stream_api(src)

        def cleanup() -> None:
            time.sleep(120)
            for item in (path, padded_path):
                try:
                    item.unlink()
                except Exception:
                    pass

        threading.Thread(target=cleanup, name="tapo-talkback-rpi-cleanup", daemon=True).start()

    def close(self) -> None:
        with self.lock:
            if self._api_ready():
                try:
                    self._stream_api("")
                except Exception:
                    pass
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
