#include <Arduino.h>

// ============================================================
// RobotLidar ESP32-WROOM controller for two e-bike BLDC drivers
// Framework: Arduino, build system: PlatformIO
// ============================================================
// IMPORTANT:
// - GPIO25/GPIO26 are true 8-bit DAC outputs (0..3.3 V nominal).
// - Do not connect 5 V Hall/speed outputs directly to ESP32 GPIO.
// - ENABLE pins must drive isolated relay/transistor interfaces; the thin
//   controller ignition wire may carry battery voltage and never goes to ESP32.
// - BRAKE/REVERSE polarity must be verified on the actual controller harness.
// ============================================================

namespace Pins {
constexpr uint8_t LEFT_THROTTLE_DAC = 25;
constexpr uint8_t RIGHT_THROTTLE_DAC = 26;
constexpr uint8_t LEFT_REVERSE = 16;
constexpr uint8_t RIGHT_REVERSE = 17;
constexpr uint8_t LEFT_BRAKE = 18;
constexpr uint8_t RIGHT_BRAKE = 19;
constexpr uint8_t LEFT_ENABLE = 21;
constexpr uint8_t RIGHT_ENABLE = 22;
constexpr uint8_t ESTOP_OK = 32;       // NC emergency loop to GND; LOW = healthy
constexpr uint8_t LEFT_HALL = 34;      // input-only, external 3.3 V conditioning
constexpr uint8_t RIGHT_HALL = 35;     // input-only, external 3.3 V conditioning
constexpr uint8_t STATUS_LED = 2;
}  // namespace Pins

namespace Config {
constexpr uint32_t SERIAL_BAUD = 115200;
constexpr uint32_t CONTROL_PERIOD_MS = 20;
constexpr uint32_t TELEMETRY_PERIOD_MS = 100;
constexpr uint32_t COMMAND_WATCHDOG_MS = 450;
constexpr uint32_t REVERSE_BRAKE_MS = 700;
constexpr uint32_t REVERSE_SETTLE_MS = 300;

// Safe initial limits. Increase only after measuring the real throttle input.
// ESP32 DAC count is approximately voltage / 3.3 * 255.
constexpr uint8_t DAC_DISARMED = 0;
constexpr uint8_t DAC_IDLE = 68;       // approximately 0.88 V
constexpr uint8_t DAC_MAX_TEST = 220;  // approximately 2.85 V, intentionally limited
constexpr int16_t RAMP_STEP_PER_TICK = 12;  // command units per 20 ms
constexpr bool HOLD_BRAKE_AT_ZERO = true;
constexpr bool REVERSE_SUPPORTED = true;

// Output polarities refer to the low-voltage interface board, not directly to
// high-voltage controller wires.
constexpr bool ENABLE_ACTIVE_HIGH = true;
constexpr bool BRAKE_ACTIVE_HIGH = true;
constexpr bool REVERSE_ACTIVE_HIGH = true;
}  // namespace Config

struct Track {
  uint8_t throttlePin;
  uint8_t reversePin;
  uint8_t brakePin;
  uint8_t enablePin;
  int16_t target = 0;      // -1000..1000
  int16_t actual = 0;      // ramped command
  int8_t appliedSign = 1;  // +1 forward, -1 reverse
  enum class Phase : uint8_t { Normal, BrakeBeforeReverse, ReverseSettle };
  Phase phase = Phase::Normal;
  uint32_t deadlineMs = 0;
};

Track leftTrack{
    Pins::LEFT_THROTTLE_DAC,
    Pins::LEFT_REVERSE,
    Pins::LEFT_BRAKE,
    Pins::LEFT_ENABLE,
};
Track rightTrack{
    Pins::RIGHT_THROTTLE_DAC,
    Pins::RIGHT_REVERSE,
    Pins::RIGHT_BRAKE,
    Pins::RIGHT_ENABLE,
};

portMUX_TYPE hallMux = portMUX_INITIALIZER_UNLOCKED;
volatile int64_t leftTicks = 0;
volatile int64_t rightTicks = 0;
volatile uint32_t leftWindowPulses = 0;
volatile uint32_t rightWindowPulses = 0;
volatile int8_t leftPulseSign = 1;
volatile int8_t rightPulseSign = 1;

bool armed = false;
bool watchdogTripped = false;
uint32_t lastDriveFrameMs = 0;
uint32_t lastControlMs = 0;
uint32_t lastTelemetryMs = 0;
uint32_t lastTelemetryPulseMs = 0;
uint32_t lastSequence = 0;

char serialLine[180];
size_t serialLineLength = 0;

