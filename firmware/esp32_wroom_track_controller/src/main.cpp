#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

// ============================================================
// RobotLidar ESP32-WROOM 30-pin dual-track controller
// Current LEGACY wiring (default):
//   GPIO25 -> throttle LEFT (internal DAC)
//   GPIO26 -> throttle RIGHT (internal DAC)
//   GPIO21 -> BTS7960 actuator RPWM
//   GPIO22 -> BTS7960 actuator LPWM
// RC: CH1=GPIO27, CH2=GPIO33, CH3=GPIO14, CH5=GPIO13
// HW-399 #1: GPIO16/17 Reverse L/R, GPIO18/19 Brake L/R
// HW-399 #2 Hall: OUT1=GPIO34, OUT2=GPIO35
// E-STOP: GPIO32 -> NC -> GND
// GM009605 OLED 128x64: SDA=GPIO4, SCL=GPIO23, VCC=3.3V
// ============================================================

#define ROBOTLIDAR_HW_LEGACY 0
#define ROBOTLIDAR_HW_MCP4725_HW399 1
#define ROBOTLIDAR_HW_MCP4725_PC817 ROBOTLIDAR_HW_MCP4725_HW399

#ifndef ROBOTLIDAR_HW_PROFILE
#define ROBOTLIDAR_HW_PROFILE ROBOTLIDAR_HW_LEGACY
#endif
#ifndef ROBOTLIDAR_ENABLE_RC_ACTUATOR
#define ROBOTLIDAR_ENABLE_RC_ACTUATOR 1
#endif
#ifndef ROBOTLIDAR_ENABLE_HALL
#define ROBOTLIDAR_ENABLE_HALL 0
#endif
#ifndef ROBOTLIDAR_HALL_INVERTED
#define ROBOTLIDAR_HALL_INVERTED 1
#endif
#ifndef ROBOTLIDAR_ENABLE_OLED
#define ROBOTLIDAR_ENABLE_OLED 1
#endif

#if ROBOTLIDAR_HW_PROFILE != ROBOTLIDAR_HW_LEGACY && ROBOTLIDAR_HW_PROFILE != ROBOTLIDAR_HW_MCP4725_HW399
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
constexpr uint8_t RC_ACTUATOR = 14;
constexpr uint8_t RC_MODE = 13;
constexpr uint8_t OLED_SDA = 4;
constexpr uint8_t OLED_SCL = 23;
constexpr uint8_t STATUS_LED = 2;
}

namespace Config {
constexpr uint32_t SERIAL_BAUD = 115200;
constexpr uint32_t CONTROL_PERIOD_MS = 20;
constexpr uint32_t TELEMETRY_PERIOD_MS = 100;
constexpr uint32_t OLED_PERIOD_MS = 150;
constexpr uint32_t COMMAND_WATCHDOG_MS = 450;
constexpr uint32_t RC_SIGNAL_TIMEOUT_US = 160000;
constexpr uint32_t REVERSE_BRAKE_MS = 700;
constexpr uint32_t REVERSE_SETTLE_MS = 300;
constexpr uint16_t RC_MIN_VALID_US = 800;
constexpr uint16_t RC_MAX_VALID_US = 2200;
constexpr uint16_t RC_ISR_MIN_US = 750;
constexpr uint16_t RC_ISR_MAX_US = 2250;
constexpr uint16_t RC_CENTER_US = 1500;
constexpr uint16_t RC_DEADBAND_US = 45;
constexpr uint16_t RC_MODE_MANUAL_MAX_US = 1300;
constexpr uint16_t RC_MODE_ROS_MIN_US = 1700;
constexpr bool RC_LEFT_REVERSED = false;
constexpr bool RC_RIGHT_REVERSED = false;
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
constexpr uint8_t OLED_ADDRESS_PRIMARY = 0x3C;
constexpr uint8_t OLED_ADDRESS_SECONDARY = 0x3D;
constexpr uint8_t OLED_WIDTH = 128;
constexpr uint8_t OLED_HEIGHT = 64;
constexpr uint32_t OLED_I2C_HZ = 400000;
#if ROBOTLIDAR_HALL_INVERTED
constexpr int HALL_INTERRUPT_EDGE = FALLING;
#else
constexpr int HALL_INTERRUPT_EDGE = RISING;
#endif
}

enum class ControlMode : uint8_t { Safe, RcManual, RosAutonomous };
enum class TrackSide : uint8_t { Left, Right };

