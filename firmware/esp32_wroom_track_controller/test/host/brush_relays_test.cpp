#include <cassert>
#include <iostream>
#include "../../src/brush_controller.cpp"

TestSerial Serial;
TwoWire OledWire;
uint32_t testNowMs = 100;
uint8_t testPinModes[40]={}, testPinLevels[40]={};
unsigned testPinWrites[40]={};
ControlMode controlMode = ControlMode::RosAutonomous;
bool armed = true, testEstop = true, testBrakeHigh = true;
bool estopOkay() { return testEstop; }
uint16_t espSettingRcMinUs(uint8_t) { return 750; }
uint16_t espSettingRcMaxUs(uint8_t) { return 2250; }
uint16_t espSettingRcCenterUs(uint8_t) { return 1500; }
uint16_t espSettingRcDeadbandUs() { return 45; }
uint16_t espSettingRcTimeoutMs() { return 160; }
uint16_t espSettingBrushIdleMv() { return 1000; }
uint16_t espSettingBrushMaxMv() { return 2850; }
uint16_t espSettingBrushStopUs() { return 1050; }
bool espSettingBrushBrakeActiveHigh() { return testBrakeHigh; }
uint16_t espSettingRosAuxTimeoutMs() { return 500; }
bool initializeMcp23017();
bool mcp23017DigitalWrite(uint8_t,bool);

void runUpdate() { testNowMs += 25; updateBrushController(); assert((OledWire.regs[0x15]&0x15)==0x15); }
bool releaseCoil() { return (OledWire.regs[0x15] & 0x20) != 0; }
bool lastDacZero() {
    const auto& bytes=OledWire.dacFrames.back();
    return bytes.size()==2 && bytes[0]==0 && bytes[1]==0;
}

int main() {
    OledWire.reset();
    assert(initializeMcp23017());
    for(uint8_t pin : {8,10,12})assert(mcp23017DigitalWrite(pin,true));
    assert(initializeDriveRelays());
    initializeBrushController();
    assert(testPinModes[12]==OUTPUT);
    assert(!releaseCoil());assert(lastDacZero());
    assert(testPinLevels[12]==LOW);
    std::cout << "PASS: brush starts at zero throttle, brake applied, forward direction\n";

    setBrushRosCommand(1000);runUpdate();
    assert(getBrushThrottleMv()==2850);assert(releaseCoil());
    assert(!getBrushBrakeActive());
    assert(testPinLevels[12]==LOW);
    setBrushRosCommand(-1000);runUpdate();
    assert(lastDacZero());assert(!releaseCoil());assert(testPinLevels[12]==LOW);
    testNowMs+=151;updateBrushController();
    assert(testPinLevels[12]==HIGH);assert(getBrushDirection()==-1);
    assert(getBrushThrottleMv()==2850);assert(releaseCoil());
    assert((OledWire.regs[0x15]&0x15)==0x15);
    assert(pulseToSignedCommand(750)==-1000);
    assert(pulseToSignedCommand(1500)==0);
    assert(pulseToSignedCommand(2250)==1000);
    std::cout << "PASS: bidirectional brush stays on GPIO12, preserving RC mapping and reversal pause\n";
    testEstop=false;runUpdate();
    assert(getBrushThrottleMv()==0);assert(getBrushBrakeActive());
    assert(!releaseCoil());assert(lastDacZero());
    std::cout << "PASS: normal brush motion and ESTOP use GPB5, not GPIO15\n";

    testEstop=true;armed=false;runUpdate();
    assert(lastDacZero());assert(!releaseCoil());
    armed=true;setBrushRosCommand(1000);runUpdate();assert(releaseCoil());
    testNowMs+=501;updateBrushController();
    assert(lastDacZero());assert(!releaseCoil());
    std::cout << "PASS: DISARM and command timeout apply brush brake and zero DAC\n";

    testBrakeHigh=false;setBrushRosCommand(1000);runUpdate();
    assert(!releaseCoil());assert(!getBrushBrakeActive());
    stopBrushController();assert(releaseCoil());assert(getBrushBrakeActive());
    testBrakeHigh=true;stopBrushController();assert(!releaseCoil());
    std::cout << "PASS: existing brush brake polarity setting still inverts output\n";

    setBrushRosCommand(1000);
    // The DAC write succeeds; the subsequent brake-release write NACKs.
    OledWire.failAt=OledWire.transactions+2;runUpdate();
    assert(!driveRelaysReady());assert(getBrushThrottleMv()==0);assert(lastDacZero());
    runUpdate();assert(getBrushThrottleMv()==0);assert(lastDacZero());
    assert(testPinWrites[12]>0);
    assert(testPinWrites[15]==0);
    std::cout << "PASS: brake write failure zeros throttle; no GPIO15 output writes\n";
}
