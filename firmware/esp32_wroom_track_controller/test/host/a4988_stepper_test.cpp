#include <cassert>
#include <iostream>
#include "Wire.h"
#include "../../src/aux_motor_controller.cpp"

TestSerial Serial;
TwoWire OledWire;
uint32_t testNowMs=100;
uint8_t testPinModes[40]={},testPinLevels[40]={};
unsigned testPinWrites[40]={};
ControlMode controlMode=ControlMode::RosAutonomous;
bool armed=false,testEstop=true;
uint32_t lastSequence=0;
bool estopOkay(){return testEstop;}
uint16_t espSettingRosAuxTimeoutMs(){return 500;}
void initializeUltrasonicController(){}
void updateUltrasonicController(){}
void initializeEsp32SettingsController(){}
void updateEsp32SettingsController(){}
void initializeBatteryMonitor(){}
void updateBatteryMonitor(){}

void tick(uint32_t ms=12){testNowMs+=ms;updateAuxMotorController();}
unsigned pulses(){
    unsigned count=0;
    for(const auto& event:pinEvents())if(event.pin==18 && event.level==HIGH)++count;
    return count;
}
void feedSbus(uint16_t ch8,uint8_t flags=0){
    uint8_t frame[25]={};frame[0]=0x0F;frame[23]=flags;
    for(unsigned bit=0;bit<11;++bit){
        unsigned at=7*11+bit;
        frame[1+at/8]|=((ch8>>bit)&1u)<<(at%8);
    }
    for(auto byte:frame)SbusSerial.rx.push_back(byte);
}

int main(){
    initializeAuxMotorController();
    assert(testPinModes[18]==OUTPUT && testPinModes[19]==OUTPUT && testPinModes[15]==OUTPUT);
    assert(testPinLevels[15]==HIGH && testPinLevels[18]==LOW);
    assert(pulses()==0 && currentStep==0 && getAuxMotorMcpReady());
    assert(SbusSerial.baud==100000 && SbusSerial.rxPin==36 && !SbusSerial.inverted);
    std::cout<<"PASS: A4988 boots disabled on STEP18/DIR19/EN15; SBUS RX36 retained\n";

    armed=true;setAuxMotorRosCommand(-990);tick();
    assert(currentStep==1 && pulses()==1 && testPinLevels[19]==HIGH);
    assert(testPinLevels[18]==LOW && testPinLevels[15]==LOW);
    tick(11);assert(testPinLevels[15]==LOW);
    tick(1);assert(testPinLevels[15]==HIGH && pulses()==1);
    setAuxMotorRosCommand(-1000);tick();
    assert(currentStep==0 && testPinLevels[19]==LOW && pulses()==2);
    std::cout<<"PASS: forward/reverse pulses; final step held for one interval before idle disable\n";

    unsigned before=pulses();
    for(int i=0;i<200;++i){setAuxMotorRosCommand(2000);tick();}
    assert(currentStep==200 && getAuxMotorActual()==360 && pulses()==before+200);
    tick();assert(pulses()==before+200 && testPinLevels[15]==HIGH);
    for(int i=0;i<200;++i){setAuxMotorRosCommand(-2000);tick();}
    assert(currentStep==0 && getAuxMotorActual()==0 && pulses()==before+400);
    tick();assert(pulses()==before+400);
    std::cout<<"PASS: 200 full steps per revolution and clamped 0..360 degree targets\n";

    uint32_t lastDir=0,rising=0,falling=0;bool haveRise=false,haveFall=false;
    for(const auto& event:pinEvents()){
        assert(event.pin==15 || event.pin==18 || event.pin==19);
        if(event.pin==19)lastDir=event.atUs;
        if(event.pin==18 && event.level==HIGH){
            assert(event.atUs-lastDir>=2);
            if(haveFall)assert(event.atUs-falling>=1);
            rising=event.atUs;haveRise=true;
        }else if(event.pin==18 && haveRise){
            assert(event.atUs-rising>=2);falling=event.atUs;haveFall=true;haveRise=false;
        }
    }
    std::cout<<"PASS: STEP HIGH/LOW and DIR setup timing; other GPIOs untouched\n";

    setAuxMotorRosCommand(1000);tick();before=pulses();
    tick(501);assert(pulses()==before && testPinLevels[15]==HIGH && targetStep==currentStep);
    setAuxMotorRosCommand(1000);tick();armed=false;before=pulses();tick(1);
    assert(pulses()==before && testPinLevels[15]==HIGH);
    armed=true;setAuxMotorRosCommand(1000);tick();testEstop=false;before=pulses();tick(1);
    assert(pulses()==before && testPinLevels[15]==HIGH);
    testEstop=true;controlMode=ControlMode::Safe;tick();assert(testPinLevels[15]==HIGH);
    std::cout<<"PASS: ROS timeout, DISARM, ESTOP and SAFE disable outputs without extra steps\n";

    controlMode=ControlMode::RcManual;feedSbus(1800);before=pulses();tick();
    assert(sbusValid && targetStep==200 && pulses()==before+1);
    feedSbus(1800,8);before=pulses();tick();
    assert(!sbusValid && pulses()==before && testPinLevels[15]==HIGH);
    feedSbus(1800);tick();before=pulses();tick(81);
    assert(!sbusValid && pulses()==before && testPinLevels[15]==HIGH);
    std::cout<<"PASS: SBUS CH8 positioning, failsafe and missing-frame timeout\n";

    controlMode=ControlMode::RosAutonomous;setAuxMotorRosCommand(1000);tick();
    setAuxMotorRosCommand(-1000);tick();stopAuxMotorController();before=pulses();
    assert(targetStep==currentStep && testPinLevels[15]==HIGH && testPinLevels[18]==LOW);
    assert(pulses()==before);
    std::cout<<"PASS: explicit stop cancels target and leaves STEP LOW, ENABLE HIGH\n";
}
