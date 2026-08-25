from __future__ import annotations

"""Windows launcher for RobotLiDAR emulator.

Python builds on some Windows machines can have a broken/outdated OpenSSL CA
chain even though Windows Schannel validates the same HTTPS endpoint correctly.
The emulator talks to the RobotLiDAR server through urllib, so on Windows we
route urllib.request.urlopen() through the system curl.exe. Windows curl uses
Schannel and therefore the normal Windows certificate store.

No certificate verification is disabled.
"""

import io
import json
import os
import shutil
import subprocess
import urllib.request
from typing import Any


_ORIGINAL_URLOPEN = urllib.request.urlopen


class CurlResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = io.BytesIO(body)
        self.status = status
        self.code = status

    def read(self, amt: int = -1) -> bytes:
        return self._body.read(amt)

    def getcode(self) -> int:
        return self.status

    def __enter__(self) -> "CurlResponse":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self._body.close()
        return False


def _curl_urlopen(url_or_request: Any, data: bytes | None = None, timeout: float | None = None, *args: Any, **kwargs: Any) -> CurlResponse:
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        # Extremely old/minimal Windows installations may not have curl.exe.
        # Keep the original behavior as a fallback.
        return _ORIGINAL_URLOPEN(url_or_request, data=data, timeout=timeout, *args, **kwargs)  # type: ignore[return-value]

    if isinstance(url_or_request, urllib.request.Request):
        request = url_or_request
        url = request.full_url
        method = request.get_method()
        body = request.data if data is None else data
        headers = dict(request.header_items())
    else:
        url = str(url_or_request)
        method = "POST" if data is not None else "GET"
        body = data
        headers = {}

    command = [
        curl,
        "--silent",
        "--show-error",
        "--fail-with-body",
        "--location",
        "--request",
        method,
        "--connect-timeout",
        "8",
        "--max-time",
        str(max(10, int(float(timeout or 10)))),
    ]
    for key, value in headers.items():
        command.extend(["--header", f"{key}: {value}"])
    if body is not None:
        command.extend(["--data-binary", "@-"])
    command.append(url)

    completed = subprocess.run(
        command,
        input=body,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        timeout=max(15, int(float(timeout or 10)) + 5),
    )
    if completed.returncode != 0:
        error = completed.stderr.decode("utf-8", errors="replace").strip()
        response = completed.stdout.decode("utf-8", errors="replace").strip()
        detail = error or response or f"curl.exe exit code {completed.returncode}"
        raise RuntimeError(f"HTTPS request failed through Windows Schannel: {detail}")

    return CurlResponse(completed.stdout)


def main() -> int:
    if os.name == "nt":
        urllib.request.urlopen = _curl_urlopen  # type: ignore[assignment]

    import emulator

    return emulator.main()


if __name__ == "__main__":
    raise SystemExit(main())
