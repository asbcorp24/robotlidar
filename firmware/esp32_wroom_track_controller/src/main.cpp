#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

#define ROBOTLIDAR_HW_LEGACY 0
#define ROBOTLIDAR_HW_MCP4725_HW399 1
#ifndef ROBOTLIDAR_HW_PROFILE
#define ROBOTLIDAR_HW_PROFILE ROBOTLIDAR_HW_LEGACY
#endif
#ifndef ROBOTLIDAR_ENABLE_RC_ACTUATOR
#define ROBOTLIDAR_ENABLE_RC_ACTUATOR 1
#endif
#ifndef ROBOTLIDAR_ENABLE_OLED
#define ROBOTLIDAR_ENABLE_OLED 1
#endif

namespace Pins {
constexpr uint8_t LEFT_THROTTLE_DAC=25,RIGHT_THROTTLE_DAC=26;
#if ROBOTLIDAR_HW_PROFILE==ROBOTLIDAR_HW_MCP4725_HW399
constexpr uint8_t ACTUATOR_RPWM=25,ACTUATOR_LPWM=26;
#else
constexpr uint8_t ACTUATOR_RPWM=21,ACTUATOR_LPWM=22;
#endif
constexpr uint8_t LEFT_REVERSE=16,RIGHT_REVERSE=17,LEFT_BRAKE=18,RIGHT_BRAKE=19;
constexpr uint8_t ESTOP_OK=32,RC_CHANNEL_1=27,RC_CHANNEL_2=33,RC_ACTUATOR=14,RC_MODE=13;
constexpr uint8_t OLED_SDA=4,OLED_SCL=23;
}
namespace Fixed {
constexpr uint32_t SERIAL_BAUD=115200,CONTROL_PERIOD_MS=20,TELEMETRY_PERIOD_MS=100,OLED_PERIOD_MS=150,COMMAND_WATCHDOG_MS=450;
constexpr uint16_t RC_ISR_MIN_US=750,RC_ISR_MAX_US=2250,INTERNAL_DAC_FULL_SCALE_MV=3300,MCP4725_FULL_SCALE_MV=5000;
constexpr uint16_t THROTTLE_DISARMED_MV=0; constexpr bool HOLD_BRAKE_AT_ZERO=true,REVERSE_SUPPORTED=true,BRAKE_ACTIVE_HIGH=true;
constexpr uint8_t MCP4725_LEFT_ADDRESS=0x60,MCP4725_RIGHT_ADDRESS=0x61;
constexpr uint32_t I2C_CLOCK_HZ=100000; constexpr uint8_t ACTUATOR_PWM=255; constexpr uint32_t ACTUATOR_PWM_HZ=20000; constexpr uint8_t ACTUATOR_PWM_BITS=8,ACTUATOR_RPWM_CHANNEL=6,ACTUATOR_LPWM_CHANNEL=7;
constexpr uint8_t OLED_ADDRESS_PRIMARY=0x3C,OLED_ADDRESS_SECONDARY=0x3D,OLED_WIDTH=128,OLED_HEIGHT=64; constexpr uint32_t OLED_I2C_HZ=400000;
}

// Persistent settings API from settings_controller.cpp
void initializeEsp32SettingsController();
uint16_t espSettingRcDeadbandUs(); uint16_t espSettingRcMinUs(uint8_t); uint16_t espSettingRcCenterUs(uint8_t); uint16_t espSettingRcMaxUs(uint8_t); uint16_t espSettingRcTimeoutMs();
uint16_t espSettingThrottleIdleMv(); uint16_t espSettingThrottleMaxMv(); uint16_t espSettingReverseBrakeMs(); uint16_t espSettingReverseSettleMs(); uint16_t espSettingRampStep(); bool espSettingTrackReverseActiveHigh();
uint16_t espSettingActuatorTimeoutMs(); uint16_t espSettingActuatorGuardMs(); bool espSettingActuatorReversed(); uint16_t espSettingRosAuxTimeoutMs(); bool espSettingHallEnabled();

// Brush API
void initializeBrushController(); void updateBrushController(); void stopBrushController(); void setBrushRosCommand(uint16_t);
uint16_t getBrushRcPulseUs(); bool getBrushRcValid(); uint16_t getBrushRosCommand(); uint16_t getBrushThrottleMv(); bool getBrushBrakeActive(); bool getBrushMcpReady();

