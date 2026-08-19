#include <Arduino.h>
#include <Wire.h>

// Brush controller extension for the current 30-pin ESP32 LEGACY wiring.
//
// RC CH4          -> GPIO5
// BRUSH BRAKE      -> GPIO15 -> HW-399 input -> brush controller Brake
// MCP4725 BRUSH    -> shared OLED I2C bus: SDA=GPIO4, SCL=GPIO23, address 0x60
//
// CH4 is an analog knob/stick:
//   ~1000 us -> STOP, brake ON
//   ~1500 us -> ~50%
//   ~2000 us -> 100%
//
// Safety:
//   E-STOP open, SAFE mode, RC not armed, lost/invalid CH4 -> throttle 0 V + brake ON.
//   Brush is intentionally RC-only for now; ROS mode keeps the brush stopped.

#include <Adafruit_SSD1306.h>

// These symbols are defined in main.cpp and are intentionally reused here so
// the brush follows the same global safety state as the tracks.
enum class ControlMode : uint8_t { Safe, RcManual, RosAutonomous };
extern ControlMode controlMode;
extern bool armed;
extern bool estopOkay();
extern TwoWire OledWire;

namespace BrushPins {
constexpr uint8_t RC_CH4 = 5;
constexpr uint8_t BRAKE = 15;
}

namespace BrushConfig {
constexpr uint8_t MCP4725_ADDRESS = 0x60;
constexpr uint16_t MCP4725_FULL_SCALE_MV = 5000;
constexpr uint16_t THROTTLE_SAFE_MV = 0;
constexpr uint16_t THROTTLE_IDLE_MV = 1000;
constexpr uint16_t THROTTLE_MAX_MV = 2850;
constexpr uint16_t RC_MIN_VALID_US = 800;
constexpr uint16_t RC_MAX_VALID_US = 2200;
constexpr uint16_t RC_ISR_MIN_US = 750;
constexpr uint16_t RC_ISR_MAX_US = 2250;
constexpr uint16_t RC_STOP_MAX_US = 1050;
constexpr uint16_t RC_FULL_US = 2000;
constexpr uint32_t RC_TIMEOUT_US = 160000;
constexpr uint32_t UPDATE_PERIOD_MS = 20;
constexpr bool BRAKE_ACTIVE_HIGH = true;
}

struct BrushRcCapture {
  volatile uint32_t riseUs = 0;
  volatile uint32_t lastPulseUs = 0;
  volatile uint16_t pulseUs = 0;
};

static portMUX_TYPE brushRcMux = portMUX_INITIALIZER_UNLOCKED;
static BrushRcCapture brushRc;
static bool brushInitialized = false;
static bool brushMcpReady = false;
static bool brushBrakeActive = true;
static uint16_t brushThrottleMv = BrushConfig::THROTTLE_SAFE_MV;
static uint32_t brushLastUpdateMs = 0;

static void IRAM_ATTR onBrushRcEdge() {
  const uint32_t nowUs = micros();
  portENTER_CRITICAL_ISR(&brushRcMux);
  if (digitalRead(BrushPins::RC_CH4) == HIGH) {
    brushRc.riseUs = nowUs;
  } else {
    const uint32_t width = nowUs - brushRc.riseUs;
    if (width >= BrushConfig::RC_ISR_MIN_US && width <= BrushConfig::RC_ISR_MAX_US) {
      brushRc.pulseUs = static_cast<uint16_t>(width);
      brushRc.lastPulseUs = nowUs;
    }
  }
  portEXIT_CRITICAL_ISR(&brushRcMux);
}

static bool brushMcpPresent() {
  OledWire.beginTransmission(BrushConfig::MCP4725_ADDRESS);
  return OledWire.endTransmission() == 0;
}

static uint16_t brushMillivoltsToCode(uint16_t mv) {
  const uint32_t limited = min<uint32_t>(mv, BrushConfig::MCP4725_FULL_SCALE_MV);
  return static_cast<uint16_t>((limited * 4095UL + BrushConfig::MCP4725_FULL_SCALE_MV / 2) /
                               BrushConfig::MCP4725_FULL_SCALE_MV);
}

static bool writeBrushThrottle(uint16_t mv) {
  if (!brushMcpReady) return false;
  const uint16_t value = brushMillivoltsToCode(mv);
  OledWire.beginTransmission(BrushConfig::MCP4725_ADDRESS);
  OledWire.write(static_cast<uint8_t>((value >> 8) & 0x0F));
  OledWire.write(static_cast<uint8_t>(value & 0xFF));
  if (OledWire.endTransmission() != 0) {
    brushMcpReady = false;
    return false;
  }
  brushThrottleMv = mv;
  return true;
}

