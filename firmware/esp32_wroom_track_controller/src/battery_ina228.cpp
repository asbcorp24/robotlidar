#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_SSD1306.h>
#include <Adafruit_INA228.h>
#include <U8g2lib.h>

// Shared physical I2C bus on ESP32:
// SDA GPIO4 / SCL GPIO23 / 100 kHz
// SSD1315 OLED: 0x3C or 0x3D
// INA228:       0x40
// MCP4725:      0x60 / 0x61
//
// The OLED is initialized with U8g2's native SSD1315 driver using software I2C.
// INA228 and the existing runtime OLED framebuffer use OledWire (hardware I2C).
// This prevents the OLED init code from reconfiguring the hardware Wire bus.
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
U8G2_SSD1315_128X64_NONAME_F_SW_I2C ssd1315(
    U8G2_R0,
    /* clock=*/ I2C_SCL,
    /* data=*/ I2C_SDA,
    /* reset=*/ U8X8_PIN_NONE
);

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

void initializeSsd1315Native(uint8_t address) {
    // U8g2 expects the 8-bit I2C address, so shift the normal 7-bit address.
    ssd1315.setI2CAddress(static_cast<uint8_t>(address << 1));
    ssd1315.setBusClock(I2C_HZ);
    ssd1315.begin();
    ssd1315.clearBuffer();
    ssd1315.setFont(u8g2_font_6x10_tf);
    ssd1315.drawStr(0, 10, "RobotLidar SSD1315");
    ssd1315.drawStr(0, 22, "native init OK");
    ssd1315.sendBuffer();
    delay(120);

    // main.cpp already allocated the Adafruit framebuffer before this function.
    // After native SSD1315 init, clear that framebuffer and switch back to the
    // existing runtime UI renderer.
    if (oledReady) {
        restoreSharedBus();
        oled.clearDisplay();
        oled.display();
    }
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

    pinMode(I2C_SDA, INPUT_PULLUP);
    pinMode(I2C_SCL, INPUT_PULLUP);
    delay(5);
    restoreSharedBus();
    delay(20);

    uint8_t displayAddress = 0;
    if (present(OLED_ADDR_1)) displayAddress = OLED_ADDR_1;
    else if (present(OLED_ADDR_2)) displayAddress = OLED_ADDR_2;

    if (displayAddress) {
        oledAddress = displayAddress;
        initializeSsd1315Native(displayAddress);
        Serial.print("SSD1315,NATIVE_ONLINE,0x");
        Serial.print(displayAddress, HEX);
        Serial.println(",128X64,100KHZ");
    } else {
        oledReady = false;
        Serial.println("SSD1315,NOT_FOUND,100KHZ");
    }

    // INA228 stays enabled on the hardware I2C bus.
    restoreSharedBus();
    if (!present(INA228_ADDRESS)) {
        batteryOnline = false;
        Serial.println("ERR,INA228_NOT_FOUND,0x40");
        lastBatterySampleMs = millis();
        return;
    }

    batteryOnline = ina228.begin(INA228_ADDRESS, &OledWire);
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