enum class ControlMode:uint8_t{Safe,RcManual,RosAutonomous}; enum class TrackSide:uint8_t{Left,Right};
struct Track{TrackSide side;uint8_t throttlePin,reversePin,brakePin;int16_t target=0,actual=0;int8_t appliedSign=1;enum class Phase:uint8_t{Normal,BrakeBeforeReverse,ReverseSettle};Phase phase=Phase::Normal;uint32_t deadlineMs=0;};
struct RcCapture{volatile uint32_t riseUs=0,lastPulseUs=0;volatile uint16_t pulseUs=0;};
struct RcSnapshot{uint16_t channel1Us=0,channel2Us=0,actuatorUs=0,modeUs=0;uint32_t channel1AgeUs=UINT32_MAX,channel2AgeUs=UINT32_MAX,actuatorAgeUs=UINT32_MAX,modeAgeUs=UINT32_MAX;bool valid=false,actuatorValid=false,modeValid=false;};

Track leftTrack{TrackSide::Left,Pins::LEFT_THROTTLE_DAC,Pins::LEFT_REVERSE,Pins::LEFT_BRAKE},rightTrack{TrackSide::Right,Pins::RIGHT_THROTTLE_DAC,Pins::RIGHT_REVERSE,Pins::RIGHT_BRAKE};
portMUX_TYPE hallMux=portMUX_INITIALIZER_UNLOCKED,rcMux=portMUX_INITIALIZER_UNLOCKED; volatile int64_t leftTicks=0,rightTicks=0;volatile uint32_t leftWindowPulses=0,rightWindowPulses=0;volatile int8_t leftPulseSign=1,rightPulseSign=1;
RcCapture rcChannel1,rcChannel2,rcActuator,rcMode; bool armed=false,watchdogTripped=false,lastRcValid=false;
#if ROBOTLIDAR_HW_PROFILE==ROBOTLIDAR_HW_LEGACY
bool throttleBackendReady=true;
#else
bool throttleBackendReady=false;
#endif
ControlMode controlMode=ControlMode::Safe; uint32_t lastDriveFrameMs=0,lastAuxFrameMs=0,lastControlMs=0,lastTelemetryMs=0,lastTelemetryPulseMs=0,lastOledMs=0,lastSequence=0; RcSnapshot lastRcSnapshot;
int8_t actuatorAppliedDirection=0,actuatorPendingDirection=0,rosActuatorCommand=0;uint32_t actuatorRunStartMs=0,actuatorGuardUntilMs=0;bool actuatorTimeoutLatched=false,actuatorNeutralSeen=false;
uint16_t leftThrottleMv=0,rightThrottleMv=0;bool leftBrakeActive=true,rightBrakeActive=true;char serialLine[220];size_t serialLineLength=0;
TwoWire OledWire=TwoWire(1);
#if ROBOTLIDAR_ENABLE_OLED
Adafruit_SSD1306 oled(Fixed::OLED_WIDTH,Fixed::OLED_HEIGHT,&OledWire,-1);bool oledReady=false;uint8_t oledAddress=0;
#endif

void IRAM_ATTR captureRcEdge(uint8_t pin,RcCapture& c){uint32_t n=micros();portENTER_CRITICAL_ISR(&rcMux);if(digitalRead(pin))c.riseUs=n;else{uint32_t w=n-c.riseUs;if(w>=Fixed::RC_ISR_MIN_US&&w<=Fixed::RC_ISR_MAX_US){c.pulseUs=(uint16_t)w;c.lastPulseUs=n;}}portEXIT_CRITICAL_ISR(&rcMux);}void IRAM_ATTR onRc1(){captureRcEdge(Pins::RC_CHANNEL_1,rcChannel1);}void IRAM_ATTR onRc2(){captureRcEdge(Pins::RC_CHANNEL_2,rcChannel2);}void IRAM_ATTR onRc3(){captureRcEdge(Pins::RC_ACTUATOR,rcActuator);}void IRAM_ATTR onRc5(){captureRcEdge(Pins::RC_MODE,rcMode);}

