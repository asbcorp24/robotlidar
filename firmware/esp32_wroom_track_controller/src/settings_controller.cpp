#include <Arduino.h>
#include <Preferences.h>

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
constexpr uint32_t OP_GET = 0;
constexpr uint32_t OP_SET = 0x00400000UL;
constexpr uint32_t OP_RESET = 0x00800000UL;
constexpr uint32_t KEY_MASK = 0x003F0000UL;
constexpr uint32_t VALUE_MASK = 0x0000FFFFUL;
constexpr uint8_t KEY_SHIFT = 16;
constexpr uint16_t VERSION = 2;
}

namespace HallPins { constexpr uint8_t LEFT = 34, RIGHT = 35; }

enum SettingKey : uint8_t {
  KEY_US_ENABLED=1, KEY_US_WARN_MM=2, KEY_US_STOP_MM=3, KEY_US_EMERGENCY_MM=4,
  KEY_US_CLEAR_MM=5, KEY_US_DANGER_SAMPLES=6, KEY_US_CLEAR_SAMPLES=7, KEY_US_SAMPLE_MS=8,
  KEY_HALL_ENABLED=16, KEY_HALL_LEFT_INV=17, KEY_HALL_RIGHT_INV=18, KEY_HALL_PPR=19,
  KEY_WHEEL_CIRC_MM=20, KEY_TRACK_WIDTH_MM=21, KEY_RC_DEADBAND_US=22,
  KEY_RC1_MIN=23, KEY_RC1_CENTER=24, KEY_RC1_MAX=25,
  KEY_RC2_MIN=26, KEY_RC2_CENTER=27, KEY_RC2_MAX=28,
  KEY_RC3_MIN=29, KEY_RC3_CENTER=30, KEY_RC3_MAX=31,
  KEY_RC4_MIN=32, KEY_RC4_CENTER=33, KEY_RC4_MAX=34,
  KEY_RC5_MIN=35, KEY_RC5_CENTER=36, KEY_RC5_MAX=37,
  KEY_RC6_MIN=38, KEY_RC6_CENTER=39, KEY_RC6_MAX=40,
  KEY_THROTTLE_IDLE_MV=41, KEY_THROTTLE_MAX_MV=42, KEY_REVERSE_BRAKE_MS=43,
  KEY_REVERSE_SETTLE_MS=44, KEY_RAMP_STEP=45, KEY_ACTUATOR_TIMEOUT_MS=46,
  KEY_ACTUATOR_GUARD_MS=47, KEY_ACTUATOR_REVERSED=48, KEY_BRUSH_IDLE_MV=49,
  KEY_BRUSH_MAX_MV=50, KEY_BRUSH_STOP_US=51, KEY_BRUSH_BRAKE_HIGH=52,
  KEY_AUX_IDLE_MV=53, KEY_AUX_MAX_MV=54, KEY_AUX_REVERSE_GUARD_MS=55,
  KEY_AUX_RAMP_STEP=56, KEY_AUX_REVERSE_HIGH=57, KEY_RC_TIMEOUT_MS=58,
  KEY_ROS_AUX_TIMEOUT_MS=59, KEY_TRACK_REVERSE_HIGH=60
};

struct RcCalibration { uint16_t minUs=1000, centerUs=1500, maxUs=2000; };
struct PersistentSettings {
  bool usEnabled=true;
  uint16_t usWarnMm=1000, usStopMm=500, usEmergencyMm=300, usClearMm=100;
  uint8_t usDangerSamples=2, usClearSamples=3;
  uint16_t usSampleMs=60;
  bool hallEnabled=false, hallLeftInverted=true, hallRightInverted=true;
  uint16_t hallPulsesPerRev=180, wheelCircumferenceMm=400, trackWidthMm=600;
  uint16_t rcDeadbandUs=45, rcTimeoutMs=160;
  RcCalibration rc[6];
  uint16_t throttleIdleMv=1000, throttleMaxMv=2850, reverseBrakeMs=700, reverseSettleMs=300, rampStep=12;
  uint16_t actuatorTimeoutMs=30000, actuatorGuardMs=150;
  bool actuatorReversed=false;
  uint16_t brushIdleMv=1000, brushMaxMv=2850, brushStopUs=1050;
  bool brushBrakeActiveHigh=true;
  uint16_t auxIdleMv=1000, auxMaxMv=2850, auxReverseGuardMs=350, auxRampStep=25;
  bool auxReverseActiveHigh=true;
  uint16_t rosAuxTimeoutMs=500;
  bool trackReverseActiveHigh=true;
};

