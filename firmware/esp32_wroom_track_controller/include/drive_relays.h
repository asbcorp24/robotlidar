#pragma once
#include <stdint.h>

// MCP23017 API numbers GPA0..7 as 0..7, GPB0..7 as 8..15.
enum class RelayDrive : uint8_t { Left, Right, Brush };
namespace DriveRelayPins {
constexpr uint8_t LEFT_BRAKE_RELEASE = 9;   // GPB1
constexpr uint8_t RIGHT_BRAKE_RELEASE = 11; // GPB3
constexpr uint8_t BRUSH_BRAKE_RELEASE = 13; // GPB5
}

bool initializeDriveRelays();
bool driveRelaysReady();
// Brake uses COM/NC: coil OFF applies brake, coil ON releases it.
bool setDriveBrakeRelay(RelayDrive drive, bool applied);