bool outputLevel(bool active,bool high){return high?active:!active;}void setBrake(const Track&t,bool a){digitalWrite(t.brakePin,outputLevel(a,Fixed::BRAKE_ACTIVE_HIGH));if(t.side==TrackSide::Left)leftBrakeActive=a;else rightBrakeActive=a;}void setReverse(Track&t,bool rev){digitalWrite(t.reversePin,outputLevel(rev,espSettingTrackReverseActiveHigh()));t.appliedSign=rev?-1:1;portENTER_CRITICAL(&hallMux);if(t.side==TrackSide::Left)leftPulseSign=t.appliedSign;else rightPulseSign=t.appliedSign;portEXIT_CRITICAL(&hallMux);}
#if ROBOTLIDAR_HW_PROFILE==ROBOTLIDAR_HW_MCP4725_HW399
bool i2cPresent(uint8_t a){Wire.beginTransmission(a);return Wire.endTransmission()==0;}uint16_t mcpCode(uint16_t mv){return (uint32_t)min<uint16_t>(mv,Fixed::MCP4725_FULL_SCALE_MV)*4095/Fixed::MCP4725_FULL_SCALE_MV;}bool writeMcp(uint8_t a,uint16_t mv){uint16_t v=mcpCode(mv);Wire.beginTransmission(a);Wire.write((v>>8)&0x0F);Wire.write(v&0xFF);return Wire.endTransmission()==0;}
#endif
uint8_t dacCode(uint16_t mv){return (uint32_t)min<uint16_t>(mv,Fixed::INTERNAL_DAC_FULL_SCALE_MV)*255/Fixed::INTERNAL_DAC_FULL_SCALE_MV;}bool writeThrottle(const Track&t,uint16_t mv){bool ok=false;
#if ROBOTLIDAR_HW_PROFILE==ROBOTLIDAR_HW_MCP4725_HW399
if(!throttleBackendReady)return false;ok=writeMcp(t.side==TrackSide::Left?Fixed::MCP4725_LEFT_ADDRESS:Fixed::MCP4725_RIGHT_ADDRESS,mv);if(!ok)throttleBackendReady=false;
#else
dacWrite(t.throttlePin,dacCode(mv));ok=true;
#endif
if(ok){if(t.side==TrackSide::Left)leftThrottleMv=mv;else rightThrottleMv=mv;}return ok;}
bool initializeThrottleBackend(){
#if ROBOTLIDAR_HW_PROFILE==ROBOTLIDAR_HW_MCP4725_HW399
Wire.begin(21,22);Wire.setClock(Fixed::I2C_CLOCK_HZ);bool l=i2cPresent(Fixed::MCP4725_LEFT_ADDRESS),r=i2cPresent(Fixed::MCP4725_RIGHT_ADDRESS);throttleBackendReady=l&&r;if(!l)Serial.println("ERR,MCP4725_LEFT_NOT_FOUND");if(!r)Serial.println("ERR,MCP4725_RIGHT_NOT_FOUND");if(throttleBackendReady){writeMcp(Fixed::MCP4725_LEFT_ADDRESS,0);writeMcp(Fixed::MCP4725_RIGHT_ADDRESS,0);}return throttleBackendReady;
#else
throttleBackendReady=true;dacWrite(Pins::LEFT_THROTTLE_DAC,0);dacWrite(Pins::RIGHT_THROTTLE_DAC,0);leftThrottleMv=rightThrottleMv=0;return true;
#endif
}
const char* hardwareProfileName(){
#if ROBOTLIDAR_HW_PROFILE==ROBOTLIDAR_HW_MCP4725_HW399
return "MCP4725_HW399";
#else
return "LEGACY_INTERNAL_DAC";
#endif
}bool estopOkay(){return digitalRead(Pins::ESTOP_OK)==LOW;}

