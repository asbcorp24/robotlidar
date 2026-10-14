#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_PWMServoDriver.h>

// ============================================================
// PCA9685 PWM test
// Encoder changes one common duty cycle for ALL 16 outputs.
// ============================================================

namespace Pins {
constexpr uint8_t I2C_SDA = 21;
constexpr uint8_t I2C_SCL = 22;

constexpr uint8_t ENC_A = 17;
constexpr uint8_t ENC_B = 16;

// Optional encoder push button. Leave disconnected if not used.
constexpr int8_t ENC_SW = -1;
}

namespace Config {
constexpr uint8_t PCA9685_ADDR = 0x40;

// Common PWM frequency for all 16 PCA9685 channels.
// Start with 1000 Hz for a generic PWM motor-controller test.
// If the controller expects RC/servo pulses, this is NOT the correct mode:
// use ~50 Hz and pulse-width generation instead.
constexpr float PWM_FREQ_HZ = 1000.0f;

// Encoder resolution.
constexpr int DUTY_STEP_PERCENT = 1;

// Start at 0% for safety.
constexpr int DUTY_START_PERCENT = 0;

// Encoder debounce / transition guard.
constexpr uint32_t ENCODER_MIN_INTERVAL_US = 800;
}

Adafruit_PWMServoDriver pca(Config::PCA9685_ADDR);

volatile int encoderDelta = 0;
volatile uint8_t lastEncoderState = 0;
volatile uint32_t lastEncoderEdgeUs = 0;

int dutyPercent = Config::DUTY_START_PERCENT;
uint16_t pwmCounts = 0;

portMUX_TYPE encoderMux = portMUX_INITIALIZER_UNLOCKED;

void IRAM_ATTR onEncoderChange() {
  const uint32_t nowUs = micros();
  if (nowUs - lastEncoderEdgeUs < Config::ENCODER_MIN_INTERVAL_US) {
    return;
  }

  const uint8_t a = digitalRead(Pins::ENC_A) ? 1 : 0;
  const uint8_t b = digitalRead(Pins::ENC_B) ? 1 : 0;
  const uint8_t state = (a << 1) | b;

  // Gray-code transition table.
  static const int8_t table[16] = {
     0, -1,  1,  0,
     1,  0,  0, -1,
    -1,  0,  0,  1,
     0,  1, -1,  0
  };

  const uint8_t index = (lastEncoderState << 2) | state;
  const int8_t step = table[index];

  if (step != 0) {
    portENTER_CRITICAL_ISR(&encoderMux);
    encoderDelta += step;
    portEXIT_CRITICAL_ISR(&encoderMux);
    lastEncoderEdgeUs = nowUs;
  }

  lastEncoderState = state;
}

void applyDutyToAllChannels(int percent) {
  percent = constrain(percent, 0, 100);

  // PCA9685 has 12-bit resolution: 0..4095.
  pwmCounts = (uint16_t)lroundf((percent / 100.0f) * 4095.0f);

  for (uint8_t ch = 0; ch < 16; ++ch) {
    if (percent <= 0) {
      // Full OFF.
      pca.setPWM(ch, 0, 4096);
    } else if (percent >= 100) {
      // Full ON.
      pca.setPWM(ch, 4096, 0);
    } else {
      pca.setPWM(ch, 0, pwmCounts);
    }
  }

  Serial.printf("PWM duty = %d%%, counts = %u/4095, freq = %.1f Hz\n",
                percent, pwmCounts, Config::PWM_FREQ_HZ);
}

bool pcaPresent() {
  Wire.beginTransmission(Config::PCA9685_ADDR);
  return Wire.endTransmission() == 0;
}

void setup() {
  Serial.begin(115200);
  delay(300);

  Serial.println();
  Serial.println("========================================");
  Serial.println("PCA9685 PWM + encoder test");
  Serial.println("All 16 outputs use the same duty cycle.");
  Serial.println("========================================");

  pinMode(Pins::ENC_A, INPUT_PULLUP);
  pinMode(Pins::ENC_B, INPUT_PULLUP);

  lastEncoderState =
      ((digitalRead(Pins::ENC_A) ? 1 : 0) << 1) |
      (digitalRead(Pins::ENC_B) ? 1 : 0);

  attachInterrupt(digitalPinToInterrupt(Pins::ENC_A), onEncoderChange, CHANGE);
  attachInterrupt(digitalPinToInterrupt(Pins::ENC_B), onEncoderChange, CHANGE);

  Wire.begin(Pins::I2C_SDA, Pins::I2C_SCL);
  Wire.setClock(400000);

  if (!pcaPresent()) {
    Serial.printf("ERROR: PCA9685 not found at address 0x%02X\n", Config::PCA9685_ADDR);
    Serial.println("Check VCC, GND, SDA, SCL and address jumpers.");
    while (true) {
      delay(1000);
    }
  }

  Serial.printf("PCA9685 found at 0x%02X\n", Config::PCA9685_ADDR);

  pca.begin();
  pca.setOscillatorFrequency(25000000);
  pca.setPWMFreq(Config::PWM_FREQ_HZ);
  delay(10);

  applyDutyToAllChannels(dutyPercent);

  Serial.println();
  Serial.println("Encoder:");
  Serial.printf("  A -> GPIO%d\n", Pins::ENC_A);
  Serial.printf("  B -> GPIO%d\n", Pins::ENC_B);
  Serial.println("Rotate encoder: duty 0..100%");
}

void loop() {
  int delta = 0;

  portENTER_CRITICAL(&encoderMux);
  delta = encoderDelta;
  encoderDelta = 0;
  portEXIT_CRITICAL(&encoderMux);

  if (delta != 0) {
    const int oldDuty = dutyPercent;

    // Most mechanical encoders generate multiple transitions per detent.
    // This deliberately processes each valid transition so even cheap
    // encoders remain responsive.
    dutyPercent += delta * Config::DUTY_STEP_PERCENT;
    dutyPercent = constrain(dutyPercent, 0, 100);

    if (dutyPercent != oldDuty) {
      applyDutyToAllChannels(dutyPercent);
    }
  }

  delay(2);
}
