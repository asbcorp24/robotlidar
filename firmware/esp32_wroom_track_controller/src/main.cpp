#include <Arduino.h>
#include <Wire.h>

// ============================================================
// RobotLidar ESP32-WROOM dual-track controller
//
// Hardware profiles:
//   ROBOTLIDAR_HW_LEGACY (default)
//     GPIO25 -> internal DAC throttle LEFT
//     GPIO26 -> internal DAC throttle RIGHT
//     GPIO21 -> BTS7960 actuator RPWM
//     GPIO22 -> BTS7960 actuator LPWM
//
//   ROBOTLIDAR_HW_MCP4725_HW399
//     GPIO21 -> I2C SDA through BSS138
//     GPIO22 -> I2C SCL through BSS138
//     MCP4725 LEFT  address 0x60
//     MCP4725 RIGHT address 0x61
//     GPIO25 -> BTS7960 actuator RPWM
//     GPIO26 -> BTS7960 actuator LPWM
//
// RC receiver:
//   CH1 -> GPIO27 left track
//   CH2 -> GPIO33 right track
//   CH3 -> GPIO36 actuator
//   CH5 -> GPIO13 three-position mode RC / SAFE / ROS
//   GPIO14 is FREE: no separate RC ARM channel.
//
// HW-399 #1:
//   GPIO16 -> Reverse LEFT
//   GPIO17 -> Reverse RIGHT
//   GPIO18 -> Brake LEFT
//   GPIO19 -> Brake RIGHT
//
// HW-399 #2 (Hall, when enabled):
//   OUT1 -> GPIO34 Hall LEFT
//   OUT2 -> GPIO35 Hall RIGHT
//
// E-stop:
//   GPIO32 -> NC contact -> GND, LOW = healthy.
// ============================================================

#define ROBOTLIDAR_HW_LEGACY 0
#define ROBOTLIDAR_HW_MCP4725_HW399 1
// Backward-compatible name for existing build flags.
#define ROBOTLIDAR_HW_MCP4725_PC817 ROBOTLIDAR_HW_MCP4725_HW399

#ifndef ROBOTLIDAR_HW_PROFILE
#define ROBOTLIDAR_HW_PROFILE ROBOTLIDAR_HW_LEGACY
#endif

#ifndef ROBOTLIDAR_ENABLE_RC_ACTUATOR
#define ROBOTLIDAR_ENABLE_RC_ACTUATOR 1
#endif

// Hall is currently not connected on the bench. Set to 1 after HW-399 #2
// and both Hall sensors are physically wired.
#ifndef ROBOTLIDAR_ENABLE_HALL
#define ROBOTLIDAR_ENABLE_HALL 0
#endif

#ifndef ROBOTLIDAR_HALL_INVERTED
// HW-399 phototransistor output normally inverts the Hall signal.
#define ROBOTLIDAR_HALL_INVERTED 1
#endif

#if ROBOTLIDAR_HW_PROFILE != ROBOTLIDAR_HW_LEGACY && \
    ROBOTLIDAR_HW_PROFILE != ROBOTLIDAR_HW_MCP4725_HW399
#error "Unsupported ROBOTLIDAR_HW_PROFILE"
#endif