struct Track {
  TrackSide side;
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

Track leftTrack{TrackSide::Left, Pins::LEFT_THROTTLE_DAC, Pins::LEFT_REVERSE, Pins::LEFT_BRAKE};
Track rightTrack{TrackSide::Right, Pins::RIGHT_THROTTLE_DAC, Pins::RIGHT_REVERSE, Pins::RIGHT_BRAKE};
portMUX_TYPE hallMux = portMUX_INITIALIZER_UNLOCKED;
portMUX_TYPE rcMux = portMUX_INITIALIZER_UNLOCKED;
volatile int64_t leftTicks = 0;
volatile int64_t rightTicks = 0;
volatile uint32_t leftWindowPulses = 0;
volatile uint32_t rightWindowPulses = 0;
volatile int8_t leftPulseSign = 1;
volatile int8_t rightPulseSign = 1;
RcCapture rcChannel1, rcChannel2, rcActuator, rcMode;
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
uint32_t lastOledMs = 0;
uint32_t lastSequence = 0;
RcSnapshot lastRcSnapshot;
int8_t actuatorAppliedDirection = 0;
int8_t actuatorPendingDirection = 0;
uint32_t actuatorRunStartMs = 0;
uint32_t actuatorGuardUntilMs = 0;
bool actuatorTimeoutLatched = false;
bool actuatorNeutralSeen = false;
uint16_t leftThrottleMv = Config::THROTTLE_DISARMED_MV;
uint16_t rightThrottleMv = Config::THROTTLE_DISARMED_MV;
bool leftBrakeActive = true;
bool rightBrakeActive = true;
char serialLine[180];
size_t serialLineLength = 0;

#if ROBOTLIDAR_ENABLE_OLED
TwoWire OledWire = TwoWire(1);
Adafruit_SSD1306 oled(Config::OLED_WIDTH, Config::OLED_HEIGHT, &OledWire, -1);
bool oledReady = false;
uint8_t oledAddress = 0;
#endif

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
    if (width >= Config::RC_ISR_MIN_US && width <= Config::RC_ISR_MAX_US) {
      channel.pulseUs = static_cast<uint16_t>(width);
      channel.lastPulseUs = nowUs;
    }
  }
  portEXIT_CRITICAL_ISR(&rcMux);
}
void IRAM_ATTR onRcChannel1() { captureRcEdge(Pins::RC_CHANNEL_1, rcChannel1); }
void IRAM_ATTR onRcChannel2() { captureRcEdge(Pins::RC_CHANNEL_2, rcChannel2); }
void IRAM_ATTR onRcActuator() { captureRcEdge(Pins::RC_ACTUATOR, rcActuator); }
void IRAM_ATTR onRcMode() { captureRcEdge(Pins::RC_MODE, rcMode); }

bool outputLevel(bool active, bool activeHigh) { return activeHigh ? active : !active; }
void setBrake(const Track& track, bool active) {
  digitalWrite(track.brakePin, outputLevel(active, Config::BRAKE_ACTIVE_HIGH));
  if (track.side == TrackSide::Left) leftBrakeActive = active; else rightBrakeActive = active;
}
void setReverse(Track& track, bool reverse) {
  digitalWrite(track.reversePin, outputLevel(reverse, Config::REVERSE_ACTIVE_HIGH));
  track.appliedSign = reverse ? -1 : 1;
  portENTER_CRITICAL(&hallMux);
  if (track.side == TrackSide::Left) leftPulseSign = track.appliedSign; else rightPulseSign = track.appliedSign;
  portEXIT_CRITICAL(&hallMux);
}

#if ROBOTLIDAR_HW_PROFILE == ROBOTLIDAR_HW_MCP4725_HW399
bool i2cDevicePresent(uint8_t address) {
  Wire.beginTransmission(address);
  return Wire.endTransmission() == 0;
}
uint16_t millivoltsToMcp4725Code(uint16_t millivolts) {
  const uint32_t limited = min<uint32_t>(millivolts, Config::MCP4725_FULL_SCALE_MV);
  return static_cast<uint16_t>((limited * 4095UL + Config::MCP4725_FULL_SCALE_MV / 2) / Config::MCP4725_FULL_SCALE_MV);
}
bool writeMcp4725(uint8_t address, uint16_t millivolts) {
  const uint16_t value = millivoltsToMcp4725Code(millivolts);
  Wire.beginTransmission(address);
  Wire.write(static_cast<uint8_t>((value >> 8) & 0x0F));
  Wire.write(static_cast<uint8_t>(value & 0xFF));
  return Wire.endTransmission() == 0;
}
#endif

uint8_t millivoltsToInternalDacCode(uint16_t millivolts) {
  const uint32_t limited = min<uint32_t>(millivolts, Config::INTERNAL_DAC_FULL_SCALE_MV);
  return static_cast<uint8_t>((limited * 255UL + Config::INTERNAL_DAC_FULL_SCALE_MV / 2) / Config::INTERNAL_DAC_FULL_SCALE_MV);
}

bool writeThrottle(const Track& track, uint16_t millivolts) {
  bool ok = false;
#if ROBOTLIDAR_HW_PROFILE == ROBOTLIDAR_HW_MCP4725_HW399
  if (!throttleBackendReady) return false;
  const uint8_t address = track.side == TrackSide::Left ? Config::MCP4725_LEFT_ADDRESS : Config::MCP4725_RIGHT_ADDRESS;
  ok = writeMcp4725(address, millivolts);
  if (!ok) throttleBackendReady = false;
#else
  dacWrite(track.throttlePin, millivoltsToInternalDacCode(millivolts));
  ok = true;
#endif
  if (ok) {
    if (track.side == TrackSide::Left) leftThrottleMv = millivolts; else rightThrottleMv = millivolts;
  }
  return ok;
}