static Preferences prefs;
static PersistentSettings settings;
static bool settingsInitialized=false, configDirty=true;
static uint32_t lastHandledSequence=0, lastConfigTelemetryMs=0;

static uint8_t checksum(const char* text){ uint8_t v=0; while(*text) v^=(uint8_t)*text++; return v; }
static void sendFrame(const String& body){ Serial.print(body); Serial.print('*'); uint8_t v=checksum(body.c_str()); if(v<16) Serial.print('0'); Serial.println(v,HEX); }
static void loadDefaults(){ settings = PersistentSettings{}; }

static void normalizeRc(RcCalibration& c){
  c.minUs=constrain(c.minUs,800,1400); c.centerUs=constrain(c.centerUs,1200,1800); c.maxUs=constrain(c.maxUs,1600,2200);
  if(c.centerUs<=c.minUs+50) c.centerUs=c.minUs+50;
  if(c.maxUs<=c.centerUs+50) c.maxUs=c.centerUs+50;
}
static void normalizeSettings(){
  settings.usStopMm=constrain(settings.usStopMm,250,3000);
  settings.usEmergencyMm=constrain(settings.usEmergencyMm,190,settings.usStopMm);
  settings.usWarnMm=constrain(settings.usWarnMm,settings.usStopMm,4000);
  settings.usClearMm=constrain(settings.usClearMm,20,1000);
  settings.usDangerSamples=constrain(settings.usDangerSamples,1,10);
  settings.usClearSamples=constrain(settings.usClearSamples,1,10);
  settings.usSampleMs=constrain(settings.usSampleMs,40,500);
  settings.hallPulsesPerRev=constrain(settings.hallPulsesPerRev,1,10000);
  settings.wheelCircumferenceMm=constrain(settings.wheelCircumferenceMm,10,10000);
  settings.trackWidthMm=constrain(settings.trackWidthMm,50,5000);
  settings.rcDeadbandUs=constrain(settings.rcDeadbandUs,0,250);
  settings.rcTimeoutMs=constrain(settings.rcTimeoutMs,50,1000);
  for(auto& c: settings.rc) normalizeRc(c);
  settings.throttleIdleMv=constrain(settings.throttleIdleMv,0,4000);
  settings.throttleMaxMv=constrain(settings.throttleMaxMv,settings.throttleIdleMv,5000);
  settings.reverseBrakeMs=constrain(settings.reverseBrakeMs,0,5000);
  settings.reverseSettleMs=constrain(settings.reverseSettleMs,0,5000);
  settings.rampStep=constrain(settings.rampStep,1,200);
  settings.actuatorTimeoutMs=constrain(settings.actuatorTimeoutMs,1000,60000);
  settings.actuatorGuardMs=constrain(settings.actuatorGuardMs,0,5000);
  settings.brushIdleMv=constrain(settings.brushIdleMv,0,4000);
  settings.brushMaxMv=constrain(settings.brushMaxMv,settings.brushIdleMv,5000);
  settings.brushStopUs=constrain(settings.brushStopUs,800,1500);
  settings.auxIdleMv=constrain(settings.auxIdleMv,0,4000);
  settings.auxMaxMv=constrain(settings.auxMaxMv,settings.auxIdleMv,5000);
  settings.auxReverseGuardMs=constrain(settings.auxReverseGuardMs,0,5000);
  settings.auxRampStep=constrain(settings.auxRampStep,1,200);
  settings.rosAuxTimeoutMs=constrain(settings.rosAuxTimeoutMs,100,5000);
}
static void saveSettings(){ normalizeSettings(); prefs.putBytes("cfg2", &settings, sizeof(settings)); }
static void loadSettings(){
  loadDefaults();
  if(prefs.getBytesLength("cfg2")==sizeof(settings)) prefs.getBytes("cfg2", &settings, sizeof(settings));
  normalizeSettings();
}

