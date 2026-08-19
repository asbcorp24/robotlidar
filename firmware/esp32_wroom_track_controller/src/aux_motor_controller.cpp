#include <Arduino.h>
#include <Wire.h>

// Bidirectional auxiliary motor for ESP32-WROOM 40-pin board.
// RC CH6        -> GPIO36 (input-only; use external 10 kOhm pulldown to GND)
// AUX REVERSE   -> GPIO12 -> HW-399 -> Reverse input of motor controller
// AUX THROTTLE  -> MCP4725 address 0x61 on shared OLED I2C SDA=GPIO4/SCL=GPIO23
// Brake is not used.
//
// RC mode: CH6 is centered at ~1500 us:
//   ~1000 us -> -100%
//   ~1500 us -> STOP
//   ~2000 us -> +100%
// ROS mode: /aux_motor/command is encoded by the Raspberry bridge into the
// sequence field of the existing AUX frame. main.cpp remains backward-compatible.
// SAFE / E-STOP / disarm / command timeout / MCP error => throttle 0 V.
// Direction changes are always performed at zero throttle with a guard delay.

enum class ControlMode : uint8_t { Safe, RcManual, RosAutonomous };
extern ControlMode controlMode;
extern bool armed;
extern bool estopOkay();
extern TwoWire OledWire;
extern uint32_t lastSequence;

// RCWL-1655 extension is serviced from the same Arduino serialEventRun hook.
void initializeUltrasonicController();
void updateUltrasonicController();

namespace AuxMotorPins {
constexpr uint8_t RC_CH6 = 36;
constexpr uint8_t REVERSE = 12;
}

namespace AuxMotorConfig {
constexpr uint8_t MCP4725_ADDRESS = 0x61;
constexpr uint16_t MCP4725_FULL_SCALE_MV = 5000;
constexpr uint16_t THROTTLE_SAFE_MV = 0;
constexpr uint16_t THROTTLE_IDLE_MV = 1000;
constexpr uint16_t THROTTLE_MAX_MV = 2850;
constexpr uint16_t RC_MIN_VALID_US = 800;
constexpr uint16_t RC_MAX_VALID_US = 2200;
constexpr uint16_t RC_ISR_MIN_US = 750;
constexpr uint16_t RC_ISR_MAX_US = 2250;
constexpr uint16_t RC_CENTER_US = 1500;
constexpr uint16_t RC_DEADBAND_US = 55;
constexpr uint32_t RC_TIMEOUT_US = 160000;
constexpr uint32_t ROS_TIMEOUT_MS = 500;
constexpr uint32_t UPDATE_PERIOD_MS = 20;
constexpr uint32_t TELEMETRY_PERIOD_MS = 200;
constexpr uint32_t REVERSE_GUARD_MS = 350;
constexpr int16_t RAMP_STEP = 25;
constexpr bool REVERSE_ACTIVE_HIGH = true;
constexpr uint32_t ROS_SEQUENCE_MAGIC = 0xA5000000UL;
constexpr uint32_t ROS_SEQUENCE_MASK = 0xFFF00000UL;
constexpr uint32_t ROS_VALUE_MASK = 0x000007FFUL;
}

struct AuxMotorRcCapture {
  volatile uint32_t riseUs = 0;
  volatile uint32_t lastPulseUs = 0;
  volatile uint16_t pulseUs = 0;
};

static portMUX_TYPE auxMotorRcMux = portMUX_INITIALIZER_UNLOCKED;
static AuxMotorRcCapture auxMotorRc;
static bool auxMotorInitialized = false;
static bool auxMotorMcpReady = false;
static bool auxMotorRcValid = false;
static uint16_t auxMotorRcPulseUs = 0;
static int16_t auxMotorRosCommand = 0;
static uint32_t auxMotorRosCommandMs = 0;
static uint32_t auxMotorLastEncodedSequence = 0;
static int16_t auxMotorTarget = 0;
static int16_t auxMotorActual = 0;
static int8_t auxMotorAppliedSign = 1;
static int8_t auxMotorPendingSign = 0;
static uint32_t auxMotorReverseReadyMs = 0;
static uint16_t auxMotorThrottleMv = AuxMotorConfig::THROTTLE_SAFE_MV;
static uint32_t auxMotorLastUpdateMs = 0;
static uint32_t auxMotorLastTelemetryMs = 0;

