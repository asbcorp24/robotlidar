# ESP32 ↔ ROS protocol fixes (draft PR #3)

The transport uses ASCII frames `BODY*XX\n` with XOR checksum `XX`.

## ARM

`ARM,<sequence>,1|0` receives `ACK,<sequence>,OK|NOT_ROS_MODE|REFUSED,...`. The ROS `/drive/arm` service only succeeds after a matching `OK` ACK; a write to the serial port alone is not confirmation. The bridge gates periodic nonzero `DRV/AUX` commands on the confirmed ARM state. In dry-run mode the service does not claim hardware ARM confirmation. The existing hardware ESTOP and ESP32 watchdog remain authoritative.

## Configuration

For compatibility with the existing web settings client, configuration remains encoded in the 32-bit sequence of `PING,<sequence>` (`0xC6......`). The ESP32 now decodes and applies the configuration **synchronously** while processing that PING, rather than sampling a shared `lastSequence` later. Its response is `ACK,<sequence>,CFG_OK,...` on successful application or `CFG_UNKNOWN_KEY` / `CFG_BAD_OP` on failure. The ROS settings bridge waits for the matching ACK and retries up to four transmissions, spaced by at least 600 ms, without blocking normal drive heartbeats. Retried SET operations are assignments; RESET is also repeatable. ACK indicates successful handling by firmware, not independent read-back of flash content.

## M4 and AUXTEL

An accepted `AUX` packet carrying the M4 magic sequence updates the stepper ROS watchdog timestamp even when its target is unchanged. AUXTEL v1.9.8 has 12 comma-separated fields including `AUXTEL`: `millis,sbus_raw,sbus_pseudo_us,sbus_valid,target_deg,actual_deg,current_step,target_step,direction,driver_ready,driver_address_hex`. The ROS bridge maps these fields explicitly. The eight-element `/aux_motor/state` payload is retained but its positions are normalized degrees/360; review downstream consumers before merging.

## Validation required before merging

- Run Python syntax checks and ROS bridge tests, including rejected ARM, disconnected serial, mismatched ACK, duplicate M4 commands, config timeout/retry and malformed telemetry.
- Build the ESP32 PlatformIO target and verify on a bench with drive power disconnected.
- Check downstream subscribers of `/aux_motor/state` for semantic compatibility.
- Verify ESTOP, RC/SAFE/ROS transitions, watchdog, USB reconnect and NVS read-back.