static void IRAM_ATTR onSettingsHallLeft(){
  const bool active=settings.hallLeftInverted?LOW:HIGH;
  if(!settings.hallEnabled || digitalRead(HallPins::LEFT)!=active) return;
  portENTER_CRITICAL_ISR(&hallMux); leftTicks+=leftPulseSign; ++leftWindowPulses; portEXIT_CRITICAL_ISR(&hallMux);
}
static void IRAM_ATTR onSettingsHallRight(){
  const bool active=settings.hallRightInverted?LOW:HIGH;
  if(!settings.hallEnabled || digitalRead(HallPins::RIGHT)!=active) return;
  portENTER_CRITICAL_ISR(&hallMux); rightTicks+=rightPulseSign; ++rightWindowPulses; portEXIT_CRITICAL_ISR(&hallMux);
}
static void configureHallInputs(){
  pinMode(HallPins::LEFT,INPUT); pinMode(HallPins::RIGHT,INPUT);
  detachInterrupt(digitalPinToInterrupt(HallPins::LEFT)); detachInterrupt(digitalPinToInterrupt(HallPins::RIGHT));
  if(settings.hallEnabled){
    attachInterrupt(digitalPinToInterrupt(HallPins::LEFT),onSettingsHallLeft,CHANGE);
    attachInterrupt(digitalPinToInterrupt(HallPins::RIGHT),onSettingsHallRight,CHANGE);
  }
}

static String kv(const String& k,uint32_t v){ return String(",")+k+"="+String(v); }
static String kvb(const String& k,bool v){ return kv(k,v?1:0); }
static void sendSettingsFrame(){
  String b=String("CFG,")+millis()+","+SettingsProtocol::VERSION;
  b+=kvb("us_enabled",settings.usEnabled)+kv("us_warn_mm",settings.usWarnMm)+kv("us_stop_mm",settings.usStopMm)+kv("us_emergency_mm",settings.usEmergencyMm)+kv("us_clear_mm",settings.usClearMm)+kv("us_danger_samples",settings.usDangerSamples)+kv("us_clear_samples",settings.usClearSamples)+kv("us_sample_ms",settings.usSampleMs);
  b+=kvb("hall_enabled",settings.hallEnabled)+kvb("hall_left_inverted",settings.hallLeftInverted)+kvb("hall_right_inverted",settings.hallRightInverted)+kv("hall_ppr",settings.hallPulsesPerRev)+kv("wheel_circ_mm",settings.wheelCircumferenceMm)+kv("track_width_mm",settings.trackWidthMm);
  b+=kv("rc_deadband_us",settings.rcDeadbandUs)+kv("rc_timeout_ms",settings.rcTimeoutMs);
  for(int i=0;i<6;i++){ String n=String(i+1); b+=kv("rc"+n+"_min_us",settings.rc[i].minUs)+kv("rc"+n+"_center_us",settings.rc[i].centerUs)+kv("rc"+n+"_max_us",settings.rc[i].maxUs); }
  b+=kv("throttle_idle_mv",settings.throttleIdleMv)+kv("throttle_max_mv",settings.throttleMaxMv)+kv("reverse_brake_ms",settings.reverseBrakeMs)+kv("reverse_settle_ms",settings.reverseSettleMs)+kv("ramp_step",settings.rampStep)+kvb("track_reverse_active_high",settings.trackReverseActiveHigh);
  b+=kv("actuator_timeout_ms",settings.actuatorTimeoutMs)+kv("actuator_guard_ms",settings.actuatorGuardMs)+kvb("actuator_reversed",settings.actuatorReversed);
  b+=kv("brush_idle_mv",settings.brushIdleMv)+kv("brush_max_mv",settings.brushMaxMv)+kv("brush_stop_us",settings.brushStopUs)+kvb("brush_brake_active_high",settings.brushBrakeActiveHigh);
  b+=kv("aux_idle_mv",settings.auxIdleMv)+kv("aux_max_mv",settings.auxMaxMv)+kv("aux_reverse_guard_ms",settings.auxReverseGuardMs)+kv("aux_ramp_step",settings.auxRampStep)+kvb("aux_reverse_active_high",settings.auxReverseActiveHigh)+kv("ros_aux_timeout_ms",settings.rosAuxTimeoutMs);
  sendFrame(b); lastConfigTelemetryMs=millis(); configDirty=false;
}

