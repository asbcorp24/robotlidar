#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_SSD1306.h>

enum class ControlMode:uint8_t{Safe,RcManual,RosAutonomous};
extern ControlMode controlMode;
extern bool armed;
extern bool estopOkay();
extern TwoWire OledWire;

uint16_t espSettingRcMinUs(uint8_t);
uint16_t espSettingRcCenterUs(uint8_t);
uint16_t espSettingRcMaxUs(uint8_t);
uint16_t espSettingRcDeadbandUs();
uint16_t espSettingRcTimeoutMs();
uint16_t espSettingBrushIdleMv();
uint16_t espSettingBrushMaxMv();
uint16_t espSettingRosAuxTimeoutMs();

namespace BrushPins {
constexpr uint8_t RC_CH4=5;
constexpr uint8_t REVERSE=12;
}

namespace BrushConfig {
constexpr uint8_t MCP4725_ADDRESS=0x60;
constexpr uint16_t MCP4725_FULL_SCALE_MV=5000;
constexpr uint16_t THROTTLE_SAFE_MV=0;
constexpr uint16_t RC_ISR_MIN_US=750;
constexpr uint16_t RC_ISR_MAX_US=2250;
constexpr uint32_t UPDATE_PERIOD_MS=20;
constexpr uint16_t DIRECTION_GUARD_MS=150;
constexpr bool REVERSE_ACTIVE_HIGH=true;
}

struct Rc {
  volatile uint32_t riseUs=0,lastPulseUs=0;
  volatile uint16_t pulseUs=0;
};

static portMUX_TYPE mux=portMUX_INITIALIZER_UNLOCKED;
static Rc rc;
static bool initialized=false,mcpReady=false,rcValid=false;
static uint16_t rcUs=0,throttleMv=0;
static int16_t rosCommand=0;
static int8_t appliedDirection=0;
static int8_t pendingDirection=0;
static uint32_t rosMs=0,lastUpdate=0,directionGuardUntilMs=0;

static void IRAM_ATTR onRc(){
  uint32_t n=micros();
  portENTER_CRITICAL_ISR(&mux);
  if(digitalRead(BrushPins::RC_CH4)) rc.riseUs=n;
  else {
    uint32_t w=n-rc.riseUs;
    if(w>=BrushConfig::RC_ISR_MIN_US&&w<=BrushConfig::RC_ISR_MAX_US){
      rc.pulseUs=(uint16_t)w;
      rc.lastPulseUs=n;
    }
  }
  portEXIT_CRITICAL_ISR(&mux);
}

static bool present(){
  OledWire.beginTransmission(BrushConfig::MCP4725_ADDRESS);
  return OledWire.endTransmission()==0;
}

static uint16_t code(uint16_t mv){
  return (uint32_t)min<uint16_t>(mv,BrushConfig::MCP4725_FULL_SCALE_MV)*4095/
         BrushConfig::MCP4725_FULL_SCALE_MV;
}

static bool writeThrottle(uint16_t mv){
  if(!mcpReady) return false;
  mv=min<uint16_t>(mv,BrushConfig::MCP4725_FULL_SCALE_MV);
  uint16_t v=code(mv);

  OledWire.beginTransmission(BrushConfig::MCP4725_ADDRESS);
  OledWire.write((v>>8)&0x0F);
  OledWire.write(v&0xFF);

  if(OledWire.endTransmission()!=0){
    mcpReady=false;
    throttleMv=0;
    return false;
  }

  throttleMv=mv;
  return true;
}

static void setReverse(bool reverse){
  const bool level=BrushConfig::REVERSE_ACTIVE_HIGH?reverse:!reverse;
  digitalWrite(BrushPins::REVERSE,level?HIGH:LOW);
}

static void applyDirection(int8_t dir){
  setReverse(dir<0);
  appliedDirection=dir;
}

void stopBrushController(){
  if(mcpReady) writeThrottle(BrushConfig::THROTTLE_SAFE_MV);
  else throttleMv=0;
  appliedDirection=0;
  pendingDirection=0;
}

static bool readRc(uint16_t&p){
  uint16_t pulse;
  uint32_t last;
  uint32_t now=micros();

  portENTER_CRITICAL(&mux);
  pulse=rc.pulseUs;
  last=rc.lastPulseUs;
  portEXIT_CRITICAL(&mux);

  const bool ok=pulse>=espSettingRcMinUs(4)&&
                pulse<=espSettingRcMaxUs(4)&&
                last&&
                now-last<=uint32_t(espSettingRcTimeoutMs())*1000UL;

  p=ok?pulse:0;
  return ok;
}