namespace Pins {
constexpr uint8_t LEFT_THROTTLE_DAC = 25;
constexpr uint8_t RIGHT_THROTTLE_DAC = 26;
constexpr uint8_t I2C_SDA = 21;
constexpr uint8_t I2C_SCL = 22;

#if ROBOTLIDAR_HW_PROFILE == ROBOTLIDAR_HW_MCP4725_HW399
constexpr uint8_t ACTUATOR_RPWM = 25;
constexpr uint8_t ACTUATOR_LPWM = 26;
#else
constexpr uint8_t ACTUATOR_RPWM = 21;
constexpr uint8_t ACTUATOR_LPWM = 22;
#endif

constexpr uint8_t LEFT_REVERSE = 16;
constexpr uint8_t RIGHT_REVERSE = 17;
constexpr uint8_t LEFT_BRAKE = 18;
constexpr uint8_t RIGHT_BRAKE = 19;

constexpr uint8_t ESTOP_OK = 32;
constexpr uint8_t LEFT_HALL = 34;
constexpr uint8_t RIGHT_HALL = 35;

constexpr uint8_t RC_CHANNEL_1 = 27;
constexpr uint8_t RC_CHANNEL_2 = 33;
constexpr uint8_t RC_ACTUATOR = 36;
constexpr uint8_t RC_MODE = 13;

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

constexpr uint16_t RC_MIN_VALID_US = 800;
constexpr uint16_t RC_MAX_VALID_US = 2200;
constexpr uint16_t RC_CENTER_US = 1500;
constexpr uint16_t RC_DEADBAND_US = 45;

// CH5 three-position switch:
//   <= 1300 us : RC manual
//   1301..1699 : SAFE
//   >= 1700 us : ROS
constexpr uint16_t RC_MODE_MANUAL_MAX_US = 1300;
constexpr uint16_t RC_MODE_ROS_MIN_US = 1700;

constexpr bool RC_PREMIXED_BY_TRANSMITTER = true;
constexpr bool RC_LEFT_REVERSED = false;
constexpr bool RC_RIGHT_REVERSED = false;
constexpr bool RC_THROTTLE_REVERSED = false;
constexpr bool RC_STEERING_REVERSED = false;

constexpr uint16_t THROTTLE_DISARMED_MV = 0;
constexpr uint16_t THROTTLE_IDLE_MV = 1000;
constexpr uint16_t THROTTLE_MAX_TEST_MV = 2850;
constexpr uint16_t INTERNAL_DAC_FULL_SCALE_MV = 3300;
constexpr uint16_t MCP4725_FULL_SCALE_MV = 5000;

constexpr int16_t RAMP_STEP_PER_TICK = 12;
constexpr bool HOLD_BRAKE_AT_ZERO = true;
constexpr bool REVERSE_SUPPORTED = true;

constexpr uint8_t MCP4725_LEFT_ADDRESS = 0x60;
constexpr uint8_t MCP4725_RIGHT_ADDRESS = 0x61;
constexpr uint32_t I2C_CLOCK_HZ = 100000;

constexpr bool BRAKE_ACTIVE_HIGH = true;
constexpr bool REVERSE_ACTIVE_HIGH = true;

constexpr uint16_t ACTUATOR_RETRACT_MAX_US = 1400;
constexpr uint16_t ACTUATOR_EXTEND_MIN_US = 1600;
constexpr bool ACTUATOR_REVERSED = false;
constexpr uint8_t ACTUATOR_PWM = 255;
constexpr uint32_t ACTUATOR_MAX_RUN_MS = 30000;
constexpr uint32_t ACTUATOR_REVERSE_GUARD_MS = 150;
constexpr uint32_t ACTUATOR_PWM_HZ = 20000;
constexpr uint8_t ACTUATOR_PWM_BITS = 8;
constexpr uint8_t ACTUATOR_RPWM_CHANNEL = 6;
constexpr uint8_t ACTUATOR_LPWM_CHANNEL = 7;

#if ROBOTLIDAR_HALL_INVERTED
constexpr int HALL_INTERRUPT_EDGE = FALLING;
#else
constexpr int HALL_INTERRUPT_EDGE = RISING;
#endif
}  // namespace Config

enum class ControlMode : uint8_t {
  Safe = 0,
  RcManual = 1,
  RosAutonomous = 2,
};

enum class TrackSide : uint8_t {
  Left = 0,
  Right = 1,
};

struct Track {
  TrackSide side;
  uint8_t throttlePin;
  uint8_t reversePin;
  uint8_t brakePin;
  int16_t target = 0;
  int16_t actual = 0;
  int8_t appliedSign = 1;

  enum class Phase : uint8_t {
    Normal,
    BrakeBeforeReverse,
    ReverseSettle,
  };

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
  uint16_t actuatorUs = 0;
  uint16_t modeUs = 0;

  uint32_t channel1AgeUs = UINT32_MAX;
  uint32_t channel2AgeUs = UINT32_MAX;
  uint32_t actuatorAgeUs = UINT32_MAX;
  uint32_t modeAgeUs = UINT32_MAX;

  bool valid = false;
  bool actuatorValid = false;
  bool modeValid = false;
};

