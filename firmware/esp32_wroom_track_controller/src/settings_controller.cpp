#include <Arduino.h>
#include <Preferences.h>

// Persistent RobotLidar settings stored in ESP32 NVS.
// Raspberry Pi sends settings through specially encoded PING sequence numbers,
// so the existing safe serial parser in main.cpp remains backward-compatible.
//
// Sequence layout:
//   0xC6000000                     -> GET all settings
//   0xC6400000 | key<<16 | value  -> SET one setting
//   0xC6800000                     -> RESET defaults

extern uint32_t lastSequence;
extern volatile int64_t leftTicks;
extern volatile int64_t rightTicks;
extern volatile uint32_t leftWindowPulses;
extern volatile uint32_t rightWindowPulses;
extern volatile int8_t leftPulseSign;
extern volatile int8_t rightPulseSign;
extern portMUX_TYPE hallMux;

namespace SettingsProtocol {
constexpr uint32_t MAGIC_MASK = 0xFF000000UL;
constexpr uint32_t MAGIC = 0xC6000000UL;
constexpr uint32_t OP_MASK = 0x00C00000UL;
constexpr uint32_t OP_GET = 0x00000000UL;
constexpr uint32_t OP_SET = 0x00400000UL;
constexpr uint32_t OP_RESET = 0x00800000UL;
constexpr uint32_t KEY_MASK = 0x003F0000UL;
constexpr uint8_t KEY_SHIFT = 16;
constexpr uint32_t VALUE_MASK = 0x0000FFFFUL;
constexpr uint16_t VERSION = 1;
}

namespace HallPins {
constexpr uint8_t LEFT = 34;
constexpr uint8_t RIGHT = 35;
}

enum SettingKey : uint8_t {
  KEY_US_ENABLED = 1,
  KEY_US_WARN_MM = 2,
  KEY_US_STOP_MM = 3,
  KEY_US_EMERGENCY_MM = 4,
  KEY_US_CLEAR_MM = 5,
  KEY_US_DANGER_SAMPLES = 6,
  KEY_US_CLEAR_SAMPLES = 7,
  KEY_US_SAMPLE_MS = 8,
  KEY_HALL_ENABLED = 16,
  KEY_HALL_LEFT_INV = 17,
  KEY_HALL_RIGHT_INV = 18,
  KEY_HALL_PPR = 19,
  KEY_WHEEL_CIRC_MM = 20,
  KEY_TRACK_WIDTH_MM = 21,
};

struct PersistentSettings {
  bool usEnabled = true;
  uint16_t usWarnMm = 1000;
  uint16_t usStopMm = 500;
  uint16_t usEmergencyMm = 300;
  uint16_t usClearMm = 100;
  uint8_t usDangerSamples = 2;
  uint8_t usClearSamples = 3;
  uint16_t usSampleMs = 60;

  bool hallEnabled = false;
  bool hallLeftInverted = true;
  bool hallRightInverted = true;
  // Existing ROS defaults were 6 motor pulses * 30:1 reduction = 180 ticks
  // per drive-sprocket revolution, 400 mm sprocket circumference.
  uint16_t hallPulsesPerRev = 180;
  uint16_t wheelCircumferenceMm = 400;
  uint16_t trackWidthMm = 600;
};

static Preferences prefs;
static PersistentSettings settings;
static bool settingsInitialized = false;
static uint32_t lastHandledSequence = 0;
static uint32_t lastConfigTelemetryMs = 0;
static bool configDirty = true;

static uint8_t checksum(const char* text) {
  uint8_t result = 0;
  while (*text) result ^= static_cast<uint8_t>(*text++);
  return result;
}

static void sendFrame(const String& body) {
  Serial.print(body);
  Serial.print('*');
  const uint8_t value = checksum(body.c_str());
  if (value < 16) Serial.print('0');
  Serial.println(value, HEX);
}

static void loadDefaults() { settings = PersistentSettings{}; }

static void normalizeSettings() {
  settings.usStopMm = constrain(settings.usStopMm, 250, 3000);
  settings.usEmergencyMm = constrain(settings.usEmergencyMm, 190, settings.usStopMm);
  settings.usWarnMm = constrain(settings.usWarnMm, settings.usStopMm, 4000);
  settings.usClearMm = constrain(settings.usClearMm, 20, 1000);
  settings.usDangerSamples = constrain(settings.usDangerSamples, 1, 10);
  settings.usClearSamples = constrain(settings.usClearSamples, 1, 10);
  settings.usSampleMs = constrain(settings.usSampleMs, 40, 500);
  settings.hallPulsesPerRev = constrain(settings.hallPulsesPerRev, 1, 10000);
  settings.wheelCircumferenceMm = constrain(settings.wheelCircumferenceMm, 10, 10000);
  settings.trackWidthMm = constrain(settings.trackWidthMm, 50, 5000);
}

