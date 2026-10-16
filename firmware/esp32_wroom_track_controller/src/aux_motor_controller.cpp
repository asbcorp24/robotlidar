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

uint16_t espSettingRosAuxTimeoutMs();

bool isMcp23017Ready();
bool mcp23017PinMode(uint8_t pin, bool output, bool pullup=false);
bool mcp23017DigitalWrite(uint8_t pin, bool high);

// MC8RE-V2 port 8/B SBUS -> K155LA3 NAND used as inverter -> GPIO36.
// K155LA3: pins 1+2 tied together = SBUS input, pin 3 = inverted output.
// pin 14 = +5V, pin 7 = GND. Pin 3 -> 1k -> GPIO36.
// GPIO36 is input-only and is used only as UART2 RX here.
namespace Pins {
constexpr uint8_t SBUS_RX = 36;
}

namespace Sbus {
constexpr uint32_t BAUD = 100000;
constexpr uint8_t FRAME_LEN = 25;
constexpr uint8_t START = 0x0F;
constexpr uint8_t END = 0x00;
constexpr uint16_t RAW_MIN = 200;
constexpr uint16_t RAW_MAX = 1800;
constexpr uint16_t TIMEOUT_MS = 80;
}

namespace Stepper {
constexpr uint8_t IN1 = 3;  // MCP23017 GPA3 -> HW-095 IN1
constexpr uint8_t IN2 = 4;  // MCP23017 GPA4 -> HW-095 IN2
constexpr uint8_t IN3 = 5;  // MCP23017 GPA5 -> HW-095 IN3
constexpr uint8_t IN4 = 6;  // MCP23017 GPA6 -> HW-095 IN4

constexpr int32_t STEPS_PER_REV = 200;
constexpr uint16_t STEP_INTERVAL_MS = 12;
constexpr uint16_t TELEMETRY_MS = 200;

constexpr uint8_t SEQ[4][4] = {
  {1,0,1,0},
  {0,1,1,0},
  {0,1,0,1},
  {1,0,0,1}
};
}

static HardwareSerial SbusSerial(2);

static bool initialized=false;
static bool sbusValid=false;
static bool outputsReady=false;

static uint8_t sbusFrame[Sbus::FRAME_LEN];
static uint8_t sbusIndex=0;
static uint16_t sbusChannels[16]{};
static uint16_t sbusCh8Raw=0;
static uint16_t sbusCh8PseudoUs=0;
static uint32_t lastSbusFrameMs=0;

static int32_t currentStep=0;
static int32_t targetStep=0;
static uint8_t phase=0;
static int8_t direction=0;

static int16_t rosPosition=0;
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

static void decodeSbusChannels(const uint8_t* f){
  sbusChannels[0]  = ((f[1]       | f[2]<<8)                         & 0x07FF);
  sbusChannels[1]  = ((f[2]>>3    | f[3]<<5)                         & 0x07FF);
  sbusChannels[2]  = ((f[3]>>6    | f[4]<<2 | f[5]<<10)              & 0x07FF);
  sbusChannels[3]  = ((f[5]>>1    | f[6]<<7)                         & 0x07FF);
  sbusChannels[4]  = ((f[6]>>4    | f[7]<<4)                         & 0x07FF);
  sbusChannels[5]  = ((f[7]>>7    | f[8]<<1 | f[9]<<9)               & 0x07FF);
  sbusChannels[6]  = ((f[9]>>2    | f[10]<<6)                        & 0x07FF);
  sbusChannels[7]  = ((f[10]>>5   | f[11]<<3)                        & 0x07FF);
  sbusChannels[8]  = ((f[12]      | f[13]<<8)                        & 0x07FF);
  sbusChannels[9]  = ((f[13]>>3   | f[14]<<5)                        & 0x07FF);
  sbusChannels[10] = ((f[14]>>6   | f[15]<<2 | f[16]<<10)            & 0x07FF);
  sbusChannels[11] = ((f[16]>>1   | f[17]<<7)                        & 0x07FF);
  sbusChannels[12] = ((f[17]>>4   | f[18]<<4)                        & 0x07FF);
  sbusChannels[13] = ((f[18]>>7   | f[19]<<1 | f[20]<<9)             & 0x07FF);
  sbusChannels[14] = ((f[20]>>2   | f[21]<<6)                        & 0x07FF);
  sbusChannels[15] = ((f[21]>>5   | f[22]<<3)                        & 0x07FF);

  const bool frameLost=(f[23] & (1<<2))!=0;
  const bool failsafe=(f[23] & (1<<3))!=0;

  sbusCh8Raw=sbusChannels[7];

  const uint16_t clamped=(uint16_t)constrain(
      (int32_t)sbusCh8Raw,
      (int32_t)Sbus::RAW_MIN,
      (int32_t)Sbus::RAW_MAX);

  sbusCh8PseudoUs=(uint16_t)map(
      clamped,
      Sbus::RAW_MIN,Sbus::RAW_MAX,
      1000,2000);

  sbusValid=!frameLost && !failsafe;
  lastSbusFrameMs=millis();
}

static void updateSbus(){
  while(SbusSerial.available()>0){
    const uint8_t b=(uint8_t)SbusSerial.read();

    if(sbusIndex==0){
      if(b!=Sbus::START) continue;
      sbusFrame[sbusIndex++]=b;
      continue;
    }

    sbusFrame[sbusIndex++]=b;

    if(sbusIndex>=Sbus::FRAME_LEN){
      if(sbusFrame[0]==Sbus::START && sbusFrame[24]==Sbus::END){
        decodeSbusChannels(sbusFrame);
      }
      sbusIndex=0;
    }
  }

  if(!lastSbusFrameMs || millis()-lastSbusFrameMs>Sbus::TIMEOUT_MS){
    sbusValid=false;
  }
}