Track leftTrack{
    TrackSide::Left,
    Pins::LEFT_THROTTLE_DAC,
    Pins::LEFT_REVERSE,
    Pins::LEFT_BRAKE,
};

Track rightTrack{
    TrackSide::Right,
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
RcCapture rcActuator;
RcCapture rcMode;

bool armed = false;
bool watchdogTripped = false;
bool lastRcValid = false;

#if ROBOTLIDAR_HW_PROFILE == ROBOTLIDAR_HW_LEGACY
bool throttleBackendReady = true;
#else
bool throttleBackendReady = false;
#endif

ControlMode controlMode = ControlMode::Safe;
uint32_t lastDriveFrameMs = 0;
uint32_t lastControlMs = 0;
uint32_t lastTelemetryMs = 0;
uint32_t lastTelemetryPulseMs = 0;
uint32_t lastSequence = 0;
RcSnapshot lastRcSnapshot;

int8_t actuatorAppliedDirection = 0;
int8_t actuatorPendingDirection = 0;
uint32_t actuatorRunStartMs = 0;
uint32_t actuatorGuardUntilMs = 0;
bool actuatorTimeoutLatched = false;
bool actuatorNeutralSeen = false;

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

void IRAM_ATTR onRcActuator() {
  captureRcEdge(Pins::RC_ACTUATOR, rcActuator);
}

void IRAM_ATTR onRcMode() {
  captureRcEdge(Pins::RC_MODE, rcMode);
}

bool outputLevel(bool active, bool activeHigh) {
  return activeHigh ? active : !active;
}

void setBrake(const Track& track, bool active) {
  digitalWrite(
      track.brakePin,
      outputLevel(active, Config::BRAKE_ACTIVE_HIGH));
}

void setReverse(Track& track, bool reverse) {
  digitalWrite(
      track.reversePin,
      outputLevel(reverse, Config::REVERSE_ACTIVE_HIGH));

  track.appliedSign = reverse ? -1 : 1;

  portENTER_CRITICAL(&hallMux);
  if (track.side == TrackSide::Left) {
    leftPulseSign = track.appliedSign;
  } else {
    rightPulseSign = track.appliedSign;
  }
  portEXIT_CRITICAL(&hallMux);
}

#if ROBOTLIDAR_HW_PROFILE == ROBOTLIDAR_HW_MCP4725_HW399
bool i2cDevicePresent(uint8_t address) {
  Wire.beginTransmission(address);
  return Wire.endTransmission() == 0;
}

uint16_t millivoltsToMcp4725Code(uint16_t millivolts) {
  uint32_t limited = millivolts;
  if (limited > Config::MCP4725_FULL_SCALE_MV) {
    limited = Config::MCP4725_FULL_SCALE_MV;
  }

  return static_cast<uint16_t>(
      (limited * 4095UL + Config::MCP4725_FULL_SCALE_MV / 2) /
      Config::MCP4725_FULL_SCALE_MV);
}

bool writeMcp4725(uint8_t address, uint16_t millivolts) {
  const uint16_t value = millivoltsToMcp4725Code(millivolts);

  // Fast-mode DAC register write only; EEPROM is not written.
  Wire.beginTransmission(address);
  Wire.write(static_cast<uint8_t>((value >> 8) & 0x0F));
  Wire.write(static_cast<uint8_t>(value & 0xFF));
  return Wire.endTransmission() == 0;
}
#endif

uint8_t millivoltsToInternalDacCode(uint16_t millivolts) {
  uint32_t limited = millivolts;
  if (limited > Config::INTERNAL_DAC_FULL_SCALE_MV) {
    limited = Config::INTERNAL_DAC_FULL_SCALE_MV;
  }

  return static_cast<uint8_t>(
      (limited * 255UL + Config::INTERNAL_DAC_FULL_SCALE_MV / 2) /
      Config::INTERNAL_DAC_FULL_SCALE_MV);
}

bool writeThrottle(const Track& track, uint16_t millivolts) {
#if ROBOTLIDAR_HW_PROFILE == ROBOTLIDAR_HW_MCP4725_HW399
  if (!throttleBackendReady) {
    return false;
  }

  const uint8_t address =
      track.side == TrackSide::Left
          ? Config::MCP4725_LEFT_ADDRESS
          : Config::MCP4725_RIGHT_ADDRESS;

  if (!writeMcp4725(address, millivolts)) {
    throttleBackendReady = false;
    return false;
  }

  return true;
#else
  dacWrite(
      track.throttlePin,
      millivoltsToInternalDacCode(millivolts));
  return true;
#endif
}

bool initializeThrottleBackend() {
#if ROBOTLIDAR_HW_PROFILE == ROBOTLIDAR_HW_MCP4725_HW399
  static_assert(
      Config::MCP4725_LEFT_ADDRESS != Config::MCP4725_RIGHT_ADDRESS,
      "MCP4725 addresses must be different");

  Wire.begin(Pins::I2C_SDA, Pins::I2C_SCL);
  Wire.setClock(Config::I2C_CLOCK_HZ);

  const bool leftPresent =
      i2cDevicePresent(Config::MCP4725_LEFT_ADDRESS);
  const bool rightPresent =
      i2cDevicePresent(Config::MCP4725_RIGHT_ADDRESS);

  if (!leftPresent) {
    Serial.println("ERR,MCP4725_LEFT_NOT_FOUND");
  }
  if (!rightPresent) {
    Serial.println("ERR,MCP4725_RIGHT_NOT_FOUND");
  }

  if (!leftPresent || !rightPresent) {
    throttleBackendReady = false;
    return false;
  }

  const bool leftSafe = writeMcp4725(
      Config::MCP4725_LEFT_ADDRESS,
      Config::THROTTLE_DISARMED_MV);
  const bool rightSafe = writeMcp4725(
      Config::MCP4725_RIGHT_ADDRESS,
      Config::THROTTLE_DISARMED_MV);

  throttleBackendReady = leftSafe && rightSafe;
  if (!throttleBackendReady) {
    Serial.println("ERR,MCP4725_SAFE_WRITE_FAILED");
  }
  return throttleBackendReady;
#else
  throttleBackendReady = true;
  dacWrite(
      Pins::LEFT_THROTTLE_DAC,
      millivoltsToInternalDacCode(Config::THROTTLE_DISARMED_MV));
  dacWrite(
      Pins::RIGHT_THROTTLE_DAC,
      millivoltsToInternalDacCode(Config::THROTTLE_DISARMED_MV));
  return true;
#endif
}

const char* hardwareProfileName() {
#if ROBOTLIDAR_HW_PROFILE == ROBOTLIDAR_HW_MCP4725_HW399
  return "MCP4725_HW399";
#else
  return "LEGACY_INTERNAL_DAC";
#endif
}

bool estopOkay() {
  return digitalRead(Pins::ESTOP_OK) == LOW;
}

void initializeActuator() {
#if ROBOTLIDAR_ENABLE_RC_ACTUATOR
  pinMode(Pins::ACTUATOR_RPWM, OUTPUT);
  pinMode(Pins::ACTUATOR_LPWM, OUTPUT);

  digitalWrite(Pins::ACTUATOR_RPWM, LOW);
  digitalWrite(Pins::ACTUATOR_LPWM, LOW);

  ledcSetup(
      Config::ACTUATOR_RPWM_CHANNEL,
      Config::ACTUATOR_PWM_HZ,
      Config::ACTUATOR_PWM_BITS);
  ledcSetup(
      Config::ACTUATOR_LPWM_CHANNEL,
      Config::ACTUATOR_PWM_HZ,
      Config::ACTUATOR_PWM_BITS);

  ledcAttachPin(
      Pins::ACTUATOR_RPWM,
      Config::ACTUATOR_RPWM_CHANNEL);
  ledcAttachPin(
      Pins::ACTUATOR_LPWM,
      Config::ACTUATOR_LPWM_CHANNEL);

  ledcWrite(Config::ACTUATOR_RPWM_CHANNEL, 0);
  ledcWrite(Config::ACTUATOR_LPWM_CHANNEL, 0);
#endif
}

void stopActuatorOutput() {
#if ROBOTLIDAR_ENABLE_RC_ACTUATOR
  ledcWrite(Config::ACTUATOR_RPWM_CHANNEL, 0);
  ledcWrite(Config::ACTUATOR_LPWM_CHANNEL, 0);
#endif
  actuatorAppliedDirection = 0;
}

void applyActuatorOutput(int8_t direction) {
#if ROBOTLIDAR_ENABLE_RC_ACTUATOR
  ledcWrite(Config::ACTUATOR_RPWM_CHANNEL, 0);
  ledcWrite(Config::ACTUATOR_LPWM_CHANNEL, 0);

  if (direction > 0) {
    ledcWrite(
        Config::ACTUATOR_RPWM_CHANNEL,
        Config::ACTUATOR_PWM);
  } else if (direction < 0) {
    ledcWrite(
        Config::ACTUATOR_LPWM_CHANNEL,
        Config::ACTUATOR_PWM);
  }
#endif

  actuatorAppliedDirection = direction;
}

int8_t requestedActuatorDirection(uint16_t pulseUs) {
  int8_t direction = 0;

  if (pulseUs <= Config::ACTUATOR_RETRACT_MAX_US) {
    direction = -1;
  } else if (pulseUs >= Config::ACTUATOR_EXTEND_MIN_US) {
    direction = 1;
  }

  if (Config::ACTUATOR_REVERSED) {
    direction = -direction;
  }
  return direction;
}

void updateRcActuator(const RcSnapshot& rc, uint32_t nowMs) {
#if !ROBOTLIDAR_ENABLE_RC_ACTUATOR
  (void)rc;
  (void)nowMs;
  return;
#else
  if (!estopOkay() ||
      controlMode == ControlMode::Safe ||
      !rc.actuatorValid) {
    stopActuatorOutput();
    actuatorPendingDirection = 0;
    actuatorGuardUntilMs = 0;
    actuatorNeutralSeen = false;
    return;
  }

  const int8_t requested = requestedActuatorDirection(rc.actuatorUs);

  // After SAFE or a mode change CH3 must be returned to neutral once.
  if (requested == 0) {
    stopActuatorOutput();
    actuatorPendingDirection = 0;
    actuatorGuardUntilMs = 0;
    actuatorTimeoutLatched = false;
    actuatorNeutralSeen = true;
    return;
  }

  if (!actuatorNeutralSeen || actuatorTimeoutLatched) {
    stopActuatorOutput();
    return;
  }

  if (actuatorAppliedDirection != 0 &&
      requested != actuatorAppliedDirection) {
    stopActuatorOutput();
    actuatorPendingDirection = requested;
    actuatorGuardUntilMs =
        nowMs + Config::ACTUATOR_REVERSE_GUARD_MS;
    return;
  }

  if (actuatorPendingDirection != 0) {
    if (requested != actuatorPendingDirection) {
      actuatorPendingDirection = requested;
      actuatorGuardUntilMs =
          nowMs + Config::ACTUATOR_REVERSE_GUARD_MS;
    }

    if (static_cast<int32_t>(nowMs - actuatorGuardUntilMs) < 0) {
      stopActuatorOutput();
      return;
    }

    applyActuatorOutput(actuatorPendingDirection);
    actuatorRunStartMs = nowMs;
    actuatorPendingDirection = 0;
    return;
  }

  if (actuatorAppliedDirection == 0) {
    applyActuatorOutput(requested);
    actuatorRunStartMs = nowMs;
    return;
  }

  if (nowMs - actuatorRunStartMs >= Config::ACTUATOR_MAX_RUN_MS) {
    stopActuatorOutput();
    actuatorTimeoutLatched = true;
    Serial.println("EVT,ACTUATOR,TIMEOUT");
  }
#endif
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

uint16_t commandToThrottleMillivolts(int16_t signedCommand) {
  const int magnitude = abs(signedCommand);
  if (magnitude <= 0) {
    return Config::THROTTLE_IDLE_MV;
  }

  const uint32_t span =
      Config::THROTTLE_MAX_TEST_MV - Config::THROTTLE_IDLE_MV;

  return static_cast<uint16_t>(
      Config::THROTTLE_IDLE_MV +
      (span * constrain(magnitude, 0, 1000)) / 1000);
}

void applyTrackSafe(Track& track) {
  track.target = 0;
  track.actual = 0;
  track.phase = Track::Phase::Normal;
  setBrake(track, true);
  writeThrottle(track, Config::THROTTLE_DISARMED_MV);
}

void disarmSystem(const char* reason) {
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
  if (!throttleBackendReady) {
    Serial.println("ERR,THROTTLE_DAC_NOT_READY");
    return false;
  }
  if (leftTrack.target != 0 || rightTrack.target != 0) {
    Serial.println("ERR,NONZERO_TARGET");
    return false;
  }

  setBrake(leftTrack, true);
  setBrake(rightTrack, true);

  const bool leftIdle = writeThrottle(leftTrack, Config::THROTTLE_IDLE_MV);
  const bool rightIdle = writeThrottle(rightTrack, Config::THROTTLE_IDLE_MV);

  if (!leftIdle || !rightIdle) {
    setBrake(leftTrack, true);
    setBrake(rightTrack, true);
    Serial.println("ERR,THROTTLE_DAC_WRITE");
    return false;
  }

  setReverse(leftTrack, false);
  setReverse(rightTrack, false);
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

  if (!Config::REVERSE_SUPPORTED && track.target < 0) {
    track.target = 0;
  }

  const int8_t desiredSign = signOf(track.target);

  if (track.phase == Track::Phase::BrakeBeforeReverse) {
    setBrake(track, true);
    writeThrottle(track, Config::THROTTLE_IDLE_MV);
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
    setBrake(track, true);
    writeThrottle(track, Config::THROTTLE_IDLE_MV);
    track.actual = 0;

    if (static_cast<int32_t>(nowMs - track.deadlineMs) >= 0) {
      track.phase = Track::Phase::Normal;
    }
    return;
  }

  if (desiredSign != 0 && desiredSign != track.appliedSign) {
    track.actual = 0;
    setBrake(track, true);
    writeThrottle(track, Config::THROTTLE_IDLE_MV);
    track.phase = Track::Phase::BrakeBeforeReverse;
    track.deadlineMs = nowMs + Config::REVERSE_BRAKE_MS;
    return;
  }

  track.actual = moveToward(
      track.actual,
      track.target,
      Config::RAMP_STEP_PER_TICK);

  if (track.actual == 0) {
    setBrake(track, Config::HOLD_BRAKE_AT_ZERO);
    if (!writeThrottle(track, Config::THROTTLE_IDLE_MV)) {
      setBrake(track, true);
    }
    return;
  }

  if (!writeThrottle(
          track,
          commandToThrottleMillivolts(track.actual))) {
    track.actual = 0;
    setBrake(track, true);
    return;
  }

  setBrake(track, false);
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
  uint32_t actuatorLast;
  uint32_t modeLast;
  const uint32_t nowUs = micros();

  portENTER_CRITICAL(&rcMux);
  result.channel1Us = rcChannel1.pulseUs;
  result.channel2Us = rcChannel2.pulseUs;
  result.actuatorUs = rcActuator.pulseUs;
  result.modeUs = rcMode.pulseUs;
  channel1Last = rcChannel1.lastPulseUs;
  channel2Last = rcChannel2.lastPulseUs;
  actuatorLast = rcActuator.lastPulseUs;
  modeLast = rcMode.lastPulseUs;
  portEXIT_CRITICAL(&rcMux);

  result.channel1AgeUs =
      channel1Last == 0 ? UINT32_MAX : nowUs - channel1Last;
  result.channel2AgeUs =
      channel2Last == 0 ? UINT32_MAX : nowUs - channel2Last;
  result.actuatorAgeUs =
      actuatorLast == 0 ? UINT32_MAX : nowUs - actuatorLast;
  result.modeAgeUs =
      modeLast == 0 ? UINT32_MAX : nowUs - modeLast;

  result.modeValid = pulseValid(result.modeUs, result.modeAgeUs);
  result.actuatorValid =
      pulseValid(result.actuatorUs, result.actuatorAgeUs);
  result.valid =
      pulseValid(result.channel1Us, result.channel1AgeUs) &&
      pulseValid(result.channel2Us, result.channel2AgeUs) &&
      result.modeValid;

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
  return Config::RC_PREMIXED_BY_TRANSMITTER
      ? "BOAT_MIX"
      : "ESP32_MIX";
}

int16_t pulseToCommand(uint16_t pulseUs, bool reversed) {
  int32_t delta =
      static_cast<int32_t>(pulseUs) - Config::RC_CENTER_US;

  if (abs(delta) <= Config::RC_DEADBAND_US) {
    return 0;
  }

  delta += delta > 0
      ? -Config::RC_DEADBAND_US
      : Config::RC_DEADBAND_US;

  const int32_t usableSpan = 500 - Config::RC_DEADBAND_US;
  int32_t command = delta * 1000 / usableSpan;
  command = constrain(command, -1000, 1000);

  if (reversed) {
    command = -command;
  }
  return static_cast<int16_t>(command);
}

void readPremixedRcTracks(
    const RcSnapshot& rc,
    int16_t& left,
    int16_t& right) {
  left = pulseToCommand(rc.channel1Us, Config::RC_LEFT_REVERSED);
  right = pulseToCommand(rc.channel2Us, Config::RC_RIGHT_REVERSED);
}

void mixRcTracksOnEsp32(
    const RcSnapshot& rc,
    int16_t& left,
    int16_t& right) {
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

void calculateRcTracks(
    const RcSnapshot& rc,
    int16_t& left,
    int16_t& right) {
  if (Config::RC_PREMIXED_BY_TRANSMITTER) {
    readPremixedRcTracks(rc, left, right);
  } else {
    mixRcTracksOnEsp32(rc, left, right);
  }
}

bool rcTracksNeutral(const RcSnapshot& rc) {
  int16_t left = 0;
  int16_t right = 0;
  calculateRcTracks(rc, left, right);
  return left == 0 && right == 0;
}

void changeMode(ControlMode nextMode) {
  if (nextMode == controlMode) {
    return;
  }

  disarmSystem("MODE_CHANGE");
  stopActuatorOutput();
  actuatorPendingDirection = 0;
  actuatorGuardUntilMs = 0;
  actuatorNeutralSeen = false;
  controlMode = nextMode;

  Serial.print("EVT,MODE,");
  Serial.println(modeName(controlMode));
}

void updateCommandSource() {
  const RcSnapshot rc = readRcSnapshot();
  lastRcSnapshot = rc;
  lastRcValid = rc.valid;

  const ControlMode requestedMode =
      rc.modeValid ? modeFromPulse(rc.modeUs) : ControlMode::Safe;
  changeMode(requestedMode);

  if (!estopOkay()) {
    disarmSystem("ESTOP");
    return;
  }
  if (!throttleBackendReady) {
    disarmSystem("THROTTLE_DAC");
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

  if (controlMode == ControlMode::RcManual) {
    // No separate ARM channel. CH5 SAFE is the physical safety mode.
    // Before RC motion starts, both track commands must be neutral once.
    if (!armed) {
      if (!rcTracksNeutral(rc)) {
        disarmSystem("WAIT_NEUTRAL");
        return;
      }
      if (!armSystem("RC")) {
        return;
      }
    }

    calculateRcTracks(
        rc,
        leftTrack.target,
        rightTrack.target);
    return;
  }

  // ROS mode is physically permitted by CH5. Software ARM over USB is kept
  // for compatibility with the ROS bridge; it consumes no RC channel.
}

uint8_t xorChecksum(const char* text) {
  uint8_t checksum = 0;
  while (*text != '\0') {
    checksum ^= static_cast<uint8_t>(*text++);
  }
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
  const String body =
      "ACK," + String(sequence) + "," + result + "," +
      String(leftTrack.target) + "," + String(rightTrack.target);
  sendFrame(body);
}

bool verifyAndStripChecksum(char* line) {
  char* star = strrchr(line, '*');
  if (star == nullptr || strlen(star + 1) != 2) {
    return false;
  }

  const int high = hexNibble(star[1]);
  const int low = hexNibble(star[2]);
  if (high < 0 || low < 0) {
    return false;
  }

  const uint8_t received =
      static_cast<uint8_t>((high << 4) | low);
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
    const bool requestArm =
        valueText != nullptr && atoi(valueText) != 0;

    if (!requestArm) {
      disarmSystem("REMOTE");
      sendAck(sequence, "OK");
      return;
    }

    if (controlMode != ControlMode::RosAutonomous) {
      sendAck(sequence, "NOT_ROS_MODE");
      return;
    }

    leftTrack.target = 0;
    rightTrack.target = 0;
    sendAck(
        sequence,
        armSystem("ROS") ? "OK" : "REFUSED");
    return;
  }

  if (strcmp(command, "STOP") == 0) {
    disarmSystem("REMOTE_STOP");
    stopActuatorOutput();
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

    if (c == '\r') {
      continue;
    }

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

  // Keep the old telemetry field positions for the ROS bridge. Field 17,
  // previously rc_arm_us, is now reserved and always 0.
  char body[340];
  snprintf(
      body,
      sizeof(body),
      "TEL,%lu,%d,%d,%d,%d,%d,%d,"
      "%lld,%lld,%lu,%lu,%d,%s,"
      "%u,%u,%u,0,%d,%s,"
      "%u,%d,%d,%d",
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
      lastRcValid ? 1 : 0,
      rcInputModeName(),
      lastRcSnapshot.actuatorUs,
      lastRcSnapshot.actuatorValid ? 1 : 0,
      actuatorAppliedDirection,
      actuatorTimeoutLatched ? 1 : 0);

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

#if ROBOTLIDAR_ENABLE_HALL
  pinMode(Pins::LEFT_HALL, INPUT);
  pinMode(Pins::RIGHT_HALL, INPUT);
#endif

  pinMode(Pins::RC_CHANNEL_1, INPUT);
  pinMode(Pins::RC_CHANNEL_2, INPUT);
  pinMode(Pins::RC_ACTUATOR, INPUT);
  pinMode(Pins::RC_MODE, INPUT);

  digitalWrite(Pins::LEFT_REVERSE, LOW);
  digitalWrite(Pins::RIGHT_REVERSE, LOW);
  digitalWrite(Pins::LEFT_BRAKE, HIGH);
  digitalWrite(Pins::RIGHT_BRAKE, HIGH);
  digitalWrite(Pins::STATUS_LED, LOW);

  setReverse(leftTrack, false);
  setReverse(rightTrack, false);

  initializeActuator();
  stopActuatorOutput();

  initializeThrottleBackend();
  applyTrackSafe(leftTrack);
  applyTrackSafe(rightTrack);

#if ROBOTLIDAR_ENABLE_HALL
  attachInterrupt(
      digitalPinToInterrupt(Pins::LEFT_HALL),
      onLeftHall,
      Config::HALL_INTERRUPT_EDGE);
  attachInterrupt(
      digitalPinToInterrupt(Pins::RIGHT_HALL),
      onRightHall,
      Config::HALL_INTERRUPT_EDGE);
#endif

  attachInterrupt(
      digitalPinToInterrupt(Pins::RC_CHANNEL_1),
      onRcChannel1,
      CHANGE);
  attachInterrupt(
      digitalPinToInterrupt(Pins::RC_CHANNEL_2),
      onRcChannel2,
      CHANGE);
  attachInterrupt(
      digitalPinToInterrupt(Pins::RC_ACTUATOR),
      onRcActuator,
      CHANGE);
  attachInterrupt(
      digitalPinToInterrupt(Pins::RC_MODE),
      onRcMode,
      CHANGE);

  lastControlMs = millis();
  lastTelemetryMs = millis();
  lastTelemetryPulseMs = millis();

  String bootBody =
      String("BOOT,ESP32_WROOM_TRACK_CONTROLLER,7,") +
      hardwareProfileName() +
      ",RC_SAFE_ROS_NO_CH6,RC_CH3_BTS7960";

#if ROBOTLIDAR_ENABLE_HALL
  bootBody += ",HALL_HW399";
#else
  bootBody += ",HALL_OFF";
#endif

  sendFrame(bootBody);

  if (!throttleBackendReady) {
    Serial.println("EVT,DISARM,THROTTLE_DAC");
  }
}

void loop() {
  readSerialFrames();
  const uint32_t nowMs = millis();

  if (!estopOkay() && armed) {
    disarmSystem("ESTOP");
  }

  if (!throttleBackendReady && armed) {
    disarmSystem("THROTTLE_DAC");
  }

  if (nowMs - lastControlMs >= Config::CONTROL_PERIOD_MS) {
    lastControlMs = nowMs;

    updateCommandSource();
    updateRcActuator(lastRcSnapshot, nowMs);

    if (controlMode == ControlMode::RosAutonomous &&
        armed &&
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