static uint8_t auxMotorChecksum(const char* text) {
  uint8_t value = 0;
  while (*text) value ^= static_cast<uint8_t>(*text++);
  return value;
}

static void sendAuxMotorFrame(const String& body) {
  Serial.print(body);
  Serial.print('*');
  const uint8_t checksum = auxMotorChecksum(body.c_str());
  if (checksum < 16) Serial.print('0');
  Serial.println(checksum, HEX);
}

static void IRAM_ATTR onAuxMotorRcEdge() {
  const uint32_t nowUs = micros();
  portENTER_CRITICAL_ISR(&auxMotorRcMux);
  if (digitalRead(AuxMotorPins::RC_CH6) == HIGH) {
    auxMotorRc.riseUs = nowUs;
  } else {
    const uint32_t width = nowUs - auxMotorRc.riseUs;
    if (width >= AuxMotorConfig::RC_ISR_MIN_US && width <= AuxMotorConfig::RC_ISR_MAX_US) {
      auxMotorRc.pulseUs = static_cast<uint16_t>(width);
      auxMotorRc.lastPulseUs = nowUs;
    }
  }
  portEXIT_CRITICAL_ISR(&auxMotorRcMux);
}

static bool auxMotorMcpPresent() {
  OledWire.beginTransmission(AuxMotorConfig::MCP4725_ADDRESS);
  return OledWire.endTransmission() == 0;
}

static uint16_t auxMotorMillivoltsToCode(uint16_t mv) {
  const uint32_t limited = min<uint32_t>(mv, AuxMotorConfig::MCP4725_FULL_SCALE_MV);
  return static_cast<uint16_t>((limited * 4095UL + AuxMotorConfig::MCP4725_FULL_SCALE_MV / 2) /
                               AuxMotorConfig::MCP4725_FULL_SCALE_MV);
}

static bool writeAuxMotorThrottle(uint16_t mv) {
  if (!auxMotorMcpReady) return false;
  const uint16_t value = auxMotorMillivoltsToCode(mv);
  OledWire.beginTransmission(AuxMotorConfig::MCP4725_ADDRESS);
  OledWire.write(static_cast<uint8_t>((value >> 8) & 0x0F));
  OledWire.write(static_cast<uint8_t>(value & 0xFF));
  if (OledWire.endTransmission() != 0) {
    auxMotorMcpReady = false;
    auxMotorThrottleMv = AuxMotorConfig::THROTTLE_SAFE_MV;
    return false;
  }
  auxMotorThrottleMv = mv;
  return true;
}

static void setAuxMotorReverse(bool reverse) {
  const bool level = AuxMotorConfig::REVERSE_ACTIVE_HIGH ? reverse : !reverse;
  digitalWrite(AuxMotorPins::REVERSE, level ? HIGH : LOW);
  auxMotorAppliedSign = reverse ? -1 : 1;
}

static bool readAuxMotorRc(uint16_t& pulseUs) {
  uint16_t pulse = 0;
  uint32_t lastPulse = 0;
  const uint32_t nowUs = micros();
  portENTER_CRITICAL(&auxMotorRcMux);
  pulse = auxMotorRc.pulseUs;
  lastPulse = auxMotorRc.lastPulseUs;
  portEXIT_CRITICAL(&auxMotorRcMux);
  const uint32_t ageUs = lastPulse ? nowUs - lastPulse : UINT32_MAX;
  const bool valid = pulse >= AuxMotorConfig::RC_MIN_VALID_US &&
                     pulse <= AuxMotorConfig::RC_MAX_VALID_US &&
                     ageUs <= AuxMotorConfig::RC_TIMEOUT_US;
  pulseUs = valid ? pulse : 0;
  return valid;
}

