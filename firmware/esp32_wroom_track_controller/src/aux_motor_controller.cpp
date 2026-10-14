#include <Arduino.h>

enum class ControlMode:uint8_t{Safe,RcManual,RosAutonomous};
extern ControlMode controlMode;
extern bool armed;
extern bool estopOkay();
extern uint32_t lastSequence;

void initializeUltrasonicController();
void updateUltrasonicController();
void initializeEsp32SettingsController();
void updateEsp32SettingsController();
void initializeBatteryMonitor();
void updateBatteryMonitor();

uint16_t espSettingRcMinUs(uint8_t);
uint16_t espSettingRcMaxUs(uint8_t);
uint16_t espSettingRcTimeoutMs();
uint16_t espSettingRosAuxTimeoutMs();

bool isMcp23017Ready();
bool mcp23017PinMode(uint8_t pin, bool output, bool pullup=false);
bool mcp23017DigitalWrite(uint8_t pin, bool high);

// CH6 on ESP32 is the absolute position command for the auxiliary stepper.
// GPIO36 is input-only, which is exactly what we need for RC PWM.
namespace Pins {
constexpr uint8_t RC_CH6 = 36;
}

// HW-095 / L298N is driven from the already-installed MCP23017.
// MCP23017 pins are numbered 0..15; 0..2 are already used by relays.
namespace Stepper {
constexpr uint8_t IN1 = 3;  // GPA3 -> HW-095 IN1
constexpr uint8_t IN2 = 4;  // GPA4 -> HW-095 IN2
constexpr uint8_t IN3 = 5;  // GPA5 -> HW-095 IN3
constexpr uint8_t IN4 = 6;  // GPA6 -> HW-095 IN4

// Default common bipolar stepper: 1.8 deg = 200 full steps/revolution.
// Change this constant if the mechanical output has a gearbox.
constexpr int32_t STEPS_PER_REV = 200;

// Fixed movement speed. CH6 changes position only, not speed.
constexpr uint16_t STEP_INTERVAL_MS = 12; // ~83 full steps/s
constexpr uint16_t TELEMETRY_MS = 200;
constexpr uint16_t ISR_MIN_US = 750;
constexpr uint16_t ISR_MAX_US = 2250;

// Four-state full-step sequence for a bipolar motor through two H-bridges.
constexpr uint8_t SEQ[4][4] = {
  {1,0,1,0},
  {0,1,1,0},
  {0,1,0,1},
  {1,0,0,1}
};
}

struct RcCapture {
  volatile uint32_t riseUs=0;
  volatile uint32_t lastPulseUs=0;
  volatile uint16_t pulseUs=0;
};

static portMUX_TYPE rcMux=portMUX_INITIALIZER_UNLOCKED;
static RcCapture rc6;

static bool initialized=false;
static bool rcValid=false;
static bool outputsReady=false;
static uint16_t rcUs=0;

static int32_t currentStep=0;
static int32_t targetStep=0;
static uint8_t phase=0;
static int8_t direction=0;

static int16_t rosPosition=0; // -1000..1000 maps to 0..360 degrees
static uint32_t rosMs=0;
static uint32_t lastSequenceSeen=0;
static uint32_t lastStepMs=0;
static uint32_t lastTelemetryMs=0;

static uint8_t checksum(const char* text){
  uint8_t v=0;
  while(*text) v^=static_cast<uint8_t>(*text++);
  return v;
}

static void sendFrame(const String& body){
  Serial.print(body);
  Serial.print('*');
  const uint8_t v=checksum(body.c_str());
  if(v<16) Serial.print('0');
  Serial.println(v,HEX);
}

static void IRAM_ATTR onRc6(){
  const uint32_t now=micros();
  portENTER_CRITICAL_ISR(&rcMux);
  if(digitalRead(Pins::RC_CH6)){
    rc6.riseUs=now;
  }else if(rc6.riseUs){
    const uint32_t width=now-rc6.riseUs;
    if(width>=Stepper::ISR_MIN_US && width<=Stepper::ISR_MAX_US){
      rc6.pulseUs=static_cast<uint16_t>(width);
      rc6.lastPulseUs=now;
    }
  }
  portEXIT_CRITICAL_ISR(&rcMux);
}

static bool readRc6(uint16_t& pulse){
  uint16_t p;
  uint32_t last;
  const uint32_t now=micros();

  portENTER_CRITICAL(&rcMux);
  p=rc6.pulseUs;
  last=rc6.lastPulseUs;
  portEXIT_CRITICAL(&rcMux);

  const bool ok=
      p>=espSettingRcMinUs(6) &&
      p<=espSettingRcMaxUs(6) &&
      last &&
      now-last<=uint32_t(espSettingRcTimeoutMs())*1000UL;

  pulse=ok?p:0;
  return ok;
}

static int32_t pulseToStep(uint16_t pulse){
  const int32_t minUs=espSettingRcMinUs(6);
  const int32_t maxUs=espSettingRcMaxUs(6);
  if(maxUs<=minUs) return 0;

  const int32_t p=constrain((int32_t)pulse,minUs,maxUs);
  return (p-minUs)*Stepper::STEPS_PER_REV/(maxUs-minUs);
}

static int32_t rosToStep(int16_t value){
  const int32_t v=constrain((int32_t)value,(int32_t)-1000,(int32_t)1000);
  return (v+1000)*Stepper::STEPS_PER_REV/2000;
}

static uint16_t stepToDegrees(int32_t step){
  step=constrain(step,(int32_t)0,(int32_t)Stepper::STEPS_PER_REV);
  return static_cast<uint16_t>((step*360L+Stepper::STEPS_PER_REV/2)/Stepper::STEPS_PER_REV);
}

