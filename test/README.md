# ESP32 bridge protocol tests

Run from the repository root with Python 3.9 or newer:

```sh
python -m unittest discover -s test -v
```

The suite uses the standard library only. ROS imports and message/publisher
objects are stubbed, but the production bridge methods and serial frame parser
are exercised directly. A fake USB serial transport validates outbound XOR
checksums and injects inbound ACK frames. A fake clock drives configuration
retry deadlines; timeout tests do not sleep. One test delivers ACK from a
separate thread to exercise the service/reader event handoff.

Coverage includes matching, unrelated, malformed, corrupt and late ACKs;
ARM/DISARM success, refusal, timeout, dry-run and disconnected serial;
command gating; cancellation by STOP, subsequent disarmed telemetry and USB
reconnect; overlapping ARM requests; configuration ACK matching, retries at
least 600 ms apart, the four-transmission limit, queue advancement, failed
writes, duplicate replies and continued drive heartbeats.

These are host-side unit tests. They do not validate ROS executor scheduling,
USB hardware, ESP32 execution, actual NVS writes or physical ESTOP behavior.

Validation: Python 3.11.7, 35 tests passed. Regression tests exposed cases where
a pending ARM completion could override STOP, newer disarmed telemetry or a
USB disconnect/reconnect. Pending requests are now invalidated by those events.