void initializeActuator(){
#if ROBOTLIDAR_ENABLE_RC_ACTUATOR
pinMode(Pins::ACTUATOR_RPWM,OUTPUT);pinMode(Pins::ACTUATOR_LPWM,OUTPUT);ledcSetup(Fixed::ACTUATOR_RPWM_CHANNEL,Fixed::ACTUATOR_PWM_HZ,Fixed::ACTUATOR_PWM_BITS);ledcSetup(Fixed::ACTUATOR_LPWM_CHANNEL,Fixed::ACTUATOR_PWM_HZ,Fixed::ACTUATOR_PWM_BITS);ledcAttachPin(Pins::ACTUATOR_RPWM,Fixed::ACTUATOR_RPWM_CHANNEL);ledcAttachPin(Pins::ACTUATOR_LPWM,Fixed::ACTUATOR_LPWM_CHANNEL);
#endif
}void stopActuatorOutput(){
#if ROBOTLIDAR_ENABLE_RC_ACTUATOR
ledcWrite(Fixed::ACTUATOR_RPWM_CHANNEL,0);ledcWrite(Fixed::ACTUATOR_LPWM_CHANNEL,0);
#endif
actuatorAppliedDirection=0;}void applyActuatorOutput(int8_t d){
#if ROBOTLIDAR_ENABLE_RC_ACTUATOR
ledcWrite(Fixed::ACTUATOR_RPWM_CHANNEL,0);ledcWrite(Fixed::ACTUATOR_LPWM_CHANNEL,0);if(d>0)ledcWrite(Fixed::ACTUATOR_RPWM_CHANNEL,Fixed::ACTUATOR_PWM);else if(d<0)ledcWrite(Fixed::ACTUATOR_LPWM_CHANNEL,Fixed::ACTUATOR_PWM);
#endif
actuatorAppliedDirection=d;}
int8_t requestedActuatorDirection(uint16_t p){uint16_t c=espSettingRcCenterUs(3),db=espSettingRcDeadbandUs();int8_t d=p+db<c?-1:(p>c+db?1:0);return espSettingActuatorReversed()?-d:d;}
void updateActuator(const RcSnapshot&rc,uint32_t now){if(!estopOkay()||controlMode==ControlMode::Safe){stopActuatorOutput();actuatorPendingDirection=0;actuatorNeutralSeen=false;return;}int8_t req=0;bool valid=false;if(controlMode==ControlMode::RcManual){valid=rc.actuatorValid;if(valid)req=requestedActuatorDirection(rc.actuatorUs);}else{valid=lastAuxFrameMs&&now-lastAuxFrameMs<=espSettingRosAuxTimeoutMs();if(valid)req=rosActuatorCommand;}if(!valid){stopActuatorOutput();actuatorPendingDirection=0;actuatorNeutralSeen=false;return;}if(!req){stopActuatorOutput();actuatorPendingDirection=0;actuatorTimeoutLatched=false;actuatorNeutralSeen=true;return;}if(!actuatorNeutralSeen||actuatorTimeoutLatched){stopActuatorOutput();return;}if(actuatorAppliedDirection&&req!=actuatorAppliedDirection){stopActuatorOutput();actuatorPendingDirection=req;actuatorGuardUntilMs=now+espSettingActuatorGuardMs();return;}if(actuatorPendingDirection){if(req!=actuatorPendingDirection){actuatorPendingDirection=req;actuatorGuardUntilMs=now+espSettingActuatorGuardMs();}if((int32_t)(now-actuatorGuardUntilMs)<0)return;applyActuatorOutput(actuatorPendingDirection);actuatorRunStartMs=now;actuatorPendingDirection=0;return;}if(!actuatorAppliedDirection){applyActuatorOutput(req);actuatorRunStartMs=now;return;}if(now-actuatorRunStartMs>=espSettingActuatorTimeoutMs()){stopActuatorOutput();actuatorTimeoutLatched=true;Serial.println("EVT,ACTUATOR,TIMEOUT");}}

int8_t signOf(int16_t v){return v>0?1:(v<0?-1:0);}int16_t clampCommand(long v){return constrain(v,-1000L,1000L);}int16_t moveToward(int16_t c,int16_t t,int16_t s){if(c<t)return min<int16_t>(c+s,t);if(c>t)return max<int16_t>(c-s,t);return c;}uint16_t commandToThrottleMv(int16_t cmd){int m=abs(cmd);uint16_t lo=espSettingThrottleIdleMv(),hi=espSettingThrottleMaxMv();if(!m)return lo;return lo+(uint32_t)(hi-lo)*constrain(m,0,1000)/1000;}void applyTrackSafe(Track&t){t.target=t.actual=0;t.phase=Track::Phase::Normal;setBrake(t,true);writeThrottle(t,0);}
void disarmSystem(const char*reason){static char last[32]="";bool was=armed;armed=false;applyTrackSafe(leftTrack);applyTrackSafe(rightTrack);stopActuatorOutput();stopBrushController();const char*r=reason?reason:"requested";bool changed=reason&&strncmp(last,r,sizeof(last));if(was||changed){Serial.print("EVT,DISARM,");Serial.println(r);}if(!reason)last[0]=0;else{strncpy(last,r,sizeof(last)-1);last[sizeof(last)-1]=0;}}
bool armSystem(const char*src){if(!estopOkay()){Serial.println("ERR,ESTOP_OPEN");return false;}if(!throttleBackendReady){Serial.println("ERR,THROTTLE_DAC_NOT_READY");return false;}if(leftTrack.target||rightTrack.target){Serial.println("ERR,NONZERO_TARGET");return false;}setBrake(leftTrack,true);setBrake(rightTrack,true);if(!writeThrottle(leftTrack,espSettingThrottleIdleMv())||!writeThrottle(rightTrack,espSettingThrottleIdleMv()))return false;setReverse(leftTrack,false);setReverse(rightTrack,false);delay(50);armed=true;watchdogTripped=false;lastDriveFrameMs=millis();Serial.print("EVT,ARMED,");Serial.println(src?src:"UNKNOWN");return true;}
void updateTrack(Track&t,uint32_t now){if(!armed){applyTrackSafe(t);return;}if(!Fixed::REVERSE_SUPPORTED&&t.target<0)t.target=0;int8_t ds=signOf(t.target);if(t.phase==Track::Phase::BrakeBeforeReverse){setBrake(t,true);writeThrottle(t,espSettingThrottleIdleMv());t.actual=0;if((int32_t)(now-t.deadlineMs)>=0){if(!ds)t.phase=Track::Phase::Normal;else{setReverse(t,ds<0);t.phase=Track::Phase::ReverseSettle;t.deadlineMs=now+espSettingReverseSettleMs();}}return;}if(t.phase==Track::Phase::ReverseSettle){setBrake(t,true);writeThrottle(t,espSettingThrottleIdleMv());t.actual=0;if((int32_t)(now-t.deadlineMs)>=0)t.phase=Track::Phase::Normal;return;}if(ds&&ds!=t.appliedSign){t.actual=0;setBrake(t,true);writeThrottle(t,espSettingThrottleIdleMv());t.phase=Track::Phase::BrakeBeforeReverse;t.deadlineMs=now+espSettingReverseBrakeMs();return;}t.actual=moveToward(t.actual,t.target,espSettingRampStep());if(!t.actual){setBrake(t,Fixed::HOLD_BRAKE_AT_ZERO);writeThrottle(t,espSettingThrottleIdleMv());return;}if(!writeThrottle(t,commandToThrottleMv(t.actual))){t.actual=0;setBrake(t,true);return;}setBrake(t,false);}

