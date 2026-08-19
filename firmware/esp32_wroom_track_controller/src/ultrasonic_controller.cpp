#include <Arduino.h>

// RCWL-1655 front ultrasonic safety sensor for ESP32-WROOM 40-pin.
// VCC  -> 3.3V
// GND  -> GND
// TRIG -> GPIO2
// ECHO -> GPIO39 (input-only)
// Runtime thresholds and enable/disable state are stored in ESP32 NVS and can
// be edited from the Raspberry Pi ESP32 settings page.

extern bool armed;
extern void disarmSystem(const char* reason);

bool espSettingUltrasonicEnabled();
uint16_t espSettingUltrasonicWarnMm();
uint16_t espSettingUltrasonicStopMm();
uint16_t espSettingUltrasonicEmergencyMm();
uint16_t espSettingUltrasonicClearMm();
uint8_t espSettingUltrasonicDangerSamples();
uint8_t espSettingUltrasonicClearSamples();
uint16_t espSettingUltrasonicSampleMs();

namespace UltrasonicPins {
constexpr uint8_t TRIG = 2;
constexpr uint8_t ECHO = 39;
}

namespace UltrasonicConfig {
constexpr uint32_t TELEMETRY_PERIOD_MS = 200;
constexpr uint32_t ECHO_TIMEOUT_US = 30000;
constexpr uint16_t MIN_VALID_MM = 190;
constexpr uint16_t MAX_VALID_MM = 4000;
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
static bool stopEventReported = false;

static uint8_t ultrasonicChecksum(const char* text) {
  uint8_t value = 0;
  while (*text) value ^= static_cast<uint8_t>(*text++);
  return value;
}

static void sendUltrasonicFrame(const String& body) {
  Serial.print(body);
  Serial.print('*');
  const uint8_t checksum = ultrasonicChecksum(body.c_str());
  if (checksum < 16) Serial.print('0');
  Serial.println(checksum, HEX);
}

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

static void clearSafetyStateForDisabledSensor() {
  ultrasonicValid = false;
  ultrasonicNear = false;
  ultrasonicStop = false;
  ultrasonicEmergency = false;
  dangerConfirm = 0;
  clearConfirm = 0;
  stopEventReported = false;
  triggerStartedUs = 0;
  digitalWrite(UltrasonicPins::TRIG, LOW);
}

static void updateSafetyState(bool valid, uint16_t distanceMm) {
  ultrasonicValid = valid;
  if (!valid) {
    dangerConfirm = 0;
    ultrasonicNear = false;
    return;
  }

  ultrasonicDistanceMm = distanceMm;
  ultrasonicNear = distanceMm <= espSettingUltrasonicWarnMm();
  const bool dangerNow = distanceMm <= espSettingUltrasonicStopMm();
  const bool clearNow = distanceMm >
      espSettingUltrasonicStopMm() + espSettingUltrasonicClearMm();

  if (dangerNow) {
    dangerConfirm = min<uint8_t>(dangerConfirm + 1, 20);
    clearConfirm = 0;
  } else if (clearNow) {
    clearConfirm = min<uint8_t>(clearConfirm + 1, 20);
    dangerConfirm = 0;
  }

  if (dangerConfirm >= espSettingUltrasonicDangerSamples()) {
    ultrasonicStop = true;
  }
  if (clearConfirm >= espSettingUltrasonicClearSamples()) {
    ultrasonicStop = false;
    ultrasonicEmergency = false;
    stopEventReported = false;
  }

  ultrasonicEmergency = ultrasonicStop &&
      distanceMm <= espSettingUltrasonicEmergencyMm();

  if (ultrasonicStop && armed) {
    disarmSystem("ULTRASONIC_STOP");
  }

  if (ultrasonicStop && !stopEventReported) {
    stopEventReported = true;
    Serial.print("EVT,ULTRASONIC_STOP,");
    Serial.println(distanceMm);
  }
}

void initializeUltrasonicController() {
  if (ultrasonicInitialized) return;
  pinMode(UltrasonicPins::TRIG, OUTPUT);
  pinMode(UltrasonicPins::ECHO, INPUT);
  digitalWrite(UltrasonicPins::TRIG, LOW);
  attachInterrupt(
      digitalPinToInterrupt(UltrasonicPins::ECHO),
      onUltrasonicEcho,
      CHANGE);
  ultrasonicInitialized = true;
  Serial.println("EVT,RCWL1655,READY,TRIG2,ECHO39,RUNTIME_CONFIG");
}

void updateUltrasonicController() {
  if (!ultrasonicInitialized) initializeUltrasonicController();
  const uint32_t nowMs = millis();
  const uint32_t nowUs = micros();

  if (!espSettingUltrasonicEnabled()) {
    clearSafetyStateForDisabledSensor();
  } else {
    if (triggerStartedUs == 0) digitalWrite(UltrasonicPins::TRIG, LOW);

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
      const uint32_t distanceMm = (widthUs * 343UL) / 2000UL;
      const bool valid = distanceMm >= UltrasonicConfig::MIN_VALID_MM &&
                         distanceMm <= UltrasonicConfig::MAX_VALID_MM;
      updateSafetyState(valid, static_cast<uint16_t>(distanceMm));
      triggerStartedUs = 0;
    } else if (triggerStartedUs != 0 &&
               nowUs - triggerStartedUs > UltrasonicConfig::ECHO_TIMEOUT_US) {
      triggerStartedUs = 0;
      updateSafetyState(false, 0);
    }

    if (triggerStartedUs == 0 &&
        nowMs - lastTriggerMs >= espSettingUltrasonicSampleMs()) {
      lastTriggerMs = nowMs;
      triggerUltrasonic();
    }
  }

  if (nowMs - lastTelemetryMs >= UltrasonicConfig::TELEMETRY_PERIOD_MS) {
    lastTelemetryMs = nowMs;
    const String body = String("USONIC,") + String(nowMs) + "," +
        String(ultrasonicDistanceMm) + "," +
        String(ultrasonicValid ? 1 : 0) + "," +
        String(ultrasonicNear ? 1 : 0) + "," +
        String(ultrasonicStop ? 1 : 0) + "," +
        String(ultrasonicEmergency ? 1 : 0) + "," +
        String(espSettingUltrasonicEnabled() ? 1 : 0);
    sendUltrasonicFrame(body);
  }
}

bool ultrasonicIsValid() { return ultrasonicValid; }
bool ultrasonicIsNear() { return ultrasonicNear; }
bool ultrasonicStopRequested() { return ultrasonicStop; }
bool ultrasonicEmergencyRequested() { return ultrasonicEmergency; }
uint16_t ultrasonicDistanceMillimeters() { return ultrasonicDistanceMm; }
