#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


class TapoArchiveError(RuntimeError):
    pass


def _import_pytapo():
    try:
        from pytapo import Tapo
        from pytapo.media_stream.downloader import Downloader
        try:
            from pytapo.media_stream.snapshot import getRecordingSnapshot
        except Exception:
            getRecordingSnapshot = None
        return Tapo, Downloader, getRecordingSnapshot
    except Exception as exc:
        raise TapoArchiveError(
            "pytapo не установлен или не загружается: {}. "
            "Для этой Orange Pi используется pytapo 3.2.15 (Python 3.8 compatible).".format(exc)
        )


def _host_from_config(cfg: dict[str, Any]) -> str:
    explicit = str(cfg.get("tapo_host") or "").strip()
    if explicit:
        return explicit
    for key in ("camera1_url", "input_url", "camera2_url"):
        value = str(cfg.get(key) or "").strip()
        if not value:
            continue
        try:
            host = urlparse(value).hostname
            if host:
                return host
        except Exception:
            pass
    raise TapoArchiveError("IP Tapo не задан и его не удалось определить из RTSP URL")


def _new_client(cfg: dict[str, Any]):
    if not bool(cfg.get("tapo_archive_enabled", False)):
        raise TapoArchiveError("Архив Tapo выключен в настройках")

    host = _host_from_config(cfg)
    user = str(cfg.get("tapo_user") or "admin").strip() or "admin"
    cloud_password = str(cfg.get("tapo_cloud_password") or "")
    if not cloud_password:
        raise TapoArchiveError("Не задан пароль TP-Link/Tapo Cloud для доступа к SD архиву")

    Tapo, _Downloader, _snapshot = _import_pytapo()
    try:
        return Tapo(
            host,
            user,
            cloud_password,
            cloudPassword=cloud_password,
            printDebugInformation=False,
        )
    except Exception as exc:
        raise TapoArchiveError("Не удалось подключиться к Tapo {}: {}".format(host, exc))


