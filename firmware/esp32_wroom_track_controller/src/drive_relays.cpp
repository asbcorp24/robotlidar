#include "drive_relays.h"

bool isMcp23017Ready();
bool mcp23017PinMode(uint8_t pin, bool output, bool pullup);
bool mcp23017DigitalWrite(uint8_t pin, bool high);

namespace {
bool initialized = false;
constexpr uint8_t brakePins[] = {
    DriveRelayPins::LEFT_BRAKE_RELEASE, DriveRelayPins::RIGHT_BRAKE_RELEASE,
    DriveRelayPins::BRUSH_BRAKE_RELEASE};
}

bool driveRelaysReady() {
    if (!isMcp23017Ready()) initialized = false;
    return initialized;
}

bool initializeDriveRelays() {
    initialized = false;
    if (!isMcp23017Ready()) return false;
    // Clear only the three brake latches BEFORE enabling their outputs.
    // GPB0/2/4 are not direction outputs; direction stays on ESP32 GPIO.
    for (uint8_t pin : brakePins) {
        if (!mcp23017DigitalWrite(pin, false)) return false;
    }
    for (uint8_t pin : brakePins) {
        if (!mcp23017PinMode(pin, true, false)) return false;
    }
    initialized = true;
    return true;
}

bool setDriveBrakeRelay(RelayDrive drive, bool applied) {
    const auto index = static_cast<uint8_t>(drive);
    if (!driveRelaysReady() || index >= 3) return false;
    if (!mcp23017DigitalWrite(brakePins[index], !applied)) {
        initialized = false;
        return false;
    }
    return true;
}