bool initializeThrottleBackend() {
#if ROBOTLIDAR_HW_PROFILE == ROBOTLIDAR_HW_MCP4725_HW399
  Wire.begin(Pins::I2C_SDA, Pins::I2C_SCL);
  Wire.setClock(Config::I2C_CLOCK_HZ);
  const bool lp = i2cDevicePresent(Config::MCP4725_LEFT_ADDRESS);
  const bool rp = i2cDevicePresent(Config::MCP4725_RIGHT_ADDRESS);
  if (!lp) Serial.println("ERR,MCP4725_LEFT_NOT_FOUND");
  if (!rp) Serial.println("ERR,MCP4725_RIGHT_NOT_FOUND");
  if (!lp || !rp) { throttleBackendReady = false; return false; }
  throttleBackendReady = writeMcp4725(Config::MCP4725_LEFT_ADDRESS, Config::THROTTLE_DISARMED_MV) &&
                         writeMcp4725(Config::MCP4725_RIGHT_ADDRESS, Config::THROTTLE_DISARMED_MV);
  return throttleBackendReady;
#else
  throttleBackendReady = true;
  dacWrite(Pins::LEFT_THROTTLE_DAC, millivoltsToInternalDacCode(Config::THROTTLE_DISARMED_MV));
  dacWrite(Pins::RIGHT_THROTTLE_DAC, millivoltsToInternalDacCode(Config::THROTTLE_DISARMED_MV));
  leftThrottleMv = rightThrottleMv = Config::THROTTLE_DISARMED_MV;
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
bool estopOkay() { return digitalRead(Pins::ESTOP_OK) == LOW; }

void initializeActuator() {
#if ROBOTLIDAR_ENABLE_RC_ACTUATOR
  pinMode(Pins::ACTUATOR_RPWM, OUTPUT);
  pinMode(Pins::ACTUATOR_LPWM, OUTPUT);
  ledcSetup(Config::ACTUATOR_RPWM_CHANNEL, Config::ACTUATOR_PWM_HZ, Config::ACTUATOR_PWM_BITS);
  ledcSetup(Config::ACTUATOR_LPWM_CHANNEL, Config::ACTUATOR_PWM_HZ, Config::ACTUATOR_PWM_BITS);
  ledcAttachPin(Pins::ACTUATOR_RPWM, Config::ACTUATOR_RPWM_CHANNEL);
  ledcAttachPin(Pins::ACTUATOR_LPWM, Config::ACTUATOR_LPWM_CHANNEL);
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
  if (direction > 0) ledcWrite(Config::ACTUATOR_RPWM_CHANNEL, Config::ACTUATOR_PWM);
  else if (direction < 0) ledcWrite(Config::ACTUATOR_LPWM_CHANNEL, Config::ACTUATOR_PWM);
#endif
  actuatorAppliedDirection = direction;
}
int8_t requestedActuatorDirection(uint16_t pulseUs) {
  int8_t direction = 0;
  if (pulseUs <= Config::ACTUATOR_RETRACT_MAX_US) direction = -1;
  else if (pulseUs >= Config::ACTUATOR_EXTEND_MIN_US) direction = 1;
  if (Config::ACTUATOR_REVERSED) direction = -direction;
  return direction;
}
void updateRcActuator(const RcSnapshot& rc, uint32_t nowMs) {
#if ROBOTLIDAR_ENABLE_RC_ACTUATOR
  if (!estopOkay() || controlMode == ControlMode::Safe || !rc.actuatorValid) {
    stopActuatorOutput(); actuatorPendingDirection = 0; actuatorGuardUntilMs = 0; actuatorNeutralSeen = false; return;
  }
  const int8_t requested = requestedActuatorDirection(rc.actuatorUs);
  if (requested == 0) {
    stopActuatorOutput(); actuatorPendingDirection = 0; actuatorGuardUntilMs = 0;
    actuatorTimeoutLatched = false; actuatorNeutralSeen = true; return;
  }
  if (!actuatorNeutralSeen || actuatorTimeoutLatched) { stopActuatorOutput(); return; }
  if (actuatorAppliedDirection != 0 && requested != actuatorAppliedDirection) {
    stopActuatorOutput(); actuatorPendingDirection = requested;
    actuatorGuardUntilMs = nowMs + Config::ACTUATOR_REVERSE_GUARD_MS; return;
  }
  if (actuatorPendingDirection != 0) {
    if (requested != actuatorPendingDirection) {
      actuatorPendingDirection = requested;
      actuatorGuardUntilMs = nowMs + Config::ACTUATOR_REVERSE_GUARD_MS;
    }
    if (static_cast<int32_t>(nowMs - actuatorGuardUntilMs) < 0) { stopActuatorOutput(); return; }
    applyActuatorOutput(actuatorPendingDirection); actuatorRunStartMs = nowMs; actuatorPendingDirection = 0; return;
  }
  if (actuatorAppliedDirection == 0) { applyActuatorOutput(requested); actuatorRunStartMs = nowMs; return; }
  if (nowMs - actuatorRunStartMs >= Config::ACTUATOR_MAX_RUN_MS) {
    stopActuatorOutput(); actuatorTimeoutLatched = true; Serial.println("EVT,ACTUATOR,TIMEOUT");
  }
#else
  (void)rc; (void)nowMs;
#endif
}

int8_t signOf(int16_t value) { return value > 0 ? 1 : (value < 0 ? -1 : 0); }
int16_t clampCommand(long value) { return static_cast<int16_t>(constrain(value, -1000L, 1000L)); }
int16_t moveToward(int16_t current, int16_t target, int16_t step) {
  if (current < target) return min<int16_t>(current + step, target);
  if (current > target) return max<int16_t>(current - step, target);
  return current;
}
uint16_t commandToThrottleMillivolts(int16_t signedCommand) {
  const int magnitude = abs(signedCommand);
  if (!magnitude) return Config::THROTTLE_IDLE_MV;
  const uint32_t span = Config::THROTTLE_MAX_TEST_MV - Config::THROTTLE_IDLE_MV;
  return static_cast<uint16_t>(Config::THROTTLE_IDLE_MV + (span * constrain(magnitude, 0, 1000)) / 1000);
}
void applyTrackSafe(Track& track) {
  track.target = 0; track.actual = 0; track.phase = Track::Phase::Normal;
  setBrake(track, true); writeThrottle(track, Config::THROTTLE_DISARMED_MV);
}
void disarmSystem(const char* reason) {
  static char lastReportedReason[32] = "";
  const bool wasArmed = armed;
  armed = false; applyTrackSafe(leftTrack); applyTrackSafe(rightTrack); digitalWrite(Pins::STATUS_LED, LOW);
  const char* eventReason = reason ? reason : "requested";
  const bool reasonChanged = reason && strncmp(lastReportedReason, eventReason, sizeof(lastReportedReason)) != 0;
  if (wasArmed || reasonChanged) { Serial.print("EVT,DISARM,"); Serial.println(eventReason); }
  if (!reason) lastReportedReason[0] = '\0';
  else { strncpy(lastReportedReason, eventReason, sizeof(lastReportedReason) - 1); lastReportedReason[sizeof(lastReportedReason) - 1] = '\0'; }
}
bool armSystem(const char* source) {
  if (!estopOkay()) { Serial.println("ERR,ESTOP_OPEN"); return false; }
  if (!throttleBackendReady) { Serial.println("ERR,THROTTLE_DAC_NOT_READY"); return false; }
  if (leftTrack.target != 0 || rightTrack.target != 0) { Serial.println("ERR,NONZERO_TARGET"); return false; }
  setBrake(leftTrack, true); setBrake(rightTrack, true);
  if (!writeThrottle(leftTrack, Config::THROTTLE_IDLE_MV) || !writeThrottle(rightTrack, Config::THROTTLE_IDLE_MV)) {
    Serial.println("ERR,THROTTLE_DAC_WRITE"); return false;
  }
  setReverse(leftTrack, false); setReverse(rightTrack, false); delay(50);
  armed = true; watchdogTripped = false; lastDriveFrameMs = millis(); digitalWrite(Pins::STATUS_LED, HIGH);
  Serial.print("EVT,ARMED,"); Serial.println(source ? source : "UNKNOWN"); return true;
}
void updateTrack(Track& track, uint32_t nowMs) {
  if (!armed) { applyTrackSafe(track); return; }
  if (!Config::REVERSE_SUPPORTED && track.target < 0) track.target = 0;
  const int8_t desiredSign = signOf(track.target);
  if (track.phase == Track::Phase::BrakeBeforeReverse) {
    setBrake(track, true); writeThrottle(track, Config::THROTTLE_IDLE_MV); track.actual = 0;
    if (static_cast<int32_t>(nowMs - track.deadlineMs) >= 0) {
      if (!desiredSign) track.phase = Track::Phase::Normal;
      else { setReverse(track, desiredSign < 0); track.phase = Track::Phase::ReverseSettle; track.deadlineMs = nowMs + Config::REVERSE_SETTLE_MS; }
    }
    return;
  }
  if (track.phase == Track::Phase::ReverseSettle) {
    setBrake(track, true); writeThrottle(track, Config::THROTTLE_IDLE_MV); track.actual = 0;
    if (static_cast<int32_t>(nowMs - track.deadlineMs) >= 0) track.phase = Track::Phase::Normal;
    return;
  }
  if (desiredSign && desiredSign != track.appliedSign) {
    track.actual = 0; setBrake(track, true); writeThrottle(track, Config::THROTTLE_IDLE_MV);
    track.phase = Track::Phase::BrakeBeforeReverse; track.deadlineMs = nowMs + Config::REVERSE_BRAKE_MS; return;
  }
  track.actual = moveToward(track.actual, track.target, Config::RAMP_STEP_PER_TICK);
  if (!track.actual) { setBrake(track, Config::HOLD_BRAKE_AT_ZERO); writeThrottle(track, Config::THROTTLE_IDLE_MV); return; }
  if (!writeThrottle(track, commandToThrottleMillivolts(track.actual))) { track.actual = 0; setBrake(track, true); return; }
  setBrake(track, false);
}

bool pulseValid(uint16_t pulseUs, uint32_t ageUs) {
  return pulseUs >= Config::RC_MIN_VALID_US && pulseUs <= Config::RC_MAX_VALID_US && ageUs <= Config::RC_SIGNAL_TIMEOUT_US;
}
RcSnapshot readRcSnapshot() {
  RcSnapshot result;
  uint32_t c1, c2, a, m;
  const uint32_t nowUs = micros();
  portENTER_CRITICAL(&rcMux);
  result.channel1Us = rcChannel1.pulseUs; result.channel2Us = rcChannel2.pulseUs;
  result.actuatorUs = rcActuator.pulseUs; result.modeUs = rcMode.pulseUs;
  c1 = rcChannel1.lastPulseUs; c2 = rcChannel2.lastPulseUs; a = rcActuator.lastPulseUs; m = rcMode.lastPulseUs;
  portEXIT_CRITICAL(&rcMux);
  result.channel1AgeUs = c1 ? nowUs - c1 : UINT32_MAX;
  result.channel2AgeUs = c2 ? nowUs - c2 : UINT32_MAX;
  result.actuatorAgeUs = a ? nowUs - a : UINT32_MAX;
  result.modeAgeUs = m ? nowUs - m : UINT32_MAX;
  result.modeValid = pulseValid(result.modeUs, result.modeAgeUs);
  result.actuatorValid = pulseValid(result.actuatorUs, result.actuatorAgeUs);
  result.valid = pulseValid(result.channel1Us, result.channel1AgeUs) && pulseValid(result.channel2Us, result.channel2AgeUs) && result.modeValid;
  return result;
}
ControlMode modeFromPulse(uint16_t pulseUs) {
  if (pulseUs <= Config::RC_MODE_MANUAL_MAX_US) return ControlMode::RcManual;
  if (pulseUs >= Config::RC_MODE_ROS_MIN_US) return ControlMode::RosAutonomous;
  return ControlMode::Safe;
}
const char* modeName(ControlMode mode) {
  if (mode == ControlMode::RcManual) return "RC";
  if (mode == ControlMode::RosAutonomous) return "ROS";
  return "SAFE";
}
const char* actuatorName() {
  if (actuatorTimeoutLatched) return "TMO";
  if (actuatorAppliedDirection > 0) return "UP";
  if (actuatorAppliedDirection < 0) return "DN";
  return "STP";
}
int16_t pulseToCommand(uint16_t pulseUs, bool reversed) {
  int32_t delta = static_cast<int32_t>(pulseUs) - Config::RC_CENTER_US;
  if (abs(delta) <= Config::RC_DEADBAND_US) return 0;
  delta += delta > 0 ? -Config::RC_DEADBAND_US : Config::RC_DEADBAND_US;
  int32_t command = constrain(delta * 1000 / (500 - Config::RC_DEADBAND_US), -1000L, 1000L);
  return static_cast<int16_t>(reversed ? -command : command);
}
void calculateRcTracks(const RcSnapshot& rc, int16_t& left, int16_t& right) {
  left = pulseToCommand(rc.channel1Us, Config::RC_LEFT_REVERSED);
  right = pulseToCommand(rc.channel2Us, Config::RC_RIGHT_REVERSED);
}
bool rcTracksNeutral(const RcSnapshot& rc) {
  int16_t l = 0, r = 0; calculateRcTracks(rc, l, r); return l == 0 && r == 0;
}
void changeMode(ControlMode nextMode) {
  if (nextMode == controlMode) return;
  disarmSystem("MODE_CHANGE"); stopActuatorOutput(); actuatorPendingDirection = 0; actuatorGuardUntilMs = 0; actuatorNeutralSeen = false;
  controlMode = nextMode; Serial.print("EVT,MODE,"); Serial.println(modeName(controlMode));
}
void updateCommandSource() {
  const RcSnapshot rc = readRcSnapshot(); lastRcSnapshot = rc; lastRcValid = rc.valid;
  changeMode(rc.modeValid ? modeFromPulse(rc.modeUs) : ControlMode::Safe);
  if (!estopOkay()) { disarmSystem("ESTOP"); return; }
  if (!throttleBackendReady) { disarmSystem("THROTTLE_DAC"); return; }
  if (controlMode == ControlMode::Safe) { disarmSystem(nullptr); return; }
  if (!rc.valid) { disarmSystem("RC_SIGNAL_LOST"); return; }
  if (controlMode == ControlMode::RcManual) {
    if (!armed) {
      if (!rcTracksNeutral(rc)) { disarmSystem("WAIT_NEUTRAL"); return; }
      if (!armSystem("RC")) return;
    }
    calculateRcTracks(rc, leftTrack.target, rightTrack.target);
  }
}

uint8_t xorChecksum(const char* text) { uint8_t c = 0; while (*text) c ^= static_cast<uint8_t>(*text++); return c; }
int hexNibble(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  return -1;
}
void sendFrame(const String& body) {
  Serial.print(body); Serial.print('*'); const uint8_t checksum = xorChecksum(body.c_str());
  if (checksum < 16) Serial.print('0'); Serial.println(checksum, HEX);
}
void sendAck(uint32_t sequence, const char* result) {
  sendFrame("ACK," + String(sequence) + "," + result + "," + String(leftTrack.target) + "," + String(rightTrack.target));
}
bool verifyAndStripChecksum(char* line) {
  char* star = strrchr(line, '*'); if (!star || strlen(star + 1) != 2) return false;
  const int h = hexNibble(star[1]), l = hexNibble(star[2]); if (h < 0 || l < 0) return false;
  const uint8_t received = static_cast<uint8_t>((h << 4) | l); *star = '\0'; return xorChecksum(line) == received;
}
void processFrame(char* line) {
  if (!verifyAndStripChecksum(line)) { Serial.println("ERR,CHECKSUM"); return; }
  char* save = nullptr; char* command = strtok_r(line, ",", &save); char* seqText = strtok_r(nullptr, ",", &save);
  if (!command || !seqText) { Serial.println("ERR,FORMAT"); return; }
  const uint32_t sequence = strtoul(seqText, nullptr, 10); lastSequence = sequence;
  if (!strcmp(command, "DRV")) {
    char* l = strtok_r(nullptr, ",", &save); char* r = strtok_r(nullptr, ",", &save);
    if (!l || !r) { sendAck(sequence, "BAD_FORMAT"); return; }
    lastDriveFrameMs = millis(); watchdogTripped = false;
    if (controlMode != ControlMode::RosAutonomous) { sendAck(sequence, "NOT_ROS_MODE"); return; }
    if (!armed) { sendAck(sequence, "DISARMED"); return; }
    leftTrack.target = clampCommand(strtol(l, nullptr, 10)); rightTrack.target = clampCommand(strtol(r, nullptr, 10)); sendAck(sequence, "OK"); return;
  }
  if (!strcmp(command, "ARM")) {
    char* v = strtok_r(nullptr, ",", &save); const bool requestArm = v && atoi(v);
    if (!requestArm) { disarmSystem("REMOTE"); sendAck(sequence, "OK"); return; }
    if (controlMode != ControlMode::RosAutonomous) { sendAck(sequence, "NOT_ROS_MODE"); return; }
    leftTrack.target = rightTrack.target = 0; sendAck(sequence, armSystem("ROS") ? "OK" : "REFUSED"); return;
  }
  if (!strcmp(command, "STOP")) { disarmSystem("REMOTE_STOP"); stopActuatorOutput(); sendAck(sequence, "OK"); return; }
  if (!strcmp(command, "PING")) { sendAck(sequence, "PONG"); return; }
  sendAck(sequence, "UNKNOWN");
}
void readSerialFrames() {
  while (Serial.available() > 0) {
    const char c = static_cast<char>(Serial.read());
    if (c == '\r') continue;
    if (c == '\n') {
      if (serialLineLength) { serialLine[serialLineLength] = '\0'; processFrame(serialLine); serialLineLength = 0; }
      continue;
    }
    if (serialLineLength + 1 < sizeof(serialLine)) serialLine[serialLineLength++] = c;
    else { serialLineLength = 0; Serial.println("ERR,LINE_TOO_LONG"); }
  }
}

void sendTelemetry(uint32_t nowMs) {
  int64_t lt, rt; uint32_t lp, rp;
  portENTER_CRITICAL(&hallMux); lt = leftTicks; rt = rightTicks; lp = leftWindowPulses; rp = rightWindowPulses;
  leftWindowPulses = rightWindowPulses = 0; portEXIT_CRITICAL(&hallMux);
  uint32_t elapsed = nowMs - lastTelemetryPulseMs; if (!elapsed) elapsed = 1; lastTelemetryPulseMs = nowMs;
  const uint32_t lpps = lp * 1000UL / elapsed, rpps = rp * 1000UL / elapsed;
  char body[360];
  snprintf(body, sizeof(body),
      "TEL,%lu,%d,%d,%d,%d,%d,%d,%lld,%lld,%lu,%lu,%d,%s,%u,%u,%u,0,%d,BOAT_MIX,%u,%d,%d,%d",
      static_cast<unsigned long>(nowMs), armed ? 1 : 0, estopOkay() ? 1 : 0,
      leftTrack.target, rightTrack.target, leftTrack.actual, rightTrack.actual,
      static_cast<long long>(lt), static_cast<long long>(rt),
      static_cast<unsigned long>(lpps), static_cast<unsigned long>(rpps), watchdogTripped ? 1 : 0,
      modeName(controlMode), lastRcSnapshot.channel1Us, lastRcSnapshot.channel2Us, lastRcSnapshot.modeUs,
      lastRcValid ? 1 : 0, lastRcSnapshot.actuatorUs, lastRcSnapshot.actuatorValid ? 1 : 0,
      actuatorAppliedDirection, actuatorTimeoutLatched ? 1 : 0);
  sendFrame(String(body));
}

#if ROBOTLIDAR_ENABLE_OLED
bool oledDevicePresent(uint8_t address) {
  OledWire.beginTransmission(address); return OledWire.endTransmission() == 0;
}
bool initializeOled() {
  OledWire.begin(Pins::OLED_SDA, Pins::OLED_SCL); OledWire.setClock(Config::OLED_I2C_HZ);
  if (oledDevicePresent(Config::OLED_ADDRESS_PRIMARY)) oledAddress = Config::OLED_ADDRESS_PRIMARY;
  else if (oledDevicePresent(Config::OLED_ADDRESS_SECONDARY)) oledAddress = Config::OLED_ADDRESS_SECONDARY;
  else { Serial.println("WARN,OLED_NOT_FOUND"); oledReady = false; return false; }
  oledReady = oled.begin(SSD1306_SWITCHCAPVCC, oledAddress, false, false);
  if (!oledReady) { Serial.println("WARN,OLED_INIT_FAILED"); return false; }
  oled.clearDisplay(); oled.setTextColor(SSD1306_WHITE); oled.setTextSize(1); oled.setCursor(0, 0);
  oled.println(F("RobotLidar ESP32")); oled.println(F("GM009605 OLED OK")); oled.print(F("I2C 0x")); oled.println(oledAddress, HEX);
  oled.println(F("SDA=4 SCL=23")); oled.display(); return true;
}

void printCompactVoltage(uint16_t mv) {
  oled.print(mv / 1000);
  oled.print('.');
  const uint16_t hundredths = (mv % 1000) / 10;
  if (hundredths < 10) oled.print('0');
  oled.print(hundredths);
}

void printRcValue(uint16_t pulseUs, uint32_t ageUs) {
  if (pulseValid(pulseUs, ageUs)) oled.print(pulseUs);
  else oled.print(F("----"));
}

void updateOled(uint32_t nowMs) {
  (void)nowMs;
  if (!oledReady) return;

  int64_t lt, rt;
  portENTER_CRITICAL(&hallMux); lt = leftTicks; rt = rightTicks; portEXIT_CRITICAL(&hallMux);

  oled.clearDisplay();
  oled.setTextColor(SSD1306_WHITE);
  oled.setTextSize(1);

  oled.setCursor(0, 0);
  oled.print(modeName(controlMode));
  oled.print(armed ? F(" A1") : F(" A0"));
  oled.print(estopOkay() ? F(" E1") : F(" E0"));
  oled.print(lastRcValid ? F(" RC1") : F(" RC0"));
  oled.print(watchdogTripped ? F(" W1") : F(" W0"));

  oled.setCursor(0, 8);
  oled.print(F("CH1:"));
  printRcValue(lastRcSnapshot.channel1Us, lastRcSnapshot.channel1AgeUs);
  oled.setCursor(66, 8);
  oled.print(F("CH2:"));
  printRcValue(lastRcSnapshot.channel2Us, lastRcSnapshot.channel2AgeUs);

  oled.setCursor(0, 16);
  oled.print(F("CMD:L")); oled.print(leftTrack.target);
  oled.setCursor(66, 16);
  oled.print(F("R:")); oled.print(rightTrack.target);

  oled.setCursor(0, 24);
  oled.print(F("DAC:")); printCompactVoltage(leftThrottleMv);
  oled.setCursor(66, 24);
  printCompactVoltage(rightThrottleMv); oled.print('V');

  oled.setCursor(0, 32);
  oled.print(F("RV:")); oled.print(leftTrack.appliedSign < 0 ? 1 : 0); oled.print('/'); oled.print(rightTrack.appliedSign < 0 ? 1 : 0);
  oled.setCursor(66, 32);
  oled.print(F("BK:")); oled.print(leftBrakeActive ? 1 : 0); oled.print('/'); oled.print(rightBrakeActive ? 1 : 0);

  oled.setCursor(0, 40);
  oled.print(F("CH3:"));
  printRcValue(lastRcSnapshot.actuatorUs, lastRcSnapshot.actuatorAgeUs);
  oled.setCursor(66, 40);
  oled.print(F("CH5:"));
  printRcValue(lastRcSnapshot.modeUs, lastRcSnapshot.modeAgeUs);

  oled.setCursor(0, 48);
#if ROBOTLIDAR_ENABLE_HALL
  oled.print(F("HL:")); oled.print(static_cast<long long>(lt));
  oled.setCursor(66, 48);
  oled.print(F("HR:")); oled.print(static_cast<long long>(rt));
#else
  oled.print(F("HL:OFF"));
  oled.setCursor(66, 48);
  oled.print(F("HR:OFF"));
#endif

  oled.setCursor(0, 56);
  oled.print(F("A:")); oled.print(actuatorName());
  oled.print(F(" DAC:")); oled.print(throttleBackendReady ? F("OK") : F("ERR"));
  oled.setCursor(96, 56);
  oled.print(actuatorTimeoutLatched ? F("AT!") : F("AT0"));

  oled.display();
}
#endif

void setup() {
  Serial.begin(Config::SERIAL_BAUD); delay(200);
  pinMode(Pins::LEFT_REVERSE, OUTPUT); pinMode(Pins::RIGHT_REVERSE, OUTPUT);
  pinMode(Pins::LEFT_BRAKE, OUTPUT); pinMode(Pins::RIGHT_BRAKE, OUTPUT);
  pinMode(Pins::STATUS_LED, OUTPUT); pinMode(Pins::ESTOP_OK, INPUT_PULLUP);
#if ROBOTLIDAR_ENABLE_HALL
  pinMode(Pins::LEFT_HALL, INPUT); pinMode(Pins::RIGHT_HALL, INPUT);
#endif
  pinMode(Pins::RC_CHANNEL_1, INPUT_PULLDOWN);
  pinMode(Pins::RC_CHANNEL_2, INPUT_PULLDOWN);
  pinMode(Pins::RC_ACTUATOR, INPUT_PULLDOWN);
  pinMode(Pins::RC_MODE, INPUT_PULLDOWN);
  digitalWrite(Pins::LEFT_REVERSE, LOW); digitalWrite(Pins::RIGHT_REVERSE, LOW);
  digitalWrite(Pins::LEFT_BRAKE, HIGH); digitalWrite(Pins::RIGHT_BRAKE, HIGH); digitalWrite(Pins::STATUS_LED, LOW);
  setReverse(leftTrack, false); setReverse(rightTrack, false);
  initializeActuator(); stopActuatorOutput(); initializeThrottleBackend(); applyTrackSafe(leftTrack); applyTrackSafe(rightTrack);
#if ROBOTLIDAR_ENABLE_OLED
  initializeOled();
#endif
#if ROBOTLIDAR_ENABLE_HALL
  attachInterrupt(digitalPinToInterrupt(Pins::LEFT_HALL), onLeftHall, Config::HALL_INTERRUPT_EDGE);
  attachInterrupt(digitalPinToInterrupt(Pins::RIGHT_HALL), onRightHall, Config::HALL_INTERRUPT_EDGE);
#endif
  attachInterrupt(digitalPinToInterrupt(Pins::RC_CHANNEL_1), onRcChannel1, CHANGE);
  attachInterrupt(digitalPinToInterrupt(Pins::RC_CHANNEL_2), onRcChannel2, CHANGE);
  attachInterrupt(digitalPinToInterrupt(Pins::RC_ACTUATOR), onRcActuator, CHANGE);
  attachInterrupt(digitalPinToInterrupt(Pins::RC_MODE), onRcMode, CHANGE);
  lastControlMs = lastTelemetryMs = lastTelemetryPulseMs = lastOledMs = millis();
  String bootBody = String("BOOT,ESP32_WROOM_TRACK_CONTROLLER,13,") + hardwareProfileName() +
      ",30PIN_RC_CH3_GPIO14,RC_SAFE_ROS,OLED_FIXED_COLUMNS,RC_PULLDOWN_FILTER";
#if ROBOTLIDAR_ENABLE_HALL
  bootBody += ",HALL_HW399";
#else
  bootBody += ",HALL_OFF";
#endif
#if ROBOTLIDAR_ENABLE_OLED
  bootBody += oledReady ? ",OLED_GM009605_OK" : ",OLED_OFFLINE";
#endif
  sendFrame(bootBody);
  if (!throttleBackendReady) Serial.println("EVT,DISARM,THROTTLE_DAC");
}

void loop() {
  readSerialFrames();
  const uint32_t nowMs = millis();
  if (!estopOkay() && armed) disarmSystem("ESTOP");
  if (!throttleBackendReady && armed) disarmSystem("THROTTLE_DAC");
  if (nowMs - lastControlMs >= Config::CONTROL_PERIOD_MS) {
    lastControlMs = nowMs; updateCommandSource(); updateRcActuator(lastRcSnapshot, nowMs);
    if (controlMode == ControlMode::RosAutonomous && armed && nowMs - lastDriveFrameMs > Config::COMMAND_WATCHDOG_MS) {
      watchdogTripped = true; disarmSystem("WATCHDOG");
    }
    updateTrack(leftTrack, nowMs); updateTrack(rightTrack, nowMs);
  }
  if (nowMs - lastTelemetryMs >= Config::TELEMETRY_PERIOD_MS) {
    lastTelemetryMs = nowMs; sendTelemetry(nowMs);
  }
#if ROBOTLIDAR_ENABLE_OLED
  if (nowMs - lastOledMs >= Config::OLED_PERIOD_MS) {
    lastOledMs = nowMs; updateOled(nowMs);
  }
#endif
}