bool pulseValid(uint8_t ch,uint16_t p,uint32_t age){return p>=espSettingRcMinUs(ch)&&p<=espSettingRcMaxUs(ch)&&age<=uint32_t(espSettingRcTimeoutMs())*1000UL;}RcSnapshot readRcSnapshot(){RcSnapshot r;uint32_t a1,a2,a3,a5,n=micros();portENTER_CRITICAL(&rcMux);r.channel1Us=rcChannel1.pulseUs;r.channel2Us=rcChannel2.pulseUs;r.actuatorUs=rcActuator.pulseUs;r.modeUs=rcMode.pulseUs;a1=rcChannel1.lastPulseUs;a2=rcChannel2.lastPulseUs;a3=rcActuator.lastPulseUs;a5=rcMode.lastPulseUs;portEXIT_CRITICAL(&rcMux);r.channel1AgeUs=a1?n-a1:UINT32_MAX;r.channel2AgeUs=a2?n-a2:UINT32_MAX;r.actuatorAgeUs=a3?n-a3:UINT32_MAX;r.modeAgeUs=a5?n-a5:UINT32_MAX;r.modeValid=pulseValid(5,r.modeUs,r.modeAgeUs);r.actuatorValid=pulseValid(3,r.actuatorUs,r.actuatorAgeUs);r.valid=pulseValid(1,r.channel1Us,r.channel1AgeUs)&&pulseValid(2,r.channel2Us,r.channel2AgeUs)&&r.modeValid;return r;}
ControlMode modeFromPulse(uint16_t p){uint16_t mn=espSettingRcMinUs(5),c=espSettingRcCenterUs(5),mx=espSettingRcMaxUs(5);uint16_t low=(mn+c)/2,high=(c+mx)/2;if(p<=low)return ControlMode::RcManual;if(p>=high)return ControlMode::RosAutonomous;return ControlMode::Safe;}const char*modeName(ControlMode m){return m==ControlMode::RcManual?"RC":(m==ControlMode::RosAutonomous?"ROS":"SAFE");}const char*actuatorName(){return actuatorTimeoutLatched?"TMO":(actuatorAppliedDirection>0?"UP":(actuatorAppliedDirection<0?"DN":"STP"));}
int16_t pulseToCommand(uint8_t ch,uint16_t p){int32_t mn=espSettingRcMinUs(ch),c=espSettingRcCenterUs(ch),mx=espSettingRcMaxUs(ch),db=espSettingRcDeadbandUs();int32_t d=p-c;if(abs(d)<=db)return 0;if(d>0){int32_t den=max<int32_t>(1,mx-c-db);return constrain((d-db)*1000/den,0L,1000L);}int32_t den=max<int32_t>(1,c-mn-db);return constrain((d+db)*1000/den,-1000L,0L);}void calculateRcTracks(const RcSnapshot&r,int16_t&l,int16_t&rr){l=pulseToCommand(1,r.channel1Us);rr=pulseToCommand(2,r.channel2Us);}bool rcTracksNeutral(const RcSnapshot&r){int16_t l,rr;calculateRcTracks(r,l,rr);return l==0&&rr==0;}
void changeMode(ControlMode m){if(m==controlMode)return;disarmSystem("MODE_CHANGE");stopActuatorOutput();stopBrushController();actuatorPendingDirection=0;actuatorNeutralSeen=false;rosActuatorCommand=0;setBrushRosCommand(0);controlMode=m;Serial.print("EVT,MODE,");Serial.println(modeName(m));}void updateCommandSource(){RcSnapshot r=readRcSnapshot();lastRcSnapshot=r;lastRcValid=r.valid;changeMode(r.modeValid?modeFromPulse(r.modeUs):ControlMode::Safe);if(!estopOkay()){disarmSystem("ESTOP");return;}if(!throttleBackendReady){disarmSystem("THROTTLE_DAC");return;}if(controlMode==ControlMode::Safe){disarmSystem(nullptr);return;}if(!r.valid){disarmSystem("RC_SIGNAL_LOST");return;}if(controlMode==ControlMode::RcManual){if(!armed){if(!rcTracksNeutral(r)){disarmSystem("WAIT_NEUTRAL");return;}if(!armSystem("RC"))return;}calculateRcTracks(r,leftTrack.target,rightTrack.target);}}

