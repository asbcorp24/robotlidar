#include <Arduino.h>

// ============================================================
// RobotLidar ESP32-WROOM dual-track controller
//
// Command sources:
//   1. Microzone MC7 + MC8RE-V2 (manual/default mode)
//   2. Raspberry Pi ROS 2 over USB Serial (autonomous mode)
//
// MC7 is normally configured as BOAT with CH1/CH2 MIX enabled.
// Therefore CH1 and CH2 are already final left/right track commands.
//
// Discrete controller inputs:
//   GPIO16 -> TLP240A -> Reverse LEFT
//   GPIO17 -> TLP240A -> Reverse RIGHT
//   GPIO18 -> TLP240A -> Low brake LEFT
//   GPIO19 -> TLP240A -> Low brake RIGHT
//
// GPIO21 and GPIO22 are intentionally free. Controller Lock/Ignition is
// switched only by a physical key/switch and the hardware emergency circuit.
// ============================================================

namespace Pins {
constexpr uint8_t LEFT_THROTTLE_DAC = 25;
constexpr uint8_t RIGHT_THROTTLE_DAC = 26;
constexpr uint8_t LEFT_REVERSE = 16;
constexpr uint8_t RIGHT_REVERSE = 17;
constexpr uint8_t LEFT_BRAKE = 18;
constexpr uint8_t RIGHT_BRAKE = 19;

constexpr uint8_t ESTOP_OK = 32;    // NC emergency loop to GND; LOW = healthy
constexpr uint8_t LEFT_HALL = 34;   // input-only; external 5 V -> 3.3 V circuit
constexpr uint8_t RIGHT_HALL = 35;  // input-only; external 5 V -> 3.3 V circuit

// MC8RE-V2 PWM inputs. Receiver GND and ESP32 GND must be common.
constexpr uint8_t RC_CHANNEL_1 = 27;  // BOAT MIX: left track
constexpr uint8_t RC_CHANNEL_2 = 33;  // BOAT MIX: right track
constexpr uint8_t RC_MODE = 13;       // CH5: RC / SAFE / ROS
constexpr uint8_t RC_ARM = 14;        // CH6: permission

constexpr uint8_t STATUS_LED = 2;
}  // namespace Pins

namespace Config {
constexpr uint32_t SERIAL_BAUD = 115200;
constexpr uint32_t CONTROL_PERIOD_MS = 20;
constexpr uint32_t TELEMETRY_PERIOD_MS = 100;
constexpr uint32_t COMMAND_WATCHDOG_MS = 450;
constexpr uint32_t RC_SIGNAL_TIMEOUT_US = 160000;
constexpr uint32_t REVERSE_BRAKE_MS = 700;
constexpr uint32_t REVERSE_SETTLE_MS = 300;

// Nominal receiver PWM: 1000..2000 us, 1500 us centre.
constexpr uint16_t RC_MIN_VALID_US = 800;
constexpr uint16_t RC_MAX_VALID_US = 2200;
constexpr uint16_t RC_CENTER_US = 1500;
constexpr uint16_t RC_DEADBAND_US = 45;
constexpr uint16_t RC_MODE_MANUAL_MAX_US = 1300;
constexpr uint16_t RC_MODE_ROS_MIN_US = 1700;
constexpr uint16_t RC_ARM_OFF_MAX_US = 1400;
constexpr uint16_t RC_ARM_ON_MIN_US = 1600;

// true: MC7 performs CH1/CH2 mixing; false: ESP32 mixes steering/throttle.
constexpr bool RC_PREMIXED_BY_TRANSMITTER = true;
constexpr bool RC_LEFT_REVERSED = false;
constexpr bool RC_RIGHT_REVERSED = false;
constexpr bool RC_THROTTLE_REVERSED = false;
constexpr bool RC_STEERING_REVERSED = false;

// In ROS mode the radio remains an independent safety permission.
constexpr bool ROS_REQUIRES_RC_ARM = true;

// Safe initial throttle limits. Calibrate on the real controller.
constexpr uint8_t DAC_DISARMED = 0;
constexpr uint8_t DAC_IDLE = 77;       // approximately 1.00 V
constexpr uint8_t DAC_MAX_TEST = 220;  // approximately 2.85 V
constexpr int16_t RAMP_STEP_PER_TICK = 12;
constexpr bool HOLD_BRAKE_AT_ZERO = true;
constexpr bool REVERSE_SUPPORTED = true;

// HIGH lights the TLP240A input LED and closes its output contact.
constexpr bool BRAKE_ACTIVE_HIGH = true;
constexpr bool REVERSE_ACTIVE_HIGH = true;
}  // namespace Config

