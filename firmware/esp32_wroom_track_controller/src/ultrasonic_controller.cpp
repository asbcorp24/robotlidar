#include <Arduino.h>

// RCWL-1655 front ultrasonic safety sensor for ESP32-WROOM 40-pin.
// VCC  -> 3.3V
// GND  -> GND
// TRIG -> GPIO2
// ECHO -> GPIO39 (input-only)
//
// Safety zones:
//   > 100 cm : clear
//   50..100  : near warning
//   30..50   : obstacle stop (forward drive blocked; reverse remains possible)
//   < 30 cm  : emergency stop request
//
// The module is measured asynchronously: ECHO timing is captured by interrupt,
// so the main 20 ms control loop is not blocked by pulseIn().

namespace UltrasonicPins {
constexpr uint8_t TRIG = 2;
constexpr uint8_t ECHO = 39;
}

namespace UltrasonicConfig {
constexpr uint32_t SAMPLE_PERIOD_MS = 60;
constexpr uint32_t TELEMETRY_PERIOD_MS = 200;
constexpr uint32_t ECHO_TIMEOUT_US = 30000;
constexpr uint16_t MIN_VALID_MM = 190;
constexpr uint16_t MAX_VALID_MM = 4000;
constexpr uint16_t NEAR_MM = 1000;
constexpr uint16_t STOP_MM = 500;
constexpr uint16_t EMERGENCY_MM = 300;
constexpr uint8_t VALID_SAMPLES_REQUIRED = 2;
constexpr uint8_t CLEAR_SAMPLES_REQUIRED = 3;
}

static portMUX_TYPE ultrasonicMux = portMUX_INITIALIZER_UNLOCKED;
static volatile uint32_t echoRiseUs = 0;
static volatile uint32_t echoWidthUs = 0;
static volatile bool echoReady = false;
static bool ultrasonicInitialized = false;
static bool ultrasonicValid = false;
static bool ultrasonicNear = false;
static bool ultrasonicStop = false;
static bool ultrasonicEmergency = false;
static uint16_t ultrasonicDistanceMm = 0;
static uint32_t lastTriggerMs = 0;
static uint32_t triggerStartedUs = 0;
static uint32_t lastTelemetryMs = 0;
static uint8_t dangerConfirm = 0;
static uint8_t clearConfirm = 0;

static void IRAM_ATTR onUltrasonicEcho() {
  const uint32_t nowUs = micros();
  portENTER_CRITICAL_ISR(&ultrasonicMux);
  if (digitalRead(UltrasonicPins::ECHO) == HIGH) {
    echoRiseUs = nowUs;
  } else if (echoRiseUs != 0) {
    echoWidthUs = nowUs - echoRiseUs;
    echoRiseUs = 0;
    echoReady = true;
  }
  portEXIT_CRITICAL_ISR(&ultrasonicMux);
}

static void triggerUltrasonic() {
  digitalWrite(UltrasonicPins::TRIG, LOW);
  delayMicroseconds(2);
  digitalWrite(UltrasonicPins::TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(UltrasonicPins::TRIG, LOW);
  triggerStartedUs = micros();
}

static void updateSafetyState(bool valid, uint16_t distanceMm) {
  ultrasonicValid = valid;
  if (!valid) {
    // A single missed echo must not cause a stop; lidar remains the primary sensor.
    dangerConfirm = 0;
    return;
  }

  const bool dangerNow = distanceMm <= UltrasonicConfig::STOP_MM;
  const bool clearNow = distanceMm > UltrasonicConfig::STOP_MM + 100;

  if (dangerNow) {
    dangerConfirm = min<uint8_t>(dangerConfirm + 1, 10);
    clearConfirm = 0;
  } else if (clearNow) {
    clearConfirm = min<uint8_t>(clearConfirm + 1, 10);
    dangerConfirm = 0;
  }

  ultrasonicNear = distanceMm <= UltrasonicConfig::NEAR_MM;
  if (dangerConfirm >= UltrasonicConfig::VALID_SAMPLES_REQUIRED) {
    ultrasonicStop = true;
  }
  if (clearConfirm >= UltrasonicConfig::CLEAR_SAMPLES_REQUIRED) {
    ultrasonicStop = false;
  }
  ultrasonicEmergency = ultrasonicStop && distanceMm <= UltrasonicConfig::EMERGENCY_MM;
}

void initializeUltrasonicController() {
  if (ultrasonicInitialized) return;
  pinMode(UltrasonicPins::TRIG, OUTPUT);
  pinMode(UltrasonicPins::ECHO, INPUT);
  digitalWrite(UltrasonicPins::TRIG, LOW);
  attachInterrupt(digitalPinToInterrupt(UltrasonicPins::ECHO), onUltrasonicEcho, CHANGE);
  ultrasonicInitialized = true;
  Serial.println("EVT,RCWL1655,READY,TRIG2,ECHO39");
}

void updateUltrasonicController() {
  if (!ultrasonicInitialized) initializeUltrasonicController();
  const uint32_t nowMs = millis();
  const uint32_t nowUs = micros();

  bool ready = false;
  uint32_t widthUs = 0;
  portENTER_CRITICAL(&ultrasonicMux);
  if (echoReady) {
    ready = true;
    widthUs = echoWidthUs;
    echoReady = false;
  }
  portEXIT_CRITICAL(&ultrasonicMux);

  if (ready) {
    // Sound round-trip: approximately 0.343 mm/us, divide by two.
    const uint32_t distanceMm = (widthUs * 343UL) / 2000UL;
    const bool valid = distanceMm >= UltrasonicConfig::MIN_VALID_MM &&
                       distanceMm <= UltrasonicConfig::MAX_VALID_MM;
    if (valid) ultrasonicDistanceMm = static_cast<uint16_t>(distanceMm);
    updateSafetyState(valid, static_cast<uint16_t>(distanceMm));
    triggerStartedUs = 0;
  } else if (triggerStartedUs != 0 && nowUs - triggerStartedUs > UltrasonicConfig::ECHO_TIMEOUT_US) {
    triggerStartedUs = 0;
    updateSafetyState(false, 0);
  }

  if (triggerStartedUs == 0 && nowMs - lastTriggerMs >= UltrasonicConfig::SAMPLE_PERIOD_MS) {
    lastTriggerMs = nowMs;
    triggerUltrasonic();
  }
}

bool ultrasonicIsValid() { return ultrasonicValid; }
bool ultrasonicIsNear() { return ultrasonicNear; }
bool ultrasonicStopRequested() { return ultrasonicStop; }
bool ultrasonicEmergencyRequested() { return ultrasonicEmergency; }
uint16_t ultrasonicDistanceMillimeters() { return ultrasonicDistanceMm; }

bool ultrasonicTelemetryDue(uint32_t nowMs) {
  if (nowMs - lastTelemetryMs < UltrasonicConfig::TELEMETRY_PERIOD_MS) return false;
  lastTelemetryMs = nowMs;
  return true;
}