static void setBrushBrake(bool active) {
  brushBrakeActive = active;
  const bool level = BrushConfig::BRAKE_ACTIVE_HIGH ? active : !active;
  digitalWrite(BrushPins::BRAKE, level ? HIGH : LOW);
}

static void stopBrush() {
  setBrushBrake(true);
  if (brushMcpReady) writeBrushThrottle(BrushConfig::THROTTLE_SAFE_MV);
  else brushThrottleMv = BrushConfig::THROTTLE_SAFE_MV;
}

static bool readBrushRc(uint16_t& pulseUs) {
  uint16_t pulse = 0;
  uint32_t lastPulse = 0;
  const uint32_t nowUs = micros();
  portENTER_CRITICAL(&brushRcMux);
  pulse = brushRc.pulseUs;
  lastPulse = brushRc.lastPulseUs;
  portEXIT_CRITICAL(&brushRcMux);

  const uint32_t ageUs = lastPulse ? nowUs - lastPulse : UINT32_MAX;
  const bool valid = pulse >= BrushConfig::RC_MIN_VALID_US &&
                     pulse <= BrushConfig::RC_MAX_VALID_US &&
                     ageUs <= BrushConfig::RC_TIMEOUT_US;
  pulseUs = valid ? pulse : 0;
  return valid;
}

static uint16_t brushPulseToThrottleMv(uint16_t pulseUs) {
  if (pulseUs <= BrushConfig::RC_STOP_MAX_US) return BrushConfig::THROTTLE_SAFE_MV;
  const uint32_t clamped = constrain(static_cast<uint32_t>(pulseUs),
                                     static_cast<uint32_t>(BrushConfig::RC_STOP_MAX_US),
                                     static_cast<uint32_t>(BrushConfig::RC_FULL_US));
  const uint32_t spanUs = BrushConfig::RC_FULL_US - BrushConfig::RC_STOP_MAX_US;
  const uint32_t spanMv = BrushConfig::THROTTLE_MAX_MV - BrushConfig::THROTTLE_IDLE_MV;
  return static_cast<uint16_t>(BrushConfig::THROTTLE_IDLE_MV +
      ((clamped - BrushConfig::RC_STOP_MAX_US) * spanMv) / spanUs);
}

static void initializeBrush() {
  pinMode(BrushPins::RC_CH4, INPUT_PULLDOWN);
  pinMode(BrushPins::BRAKE, OUTPUT);
  setBrushBrake(true);
  attachInterrupt(digitalPinToInterrupt(BrushPins::RC_CH4), onBrushRcEdge, CHANGE);

  // OledWire is already initialized by main.cpp on GPIO4/GPIO23.
  brushMcpReady = brushMcpPresent();
  if (brushMcpReady) {
    writeBrushThrottle(BrushConfig::THROTTLE_SAFE_MV);
    Serial.println("EVT,BRUSH,MCP4725_OK,0x60");
  } else {
    Serial.println("ERR,BRUSH_MCP4725_NOT_FOUND,0x60");
  }
  brushInitialized = true;
}

static void updateBrush() {
  if (!brushInitialized) initializeBrush();

  const uint32_t nowMs = millis();
  if (nowMs - brushLastUpdateMs < BrushConfig::UPDATE_PERIOD_MS) return;
  brushLastUpdateMs = nowMs;

  uint16_t ch4Us = 0;
  const bool ch4Valid = readBrushRc(ch4Us);

  // Brush is RC-only in this version and always follows global safety.
  if (!estopOkay() || !armed || controlMode != ControlMode::RcManual || !ch4Valid || !brushMcpReady) {
    stopBrush();
    return;
  }

  if (ch4Us <= BrushConfig::RC_STOP_MAX_US) {
    stopBrush();
    return;
  }

  const uint16_t requestedMv = brushPulseToThrottleMv(ch4Us);
  if (!writeBrushThrottle(requestedMv)) {
    setBrushBrake(true);
    return;
  }
  setBrushBrake(false);
}

// Arduino-ESP32 calls serialEventRun() after loop() when this symbol exists.
// This lets the brush extension stay isolated from the main controller code.
void serialEventRun(void) {
  updateBrush();
}