static int16_t pulseToSignedCommand(uint16_t p){
  const int32_t lo=espSettingRcMinUs(4);
  const int32_t center=espSettingRcCenterUs(4);
  const int32_t hi=espSettingRcMaxUs(4);
  const int32_t db=espSettingRcDeadbandUs();

  const int32_t leftEdge=center-db;
  const int32_t rightEdge=center+db;

  if((int32_t)p>=leftEdge && (int32_t)p<=rightEdge) return 0;

  if((int32_t)p>rightEdge){
    if(hi<=rightEdge) return 0;
    const int32_t cl=constrain((int32_t)p,rightEdge,hi);
    return (int16_t)((cl-rightEdge)*1000/(hi-rightEdge));
  }

  if(leftEdge<=lo) return 0;
  const int32_t cl=constrain((int32_t)p,lo,leftEdge);
  return (int16_t)(-((leftEdge-cl)*1000/(leftEdge-lo)));
}

static uint16_t magnitudeToMv(uint16_t magnitude){
  if(!magnitude) return 0;

  const uint32_t lo=espSettingBrushIdleMv();
  const uint32_t hi=espSettingBrushMaxMv();

  if(hi<=lo) return (uint16_t)hi;

  magnitude=min<uint16_t>(magnitude,1000);

  return (uint16_t)(lo+(uint32_t)magnitude*(hi-lo)/1000);
}

void initializeBrushController(){
  if(initialized) return;

  pinMode(BrushPins::RC_CH4,INPUT_PULLDOWN);
  pinMode(BrushPins::REVERSE,OUTPUT);

  setReverse(false);

  attachInterrupt(digitalPinToInterrupt(BrushPins::RC_CH4),onRc,CHANGE);

  mcpReady=present();
  if(mcpReady){
    writeThrottle(0);
    Serial.println("EVT,BRUSH,MCP4725_OK,0x60,BIDIRECTIONAL_GPIO12_NO_BRAKE");
  } else {
    Serial.println("ERR,BRUSH_MCP4725_NOT_FOUND,0x60");
  }

  initialized=true;
}

void setBrushRosCommand(int16_t command){
  rosCommand=(int16_t)constrain((int32_t)command,(int32_t)-1000,(int32_t)1000);
  rosMs=millis();
}

void updateBrushController(){
  if(!initialized) initializeBrushController();

  const uint32_t now=millis();
  if(now-lastUpdate<BrushConfig::UPDATE_PERIOD_MS) return;
  lastUpdate=now;

  rcValid=readRc(rcUs);

  if(!estopOkay()||!armed||!mcpReady||controlMode==ControlMode::Safe){
    stopBrushController();
    return;
  }

  int16_t cmd=0;
  bool valid=false;

  if(controlMode==ControlMode::RcManual){
    valid=rcValid;
    if(valid) cmd=pulseToSignedCommand(rcUs);
  } else {
    valid=rosMs&&now-rosMs<=espSettingRosAuxTimeoutMs();
    if(valid) cmd=rosCommand;
  }

  if(!valid||cmd==0){
    stopBrushController();
    return;
  }

  const int8_t requestedDirection=cmd>0?1:-1;
  const uint16_t magnitude=(uint16_t)abs((int)cmd);

  // При смене направления сначала снимаем газ,
  // переключаем Reverse и выдерживаем небольшую паузу.
  if(appliedDirection!=0 && requestedDirection!=appliedDirection){
    writeThrottle(0);
    pendingDirection=requestedDirection;
    appliedDirection=0;
    directionGuardUntilMs=now+BrushConfig::DIRECTION_GUARD_MS;
    return;
  }

  if(pendingDirection!=0){
    if(requestedDirection!=pendingDirection){
      pendingDirection=requestedDirection;
      directionGuardUntilMs=now+BrushConfig::DIRECTION_GUARD_MS;
      return;
    }

    if((int32_t)(now-directionGuardUntilMs)<0) return;

    applyDirection(pendingDirection);
    pendingDirection=0;
  } else if(appliedDirection==0){
    applyDirection(requestedDirection);
  }

  writeThrottle(magnitudeToMv(magnitude));
}

uint16_t getBrushRcPulseUs(){return rcUs;}
bool getBrushRcValid(){return rcValid;}
int16_t getBrushRosCommand(){return rosCommand;}
uint16_t getBrushThrottleMv(){return throttleMv;}
bool getBrushBrakeActive(){return false;}
bool getBrushMcpReady(){return mcpReady;}
int8_t getBrushDirection(){return appliedDirection;}
