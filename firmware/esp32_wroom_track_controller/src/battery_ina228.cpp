#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_SSD1306.h>
#include <Adafruit_INA228.h>

// Shared hardware I2C bus is created once in main.cpp:
//   OledWire.begin(GPIO4, GPIO23)
// This module must NOT call begin() again.
//
// Devices on the same bus:
// SSD1315 OLED: 0x3C / 0x3D (7-bit, datasheet 0x78 / 0x7A)
// INA228:       0x40
// MCP4725:      0x60 / 0x61
extern TwoWire OledWire;
extern Adafruit_SSD1306 oled;
extern bool oledReady;
extern uint8_t oledAddress;

namespace {
constexpr uint32_t I2C_HZ = 100000;
constexpr uint8_t OLED_ADDR_1 = 0x3C;
constexpr uint8_t OLED_ADDR_2 = 0x3D;
constexpr uint8_t INA228_ADDRESS = 0x40;
constexpr uint32_t BATTERY_SAMPLE_PERIOD_MS = 1000;

Adafruit_INA228 ina228;
bool batteryInitialized = false;
bool batteryOnline = false;
uint32_t lastBatterySampleMs = 0;
float lastVoltage = 0.0f;
float lastCurrent = 0.0f;
float lastPower = 0.0f;
float lastTemperature = 0.0f;

bool present(uint8_t address) {
    OledWire.beginTransmission(address);
    return OledWire.endTransmission() == 0;
}

uint8_t checksum(const char* text) {
    uint8_t c = 0;
    while (*text) c ^= static_cast<uint8_t>(*text++);
    return c;
}

void publishBattery(uint32_t now, float voltage, float current, float power, float temperature) {
    char body[112];
    char frame[120];
    snprintf(body, sizeof(body), "BAT,%lu,%d,%.3f,%.3f,%.3f,%.2f",
             static_cast<unsigned long>(now), batteryOnline ? 1 : 0,
             voltage, current, power, temperature);
    snprintf(frame, sizeof(frame), "%s*%02X", body, checksum(body));
    Serial.println(frame);
}
}

void initializeBatteryMonitor() {
    if (batteryInitialized) return;
    batteryInitialized = true;

    // main.cpp has already called OledWire.begin(4,23).
    // Only normalize the clock; do not recreate/rebind the bus.
    OledWire.setClock(I2C_HZ);
    delay(20);

    // Retry OLED at the known-good 100 kHz speed.
    uint8_t address = 0;
    if (present(OLED_ADDR_1)) address = OLED_ADDR_1;
    else if (present(OLED_ADDR_2)) address = OLED_ADDR_2;

    if (address) {
        oledAddress = address;
        oledReady = oled.begin(SSD1306_SWITCHCAPVCC, oledAddress, false, false);
        OledWire.setClock(I2C_HZ);
        if (oledReady) {
            oled.clearDisplay();
            oled.setTextColor(SSD1306_WHITE);
            oled.setTextSize(1);
            oled.setCursor(0, 0);
            oled.println("RobotLidar SSD1315");
            oled.println("I2C 100k OK");
            oled.display();
            Serial.print("SSD1315,ONLINE,0x");
            Serial.print(oledAddress, HEX);
            Serial.println(",128X64,100KHZ");
        } else {
            Serial.println("SSD1315,BEGIN_FAILED,100KHZ");
        }
    } else {
        oledReady = false;
        Serial.println("SSD1315,NOT_FOUND,100KHZ");
    }

    // INA228 uses exactly the same already-running TwoWire instance.
    OledWire.setClock(I2C_HZ);
    if (!present(INA228_ADDRESS)) {
        batteryOnline = false;
        Serial.println("ERR,INA228_NOT_FOUND,0x40");
        lastBatterySampleMs = millis();
        return;
    }

    batteryOnline = ina228.begin(INA228_ADDRESS, &OledWire);
    OledWire.setClock(I2C_HZ);

    if (batteryOnline) Serial.println("EVT,INA228,ONLINE,0x40");
    else Serial.println("ERR,INA228_INIT_FAILED,0x40");

    lastBatterySampleMs = millis();
}

void updateBatteryMonitor() {
    if (!batteryInitialized) return;
    const uint32_t now = millis();
    if (now - lastBatterySampleMs < BATTERY_SAMPLE_PERIOD_MS) return;
    lastBatterySampleMs = now;

    if (!batteryOnline) {
        lastVoltage = lastCurrent = lastPower = lastTemperature = 0.0f;
        publishBattery(now, 0.0f, 0.0f, 0.0f, 0.0f);
        return;
    }

    OledWire.setClock(I2C_HZ);
    float voltage = ina228.getBusVoltage_V();
    float current = ina228.getCurrent_mA() / 1000.0f;
    float power = ina228.getPower_mW() / 1000.0f;
    float temperature = ina228.readDieTemp();

    if (!isfinite(voltage) || !isfinite(current) || !isfinite(power) || !isfinite(temperature)) {
        batteryOnline = false;
        voltage = current = power = temperature = 0.0f;
        Serial.println("ERR,INA228_INVALID_DATA");
    }

    lastVoltage = voltage;
    lastCurrent = current;
    lastPower = power;
    lastTemperature = temperature;

    OledWire.setClock(I2C_HZ);
    publishBattery(now, voltage, current, power, temperature);
}

bool batteryMonitorOnline() { return batteryOnline; }
float batteryVoltageVolts() { return lastVoltage; }
float batteryCurrentAmps() { return lastCurrent; }
float batteryPowerWatts() { return lastPower; }
float batteryTemperatureC() { return lastTemperature; }