void IRAM_ATTR onLeftHall() {
  portENTER_CRITICAL_ISR(&hallMux);
  leftTicks += leftPulseSign;
  ++leftWindowPulses;
  portEXIT_CRITICAL_ISR(&hallMux);
}

void IRAM_ATTR onRightHall() {
  portENTER_CRITICAL_ISR(&hallMux);
  rightTicks += rightPulseSign;
  ++rightWindowPulses;
  portEXIT_CRITICAL_ISR(&hallMux);
}

bool outputLevel(bool active, bool activeHigh) {
  return activeHigh ? active : !active;
}

void setBrake(const Track& track, bool active) {
  digitalWrite(track.brakePin, outputLevel(active, Config::BRAKE_ACTIVE_HIGH));
}

void setEnable(const Track& track, bool active) {
  digitalWrite(track.enablePin, outputLevel(active, Config::ENABLE_ACTIVE_HIGH));
}

void setReverse(Track& track, bool reverse) {
  digitalWrite(track.reversePin, outputLevel(reverse, Config::REVERSE_ACTIVE_HIGH));
  track.appliedSign = reverse ? -1 : 1;
  portENTER_CRITICAL(&hallMux);
  if (&track == &leftTrack) {
    leftPulseSign = track.appliedSign;
  } else {
    rightPulseSign = track.appliedSign;
  }
  portEXIT_CRITICAL(&hallMux);
}

void writeThrottle(const Track& track, uint8_t value) {
  dacWrite(track.throttlePin, value);
}

int8_t signOf(int16_t value) {
  if (value > 0) return 1;
  if (value < 0) return -1;
  return 0;
}

int16_t clampCommand(long value) {
  if (value > 1000) return 1000;
  if (value < -1000) return -1000;
  return static_cast<int16_t>(value);
}

int16_t moveToward(int16_t current, int16_t target, int16_t step) {
  if (current < target) {
    const int32_t next = static_cast<int32_t>(current) + step;
    return static_cast<int16_t>(next > target ? target : next);
  }
  if (current > target) {
    const int32_t next = static_cast<int32_t>(current) - step;
    return static_cast<int16_t>(next < target ? target : next);
  }
  return current;
}

uint8_t commandToDac(int16_t signedCommand) {
  const int magnitude = abs(signedCommand);
  if (magnitude <= 0) return Config::DAC_IDLE;
  const long span = Config::DAC_MAX_TEST - Config::DAC_IDLE;
  return static_cast<uint8_t>(
      Config::DAC_IDLE + (span * constrain(magnitude, 0, 1000)) / 1000);
}

bool estopOkay() {
  // Fail-safe wiring: external normally-closed contact pulls this pin to GND.
  // Open wire, pressed button, or disconnected plug reads HIGH and stops drive.
  return digitalRead(Pins::ESTOP_OK) == LOW;
}

void applyTrackSafe(Track& track, bool disableController) {
  track.target = 0;
  track.actual = 0;
  track.phase = Track::Phase::Normal;
  writeThrottle(track, Config::DAC_DISARMED);
  setBrake(track, true);
  if (disableController) setEnable(track, false);
}

void disarmSystem(const char* reason) {
  const bool wasArmed = armed;
  armed = false;
  applyTrackSafe(leftTrack, true);
  applyTrackSafe(rightTrack, true);
  digitalWrite(Pins::STATUS_LED, LOW);
  if (wasArmed || reason != nullptr) {
    Serial.print("EVT,DISARM,");
    Serial.println(reason != nullptr ? reason : "requested");
  }
}

bool armSystem() {
  if (!estopOkay()) {
    Serial.println("ERR,ESTOP_OPEN");
    return false;
  }
  if (leftTrack.target != 0 || rightTrack.target != 0) {
    Serial.println("ERR,NONZERO_TARGET");
    return false;
  }

  writeThrottle(leftTrack, Config::DAC_IDLE);
  writeThrottle(rightTrack, Config::DAC_IDLE);
  setReverse(leftTrack, false);
  setReverse(rightTrack, false);
  setBrake(leftTrack, true);
  setBrake(rightTrack, true);
  setEnable(leftTrack, true);
  setEnable(rightTrack, true);
  delay(50);

  armed = true;
  watchdogTripped = false;
  lastDriveFrameMs = millis();
  digitalWrite(Pins::STATUS_LED, HIGH);
  Serial.println("EVT,ARMED");
  return true;
}