uint8_t xorChecksum(const char*t){uint8_t c=0;while(*t)c^=(uint8_t)*t++;return c;}int hexNibble(char c){if(c>='0'&&c<='9')return c-'0';if(c>='A'&&c<='F')return c-'A'+10;if(c>='a'&&c<='f')return c-'a'+10;return -1;}void sendFrame(const String&b){Serial.print(b);Serial.print('*');uint8_t c=xorChecksum(b.c_str());if(c<16)Serial.print('0');Serial.println(c,HEX);}void sendAck(uint32_t s,const char*r){sendFrame("ACK,"+String(s)+","+r+","+String(leftTrack.target)+","+String(rightTrack.target));}bool verifyAndStripChecksum(char*l){char*star=strrchr(l,'*');if(!star||strlen(star+1)!=2)return false;int h=hexNibble(star[1]),q=hexNibble(star[2]);if(h<0||q<0)return false;uint8_t got=(h<<4)|q;*star=0;return xorChecksum(l)==got;}
void processFrame(char*l){if(!verifyAndStripChecksum(l)){Serial.println("ERR,CHECKSUM");return;}char*save=nullptr;char*cmd=strtok_r(l,",",&save);char*seq=strtok_r(nullptr,",",&save);if(!cmd||!seq){Serial.println("ERR,FORMAT");return;}uint32_t s=strtoul(seq,nullptr,10);lastSequence=s;if(!strcmp(cmd,"DRV")){char*a=strtok_r(nullptr,",",&save),*b=strtok_r(nullptr,",",&save);if(!a||!b){sendAck(s,"BAD_FORMAT");return;}lastDriveFrameMs=millis();watchdogTripped=false;if(controlMode!=ControlMode::RosAutonomous){sendAck(s,"NOT_ROS_MODE");return;}if(!armed){sendAck(s,"DISARMED");return;}leftTrack.target=clampCommand(strtol(a,nullptr,10));rightTrack.target=clampCommand(strtol(b,nullptr,10));sendAck(s,"OK");return;}if(!strcmp(cmd,"AUX")){char*a=strtok_r(nullptr,",",&save),*b=strtok_r(nullptr,",",&save);if(!a||!b){sendAck(s,"BAD_FORMAT");return;}if(controlMode!=ControlMode::RosAutonomous){sendAck(s,"NOT_ROS_MODE");return;}long av=strtol(a,nullptr,10),bv=strtol(b,nullptr,10);rosActuatorCommand=av>0?1:(av<0?-1:0);if(espSettingActuatorReversed())rosActuatorCommand=-rosActuatorCommand;setBrushRosCommand(constrain(bv,0L,1000L));lastAuxFrameMs=millis();sendAck(s,"OK");return;}if(!strcmp(cmd,"ARM")){char*v=strtok_r(nullptr,",",&save);bool req=v&&atoi(v);if(!req){rosActuatorCommand=0;setBrushRosCommand(0);disarmSystem("REMOTE");sendAck(s,"OK");return;}if(controlMode!=ControlMode::RosAutonomous){sendAck(s,"NOT_ROS_MODE");return;}leftTrack.target=rightTrack.target=0;sendAck(s,armSystem("ROS")?"OK":"REFUSED");return;}if(!strcmp(cmd,"STOP")){rosActuatorCommand=0;setBrushRosCommand(0);lastAuxFrameMs=0;disarmSystem("REMOTE_STOP");sendAck(s,"OK");return;}if(!strcmp(cmd,"PING")){sendAck(s,"PONG");return;}sendAck(s,"UNKNOWN");}
void readSerialFrames(){while(Serial.available()){char c=Serial.read();if(c=='\r')continue;if(c=='\n'){if(serialLineLength){serialLine[serialLineLength]=0;processFrame(serialLine);serialLineLength=0;}continue;}if(serialLineLength+1<sizeof(serialLine))serialLine[serialLineLength++]=c;else{serialLineLength=0;Serial.println("ERR,LINE_TOO_LONG");}}}

