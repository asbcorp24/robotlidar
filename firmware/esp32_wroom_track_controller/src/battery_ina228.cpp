// INA228 battery monitor disabled.
// Kept as a stub so the main controller can be built without touching
// the rest of the control code. No I2C access and no BAT telemetry.

void initializeBatteryMonitor() {
    // Disabled by design.
}

void updateBatteryMonitor() {
    // Disabled by design.
}

bool batteryMonitorOnline() {
    return false;
}

float batteryVoltageVolts() {
    return 0.0f;
}

float batteryCurrentAmps() {
    return 0.0f;
}

float batteryPowerWatts() {
    return 0.0f;
}

float batteryTemperatureC() {
    return 0.0f;
}