static bool setSetting(uint8_t key,uint16_t v){
  bool hall=false;
  if(key>=KEY_RC1_MIN && key<=KEY_RC6_MAX){
    const uint8_t off=key-KEY_RC1_MIN, ch=off/3, part=off%3;
    if(part==0) settings.rc[ch].minUs=v; else if(part==1) settings.rc[ch].centerUs=v; else settings.rc[ch].maxUs=v;
  } else switch(key){
    case KEY_US_ENABLED:settings.usEnabled=v;break; case KEY_US_WARN_MM:settings.usWarnMm=v;break; case KEY_US_STOP_MM:settings.usStopMm=v;break; case KEY_US_EMERGENCY_MM:settings.usEmergencyMm=v;break; case KEY_US_CLEAR_MM:settings.usClearMm=v;break; case KEY_US_DANGER_SAMPLES:settings.usDangerSamples=v;break; case KEY_US_CLEAR_SAMPLES:settings.usClearSamples=v;break; case KEY_US_SAMPLE_MS:settings.usSampleMs=v;break;
    case KEY_HALL_ENABLED:settings.hallEnabled=v;hall=true;break; case KEY_HALL_LEFT_INV:settings.hallLeftInverted=v;hall=true;break; case KEY_HALL_RIGHT_INV:settings.hallRightInverted=v;hall=true;break; case KEY_HALL_PPR:settings.hallPulsesPerRev=v;break; case KEY_WHEEL_CIRC_MM:settings.wheelCircumferenceMm=v;break; case KEY_TRACK_WIDTH_MM:settings.trackWidthMm=v;break;
    case KEY_RC_DEADBAND_US:settings.rcDeadbandUs=v;break; case KEY_THROTTLE_IDLE_MV:settings.throttleIdleMv=v;break; case KEY_THROTTLE_MAX_MV:settings.throttleMaxMv=v;break; case KEY_REVERSE_BRAKE_MS:settings.reverseBrakeMs=v;break; case KEY_REVERSE_SETTLE_MS:settings.reverseSettleMs=v;break; case KEY_RAMP_STEP:settings.rampStep=v;break;
    case KEY_ACTUATOR_TIMEOUT_MS:settings.actuatorTimeoutMs=v;break; case KEY_ACTUATOR_GUARD_MS:settings.actuatorGuardMs=v;break; case KEY_ACTUATOR_REVERSED:settings.actuatorReversed=v;break;
    case KEY_BRUSH_IDLE_MV:settings.brushIdleMv=v;break; case KEY_BRUSH_MAX_MV:settings.brushMaxMv=v;break; case KEY_BRUSH_STOP_US:settings.brushStopUs=v;break; case KEY_BRUSH_BRAKE_HIGH:settings.brushBrakeActiveHigh=v;break;
    case KEY_AUX_IDLE_MV:settings.auxIdleMv=v;break; case KEY_AUX_MAX_MV:settings.auxMaxMv=v;break; case KEY_AUX_REVERSE_GUARD_MS:settings.auxReverseGuardMs=v;break; case KEY_AUX_RAMP_STEP:settings.auxRampStep=v;break; case KEY_AUX_REVERSE_HIGH:settings.auxReverseActiveHigh=v;break; case KEY_RC_TIMEOUT_MS:settings.rcTimeoutMs=v;break; case KEY_ROS_AUX_TIMEOUT_MS:settings.rosAuxTimeoutMs=v;break; case KEY_TRACK_REVERSE_HIGH:settings.trackReverseActiveHigh=v;break;
    default:return false;
  }
  normalizeSettings(); saveSettings(); if(hall) configureHallInputs(); configDirty=true; return true;
}