void updateTrack(Track& track, uint32_t nowMs) {
  if (!armed) {
    applyTrackSafe(track, true);
    return;
  }

  if (!Config::REVERSE_SUPPORTED && track.target < 0) track.target = 0;
  const int8_t desiredSign = signOf(track.target);

  if (track.phase == Track::Phase::BrakeBeforeReverse) {
    writeThrottle(track, Config::DAC_IDLE);
    setBrake(track, true);
    track.actual = 0;
    if (static_cast<int32_t>(nowMs - track.deadlineMs) >= 0) {
      if (desiredSign == 0) {
        track.phase = Track::Phase::Normal;
        return;
      }
      setReverse(track, desiredSign < 0);
      track.phase = Track::Phase::ReverseSettle;
      track.deadlineMs = nowMs + Config::REVERSE_SETTLE_MS;
    }
    return;
  }

  if (track.phase == Track::Phase::ReverseSettle) {
    writeThrottle(track, Config::DAC_IDLE);
    setBrake(track, true);
    track.actual = 0;
    if (static_cast<int32_t>(nowMs - track.deadlineMs) >= 0) {
      track.phase = Track::Phase::Normal;
    }
    return;
  }

  if (desiredSign != 0 && desiredSign != track.appliedSign) {
    track.actual = 0;
    writeThrottle(track, Config::DAC_IDLE);
    setBrake(track, true);
    track.phase = Track::Phase::BrakeBeforeReverse;
    track.deadlineMs = nowMs + Config::REVERSE_BRAKE_MS;
    return;
  }

  track.actual = moveToward(
      track.actual,
      track.target,
      Config::RAMP_STEP_PER_TICK);

  if (track.actual == 0) {
    writeThrottle(track, Config::DAC_IDLE);
    setBrake(track, Config::HOLD_BRAKE_AT_ZERO);
  } else {
    setBrake(track, false);
    writeThrottle(track, commandToDac(track.actual));
  }
}

uint8_t xorChecksum(const char* text) {
  uint8_t checksum = 0;
  while (*text != '\0') checksum ^= static_cast<uint8_t>(*text++);
  return checksum;
}

int hexNibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;
}

void sendFrame(const String& body) {
  Serial.print(body);
  Serial.print('*');
  const uint8_t checksum = xorChecksum(body.c_str());
  if (checksum < 16) Serial.print('0');
  Serial.println(checksum, HEX);
}

void sendAck(uint32_t sequence, const char* result) {
  String body = "ACK," + String(sequence) + "," + result + "," +
                String(leftTrack.target) + "," + String(rightTrack.target);
  sendFrame(body);
}

bool verifyAndStripChecksum(char* line) {
  char* star = strrchr(line, '*');
  if (star == nullptr || strlen(star + 1) != 2) return false;
  const int high = hexNibble(star[1]);
  const int low = hexNibble(star[2]);
  if (high < 0 || low < 0) return false;
  const uint8_t received = static_cast<uint8_t>((high << 4) | low);
  *star = '\0';
  return xorChecksum(line) == received;
}

void processFrame(char* line) {
  if (!verifyAndStripChecksum(line)) {
    Serial.println("ERR,CHECKSUM");
    return;
  }

  char* save = nullptr;
  char* command = strtok_r(line, ",", &save);
  char* seqText = strtok_r(nullptr, ",", &save);
  if (command == nullptr || seqText == nullptr) {
    Serial.println("ERR,FORMAT");
    return;
  }

  const uint32_t sequence = strtoul(seqText, nullptr, 10);
  lastSequence = sequence;

  if (strcmp(command, "DRV") == 0) {
    char* leftText = strtok_r(nullptr, ",", &save);
    char* rightText = strtok_r(nullptr, ",", &save);
    if (leftText == nullptr || rightText == nullptr) {
      sendAck(sequence, "BAD_FORMAT");
      return;
    }
    const int16_t left = clampCommand(strtol(leftText, nullptr, 10));
    const int16_t right = clampCommand(strtol(rightText, nullptr, 10));
    lastDriveFrameMs = millis();
    watchdogTripped = false;
    if (!armed) {
      leftTrack.target = 0;
      rightTrack.target = 0;
      sendAck(sequence, "DISARMED");
      return;
    }
    leftTrack.target = left;
    rightTrack.target = right;
    sendAck(sequence, "OK");
    return;
  }

  if (strcmp(command, "ARM") == 0) {
    char* valueText = strtok_r(nullptr, ",", &save);
    const bool requestArm = valueText != nullptr && atoi(valueText) != 0;
    if (requestArm) {
      leftTrack.target = 0;
      rightTrack.target = 0;
      sendAck(sequence, armSystem() ? "OK" : "REFUSED");
    } else {
      disarmSystem("REMOTE");
      sendAck(sequence, "OK");
    }
    return;
  }

  if (strcmp(command, "STOP") == 0) {
    disarmSystem("REMOTE_STOP");
    sendAck(sequence, "OK");
    return;
  }

  if (strcmp(command, "PING") == 0) {
    sendAck(sequence, "PONG");
    return;
  }

  sendAck(sequence, "UNKNOWN");
}