static int16_t auxMotorPulseToCommand(uint16_t pulseUs) {
  int32_t delta = static_cast<int32_t>(pulseUs) - AuxMotorConfig::RC_CENTER_US;
  if (abs(delta) <= AuxMotorConfig::RC_DEADBAND_US) return 0;
  delta += delta > 0 ? -AuxMotorConfig::RC_DEADBAND_US : AuxMotorConfig::RC_DEADBAND_US;
  const int32_t denominator = 500 - AuxMotorConfig::RC_DEADBAND_US;
  return static_cast<int16_t>(constrain(delta * 1000 / denominator, -1000L, 1000L));
}

static uint16_t auxMotorCommandToThrottleMv(int16_t command) {
  const uint16_t magnitude = static_cast<uint16_t>(constrain(abs(command), 0, 1000));
  if (magnitude == 0) return AuxMotorConfig::THROTTLE_SAFE_MV;
  const uint32_t span = AuxMotorConfig::THROTTLE_MAX_MV - AuxMotorConfig::THROTTLE_IDLE_MV;
  return static_cast<uint16_t>(AuxMotorConfig::THROTTLE_IDLE_MV + (span * magnitude) / 1000U);
}

static int8_t auxMotorSign(int16_t value) {
  return value > 0 ? 1 : (value < 0 ? -1 : 0);
}

static int16_t auxMotorMoveToward(int16_t current, int16_t target) {
  if (current < target) return min<int16_t>(current + AuxMotorConfig::RAMP_STEP, target);
  if (current > target) return max<int16_t>(current - AuxMotorConfig::RAMP_STEP, target);
  return current;
}

static void captureRosCommandFromSequence() {
  const uint32_t sequence = lastSequence;
  if (sequence == auxMotorLastEncodedSequence) return;
  if ((sequence & AuxMotorConfig::ROS_SEQUENCE_MASK) != AuxMotorConfig::ROS_SEQUENCE_MAGIC) return;
  auxMotorLastEncodedSequence = sequence;
  const int32_t encoded = static_cast<int32_t>(sequence & AuxMotorConfig::ROS_VALUE_MASK);
  const int32_t decoded = encoded - 1000;
  auxMotorRosCommand = static_cast<int16_t>(constrain(decoded, -1000L, 1000L));
  auxMotorRosCommandMs = millis();
}

void stopAuxMotorController() {
  auxMotorTarget = 0;
  auxMotorActual = 0;
  auxMotorPendingSign = 0;
  auxMotorReverseReadyMs = 0;
  if (auxMotorMcpReady) writeAuxMotorThrottle(AuxMotorConfig::THROTTLE_SAFE_MV);
  else auxMotorThrottleMv = AuxMotorConfig::THROTTLE_SAFE_MV;
}

void initializeAuxMotorController() {
  if (auxMotorInitialized) return;
  // GPIO36 has no internal pull-up/pull-down on classic ESP32.
  pinMode(AuxMotorPins::RC_CH6, INPUT);
  pinMode(AuxMotorPins::REVERSE, OUTPUT);
  setAuxMotorReverse(false);
  attachInterrupt(digitalPinToInterrupt(AuxMotorPins::RC_CH6), onAuxMotorRcEdge, CHANGE);

  auxMotorMcpReady = auxMotorMcpPresent();
  if (auxMotorMcpReady) {
    writeAuxMotorThrottle(AuxMotorConfig::THROTTLE_SAFE_MV);
    Serial.println("EVT,AUXMOTOR,MCP4725_OK,0x61");
  } else {
    Serial.println("ERR,AUXMOTOR_MCP4725_NOT_FOUND,0x61");
  }
  Serial.println("EVT,BOARD,ESP32_40PIN,CH6_GPIO36,AUXREV_GPIO12");
  auxMotorInitialized = true;
}

void setAuxMotorRosCommand(int16_t command) {
  auxMotorRosCommand = static_cast<int16_t>(constrain(command, -1000, 1000));
  auxMotorRosCommandMs = millis();
}

