# Camera resilience validation

Run the complete offline suite from the repository root:

```sh
python -m unittest discover -s test -v
```

The 51 tests include the ESP32 protocol tests, failover/failback tests and camera
regressions for preserving a runtime reserve selection when unrelated settings
change. Explicit startup-camera changes and replaced RTSP URLs still apply.
Backoff starts after the reserve probe completes. Watchdog tests cover stalled
video, startup grace, disabling the watchdog, duplicate progress and output
from an obsolete FFmpeg process.

An optional integration check uses real FFmpeg and MediaMTX, synthetic H.264
publishers and a localhost RTP receiver:

```sh
python tools/check_camera_resilience.py --mediamtx /path/to/mediamtx --ffmpeg ffmpeg --output-dir /tmp/robotlidar-rtsp-check
```

The script generates its own temporary MediaMTX configuration and dynamically
allocated localhost ports. It starts hidden child processes on Windows and
terminates its publishers, relay and server on completion or failure.
Logs and a machine-readable result.json are written to the output directory.

Validated on Windows with Python 3.11.7 and FFmpeg 7.1:

- 51 unit tests passed; all 44 Python source files compiled.
- Ten integration checkpoints passed: RTSP startup, both publishers, primary
  RTP delivery, watchdog after freezing the primary publisher, reserve RTP
  delivery, return to a restored primary, resumed primary RTP delivery,
  failover after disconnecting the primary, and reserve RTP delivery afterward.
- The real probe exposed an unsupported RTSP -rw_timeout option. It now uses
  the RTSP demuxer's -timeout option, while subprocess.run retains its outer
  process deadline.

This local integration check exercises the RTSP/TCP -> H.264 copy -> RTP path.
It does not validate physical cameras, Raspberry Pi hardware, SRT ingest or the
central server's registration/control services.
