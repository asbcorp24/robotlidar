#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_INA228.h>

// Shared ESP32 peripheral bus:
// GPIO4 SDA / GPIO23 SCL
// 0x3C/0x3D OLED, 0x40 INA228, 0x60 brush MCP4725, 0x61 auxiliary MCP4725.
extern TwoWire OledWire;

namespace {
Adafruit_INA228 ina228;
bool initialized = false;
bool online = false;
uint32_t lastSampleMs = 0;
float lastVoltage = 0.0f;
float lastCurrent = 0.0f;
float lastPower = 0.0f;
float lastTemperature = 0.0f;

constexpr uint8_t INA228_ADDRESS = 0x40;
constexpr uint8_t SHARED_I2C_SDA = 4;
constexpr uint8_t SHARED_I2C_SCL = 23;
constexpr uint32_t SHARED_I2C_HZ = 100000;
constexpr uint32_t SAMPLE_PERIOD_MS = 1000;

uint8_t checksum(const char* text) {
    uint8_t c = 0;
    while (*text) c ^= static_cast<uint8_t>(*text++);
    return c;
}

bool i2cPresent(uint8_t address) {
    OledWire.beginTransmission(address);
    return OledWire.endTransmission() == 0;
}

void restoreSharedBus() {
    OledWire.begin(SHARED_I2C_SDA, SHARED_I2C_SCL);
    OledWire.setClock(SHARED_I2C_HZ);
}

void reportSharedBus() {
    const uint8_t addresses[] = {0x3C, 0x3D, 0x40, 0x60, 0x61};
    bool any = false;
    for (uint8_t address : addresses) {
        if (i2cPresent(address)) {
            any = true;
            Serial.print("I2C,FOUND,0x");
            if (address < 0x10) Serial.print('0');
            Serial.println(address, HEX);
        }
    }
    if (!any) {
        Serial.println("I2C,NO_DEVICES,SDA4,SCL23");
    }
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

    // The OLED is already initialized by main.cpp on this bus. Do a plain
    // address probe first. If INA228 is absent, do NOT call Adafruit begin(),
    // because that routine can touch/reinitialize the shared TwoWire instance.
    OledWire.setClock(SHARED_I2C_HZ);
    reportSharedBus();

    if (!i2cPresent(INA228_ADDRESS)) {
        online = false;
        Serial.println("ERR,INA228_NOT_FOUND,0x40");
        lastSampleMs = millis();
        return;
    }

    online = ina228.begin(INA228_ADDRESS, &OledWire);

    // Adafruit BusIO may call TwoWire::begin(). Restore our explicit pins
    // afterwards so OLED and the MCP4725 devices stay on GPIO4/GPIO23.
    restoreSharedBus();

    if (online) Serial.println("EVT,INA228,ONLINE,0x40");
    else Serial.println("ERR,INA228_INIT_FAILED,0x40");
    lastSampleMs = millis();
}

void updateBatteryMonitor() {
    if (!initialized) return;
    const uint32_t now = millis();
    if (now - lastSampleMs < SAMPLE_PERIOD_MS) return;
    lastSampleMs = now;

    // Do not repeatedly initialize a missing INA228. This keeps the OLED bus
    // untouched when the battery monitor is not installed or disconnected.
    if (!online) {
        lastVoltage = lastCurrent = lastPower = lastTemperature = 0.0f;
        publishBattery(now, 0.0f, 0.0f, 0.0f, 0.0f);
        return;
    }

    OledWire.setClock(SHARED_I2C_HZ);

    float voltage = ina228.getBusVoltage_V();
    float current = ina228.getCurrent_mA() / 1000.0f;
    float power = ina228.getPower_mW() / 1000.0f;
    float temperature = ina228.readDieTemp();

    if (!isfinite(voltage) || !isfinite(current) || !isfinite(power) || !isfinite(temperature)) {
        online = false;
        voltage = current = power = temperature = 0.0f;
        Serial.println("ERR,INA228_INVALID_DATA");
    }

    lastVoltage = voltage;
    lastCurrent = current;
    lastPower = power;
    lastTemperature = temperature;

    OledWire.setClock(SHARED_I2C_HZ);
    publishBattery(now, voltage, current, power, temperature);
}

bool batteryMonitorOnline() { return online; }
float batteryVoltageVolts() { return lastVoltage; }
float batteryCurrentAmps() { return lastCurrent; }
float batteryPowerWatts() { return lastPower; }
float batteryTemperatureC() { return lastTemperature; }