enum class ControlMode : uint8_t {
  Safe = 0,
  RcManual = 1,
  RosAutonomous = 2,
};

struct Track {
  uint8_t throttlePin;
  uint8_t reversePin;
  uint8_t brakePin;
  int16_t target = 0;
  int16_t actual = 0;
  int8_t appliedSign = 1;
  enum class Phase : uint8_t { Normal, BrakeBeforeReverse, ReverseSettle };
  Phase phase = Phase::Normal;
  uint32_t deadlineMs = 0;
};

struct RcCapture {
  volatile uint32_t riseUs = 0;
  volatile uint32_t lastPulseUs = 0;
  volatile uint16_t pulseUs = 0;
};

struct RcSnapshot {
  uint16_t channel1Us = 0;
  uint16_t channel2Us = 0;
  uint16_t modeUs = 0;
  uint16_t armUs = 0;
  uint32_t channel1AgeUs = UINT32_MAX;
  uint32_t channel2AgeUs = UINT32_MAX;
  uint32_t modeAgeUs = UINT32_MAX;
  uint32_t armAgeUs = UINT32_MAX;
  bool valid = false;
};

Track leftTrack{
    Pins::LEFT_THROTTLE_DAC,
    Pins::LEFT_REVERSE,
    Pins::LEFT_BRAKE,
};
Track rightTrack{
    Pins::RIGHT_THROTTLE_DAC,
    Pins::RIGHT_REVERSE,
    Pins::RIGHT_BRAKE,
};

portMUX_TYPE hallMux = portMUX_INITIALIZER_UNLOCKED;
portMUX_TYPE rcMux = portMUX_INITIALIZER_UNLOCKED;

volatile int64_t leftTicks = 0;
volatile int64_t rightTicks = 0;
volatile uint32_t leftWindowPulses = 0;
volatile uint32_t rightWindowPulses = 0;
volatile int8_t leftPulseSign = 1;
volatile int8_t rightPulseSign = 1;

RcCapture rcChannel1;
RcCapture rcChannel2;
RcCapture rcMode;
RcCapture rcArm;

bool armed = false;
bool watchdogTripped = false;
bool rcArmSeenOff = false;
bool lastRcValid = false;
ControlMode controlMode = ControlMode::Safe;
uint32_t lastDriveFrameMs = 0;
uint32_t lastControlMs = 0;
uint32_t lastTelemetryMs = 0;
uint32_t lastTelemetryPulseMs = 0;
uint32_t lastSequence = 0;
RcSnapshot lastRcSnapshot;

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

void IRAM_ATTR captureRcEdge(uint8_t pin, RcCapture& channel) {
  const uint32_t nowUs = micros();
  portENTER_CRITICAL_ISR(&rcMux);
  if (digitalRead(pin) == HIGH) {
    channel.riseUs = nowUs;
  } else {
    const uint32_t width = nowUs - channel.riseUs;
    if (width <= 65535U) {
      channel.pulseUs = static_cast<uint16_t>(width);
      channel.lastPulseUs = nowUs;
    }
  }
  portEXIT_CRITICAL_ISR(&rcMux);
}

void IRAM_ATTR onRcChannel1() {
  captureRcEdge(Pins::RC_CHANNEL_1, rcChannel1);
}
void IRAM_ATTR onRcChannel2() {
  captureRcEdge(Pins::RC_CHANNEL_2, rcChannel2);
}
void IRAM_ATTR onRcMode() {
  captureRcEdge(Pins::RC_MODE, rcMode);
}
void IRAM_ATTR onRcArm() {
  captureRcEdge(Pins::RC_ARM, rcArm);
}

bool outputLevel(bool active, bool activeHigh) {
  return activeHigh ? active : !active;
}

void setBrake(const Track& track, bool active) {
  digitalWrite(track.brakePin, outputLevel(active, Config::BRAKE_ACTIVE_HIGH));
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
  return digitalRead(Pins::ESTOP_OK) == LOW;
}

void applyTrackSafe(Track& track) {
  track.target = 0;
  track.actual = 0;
  track.phase = Track::Phase::Normal;
  writeThrottle(track, Config::DAC_DISARMED);
  setBrake(track, true);
}

