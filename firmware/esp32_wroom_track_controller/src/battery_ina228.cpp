#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_SSD1306.h>

// INA228 is disabled. This module now only normalizes the shared OLED bus
// to the same 100 kHz settings proven to work in the standalone OLED test.
extern TwoWire OledWire;
extern Adafruit_SSD1306 oled;
extern bool oledReady;
extern uint8_t oledAddress;

namespace {
constexpr uint8_t OLED_SDA = 4;
constexpr uint8_t OLED_SCL = 23;
constexpr uint32_t OLED_I2C_HZ = 100000;
constexpr uint8_t OLED_ADDR_1 = 0x3C;
constexpr uint8_t OLED_ADDR_2 = 0x3D;

bool present(uint8_t address) {
    OledWire.beginTransmission(address);
    return OledWire.endTransmission() == 0;
}
}

void initializeBatteryMonitor() {
    // INA228 intentionally disabled.
    // Re-open the shared bus at 100 kHz because the standalone OLED test
    // is stable at this speed while the former 400 kHz main setting was not.
    OledWire.begin(OLED_SDA, OLED_SCL);
    OledWire.setClock(OLED_I2C_HZ);
    delay(20);

    uint8_t address = 0;
    if (present(OLED_ADDR_1)) address = OLED_ADDR_1;
    else if (present(OLED_ADDR_2)) address = OLED_ADDR_2;

    if (!address) {
        oledReady = false;
        Serial.println("OLED,NOT_FOUND,100KHZ");
        return;
    }

    oledAddress = address;
    oledReady = oled.begin(SSD1306_SWITCHCAPVCC, oledAddress, false, false);

    if (oledReady) {
        Serial.print("OLED,ONLINE,0x");
        Serial.print(oledAddress, HEX);
        Serial.println(",100KHZ");
    } else {
        Serial.println("OLED,BEGIN_FAILED,100KHZ");
    }
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