static int32_t sbusCh8ToStep(){
  const int32_t raw=constrain(
      (int32_t)sbusCh8Raw,
      (int32_t)Sbus::RAW_MIN,
      (int32_t)Sbus::RAW_MAX);

  return (raw-Sbus::RAW_MIN)*Stepper::STEPS_PER_REV/
         (Sbus::RAW_MAX-Sbus::RAW_MIN);
}

static int32_t rosToStep(int16_t value){
  const int32_t v=constrain((int32_t)value,(int32_t)-1000,(int32_t)1000);
  return (v+1000)*Stepper::STEPS_PER_REV/2000;
}

static uint16_t stepToDegrees(int32_t step){
  step=constrain(step,(int32_t)0,(int32_t)Stepper::STEPS_PER_REV);
  return static_cast<uint16_t>(
      (step*360L+Stepper::STEPS_PER_REV/2)/Stepper::STEPS_PER_REV);
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

  currentStep=constrain(
      currentStep,
      (int32_t)0,
      (int32_t)Stepper::STEPS_PER_REV);

  direction=dir;
}

static void captureRosFromSequence(){
  const uint32_t s=lastSequence;
  constexpr uint32_t MAGIC=0xA5000000UL;
  constexpr uint32_t MASK =0xFFF00000UL;
  constexpr uint32_t VMASK=0x7FFUL;

  if(s==lastSequenceSeen || (s&MASK)!=MAGIC) return;

  lastSequenceSeen=s;
  rosPosition=constrain(
      (int32_t)(s&VMASK)-1000,
      (int32_t)-1000,
      (int32_t)1000);

  rosMs=millis();
}

void stopAuxMotorController(){
  targetStep=currentStep;
  releaseStepper();
}

void initializeAuxMotorController(){
  if(initialized) return;

  // IMPORTANT:
  // SBUS is inverted externally by K155LA3.
  // K155LA3 pins 1+2 = SBUS, pin 3 -> 1k -> GPIO36,
  // pin 14 = +5V, pin 7 = GND.
  // Therefore Serial2 uses normal polarity here.
  SbusSerial.begin(
      Sbus::BAUD,
      SERIAL_8E2,
      Pins::SBUS_RX,
      -1,
      false);

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
    Serial.println(
      "EVT,AUXSTEPPER,HW095_READY,MCP23017_GPA3_GPA6,SBUS_CH8_GPIO36,0_360");
  }else{
    Serial.println("ERR,AUXSTEPPER,MCP23017_OUTPUTS_NOT_READY");
  }

  currentStep=0;
  targetStep=0;
  phase=0;
  initialized=true;
}

void setAuxMotorRosCommand(int16_t command){
  rosPosition=constrain(
      (int32_t)command,
      (int32_t)-1000,
      (int32_t)1000);

  rosMs=millis();
}

void updateAuxMotorController(){
  if(!initialized) initializeAuxMotorController();

  updateSbus();
  captureRosFromSequence();

  const uint32_t now=millis();

  bool commandValid=false;
  int32_t requestedStep=currentStep;

  if(estopOkay() && armed && outputsReady && controlMode!=ControlMode::Safe){
    if(controlMode==ControlMode::RcManual){
      commandValid=sbusValid;

      if(commandValid){
        requestedStep=sbusCh8ToStep();
      }
    }else{
      commandValid=rosMs &&
                   now-rosMs<=espSettingRosAuxTimeoutMs();

      if(commandValid){
        requestedStep=rosToStep(rosPosition);
      }
    }
  }

  if(!commandValid){
    targetStep=currentStep;
    releaseStepper();
  }else{
    targetStep=constrain(
        requestedStep,
        (int32_t)0,
        (int32_t)Stepper::STEPS_PER_REV);

    if(targetStep==currentStep){
      releaseStepper();
    }else if(now-lastStepMs>=Stepper::STEP_INTERVAL_MS){
      lastStepMs=now;

      stepOnce(targetStep>currentStep?1:-1);

      if(targetStep==currentStep){
        releaseStepper();
      }
    }
  }

  if(now-lastTelemetryMs>=Stepper::TELEMETRY_MS){
    lastTelemetryMs=now;

    sendFrame(
      String("AUXTEL,")+
      now+","+
      sbusCh8Raw+","+
      sbusCh8PseudoUs+","+
      (sbusValid?1:0)+","+
      stepToDegrees(targetStep)+","+
      stepToDegrees(currentStep)+","+
      currentStep+","+
      targetStep+","+
      direction+","+
      (outputsReady?1:0));
  }
}

// Compatibility API for existing OLED/server.
// Getter returns CH8 mapped to familiar 1000..2000 us range.
uint16_t getAuxMotorRcPulseUs(){return sbusCh8PseudoUs;}
bool getAuxMotorRcValid(){return sbusValid;}

int16_t getAuxMotorRosCommand(){return rosPosition;}
int16_t getAuxMotorTarget(){
  return static_cast<int16_t>(stepToDegrees(targetStep));
}
int16_t getAuxMotorActual(){
  return static_cast<int16_t>(stepToDegrees(currentStep));
}
int8_t getAuxMotorDirection(){return direction;}
uint16_t getAuxMotorThrottleMv(){return 0;}
bool getAuxMotorMcpReady(){
  return outputsReady && isMcp23017Ready();
}

void serialEventRun(void){
  initializeEsp32SettingsController();
  updateEsp32SettingsController();
  initializeBatteryMonitor();
  updateBatteryMonitor();
  updateAuxMotorController();
  updateUltrasonicController();
}
