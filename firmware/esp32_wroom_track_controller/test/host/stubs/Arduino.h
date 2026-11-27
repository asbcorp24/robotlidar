#pragma once
#include <stdint.h>
#include <algorithm>
#include <deque>
#include <string>
#include <sstream>
#include <type_traits>
#include <vector>
using std::min;
template<class T> T constrain(T value,T low,T high) {
    return value<low?low:(value>high?high:value);
}
#define IRAM_ATTR
#define INPUT_PULLDOWN 2
#define OUTPUT 1
#define HIGH 1
#define LOW 0
#define CHANGE 3
#define HEX 16
#define SERIAL_8E2 1
using portMUX_TYPE = int;
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(x) (void)(x)
#define portEXIT_CRITICAL(x) (void)(x)
#define portENTER_CRITICAL_ISR(x) (void)(x)
#define portEXIT_CRITICAL_ISR(x) (void)(x)
extern uint32_t testNowMs;
extern uint8_t testPinModes[40], testPinLevels[40];
extern unsigned testPinWrites[40];
struct PinEvent { uint8_t pin, level; uint32_t atUs; };
inline std::vector<PinEvent>& pinEvents() { static std::vector<PinEvent> events; return events; }
inline uint32_t& delayedUs() { static uint32_t elapsed=0; return elapsed; }
inline uint32_t millis() { return testNowMs; }
inline uint32_t micros() { return testNowMs*1000+delayedUs(); }
inline void delayMicroseconds(unsigned us) { delayedUs()+=us; }
inline long map(long value,long lo,long hi,long outLo,long outHi) {
    return (value-lo)*(outHi-outLo)/(hi-lo)+outLo;
}
inline int digitalRead(uint8_t) { return 0; }
inline void pinMode(uint8_t pin,int mode) { testPinModes[pin]=mode; }
inline void digitalWrite(uint8_t pin,int level) {
    testPinLevels[pin]=level;
    ++testPinWrites[pin];
    pinEvents().push_back({pin,static_cast<uint8_t>(level),micros()});
}
inline int digitalPinToInterrupt(uint8_t pin) { return pin; }
inline void attachInterrupt(int,void(*)(),int) {}
class String {
    std::string value;
public:
    String(const char* text="") : value(text) {}
    template<class T,typename std::enable_if<std::is_arithmetic<T>::value,int>::type=0>
    String(T number) : value(std::to_string(number)) {}
    const char* c_str() const { return value.c_str(); }
    friend String operator+(const String& left,const String& right) {
        String result; result.value=left.value+right.value; return result;
    }
};
class HardwareSerial {
public:
    std::deque<uint8_t> rx;
    unsigned baud=0; int format=0, rxPin=-1, txPin=-1; bool inverted=false;
    explicit HardwareSerial(int) {}
    void begin(unsigned speed,int bits,int input,int output,bool invert) {
        baud=speed;format=bits;rxPin=input;txPin=output;inverted=invert;
    }
    int available() const { return static_cast<int>(rx.size()); }
    int read() { int value=rx.front();rx.pop_front();return value; }
};
struct TestSerial {
    template<class T> void print(const T&) {}
    template<class T> void println(const T&) {}
    template<class T> void println(const T&,int) {}
};
extern TestSerial Serial;
