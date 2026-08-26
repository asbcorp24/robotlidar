from __future__ import annotations

"""Windows launcher for the RobotLiDAR emulator.

The emulator uses HTTPS for registration/telemetry and WSS for the persistent
control channel.  On Windows we install ``truststore`` and inject the native
Windows certificate store into Python's SSL stack before importing emulator.

Older revisions spawned curl.exe for every HTTPS request.  That worked around
broken Python CA bundles, but telemetry runs frequently and repeatedly spawning
curl.exe could introduce connection timeouts and unnecessary process overhead.
With truststore both HTTPS and WSS now use the same Windows trust store directly.
TLS certificate verification remains enabled.
"""

import os


def _enable_windows_trust_store() -> None:
    if os.name != "nt":
        return
    try:
        import truststore

        truststore.inject_into_ssl()
        print("TLS transport: Python SSL + Windows certificate store (truststore)")
    except Exception as exc:
        raise RuntimeError(
            "Не удалось подключить хранилище сертификатов Windows. "
            "Запустите run.bat ещё раз, чтобы установить/обновить truststore. "
            f"Ошибка: {exc}"
        ) from exc


def main() -> int:
    _enable_windows_trust_store()

    import emulator

    return emulator.main()


if __name__ == "__main__":
    raise SystemExit(main())