void readSerialFrames() {
  while (Serial.available() > 0) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\r') continue;
    if (c == '\n') {
      if (serialLineLength > 0) {
        serialLine[serialLineLength] = '\0';
        processFrame(serialLine);
        serialLineLength = 0;
      }
      continue;
    }
    if (serialLineLength + 1 < sizeof(serialLine)) {
      serialLine[serialLineLength++] = c;
    } else {
      serialLineLength = 0;
      Serial.println("ERR,LINE_TOO_LONG");
    }
  }
}

void sendTelemetry(uint32_t nowMs) {
  int64_t leftTickSnapshot;
  int64_t rightTickSnapshot;
  uint32_t leftPulseSnapshot;
  uint32_t rightPulseSnapshot;

  portENTER_CRITICAL(&hallMux);
  leftTickSnapshot = leftTicks;
  rightTickSnapshot = rightTicks;
  leftPulseSnapshot = leftWindowPulses;
  rightPulseSnapshot = rightWindowPulses;
  leftWindowPulses = 0;
  rightWindowPulses = 0;
  portEXIT_CRITICAL(&hallMux);

  const uint32_t elapsed = max<uint32_t>(1, nowMs - lastTelemetryPulseMs);
  lastTelemetryPulseMs = nowMs;
  const uint32_t leftPps = leftPulseSnapshot * 1000UL / elapsed;
  const uint32_t rightPps = rightPulseSnapshot * 1000UL / elapsed;

  String body = "TEL," + String(nowMs) + "," + String(armed ? 1 : 0) + "," +
                String(estopOkay() ? 1 : 0) + "," +
                String(leftTrack.target) + "," + String(rightTrack.target) + "," +
                String(leftTrack.actual) + "," + String(rightTrack.actual) + "," +
                String(static_cast<long long>(leftTickSnapshot)) + "," +
                String(static_cast<long long>(rightTickSnapshot)) + "," +
                String(leftPps) + "," + String(rightPps) + "," +
                String(watchdogTripped ? 1 : 0);
  sendFrame(body);
}

void setup() {
  Serial.begin(Config::SERIAL_BAUD);
  delay(200);

  pinMode(Pins::LEFT_REVERSE, OUTPUT);
  pinMode(Pins::RIGHT_REVERSE, OUTPUT);
  pinMode(Pins::LEFT_BRAKE, OUTPUT);
  pinMode(Pins::RIGHT_BRAKE, OUTPUT);
  pinMode(Pins::LEFT_ENABLE, OUTPUT);
  pinMode(Pins::RIGHT_ENABLE, OUTPUT);
  pinMode(Pins::STATUS_LED, OUTPUT);
  pinMode(Pins::ESTOP_OK, INPUT_PULLUP);
  pinMode(Pins::LEFT_HALL, INPUT);
  pinMode(Pins::RIGHT_HALL, INPUT);

  setReverse(leftTrack, false);
  setReverse(rightTrack, false);
  applyTrackSafe(leftTrack, true);
  applyTrackSafe(rightTrack, true);
  digitalWrite(Pins::STATUS_LED, LOW);

  attachInterrupt(digitalPinToInterrupt(Pins::LEFT_HALL), onLeftHall, RISING);
  attachInterrupt(digitalPinToInterrupt(Pins::RIGHT_HALL), onRightHall, RISING);

  lastControlMs = millis();
  lastTelemetryMs = millis();
  lastTelemetryPulseMs = millis();
  sendFrame("BOOT,ESP32_WROOM_TRACK_CONTROLLER,1");
}

void loop() {
  readSerialFrames();
  const uint32_t nowMs = millis();

  if (!estopOkay() && armed) {
    disarmSystem("ESTOP");
  }

  if (armed && nowMs - lastDriveFrameMs > Config::COMMAND_WATCHDOG_MS) {
    watchdogTripped = true;
    disarmSystem("WATCHDOG");
  }

  if (nowMs - lastControlMs >= Config::CONTROL_PERIOD_MS) {
    lastControlMs = nowMs;
    updateTrack(leftTrack, nowMs);
    updateTrack(rightTrack, nowMs);
  }

  if (nowMs - lastTelemetryMs >= Config::TELEMETRY_PERIOD_MS) {
    lastTelemetryMs = nowMs;
    sendTelemetry(nowMs);
  }
}