void sendTelemetry(uint32_t now){int64_t lt,rt;uint32_t lp,rp;portENTER_CRITICAL(&hallMux);lt=leftTicks;rt=rightTicks;lp=leftWindowPulses;rp=rightWindowPulses;leftWindowPulses=rightWindowPulses=0;portEXIT_CRITICAL(&hallMux);uint32_t e=now-lastTelemetryPulseMs;if(!e)e=1;lastTelemetryPulseMs=now;char b[460];snprintf(b,sizeof(b),"TEL,%lu,%d,%d,%d,%d,%d,%d,%lld,%lld,%lu,%lu,%d,%s,%u,%u,%u,0,%d,BOAT_MIX,%u,%d,%d,%d,%u,%d,%u,%u,%d,%d",(unsigned long)now,armed,estopOkay(),leftTrack.target,rightTrack.target,leftTrack.actual,rightTrack.actual,(long long)lt,(long long)rt,(unsigned long)(lp*1000/e),(unsigned long)(rp*1000/e),watchdogTripped,modeName(controlMode),lastRcSnapshot.channel1Us,lastRcSnapshot.channel2Us,lastRcSnapshot.modeUs,lastRcValid,lastRcSnapshot.actuatorUs,lastRcSnapshot.actuatorValid,actuatorAppliedDirection,actuatorTimeoutLatched,getBrushRcPulseUs(),getBrushRcValid(),getBrushRosCommand(),getBrushThrottleMv(),getBrushBrakeActive(),getBrushMcpReady());sendFrame(String(b));}

#if ROBOTLIDAR_ENABLE_OLED
bool oledDevicePresent(uint8_t a){OledWire.beginTransmission(a);return OledWire.endTransmission()==0;}bool initializeOled(){if(oledDevicePresent(Fixed::OLED_ADDRESS_PRIMARY))oledAddress=Fixed::OLED_ADDRESS_PRIMARY;else if(oledDevicePresent(Fixed::OLED_ADDRESS_SECONDARY))oledAddress=Fixed::OLED_ADDRESS_SECONDARY;else return false;oledReady=oled.begin(SSD1306_SWITCHCAPVCC,oledAddress,false,false);return oledReady;}void printRc(uint8_t ch,uint16_t p,uint32_t a){if(pulseValid(ch,p,a))oled.print(p);else oled.print(F("----"));}void updateOled(){if(!oledReady)return;oled.clearDisplay();oled.setTextColor(SSD1306_WHITE);oled.setTextSize(1);oled.setCursor(0,0);oled.print(modeName(controlMode));oled.print(armed?F(" A1 "):F(" A0 "));oled.print(estopOkay()?F("EST:OK"):F("EST:STOP"));oled.setCursor(0,8);oled.print(F("CH1:"));printRc(1,lastRcSnapshot.channel1Us,lastRcSnapshot.channel1AgeUs);oled.setCursor(66,8);oled.print(F("CH2:"));printRc(2,lastRcSnapshot.channel2Us,lastRcSnapshot.channel2AgeUs);oled.setCursor(0,16);oled.print(F("CMD:L"));oled.print(leftTrack.target);oled.setCursor(66,16);oled.print(F("R:"));oled.print(rightTrack.target);oled.setCursor(0,24);oled.print(F("DAC:"));oled.print(leftThrottleMv);oled.setCursor(66,24);oled.print(rightThrottleMv);oled.setCursor(0,32);oled.print(F("RV:"));oled.print(leftTrack.appliedSign<0);oled.print('/');oled.print(rightTrack.appliedSign<0);oled.setCursor(66,32);oled.print(F("BK:"));oled.print(leftBrakeActive);oled.print('/');oled.print(rightBrakeActive);oled.setCursor(0,40);oled.print(F("CH3:"));printRc(3,lastRcSnapshot.actuatorUs,lastRcSnapshot.actuatorAgeUs);oled.setCursor(66,40);oled.print(F("CH5:"));printRc(5,lastRcSnapshot.modeUs,lastRcSnapshot.modeAgeUs);oled.setCursor(0,48);oled.print(F("HL:"));if(espSettingHallEnabled())oled.print((long long)leftTicks);else oled.print(F("OFF"));oled.setCursor(66,48);oled.print(F("HR:"));if(espSettingHallEnabled())oled.print((long long)rightTicks);else oled.print(F("OFF"));oled.setCursor(0,56);oled.print(F("A:"));oled.print(actuatorName());oled.setCursor(48,56);oled.print(F("B:"));oled.print(getBrushRosCommand());oled.display();}
#endif

