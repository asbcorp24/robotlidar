#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>

namespace TestPins {
constexpr uint8_t SDA = 4;
constexpr uint8_t SCL = 23;
}

namespace TestConfig {
constexpr uint32_t SERIAL_BAUD = 115200;
constexpr uint32_t I2C_HZ = 100000;
constexpr uint8_t OLED_WIDTH = 128;
constexpr uint8_t OLED_HEIGHT = 64;
constexpr uint8_t OLED_ADDR_1 = 0x3C;
constexpr uint8_t OLED_ADDR_2 = 0x3D;
}

TwoWire TestWire = TwoWire(1);
Adafruit_SSD1306 display(TestConfig::OLED_WIDTH, TestConfig::OLED_HEIGHT, &TestWire, -1);

bool ping(uint8_t address) {
  TestWire.beginTransmission(address);
  return TestWire.endTransmission() == 0;
}

uint8_t scanI2c() {
  Serial.println("I2C,SCAN_BEGIN,SDA4,SCL23,100KHZ");
  uint8_t found = 0;
  for (uint8_t address = 0x08; address <= 0x77; ++address) {
    TestWire.beginTransmission(address);
    const uint8_t result = TestWire.endTransmission();
    if (result == 0) {
      ++found;
      Serial.print("I2C,FOUND,0x");
      if (address < 0x10) Serial.print('0');
      Serial.println(address, HEX);
    }
  }
  if (!found) Serial.println("I2C,NO_DEVICES,SDA4,SCL23");
  Serial.print("I2C,SCAN_END,FOUND=");
  Serial.println(found);
  return found;
}

void printLineLevels() {
  pinMode(TestPins::SDA, INPUT_PULLUP);
  pinMode(TestPins::SCL, INPUT_PULLUP);
  delay(5);
  Serial.print("I2C,LINES,SDA=");
  Serial.print(digitalRead(TestPins::SDA));
  Serial.print(",SCL=");
  Serial.println(digitalRead(TestPins::SCL));
}

bool initDisplay() {
  uint8_t address = 0;
  if (ping(TestConfig::OLED_ADDR_1)) address = TestConfig::OLED_ADDR_1;
  else if (ping(TestConfig::OLED_ADDR_2)) address = TestConfig::OLED_ADDR_2;

  if (!address) {
    Serial.println("OLED,NOT_FOUND,EXPECTED_0x3C_OR_0x3D");
    return false;
  }

  Serial.print("OLED,FOUND,0x");
  Serial.println(address, HEX);

  if (!display.begin(SSD1306_SWITCHCAPVCC, address, false, false)) {
    Serial.println("OLED,BEGIN_FAILED");
    return false;
  }

  display.clearDisplay();
  display.setTextColor(SSD1306_WHITE);
  display.setTextSize(1);
  display.setCursor(0, 0);
  display.println("RobotLidar OLED TEST");
  display.println("SDA GPIO4");
  display.println("SCL GPIO23");
  display.print("ADDR 0x");
  display.println(address, HEX);
  display.println("I2C OK");
  display.display();
  Serial.println("OLED,DISPLAY_OK");
  return true;
}

void setup() {
  Serial.begin(TestConfig::SERIAL_BAUD);
  delay(500);

  Serial.println();
  Serial.println("BOOT,ESP32_OLED_I2C_MINIMAL_TEST");

  printLineLevels();

  TestWire.begin(TestPins::SDA, TestPins::SCL);
  TestWire.setClock(TestConfig::I2C_HZ);
  delay(20);

  scanI2c();
  initDisplay();
}

void loop() {
  delay(3000);
  scanI2c();
}