static bool writePhase(uint8_t p){
  if(!outputsReady || !isMcp23017Ready()) return false;
  p&=3;
  const uint8_t* s=Stepper::SEQ[p];
  return
      mcp23017DigitalWrite(Stepper::IN1,s[0]) &&
      mcp23017DigitalWrite(Stepper::IN2,s[1]) &&
      mcp23017DigitalWrite(Stepper::IN3,s[2]) &&
      mcp23017DigitalWrite(Stepper::IN4,s[3]);
}

static void releaseStepper(){
  if(!isMcp23017Ready()) return;
  mcp23017DigitalWrite(Stepper::IN1,false);
  mcp23017DigitalWrite(Stepper::IN2,false);
  mcp23017DigitalWrite(Stepper::IN3,false);
  mcp23017DigitalWrite(Stepper::IN4,false);
  direction=0;
}

static void stepOnce(int8_t dir){
  if(dir>0){
    phase=(phase+1)&3;
    if(writePhase(phase)) currentStep++;
  }else if(dir<0){
    phase=(phase+3)&3;
    if(writePhase(phase)) currentStep--;
  }
  currentStep=constrain(currentStep,(int32_t)0,(int32_t)Stepper::STEPS_PER_REV);
  direction=dir;
}

static void captureRosFromSequence(){
  const uint32_t s=lastSequence;
  constexpr uint32_t MAGIC=0xA5000000UL;
  constexpr uint32_t MASK =0xFFF00000UL;
  constexpr uint32_t VMASK=0x7FFUL;
  if(s==lastSequenceSeen || (s&MASK)!=MAGIC) return;
  lastSequenceSeen=s;
  rosPosition=constrain((int32_t)(s&VMASK)-1000,(int32_t)-1000,(int32_t)1000);
  rosMs=millis();
}

void stopAuxMotorController(){
  targetStep=currentStep;
  releaseStepper();
}

void initializeAuxMotorController(){
  if(initialized) return;

  pinMode(Pins::RC_CH6,INPUT);
  attachInterrupt(digitalPinToInterrupt(Pins::RC_CH6),onRc6,CHANGE);

  outputsReady=isMcp23017Ready();
  if(outputsReady){
    outputsReady=
        mcp23017PinMode(Stepper::IN1,true,false) &&
        mcp23017PinMode(Stepper::IN2,true,false) &&
        mcp23017PinMode(Stepper::IN3,true,false) &&
        mcp23017PinMode(Stepper::IN4,true,false);
  }

  if(outputsReady){
    releaseStepper();
    Serial.println("EVT,AUXSTEPPER,HW095_READY,MCP23017_GPA3_GPA6,CH6_GPIO36,0_360");
  }else{
    Serial.println("ERR,AUXSTEPPER,MCP23017_OUTPUTS_NOT_READY");
  }

  // No home switch / absolute encoder is installed yet.
  // Therefore the mechanical position at boot is defined as 0 degrees.
  currentStep=0;
  targetStep=0;
  phase=0;
  initialized=true;
}

void setAuxMotorRosCommand(int16_t command){
  rosPosition=constrain((int32_t)command,(int32_t)-1000,(int32_t)1000);
  rosMs=millis();
}

void updateAuxMotorController(){
  if(!initialized) initializeAuxMotorController();
  captureRosFromSequence();

  const uint32_t now=millis();
  rcValid=readRc6(rcUs);

  bool commandValid=false;
  int32_t requestedStep=currentStep;

  if(estopOkay() && armed && outputsReady && controlMode!=ControlMode::Safe){
    if(controlMode==ControlMode::RcManual){
      commandValid=rcValid;
      if(commandValid) requestedStep=pulseToStep(rcUs);
    }else{
      commandValid=rosMs && now-rosMs<=espSettingRosAuxTimeoutMs();
      if(commandValid) requestedStep=rosToStep(rosPosition);
    }
  }

  if(!commandValid){
    targetStep=currentStep;
    releaseStepper();
  }else{
    targetStep=constrain(requestedStep,(int32_t)0,(int32_t)Stepper::STEPS_PER_REV);

    if(targetStep==currentStep){
      releaseStepper();
    }else if(now-lastStepMs>=Stepper::STEP_INTERVAL_MS){
      lastStepMs=now;
      stepOnce(targetStep>currentStep?1:-1);
      if(targetStep==currentStep) releaseStepper();
    }
  }

  if(now-lastTelemetryMs>=Stepper::TELEMETRY_MS){
    lastTelemetryMs=now;
    sendFrame(
      String("AUXTEL,")+
      now+","+
      rcUs+","+
      (rcValid?1:0)+","+
      stepToDegrees(targetStep)+","+
      stepToDegrees(currentStep)+","+
      currentStep+","+
      targetStep+","+
      direction+","+
      (outputsReady?1:0));
  }
}

uint16_t getAuxMotorRcPulseUs(){return rcUs;}
bool getAuxMotorRcValid(){return rcValid;}

// Compatibility getters used by the existing server/UI.
int16_t getAuxMotorRosCommand(){return rosPosition;}
int16_t getAuxMotorTarget(){return static_cast<int16_t>(stepToDegrees(targetStep));}
int16_t getAuxMotorActual(){return static_cast<int16_t>(stepToDegrees(currentStep));}
int8_t getAuxMotorDirection(){return direction;}
uint16_t getAuxMotorThrottleMv(){return 0;}
bool getAuxMotorMcpReady(){return outputsReady && isMcp23017Ready();}

void serialEventRun(void){
  initializeEsp32SettingsController();
  updateEsp32SettingsController();
  initializeBatteryMonitor();
  updateBatteryMonitor();
  updateAuxMotorController();
  updateUltrasonicController();
}