void initializeEsp32SettingsController(){
  if(settingsInitialized) return;
  prefs.begin("robotlidar",false); loadSettings(); configureHallInputs(); settingsInitialized=true; configDirty=true;
  Serial.println("EVT,ESP32_CONFIG,NVS_READY,V2");
}
void updateEsp32SettingsController(){
  if(!settingsInitialized) initializeEsp32SettingsController();
  const uint32_t s=lastSequence;
  if(s!=lastHandledSequence && (s&SettingsProtocol::MAGIC_MASK)==SettingsProtocol::MAGIC){
    lastHandledSequence=s; const uint32_t op=s&SettingsProtocol::OP_MASK;
    if(op==SettingsProtocol::OP_GET) configDirty=true;
    else if(op==SettingsProtocol::OP_SET){ const uint8_t k=(s&SettingsProtocol::KEY_MASK)>>SettingsProtocol::KEY_SHIFT; const uint16_t v=s&SettingsProtocol::VALUE_MASK; if(!setSetting(k,v)){Serial.print("ERR,ESP32_CONFIG_UNKNOWN_KEY,");Serial.println(k);} }
    else if(op==SettingsProtocol::OP_RESET){ loadDefaults(); saveSettings(); configureHallInputs(); configDirty=true; Serial.println("EVT,ESP32_CONFIG,DEFAULTS_RESTORED"); }
  }
  if(configDirty || millis()-lastConfigTelemetryMs>=5000) sendSettingsFrame();
}

bool espSettingUltrasonicEnabled(){return settings.usEnabled;}
uint16_t espSettingUltrasonicWarnMm(){return settings.usWarnMm;}
uint16_t espSettingUltrasonicStopMm(){return settings.usStopMm;}
uint16_t espSettingUltrasonicEmergencyMm(){return settings.usEmergencyMm;}
uint16_t espSettingUltrasonicClearMm(){return settings.usClearMm;}
uint8_t espSettingUltrasonicDangerSamples(){return settings.usDangerSamples;}
uint8_t espSettingUltrasonicClearSamples(){return settings.usClearSamples;}
uint16_t espSettingUltrasonicSampleMs(){return settings.usSampleMs;}
bool espSettingHallEnabled(){return settings.hallEnabled;}
bool espSettingHallLeftInverted(){return settings.hallLeftInverted;}
bool espSettingHallRightInverted(){return settings.hallRightInverted;}
uint16_t espSettingHallPulsesPerRev(){return settings.hallPulsesPerRev;}
uint16_t espSettingWheelCircumferenceMm(){return settings.wheelCircumferenceMm;}
uint16_t espSettingTrackWidthMm(){return settings.trackWidthMm;}
uint16_t espSettingRcDeadbandUs(){return settings.rcDeadbandUs;}
uint16_t espSettingRcTimeoutMs(){return settings.rcTimeoutMs;}
uint16_t espSettingRcMinUs(uint8_t channel){return settings.rc[constrain((int)channel,1,6)-1].minUs;}
uint16_t espSettingRcCenterUs(uint8_t channel){return settings.rc[constrain((int)channel,1,6)-1].centerUs;}
uint16_t espSettingRcMaxUs(uint8_t channel){return settings.rc[constrain((int)channel,1,6)-1].maxUs;}
uint16_t espSettingThrottleIdleMv(){return settings.throttleIdleMv;}
uint16_t espSettingThrottleMaxMv(){return settings.throttleMaxMv;}
uint16_t espSettingReverseBrakeMs(){return settings.reverseBrakeMs;}
uint16_t espSettingReverseSettleMs(){return settings.reverseSettleMs;}
uint16_t espSettingRampStep(){return settings.rampStep;}
bool espSettingTrackReverseActiveHigh(){return settings.trackReverseActiveHigh;}
uint16_t espSettingActuatorTimeoutMs(){return settings.actuatorTimeoutMs;}
uint16_t espSettingActuatorGuardMs(){return settings.actuatorGuardMs;}
bool espSettingActuatorReversed(){return settings.actuatorReversed;}
uint16_t espSettingBrushIdleMv(){return settings.brushIdleMv;}
uint16_t espSettingBrushMaxMv(){return settings.brushMaxMv;}
uint16_t espSettingBrushStopUs(){return settings.brushStopUs;}
bool espSettingBrushBrakeActiveHigh(){return settings.brushBrakeActiveHigh;}
uint16_t espSettingAuxIdleMv(){return settings.auxIdleMv;}
uint16_t espSettingAuxMaxMv(){return settings.auxMaxMv;}
uint16_t espSettingAuxReverseGuardMs(){return settings.auxReverseGuardMs;}
uint16_t espSettingAuxRampStep(){return settings.auxRampStep;}
bool espSettingAuxReverseActiveHigh(){return settings.auxReverseActiveHigh;}
uint16_t espSettingRosAuxTimeoutMs(){return settings.rosAuxTimeoutMs;}
