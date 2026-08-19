#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_INA228.h>

// OledWire is initialized by main.cpp on GPIO4/GPIO23. INA228 shares that I2C bus.
extern TwoWire OledWire;

namespace {
Adafruit_INA228 ina228;
bool online=false;

uint8_t checksum(const char* text){uint8_t c=0;while(*text)c^=(uint8_t)*text++;return c;}

void batteryTask(void*){
  delay(1500);
  for(;;){
    if(!online){
      online=ina228.begin(0x40,&OledWire);
      if(online) Serial.println("EVT,INA228,ONLINE");
    }
    float v=0.0f,a=0.0f,w=0.0f,t=0.0f;
    if(online){
      v=ina228.getBusVoltage_V();a=ina228.getCurrent_mA()/1000.0f;w=ina228.getPower_mW()/1000.0f;t=ina228.readDieTemp();
      if(!isfinite(v)||!isfinite(a)||!isfinite(w)||!isfinite(t)){online=false;v=a=w=t=0.0f;}
    }
    char body[112];char frame[120];
    snprintf(body,sizeof(body),"BAT,%lu,%d,%.3f,%.3f,%.3f,%.2f",(unsigned long)millis(),online?1:0,v,a,w,t);
    snprintf(frame,sizeof(frame),"%s*%02X",body,checksum(body));
    Serial.println(frame);
    delay(1000);
  }
}

struct BatteryTaskBootstrap{BatteryTaskBootstrap(){xTaskCreatePinnedToCore(batteryTask,"ina228",4096,nullptr,1,nullptr,0);}} bootstrap;
}