void disarmSystem(const char* reason) {
  // Remember the last reported reason so a persistent fault does not flood
  // USB Serial and the ROS log every 20 ms. A changed reason is still reported
  // immediately, and a new disarm after arming is always reported.
  static char lastReportedReason[32] = "";

  const bool wasArmed = armed;
  armed = false;
  applyTrackSafe(leftTrack);
  applyTrackSafe(rightTrack);
  digitalWrite(Pins::STATUS_LED, LOW);

  const char* eventReason = reason != nullptr ? reason : "requested";
  const bool reasonChanged =
      reason != nullptr &&
      strncmp(lastReportedReason, eventReason, sizeof(lastReportedReason)) != 0;

  if (wasArmed || reasonChanged) {
    Serial.print("EVT,DISARM,");
    Serial.println(eventReason);
  }

  if (reason == nullptr) {
    // A normal safe/disarmed pass clears the latch. The same fault will then
    // be reported again if it disappears and later returns.
    lastReportedReason[0] = '\0';
  } else {
    strncpy(lastReportedReason, eventReason, sizeof(lastReportedReason) - 1);
    lastReportedReason[sizeof(lastReportedReason) - 1] = '\0';
  }
}

bool armSystem(const char* source) {
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
  delay(50);

  armed = true;
  watchdogTripped = false;
  lastDriveFrameMs = millis();
  digitalWrite(Pins::STATUS_LED, HIGH);
  Serial.print("EVT,ARMED,");
  Serial.println(source != nullptr ? source : "UNKNOWN");
  return true;
}

