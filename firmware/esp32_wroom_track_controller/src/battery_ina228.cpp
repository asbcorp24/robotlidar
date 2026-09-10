#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_SSD1306.h>
#include <Adafruit_INA228.h>

// Shared I2C bus on ESP32:
// SDA GPIO4 / SCL GPIO23 / 100 kHz
// SSD1315 OLED: 0x3C or 0x3D
// INA228:       0x40
// MCP4725:      0x60 / 0x61
extern TwoWire OledWire;
extern Adafruit_SSD1306 oled;
extern bool oledReady;
extern uint8_t oledAddress;

namespace {
constexpr uint8_t I2C_SDA = 4;
constexpr uint8_t I2C_SCL = 23;
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

void restoreSharedBus() {
    OledWire.begin(I2C_SDA, I2C_SCL);
    OledWire.setClock(I2C_HZ);
}

void applySsd1315Init() {
    oled.ssd1306_command(0xAE);
    oled.ssd1306_command(0xD5); oled.ssd1306_command(0x80);
    oled.ssd1306_command(0xA8); oled.ssd1306_command(0x3F);
    oled.ssd1306_command(0xD3); oled.ssd1306_command(0x00);
    oled.ssd1306_command(0x40);
    oled.ssd1306_command(0x8D); oled.ssd1306_command(0x14);
    oled.ssd1306_command(0x20); oled.ssd1306_command(0x00);
    oled.ssd1306_command(0xA1);
    oled.ssd1306_command(0xC8);
    oled.ssd1306_command(0xDA); oled.ssd1306_command(0x12);
    oled.ssd1306_command(0x81); oled.ssd1306_command(0xCF);
    oled.ssd1306_command(0xD9); oled.ssd1306_command(0xF1);
    oled.ssd1306_command(0xDB); oled.ssd1306_command(0x40);
    oled.ssd1306_command(0xA4);
    oled.ssd1306_command(0xA6);
    oled.ssd1306_command(0x2E);
    oled.ssd1306_command(0xAF);
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

    // Use the exact bus startup that worked with the OLED test.
    pinMode(I2C_SDA, INPUT_PULLUP);
    pinMode(I2C_SCL, INPUT_PULLUP);
    delay(5);
    restoreSharedBus();
    delay(20);

    // SSD1315 OLED initialization.
    uint8_t address = 0;
    if (present(OLED_ADDR_1)) address = OLED_ADDR_1;
    else if (present(OLED_ADDR_2)) address = OLED_ADDR_2;

    if (address) {
        oledAddress = address;
        oledReady = oled.begin(SSD1306_SWITCHCAPVCC, oledAddress, false, false);
        if (oledReady) {
            OledWire.setClock(I2C_HZ);
            applySsd1315Init();
            oled.clearDisplay();
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

    // INA228 initialization on the same bus. No full I2C scanner is used.
    if (!present(INA228_ADDRESS)) {
        batteryOnline = false;
        Serial.println("ERR,INA228_NOT_FOUND,0x40");
        lastBatterySampleMs = millis();
        return;
    }

    batteryOnline = ina228.begin(INA228_ADDRESS, &OledWire);
    // Adafruit BusIO may alter the TwoWire state; restore our fixed bus setup.
    restoreSharedBus();

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