void updateAuxMotorController() {
  if (!auxMotorInitialized) initializeAuxMotorController();
  captureRosCommandFromSequence();

  const uint32_t nowMs = millis();
  if (nowMs - auxMotorLastUpdateMs < AuxMotorConfig::UPDATE_PERIOD_MS) return;
  auxMotorLastUpdateMs = nowMs;

  uint16_t ch6Us = 0;
  auxMotorRcValid = readAuxMotorRc(ch6Us);
  auxMotorRcPulseUs = ch6Us;

  if (!estopOkay() || !armed || !auxMotorMcpReady || controlMode == ControlMode::Safe) {
    stopAuxMotorController();
  } else {
    bool commandValid = false;
    int16_t requested = 0;
    if (controlMode == ControlMode::RcManual) {
      commandValid = auxMotorRcValid;
      if (commandValid) requested = auxMotorPulseToCommand(ch6Us);
    } else if (controlMode == ControlMode::RosAutonomous) {
      commandValid = auxMotorRosCommandMs != 0 &&
                     nowMs - auxMotorRosCommandMs <= AuxMotorConfig::ROS_TIMEOUT_MS;
      if (commandValid) requested = auxMotorRosCommand;
    }

    if (!commandValid || requested == 0) {
      stopAuxMotorController();
    } else {
      auxMotorTarget = requested;
      const int8_t requestedSign = auxMotorSign(requested);
      if (requestedSign != auxMotorAppliedSign) {
        auxMotorActual = 0;
        writeAuxMotorThrottle(AuxMotorConfig::THROTTLE_SAFE_MV);
        if (auxMotorPendingSign != requestedSign) {
          auxMotorPendingSign = requestedSign;
          auxMotorReverseReadyMs = nowMs + AuxMotorConfig::REVERSE_GUARD_MS;
        } else if (static_cast<int32_t>(nowMs - auxMotorReverseReadyMs) >= 0) {
          setAuxMotorReverse(requestedSign < 0);
          auxMotorPendingSign = 0;
          auxMotorReverseReadyMs = nowMs + AuxMotorConfig::REVERSE_GUARD_MS;
        }
      } else if (auxMotorReverseReadyMs && static_cast<int32_t>(nowMs - auxMotorReverseReadyMs) < 0) {
        writeAuxMotorThrottle(AuxMotorConfig::THROTTLE_SAFE_MV);
      } else {
        auxMotorReverseReadyMs = 0;
        auxMotorPendingSign = 0;
        auxMotorActual = auxMotorMoveToward(auxMotorActual, requested);
        if (!writeAuxMotorThrottle(auxMotorCommandToThrottleMv(auxMotorActual))) auxMotorActual = 0;
      }
    }
  }

  if (nowMs - auxMotorLastTelemetryMs >= AuxMotorConfig::TELEMETRY_PERIOD_MS) {
    auxMotorLastTelemetryMs = nowMs;
    String body = String("AUXTEL,") + String(nowMs) + "," +
                  String(auxMotorRcPulseUs) + "," + String(auxMotorRcValid ? 1 : 0) + "," +
                  String(auxMotorRosCommand) + "," + String(auxMotorTarget) + "," +
                  String(auxMotorActual) + "," + String(auxMotorAppliedSign) + "," +
                  String(auxMotorThrottleMv) + "," + String(auxMotorMcpReady ? 1 : 0);
    sendAuxMotorFrame(body);
  }
}

uint16_t getAuxMotorRcPulseUs() { return auxMotorRcPulseUs; }
bool getAuxMotorRcValid() { return auxMotorRcValid; }
int16_t getAuxMotorRosCommand() { return auxMotorRosCommand; }
int16_t getAuxMotorTarget() { return auxMotorTarget; }
int16_t getAuxMotorActual() { return auxMotorActual; }
int8_t getAuxMotorDirection() { return auxMotorAppliedSign; }
uint16_t getAuxMotorThrottleMv() { return auxMotorThrottleMv; }
bool getAuxMotorMcpReady() { return auxMotorMcpReady; }

// Arduino core calls serialEventRun() after every loop() iteration when present.
// Service all 40-pin extensions here so main.cpp stays backward-compatible.
void serialEventRun(void) {
  updateAuxMotorController();
  updateUltrasonicController();
}