void updateTrack(Track& track, uint32_t nowMs) {
  if (!armed) {
    applyTrackSafe(track);
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

bool pulseValid(uint16_t pulseUs, uint32_t ageUs) {
  return pulseUs >= Config::RC_MIN_VALID_US &&
         pulseUs <= Config::RC_MAX_VALID_US &&
         ageUs <= Config::RC_SIGNAL_TIMEOUT_US;
}

RcSnapshot readRcSnapshot() {
  RcSnapshot result;
  uint32_t channel1Last;
  uint32_t channel2Last;
  uint32_t modeLast;
  uint32_t armLast;
  const uint32_t nowUs = micros();

  portENTER_CRITICAL(&rcMux);
  result.channel1Us = rcChannel1.pulseUs;
  result.channel2Us = rcChannel2.pulseUs;
  result.modeUs = rcMode.pulseUs;
  result.armUs = rcArm.pulseUs;
  channel1Last = rcChannel1.lastPulseUs;
  channel2Last = rcChannel2.lastPulseUs;
  modeLast = rcMode.lastPulseUs;
  armLast = rcArm.lastPulseUs;
  portEXIT_CRITICAL(&rcMux);

  result.channel1AgeUs = channel1Last == 0 ? UINT32_MAX : nowUs - channel1Last;
  result.channel2AgeUs = channel2Last == 0 ? UINT32_MAX : nowUs - channel2Last;
  result.modeAgeUs = modeLast == 0 ? UINT32_MAX : nowUs - modeLast;
  result.armAgeUs = armLast == 0 ? UINT32_MAX : nowUs - armLast;
  result.valid =
      pulseValid(result.channel1Us, result.channel1AgeUs) &&
      pulseValid(result.channel2Us, result.channel2AgeUs) &&
      pulseValid(result.modeUs, result.modeAgeUs) &&
      pulseValid(result.armUs, result.armAgeUs);
  return result;
}

ControlMode modeFromPulse(uint16_t pulseUs) {
  if (pulseUs <= Config::RC_MODE_MANUAL_MAX_US) {
    return ControlMode::RcManual;
  }
  if (pulseUs >= Config::RC_MODE_ROS_MIN_US) {
    return ControlMode::RosAutonomous;
  }
  return ControlMode::Safe;
}

const char* modeName(ControlMode mode) {
  switch (mode) {
    case ControlMode::RcManual:
      return "RC";
    case ControlMode::RosAutonomous:
      return "ROS";
    default:
      return "SAFE";
  }
}

const char* rcInputModeName() {
  return Config::RC_PREMIXED_BY_TRANSMITTER ? "BOAT_MIX" : "ESP32_MIX";
}

int16_t pulseToCommand(uint16_t pulseUs, bool reversed) {
  int32_t delta = static_cast<int32_t>(pulseUs) - Config::RC_CENTER_US;
  if (abs(delta) <= Config::RC_DEADBAND_US) return 0;

  delta += delta > 0 ? -Config::RC_DEADBAND_US : Config::RC_DEADBAND_US;
  const int32_t usableSpan = 500 - Config::RC_DEADBAND_US;
  int32_t command = delta * 1000 / usableSpan;
  command = constrain(command, -1000, 1000);
  if (reversed) command = -command;
  return static_cast<int16_t>(command);
}

void readPremixedRcTracks(const RcSnapshot& rc, int16_t& left, int16_t& right) {
  left = pulseToCommand(rc.channel1Us, Config::RC_LEFT_REVERSED);
  right = pulseToCommand(rc.channel2Us, Config::RC_RIGHT_REVERSED);
}

void mixRcTracksOnEsp32(const RcSnapshot& rc, int16_t& left, int16_t& right) {
  const int16_t steering = pulseToCommand(
      rc.channel1Us,
      Config::RC_STEERING_REVERSED);
  const int16_t throttle = pulseToCommand(
      rc.channel2Us,
      Config::RC_THROTTLE_REVERSED);

  int32_t mixedLeft = static_cast<int32_t>(throttle) + steering;
  int32_t mixedRight = static_cast<int32_t>(throttle) - steering;
  const int32_t peak = max(abs(mixedLeft), abs(mixedRight));
  if (peak > 1000) {
    mixedLeft = mixedLeft * 1000 / peak;
    mixedRight = mixedRight * 1000 / peak;
  }

  left = static_cast<int16_t>(constrain(mixedLeft, -1000, 1000));
  right = static_cast<int16_t>(constrain(mixedRight, -1000, 1000));
}

void calculateRcTracks(const RcSnapshot& rc, int16_t& left, int16_t& right) {
  if (Config::RC_PREMIXED_BY_TRANSMITTER) {
    readPremixedRcTracks(rc, left, right);
  } else {
    mixRcTracksOnEsp32(rc, left, right);
  }
}

void changeMode(ControlMode nextMode) {
  if (nextMode == controlMode) return;
  disarmSystem("MODE_CHANGE");
  controlMode = nextMode;
  rcArmSeenOff = false;
  Serial.print("EVT,MODE,");
  Serial.println(modeName(controlMode));
}

void updateCommandSource() {
  const RcSnapshot rc = readRcSnapshot();
  lastRcSnapshot = rc;
  lastRcValid = rc.valid;

  const ControlMode requestedMode = rc.valid
      ? modeFromPulse(rc.modeUs)
      : ControlMode::Safe;
  changeMode(requestedMode);

  if (!estopOkay()) {
    disarmSystem("ESTOP");
    return;
  }
  if (controlMode == ControlMode::Safe) {
    disarmSystem(nullptr);
    return;
  }
  if (!rc.valid) {
    disarmSystem("RC_SIGNAL_LOST");
    return;
  }

  if (rc.armUs <= Config::RC_ARM_OFF_MAX_US) {
    rcArmSeenOff = true;
    disarmSystem(nullptr);
    return;
  }

  const bool physicalPermission =
      rcArmSeenOff && rc.armUs >= Config::RC_ARM_ON_MIN_US;

  if (controlMode == ControlMode::RcManual) {
    if (!physicalPermission) {
      disarmSystem(nullptr);
      return;
    }
    if (!armed && !armSystem("RC")) return;
    calculateRcTracks(rc, leftTrack.target, rightTrack.target);
    return;
  }

  if (Config::ROS_REQUIRES_RC_ARM && !physicalPermission) {
    disarmSystem(nullptr);
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
  const String body = "ACK," + String(sequence) + "," + result + "," +
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

    lastDriveFrameMs = millis();
    watchdogTripped = false;
    if (controlMode != ControlMode::RosAutonomous) {
      sendAck(sequence, "NOT_ROS_MODE");
      return;
    }
    if (!armed) {
      sendAck(sequence, "DISARMED");
      return;
    }

    leftTrack.target = clampCommand(strtol(leftText, nullptr, 10));
    rightTrack.target = clampCommand(strtol(rightText, nullptr, 10));
    sendAck(sequence, "OK");
    return;
  }

  if (strcmp(command, "ARM") == 0) {
    char* valueText = strtok_r(nullptr, ",", &save);
    const bool requestArm = valueText != nullptr && atoi(valueText) != 0;
    if (!requestArm) {
      disarmSystem("REMOTE");
      sendAck(sequence, "OK");
      return;
    }
    if (controlMode != ControlMode::RosAutonomous) {
      sendAck(sequence, "NOT_ROS_MODE");
      return;
    }
    if (!lastRcSnapshot.valid ||
        (Config::ROS_REQUIRES_RC_ARM &&
         !(rcArmSeenOff &&
           lastRcSnapshot.armUs >= Config::RC_ARM_ON_MIN_US))) {
      sendAck(sequence, "RC_PERMISSION_REQUIRED");
      return;
    }

    leftTrack.target = 0;
    rightTrack.target = 0;
    sendAck(sequence, armSystem("ROS") ? "OK" : "REFUSED");
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

  uint32_t elapsed = nowMs - lastTelemetryPulseMs;
  if (elapsed == 0) elapsed = 1;
  lastTelemetryPulseMs = nowMs;
  const uint32_t leftPps = leftPulseSnapshot * 1000UL / elapsed;
  const uint32_t rightPps = rightPulseSnapshot * 1000UL / elapsed;

  char body[280];
  snprintf(
      body,
      sizeof(body),
      "TEL,%lu,%d,%d,%d,%d,%d,%d,%lld,%lld,%lu,%lu,%d,%s,%u,%u,%u,%u,%d,%s",
      static_cast<unsigned long>(nowMs),
      armed ? 1 : 0,
      estopOkay() ? 1 : 0,
      leftTrack.target,
      rightTrack.target,
      leftTrack.actual,
      rightTrack.actual,
      static_cast<long long>(leftTickSnapshot),
      static_cast<long long>(rightTickSnapshot),
      static_cast<unsigned long>(leftPps),
      static_cast<unsigned long>(rightPps),
      watchdogTripped ? 1 : 0,
      modeName(controlMode),
      lastRcSnapshot.channel1Us,
      lastRcSnapshot.channel2Us,
      lastRcSnapshot.modeUs,
      lastRcSnapshot.armUs,
      lastRcValid ? 1 : 0,
      rcInputModeName());
  sendFrame(String(body));
}

void setup() {
  Serial.begin(Config::SERIAL_BAUD);
  delay(200);

  pinMode(Pins::LEFT_REVERSE, OUTPUT);
  pinMode(Pins::RIGHT_REVERSE, OUTPUT);
  pinMode(Pins::LEFT_BRAKE, OUTPUT);
  pinMode(Pins::RIGHT_BRAKE, OUTPUT);
  pinMode(Pins::STATUS_LED, OUTPUT);
  pinMode(Pins::ESTOP_OK, INPUT_PULLUP);
  pinMode(Pins::LEFT_HALL, INPUT);
  pinMode(Pins::RIGHT_HALL, INPUT);
  pinMode(Pins::RC_CHANNEL_1, INPUT);
  pinMode(Pins::RC_CHANNEL_2, INPUT);
  pinMode(Pins::RC_MODE, INPUT);
  pinMode(Pins::RC_ARM, INPUT);

  // Safe output state before interrupts and command processing start.
  digitalWrite(Pins::LEFT_REVERSE, LOW);
  digitalWrite(Pins::RIGHT_REVERSE, LOW);
  digitalWrite(Pins::LEFT_BRAKE, HIGH);
  digitalWrite(Pins::RIGHT_BRAKE, HIGH);
  digitalWrite(Pins::STATUS_LED, LOW);
  setReverse(leftTrack, false);
  setReverse(rightTrack, false);
  applyTrackSafe(leftTrack);
  applyTrackSafe(rightTrack);

  attachInterrupt(digitalPinToInterrupt(Pins::LEFT_HALL), onLeftHall, RISING);
  attachInterrupt(digitalPinToInterrupt(Pins::RIGHT_HALL), onRightHall, RISING);
  attachInterrupt(digitalPinToInterrupt(Pins::RC_CHANNEL_1), onRcChannel1, CHANGE);
  attachInterrupt(digitalPinToInterrupt(Pins::RC_CHANNEL_2), onRcChannel2, CHANGE);
  attachInterrupt(digitalPinToInterrupt(Pins::RC_MODE), onRcMode, CHANGE);
  attachInterrupt(digitalPinToInterrupt(Pins::RC_ARM), onRcArm, CHANGE);

  lastControlMs = millis();
  lastTelemetryMs = millis();
  lastTelemetryPulseMs = millis();
  sendFrame("BOOT,ESP32_WROOM_TRACK_CONTROLLER,4,TLP240A_GPIO21_22_FREE");
}

void loop() {
  readSerialFrames();
  const uint32_t nowMs = millis();

  if (!estopOkay() && armed) {
    disarmSystem("ESTOP");
  }

  if (nowMs - lastControlMs >= Config::CONTROL_PERIOD_MS) {
    lastControlMs = nowMs;
    updateCommandSource();

    if (controlMode == ControlMode::RosAutonomous && armed &&
        nowMs - lastDriveFrameMs > Config::COMMAND_WATCHDOG_MS) {
      watchdogTripped = true;
      disarmSystem("WATCHDOG");
    }

    updateTrack(leftTrack, nowMs);
    updateTrack(rightTrack, nowMs);
  }

  if (nowMs - lastTelemetryMs >= Config::TELEMETRY_PERIOD_MS) {
    lastTelemetryMs = nowMs;
    sendTelemetry(nowMs);
  }
}
