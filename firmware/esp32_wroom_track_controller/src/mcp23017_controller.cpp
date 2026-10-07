#include <Arduino.h>
#include <Wire.h>

extern TwoWire OledWire;

namespace Mcp23017Config {
constexpr uint8_t ADDRESS = 0x20;

// BANK=0 register map
constexpr uint8_t IODIRA = 0x00;
constexpr uint8_t IODIRB = 0x01;
constexpr uint8_t GPPUA  = 0x0C;
constexpr uint8_t GPPUB  = 0x0D;
constexpr uint8_t GPIOA  = 0x12;
constexpr uint8_t GPIOB  = 0x13;
constexpr uint8_t OLATA  = 0x14;
constexpr uint8_t OLATB  = 0x15;
}

static bool mcp23017Ready = false;
static uint16_t mcp23017Direction = 0xFFFF; // 1=input, 0=output
static uint16_t mcp23017Pullups = 0x0000;
static uint16_t mcp23017OutputLatch = 0x0000;

static bool mcp23017Write8(uint8_t reg, uint8_t value) {
    if (!mcp23017Ready) return false;
    OledWire.beginTransmission(Mcp23017Config::ADDRESS);
    OledWire.write(reg);
    OledWire.write(value);
    if (OledWire.endTransmission() != 0) {
        mcp23017Ready = false;
        return false;
    }
    return true;
}

static bool mcp23017Read8(uint8_t reg, uint8_t &value) {
    if (!mcp23017Ready) return false;
    OledWire.beginTransmission(Mcp23017Config::ADDRESS);
    OledWire.write(reg);
    if (OledWire.endTransmission(false) != 0) {
        mcp23017Ready = false;
        return false;
    }
    if (OledWire.requestFrom((int)Mcp23017Config::ADDRESS, 1) != 1) {
        mcp23017Ready = false;
        return false;
    }
    value = OledWire.read();
    return true;
}

bool initializeMcp23017() {
    OledWire.beginTransmission(Mcp23017Config::ADDRESS);
    if (OledWire.endTransmission() != 0) {
        mcp23017Ready = false;
        Serial.println("ERR,MCP23017_NOT_FOUND,0x20");
        return false;
    }

    mcp23017Ready = true;

    // Safe boot state: all 16 GPIO are inputs, internal pull-ups disabled.
    // Output latches are cleared before any line can later be switched to output.
    if (!mcp23017Write8(Mcp23017Config::OLATA, 0x00) ||
        !mcp23017Write8(Mcp23017Config::OLATB, 0x00) ||
        !mcp23017Write8(Mcp23017Config::GPPUA, 0x00) ||
        !mcp23017Write8(Mcp23017Config::GPPUB, 0x00) ||
        !mcp23017Write8(Mcp23017Config::IODIRA, 0xFF) ||
        !mcp23017Write8(Mcp23017Config::IODIRB, 0xFF)) {
        Serial.println("ERR,MCP23017_INIT_FAILED,0x20");
        return false;
    }

    mcp23017Direction = 0xFFFF;
    mcp23017Pullups = 0x0000;
    mcp23017OutputLatch = 0x0000;
    Serial.println("EVT,MCP23017,OK,0x20,GPIOA0-A7,GPIOB0-B7");
    return true;
}

bool isMcp23017Ready() {
    return mcp23017Ready;
}

bool mcp23017PinMode(uint8_t pin, bool output, bool pullup) {
    if (!mcp23017Ready || pin >= 16) return false;

    const uint16_t mask = uint16_t(1U) << pin;
    if (output) mcp23017Direction &= ~mask;
    else mcp23017Direction |= mask;

    if (!output && pullup) mcp23017Pullups |= mask;
    else mcp23017Pullups &= ~mask;

    const bool portB = pin >= 8;
    const uint8_t dir = portB ? uint8_t(mcp23017Direction >> 8) : uint8_t(mcp23017Direction);
    const uint8_t pu = portB ? uint8_t(mcp23017Pullups >> 8) : uint8_t(mcp23017Pullups);

    return mcp23017Write8(portB ? Mcp23017Config::GPPUB : Mcp23017Config::GPPUA, pu) &&
           mcp23017Write8(portB ? Mcp23017Config::IODIRB : Mcp23017Config::IODIRA, dir);
}

bool mcp23017DigitalWrite(uint8_t pin, bool high) {
    if (!mcp23017Ready || pin >= 16) return false;

    const uint16_t mask = uint16_t(1U) << pin;
    if (high) mcp23017OutputLatch |= mask;
    else mcp23017OutputLatch &= ~mask;

    const bool portB = pin >= 8;
    const uint8_t value = portB ? uint8_t(mcp23017OutputLatch >> 8) : uint8_t(mcp23017OutputLatch);
    return mcp23017Write8(portB ? Mcp23017Config::OLATB : Mcp23017Config::OLATA, value);
}

bool mcp23017DigitalRead(uint8_t pin, bool &high) {
    if (!mcp23017Ready || pin >= 16) return false;

    uint8_t value = 0;
    const bool portB = pin >= 8;
    if (!mcp23017Read8(portB ? Mcp23017Config::GPIOB : Mcp23017Config::GPIOA, value)) return false;

    high = (value & (uint8_t(1U) << (pin & 7))) != 0;
    return true;
}

bool mcp23017ReadAll(uint16_t &value) {
    uint8_t a = 0, b = 0;
    if (!mcp23017Read8(Mcp23017Config::GPIOA, a)) return false;
    if (!mcp23017Read8(Mcp23017Config::GPIOB, b)) return false;
    value = uint16_t(a) | (uint16_t(b) << 8);
    return true;
}