static void saveSettings() {
  normalizeSettings();
  prefs.putBool("us_en", settings.usEnabled);
  prefs.putUShort("us_warn", settings.usWarnMm);
  prefs.putUShort("us_stop", settings.usStopMm);
  prefs.putUShort("us_emerg", settings.usEmergencyMm);
  prefs.putUShort("us_clear", settings.usClearMm);
  prefs.putUChar("us_dang_n", settings.usDangerSamples);
  prefs.putUChar("us_clr_n", settings.usClearSamples);
  prefs.putUShort("us_sample", settings.usSampleMs);
  prefs.putBool("hall_en", settings.hallEnabled);
  prefs.putBool("hall_li", settings.hallLeftInverted);
  prefs.putBool("hall_ri", settings.hallRightInverted);
  prefs.putUShort("hall_ppr", settings.hallPulsesPerRev);
  prefs.putUShort("wheel_mm", settings.wheelCircumferenceMm);
  prefs.putUShort("track_mm", settings.trackWidthMm);
}

static void loadSettings() {
  loadDefaults();
  settings.usEnabled = prefs.getBool("us_en", settings.usEnabled);
  settings.usWarnMm = prefs.getUShort("us_warn", settings.usWarnMm);
  settings.usStopMm = prefs.getUShort("us_stop", settings.usStopMm);
  settings.usEmergencyMm = prefs.getUShort("us_emerg", settings.usEmergencyMm);
  settings.usClearMm = prefs.getUShort("us_clear", settings.usClearMm);
  settings.usDangerSamples = prefs.getUChar("us_dang_n", settings.usDangerSamples);
  settings.usClearSamples = prefs.getUChar("us_clr_n", settings.usClearSamples);
  settings.usSampleMs = prefs.getUShort("us_sample", settings.usSampleMs);
  settings.hallEnabled = prefs.getBool("hall_en", settings.hallEnabled);
  settings.hallLeftInverted = prefs.getBool("hall_li", settings.hallLeftInverted);
  settings.hallRightInverted = prefs.getBool("hall_ri", settings.hallRightInverted);
  settings.hallPulsesPerRev = prefs.getUShort("hall_ppr", settings.hallPulsesPerRev);
  settings.wheelCircumferenceMm = prefs.getUShort("wheel_mm", settings.wheelCircumferenceMm);
  settings.trackWidthMm = prefs.getUShort("track_mm", settings.trackWidthMm);
  normalizeSettings();
}

static void IRAM_ATTR onSettingsHallLeft() {
  const bool activeLevel = settings.hallLeftInverted ? LOW : HIGH;
  if (!settings.hallEnabled || digitalRead(HallPins::LEFT) != activeLevel) return;
  portENTER_CRITICAL_ISR(&hallMux);
  leftTicks += leftPulseSign;
  ++leftWindowPulses;
  portEXIT_CRITICAL_ISR(&hallMux);
}

static void IRAM_ATTR onSettingsHallRight() {
  const bool activeLevel = settings.hallRightInverted ? LOW : HIGH;
  if (!settings.hallEnabled || digitalRead(HallPins::RIGHT) != activeLevel) return;
  portENTER_CRITICAL_ISR(&hallMux);
  rightTicks += rightPulseSign;
  ++rightWindowPulses;
  portEXIT_CRITICAL_ISR(&hallMux);
}

static void configureHallInputs() {
  pinMode(HallPins::LEFT, INPUT);
  pinMode(HallPins::RIGHT, INPUT);
  detachInterrupt(digitalPinToInterrupt(HallPins::LEFT));
  detachInterrupt(digitalPinToInterrupt(HallPins::RIGHT));
  if (!settings.hallEnabled) return;
  // CHANGE lets inversion change at runtime while counting one selected edge.
  attachInterrupt(digitalPinToInterrupt(HallPins::LEFT), onSettingsHallLeft, CHANGE);
  attachInterrupt(digitalPinToInterrupt(HallPins::RIGHT), onSettingsHallRight, CHANGE);
}

static void sendSettingsFrame() {
  String body = String("CFG,") + String(millis()) + "," + String(SettingsProtocol::VERSION) +
      ",us_enabled=" + String(settings.usEnabled ? 1 : 0) +
      ",us_warn_mm=" + String(settings.usWarnMm) +
      ",us_stop_mm=" + String(settings.usStopMm) +
      ",us_emergency_mm=" + String(settings.usEmergencyMm) +
      ",us_clear_mm=" + String(settings.usClearMm) +
      ",us_danger_samples=" + String(settings.usDangerSamples) +
      ",us_clear_samples=" + String(settings.usClearSamples) +
      ",us_sample_ms=" + String(settings.usSampleMs) +
      ",hall_enabled=" + String(settings.hallEnabled ? 1 : 0) +
      ",hall_left_inverted=" + String(settings.hallLeftInverted ? 1 : 0) +
      ",hall_right_inverted=" + String(settings.hallRightInverted ? 1 : 0) +
      ",hall_ppr=" + String(settings.hallPulsesPerRev) +
      ",wheel_circ_mm=" + String(settings.wheelCircumferenceMm) +
      ",track_width_mm=" + String(settings.trackWidthMm);
  sendFrame(body);
  lastConfigTelemetryMs = millis();
  configDirty = false;
}

