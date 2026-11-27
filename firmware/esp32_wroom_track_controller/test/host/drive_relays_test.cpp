#include <cassert>
#include <iostream>
#include "Arduino.h"
#include "Wire.h"
#include "drive_relays.h"

TestSerial Serial;
TwoWire OledWire;
bool initializeMcp23017();
bool mcp23017DigitalWrite(uint8_t,bool);
bool mcp23017PinMode(uint8_t,bool,bool);

void resetDevice() {
    OledWire.reset();
    assert(initializeMcp23017());
}

int main() {
    resetDevice();
    OledWire.failAt = OledWire.transactions + 1;
    assert(!initializeDriveRelays());
    assert(!driveRelaysReady());
    std::cout << "PASS: missing expander refuses relay initialization\n";

    resetDevice();
    for (uint8_t pin : {8,10,12}) assert(mcp23017DigitalWrite(pin,true));
    assert(mcp23017DigitalWrite(14,true));
    assert(mcp23017DigitalWrite(15,true));
    assert(mcp23017DigitalWrite(3,true));
    assert(mcp23017PinMode(3,true,false));
    const uint8_t portALatch = OledWire.regs[0x14];
    const uint8_t portADirection = OledWire.regs[0];
    OledWire.writes.clear();
    assert(initializeDriveRelays());
    assert(driveRelaysReady());
    assert(OledWire.regs[0x15] == 0xD5);
    assert(OledWire.regs[1] == 0xD5);
    assert(OledWire.regs[0x14] == portALatch);
    assert(OledWire.regs[0] == portADirection);
    for (unsigned i=0;i<3;++i) assert(OledWire.writes[i].address == 0x15);
    std::cout << "PASS: zero latches precede output enable; GPA and unused GPB0/2/4/6/7 preserved\n";

    for (int i=0;i<3;++i) {
        const auto drive = static_cast<RelayDrive>(i);
        const uint8_t releaseBit = uint8_t(1u << (2*i+1));
        assert(setDriveBrakeRelay(drive,false));
        assert(OledWire.regs[0x15] == uint8_t(0xD5 | releaseBit));
        assert(setDriveBrakeRelay(drive,true));
        assert(OledWire.regs[0x15] == 0xD5);
    }
    std::cout << "PASS: three brake channels, NC polarity; unused GPB0/2/4 unchanged\n";

    const auto writesBeforeInvalid = OledWire.writes.size();
    assert(!setDriveBrakeRelay(static_cast<RelayDrive>(3),false));
    assert(OledWire.writes.size() == writesBeforeInvalid);
    std::cout << "PASS: invalid drive cannot write an unrelated output\n";

    for (int failure=1;failure<=9;++failure) {
        resetDevice();
        OledWire.failAt = OledWire.transactions + failure;
        assert(!initializeDriveRelays());
        assert(!driveRelaysReady());
    }
    std::cout << "PASS: all 9 initialization transaction failures keep relays not ready\n";

    resetDevice(); assert(initializeDriveRelays());
    OledWire.failAt = OledWire.transactions + 1;
    assert(!setDriveBrakeRelay(RelayDrive::Left,false));
    assert(!driveRelaysReady());
    const int attempts = OledWire.transactions;
    assert(!setDriveBrakeRelay(RelayDrive::Right,false));
    assert(OledWire.transactions == attempts);
    assert(initializeMcp23017());
    assert(!driveRelaysReady());
    assert(initializeDriveRelays());
    assert(driveRelaysReady());
    std::cout << "PASS: running I2C fault blocks subsequent commands until relay reinitialization\n";

    OledWire.failAt = OledWire.transactions + 1;
    assert(!mcp23017DigitalWrite(0,true));
    assert(!driveRelaysReady());
    assert(initializeMcp23017());
    assert(!driveRelaysReady());
    std::cout << "PASS: an expander fault in another peripheral invalidates drive readiness\n";
}