void setup(){Serial.begin(Fixed::SERIAL_BAUD);delay(200);initializeEsp32SettingsController();pinMode(Pins::LEFT_REVERSE,OUTPUT);pinMode(Pins::RIGHT_REVERSE,OUTPUT);pinMode(Pins::LEFT_BRAKE,OUTPUT);pinMode(Pins::RIGHT_BRAKE,OUTPUT);pinMode(Pins::ESTOP_OK,INPUT_PULLUP);pinMode(Pins::RC_CHANNEL_1,INPUT_PULLDOWN);pinMode(Pins::RC_CHANNEL_2,INPUT_PULLDOWN);pinMode(Pins::RC_ACTUATOR,INPUT_PULLDOWN);pinMode(Pins::RC_MODE,INPUT_PULLDOWN);digitalWrite(Pins::LEFT_BRAKE,HIGH);digitalWrite(Pins::RIGHT_BRAKE,HIGH);setReverse(leftTrack,false);setReverse(rightTrack,false);initializeActuator();stopActuatorOutput();initializeThrottleBackend();applyTrackSafe(leftTrack);applyTrackSafe(rightTrack);OledWire.begin(Pins::OLED_SDA,Pins::OLED_SCL);OledWire.setClock(Fixed::OLED_I2C_HZ);
#if ROBOTLIDAR_ENABLE_OLED
initializeOled();
#endif
initializeBrushController();attachInterrupt(digitalPinToInterrupt(Pins::RC_CHANNEL_1),onRc1,CHANGE);attachInterrupt(digitalPinToInterrupt(Pins::RC_CHANNEL_2),onRc2,CHANGE);attachInterrupt(digitalPinToInterrupt(Pins::RC_ACTUATOR),onRc3,CHANGE);attachInterrupt(digitalPinToInterrupt(Pins::RC_MODE),onRc5,CHANGE);lastControlMs=lastTelemetryMs=lastTelemetryPulseMs=lastOledMs=millis();String boot=String("BOOT,ESP32_WROOM_TRACK_CONTROLLER,16,")+hardwareProfileName()+",40PIN,RUNTIME_CONFIG_V2,RC_SAFE_ROS,ROS_AUX_ACTUATOR_BRUSH";boot+=espSettingHallEnabled()?",HALL_ON":",HALL_OFF";sendFrame(boot);}
void loop(){readSerialFrames();uint32_t now=millis();if(!estopOkay()&&armed)disarmSystem("ESTOP");if(!throttleBackendReady&&armed)disarmSystem("THROTTLE_DAC");if(now-lastControlMs>=Fixed::CONTROL_PERIOD_MS){lastControlMs=now;updateCommandSource();updateActuator(lastRcSnapshot,now);updateBrushController();if(controlMode==ControlMode::RosAutonomous&&armed&&now-lastDriveFrameMs>Fixed::COMMAND_WATCHDOG_MS){watchdogTripped=true;disarmSystem("WATCHDOG");}if(controlMode==ControlMode::RosAutonomous&&(lastAuxFrameMs==0||now-lastAuxFrameMs>espSettingRosAuxTimeoutMs())){rosActuatorCommand=0;setBrushRosCommand(0);}updateTrack(leftTrack,now);updateTrack(rightTrack,now);}if(now-lastTelemetryMs>=Fixed::TELEMETRY_PERIOD_MS){lastTelemetryMs=now;sendTelemetry(now);}
#if ROBOTLIDAR_ENABLE_OLED
if(now-lastOledMs>=Fixed::OLED_PERIOD_MS){lastOledMs=now;updateOled();}
#endif
}