static bool setSetting(uint8_t key, uint16_t value) {
  bool reconfigureHall = false;
  switch (key) {
    case KEY_US_ENABLED: settings.usEnabled = value != 0; break;
    case KEY_US_WARN_MM: settings.usWarnMm = value; break;
    case KEY_US_STOP_MM: settings.usStopMm = value; break;
    case KEY_US_EMERGENCY_MM: settings.usEmergencyMm = value; break;
    case KEY_US_CLEAR_MM: settings.usClearMm = value; break;
    case KEY_US_DANGER_SAMPLES: settings.usDangerSamples = static_cast<uint8_t>(value); break;
    case KEY_US_CLEAR_SAMPLES: settings.usClearSamples = static_cast<uint8_t>(value); break;
    case KEY_US_SAMPLE_MS: settings.usSampleMs = value; break;
    case KEY_HALL_ENABLED: settings.hallEnabled = value != 0; reconfigureHall = true; break;
    case KEY_HALL_LEFT_INV: settings.hallLeftInverted = value != 0; reconfigureHall = true; break;
    case KEY_HALL_RIGHT_INV: settings.hallRightInverted = value != 0; reconfigureHall = true; break;
    case KEY_HALL_PPR: settings.hallPulsesPerRev = value; break;
    case KEY_WHEEL_CIRC_MM: settings.wheelCircumferenceMm = value; break;
    case KEY_TRACK_WIDTH_MM: settings.trackWidthMm = value; break;
    default: return false;
  }
  normalizeSettings();
  saveSettings();
  if (reconfigureHall) configureHallInputs();
  configDirty = true;
  return true;
}

void initializeEsp32SettingsController() {
  if (settingsInitialized) return;
  prefs.begin("robotlidar", false);
  loadSettings();
  configureHallInputs();
  settingsInitialized = true;
  configDirty = true;
  Serial.println("EVT,ESP32_CONFIG,NVS_READY,V1");
}

void updateEsp32SettingsController() {
  if (!settingsInitialized) initializeEsp32SettingsController();
  const uint32_t sequence = lastSequence;
  if (sequence != lastHandledSequence &&
      (sequence & SettingsProtocol::MAGIC_MASK) == SettingsProtocol::MAGIC) {
    lastHandledSequence = sequence;
    const uint32_t op = sequence & SettingsProtocol::OP_MASK;
    if (op == SettingsProtocol::OP_GET) {
      configDirty = true;
    } else if (op == SettingsProtocol::OP_SET) {
      const uint8_t key = static_cast<uint8_t>((sequence & SettingsProtocol::KEY_MASK) >> SettingsProtocol::KEY_SHIFT);
      const uint16_t value = static_cast<uint16_t>(sequence & SettingsProtocol::VALUE_MASK);
      if (!setSetting(key, value)) {
        Serial.print("ERR,ESP32_CONFIG_UNKNOWN_KEY,");
        Serial.println(key);
      }
    } else if (op == SettingsProtocol::OP_RESET) {
      loadDefaults();
      saveSettings();
      configureHallInputs();
      configDirty = true;
      Serial.println("EVT,ESP32_CONFIG,DEFAULTS_RESTORED");
    }
  }
  if (configDirty || millis() - lastConfigTelemetryMs >= 5000) sendSettingsFrame();
}

bool espSettingUltrasonicEnabled() { return settings.usEnabled; }
uint16_t espSettingUltrasonicWarnMm() { return settings.usWarnMm; }
uint16_t espSettingUltrasonicStopMm() { return settings.usStopMm; }
uint16_t espSettingUltrasonicEmergencyMm() { return settings.usEmergencyMm; }
uint16_t espSettingUltrasonicClearMm() { return settings.usClearMm; }
uint8_t espSettingUltrasonicDangerSamples() { return settings.usDangerSamples; }
uint8_t espSettingUltrasonicClearSamples() { return settings.usClearSamples; }
uint16_t espSettingUltrasonicSampleMs() { return settings.usSampleMs; }
bool espSettingHallEnabled() { return settings.hallEnabled; }
bool espSettingHallLeftInverted() { return settings.hallLeftInverted; }
bool espSettingHallRightInverted() { return settings.hallRightInverted; }
uint16_t espSettingHallPulsesPerRev() { return settings.hallPulsesPerRev; }
uint16_t espSettingWheelCircumferenceMm() { return settings.wheelCircumferenceMm; }
uint16_t espSettingTrackWidthMm() { return settings.trackWidthMm; }
