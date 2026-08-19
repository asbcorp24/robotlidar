#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_INA228.h>

// Shared ESP32 peripheral bus:
// GPIO4 SDA / GPIO23 SCL, 100 kHz
// 0x3C/0x3D OLED, 0x40 INA228, 0x60 brush MCP4725, 0x61 auxiliary MCP4725.
extern TwoWire OledWire;

namespace {
Adafruit_INA228 ina228;
bool initialized = false;
bool online = false;
uint32_t lastSampleMs = 0;
constexpr uint8_t INA228_ADDRESS = 0x40;
constexpr uint32_t SAMPLE_PERIOD_MS = 1000;

uint8_t checksum(const char* text) {
    uint8_t c = 0;
    while (*text) c ^= static_cast<uint8_t>(*text++);
    return c;
}

void publishBattery(uint32_t now, float voltage, float current, float power, float temperature) {
    char body[112];
    char frame[120];
    snprintf(
        body,
        sizeof(body),
        "BAT,%lu,%d,%.3f,%.3f,%.3f,%.2f",
        static_cast<unsigned long>(now),
        online ? 1 : 0,
        voltage,
        current,
        power,
        temperature
    );
    snprintf(frame, sizeof(frame), "%s*%02X", body, checksum(body));
    Serial.println(frame);
}
}

void initializeBatteryMonitor() {
    if (initialized) return;
    initialized = true;
    online = ina228.begin(INA228_ADDRESS, &OledWire);
    if (online) Serial.println("EVT,INA228,ONLINE,0x40");
    else Serial.println("ERR,INA228_NOT_FOUND,0x40");
    lastSampleMs = millis();
}

void updateBatteryMonitor() {
    if (!initialized) return;
    const uint32_t now = millis();
    if (now - lastSampleMs < SAMPLE_PERIOD_MS) return;
    lastSampleMs = now;

    if (!online) {
        online = ina228.begin(INA228_ADDRESS, &OledWire);
        if (online) Serial.println("EVT,INA228,ONLINE,0x40");
    }

    float voltage = 0.0f;
    float current = 0.0f;
    float power = 0.0f;
    float temperature = 0.0f;

    if (online) {
        voltage = ina228.getBusVoltage_V();
        current = ina228.getCurrent_mA() / 1000.0f;
        power = ina228.getPower_mW() / 1000.0f;
        temperature = ina228.readDieTemp();
        if (!isfinite(voltage) || !isfinite(current) || !isfinite(power) || !isfinite(temperature)) {
            online = false;
            voltage = current = power = temperature = 0.0f;
            Serial.println("ERR,INA228_INVALID_DATA");
        }
    }

    publishBattery(now, voltage, current, power, temperature);
}
