#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_SSD1306.h>

// INA228 is disabled.
// This module now prepares the shared OLED bus and applies an SSD1315-specific
// initialization sequence while keeping the existing Adafruit_GFX framebuffer
// API used by main.cpp.
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

void applySsd1315Init() {
    // SSD1315 128x64 I2C initialization. The controller is highly compatible
    // with SSD1306, but these commands make the intended controller explicit.
    oled.ssd1306_command(0xAE); // display OFF
    oled.ssd1306_command(0xD5); // display clock divide / oscillator
    oled.ssd1306_command(0x80);
    oled.ssd1306_command(0xA8); // multiplex ratio
    oled.ssd1306_command(0x3F); // 1/64 duty
    oled.ssd1306_command(0xD3); // display offset
    oled.ssd1306_command(0x00);
    oled.ssd1306_command(0x40); // display start line = 0
    oled.ssd1306_command(0x8D); // charge pump
    oled.ssd1306_command(0x14); // enable charge pump
    oled.ssd1306_command(0x20); // memory addressing mode
    oled.ssd1306_command(0x00); // horizontal addressing
    oled.ssd1306_command(0xA1); // segment remap
    oled.ssd1306_command(0xC8); // COM scan direction remapped
    oled.ssd1306_command(0xDA); // COM pins configuration
    oled.ssd1306_command(0x12);
    oled.ssd1306_command(0x81); // contrast
    oled.ssd1306_command(0xCF);
    oled.ssd1306_command(0xD9); // pre-charge period
    oled.ssd1306_command(0xF1);
    oled.ssd1306_command(0xDB); // VCOMH deselect level
    oled.ssd1306_command(0x40);
    oled.ssd1306_command(0xA4); // display follows RAM
    oled.ssd1306_command(0xA6); // normal display
    oled.ssd1306_command(0x2E); // deactivate scroll
    oled.ssd1306_command(0xAF); // display ON
}
}

void initializeBatteryMonitor() {
    // INA228 intentionally disabled.
    // Recreate the bus startup sequence exactly like the known-good OLED test.
    pinMode(OLED_SDA, INPUT_PULLUP);
    pinMode(OLED_SCL, INPUT_PULLUP);
    delay(5);

    OledWire.begin(OLED_SDA, OLED_SCL);
    OledWire.setClock(OLED_I2C_HZ);
    delay(20);

    uint8_t address = 0;
    if (present(OLED_ADDR_1)) address = OLED_ADDR_1;
    else if (present(OLED_ADDR_2)) address = OLED_ADDR_2;

    if (!address) {
        oledReady = false;
        Serial.println("SSD1315,NOT_FOUND,100KHZ");
        return;
    }

    oledAddress = address;

    // Allocate/prepare the framebuffer using the SSD1306-compatible transport,
    // then explicitly apply the SSD1315 controller setup above.
    oledReady = oled.begin(SSD1306_SWITCHCAPVCC, oledAddress, false, false);
    if (!oledReady) {
        Serial.println("SSD1315,BEGIN_FAILED,100KHZ");
        return;
    }

    OledWire.setClock(OLED_I2C_HZ);
    applySsd1315Init();
    oled.clearDisplay();
    oled.display();

    Serial.print("SSD1315,ONLINE,0x");
    Serial.print(oledAddress, HEX);
    Serial.println(",128X64,100KHZ");
}

void updateBatteryMonitor() {
    // INA228 disabled by design.
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