def _walk_recordings(value: Any, out: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        if "startTime" in value and "endTime" in value:
            try:
                start = int(value.get("startTime"))
                end = int(value.get("endTime"))
            except (TypeError, ValueError):
                start = end = 0
            if start > 0 and end >= start:
                item = dict(value)
                item["startTime"] = start
                item["endTime"] = end
                out.append(item)
        for child in value.values():
            _walk_recordings(child, out)
    elif isinstance(value, list):
        for child in value:
            _walk_recordings(child, out)


def _normalize_recordings(raw: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    _walk_recordings(raw, found)

    unique: dict[tuple[int, int], dict[str, Any]] = {}
    for item in found:
        key = (int(item["startTime"]), int(item["endTime"]))
        unique[key] = item

    result: list[dict[str, Any]] = []
    for (_start, _end), item in sorted(unique.items()):
        start = int(item["startTime"])
        end = int(item["endTime"])
        clean = {
            "startTime": start,
            "endTime": end,
            "duration": max(0, end - start),
            "video_type": str(item.get("video_type") or item.get("videoType") or ""),
        }
        for key in ("event_type", "eventType", "alarm_type", "alarmType"):
            if key in item:
                clean[key] = item[key]
        try:
            clean["startLocal"] = datetime.fromtimestamp(start).isoformat(timespec="seconds")
            clean["endLocal"] = datetime.fromtimestamp(end).isoformat(timespec="seconds")
        except Exception:
            clean["startLocal"] = ""
            clean["endLocal"] = ""
        result.append(clean)
    return result


def list_recordings(cfg: dict[str, Any], date_text: str) -> dict[str, Any]:
    date_text = str(date_text or "").strip()
    if not re.fullmatch(r"\d{4}-?\d{2}-?\d{2}", date_text):
        raise TapoArchiveError("Дата должна быть в формате YYYY-MM-DD")
    date_key = date_text.replace("-", "")

    tapo = _new_client(cfg)
    try:
        raw = tapo.getRecordings(date_key)
        recordings = _normalize_recordings(raw)
        return {
            "host": _host_from_config(cfg),
            "date": "{}-{}-{}".format(date_key[:4], date_key[4:6], date_key[6:8]),
            "count": len(recordings),
            "recordings": recordings,
        }
    except Exception as exc:
        raise TapoArchiveError("Не удалось получить список записей: {}".format(exc))
    finally:
        try:
            tapo.close()
        except Exception:
            pass


def recording_snapshot(cfg: dict[str, Any], start_time: int) -> bytes:
    _Tapo, _Downloader, getRecordingSnapshot = _import_pytapo()
    if getRecordingSnapshot is None:
        raise TapoArchiveError("Миниатюры записей не поддерживаются версией pytapo для Python 3.8")
    tapo = _new_client(cfg)
    try:
        jpeg = asyncio.run(getRecordingSnapshot(tapo, int(start_time), timeout=8))
        if not jpeg:
            raise TapoArchiveError("Для этой записи камера не вернула миниатюру")
        return bytes(jpeg)
    except TapoArchiveError:
        raise
    except Exception as exc:
        raise TapoArchiveError("Не удалось получить миниатюру: {}".format(exc))
    finally:
        try:
            tapo.close()
        except Exception:
            pass


def download_recording(
    cfg: dict[str, Any],
    start_time: int,
    end_time: int,
    output: str = "mp4",
) -> tuple[str, str, str]:
    if output not in ("mp4", "ts"):
        output = "mp4"
    start_time = int(start_time)
    end_time = int(end_time)
    if start_time <= 0 or end_time <= start_time:
        raise TapoArchiveError("Некорректный интервал записи")

    _Tapo, Downloader, _snapshot = _import_pytapo()
    tapo = _new_client(cfg)
    temp_dir = tempfile.mkdtemp(prefix="robotlidar-tapo-")
    file_name = "tapo-{}-{}.{}".format(start_time, end_time, output)
    expected = os.path.join(temp_dir, file_name)

    async def run_download() -> str:
        time_correction = await asyncio.get_event_loop().run_in_executor(
            None, tapo.getTimeCorrection
        )
        # pytapo 3.2.15 is used on this legacy Python 3.8 / ARMv7 image.
        # Its Downloader supports MP4 playback-download, but not the newer
        # fast-download/output/stall_timeout arguments.
        try:
            downloader = Downloader(
                tapo,
                start_time,
                end_time,
                time_correction,
                temp_dir + os.sep,
                overwriteFiles=True,
                window_size=int(cfg.get("tapo_download_window") or 50),
                fileName=file_name,
                stall_timeout=int(cfg.get("tapo_download_timeout") or 120),
                progressInterval=5.0,
                output=output,
                method="download",
            )
        except TypeError:
            if output != "mp4":
                raise TapoArchiveError("На Python 3.8 доступно скачивание архива только в MP4")
            downloader = Downloader(
                tapo,
                start_time,
                end_time,
                time_correction,
                temp_dir + os.sep,
                overwriteFiles=True,
                window_size=int(cfg.get("tapo_download_window") or 50),
                fileName=file_name,
            )
        last_file = expected
        async for status in downloader.download():
            if isinstance(status, dict) and status.get("fileName"):
                last_file = str(status["fileName"])
        return last_file

    try:
        path = asyncio.run(run_download())
        if not os.path.isfile(path):
            path = expected
        if not os.path.isfile(path):
            raise TapoArchiveError("Камера завершила передачу, но файл записи не создан")
        mime = "video/mp4" if output == "mp4" else "video/mp2t"
        return path, file_name, mime
    except TapoArchiveError:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise TapoArchiveError("Не удалось скачать запись: {}".format(exc))
    finally:
        try:
            tapo.close()
        except Exception:
            pass


def cleanup_download(path: str) -> None:
    try:
        parent = str(Path(path).parent)
        if os.path.basename(parent).startswith("robotlidar-tapo-"):
            shutil.rmtree(parent, ignore_errors=True)
        elif os.path.isfile(path):
            os.unlink(path)
    except Exception:
        pass
