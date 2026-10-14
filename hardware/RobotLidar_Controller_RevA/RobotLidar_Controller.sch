EESchema Schematic File Version 4
LIBS:RobotLidar_Controller-cache
EELAYER 29 0
EELAYER END
$Descr A3 16535 11693
encoding utf-8
Sheet 1 1
Title "RobotLidar Controller Rev.A"
Date "2026-10-14"
Rev "A"
Comp "RobotLidar"
Comment1 "ESP32 + RC + Hall + isolated BLDC interface + DRV8825"
Comment2 "Engineering starter - run ERC/DRC before fabrication"
$EndDescr
Text Notes 700 650 0 140 ~ 28
ROBOTLIDAR CONTROLLER REV.A
Text Notes 700 900 0 70 ~ 14
See docs/nets.csv and docs/pinmap.csv for complete net assignment.
Text Notes 700 1250 0 90 ~ 18
POWER
Text Notes 700 2500 0 90 ~ 18
MCU / USB
Text Notes 700 5300 0 60 ~ 12
GPIO map: CH1=27 CH2=33 CH3=14 CH4=5 CH5=13 CH6=36; Hall L=34 R=35; ESTOP=32; I2C=21/22.
Text Notes 5200 1250 0 90 ~ 18
I2C / EXPANDER
Text Notes 5650 3550 0 60 ~ 12
MCP23017: GPA0 LEFT activity; GPA1 RIGHT; GPA2 BRUSH; GPA3 STEP; GPA4 DIR; GPA5 ENABLE; GPA6 HOME reserve.
Text Notes 5200 4300 0 90 ~ 18
HALL INPUTS 5V -> 3.3V
Text Notes 5650 5450 0 60 ~ 12
Each input: Hall OUT --10k-- node --20k-- GND; node -> 74LVC2G14; outputs -> GPIO34/GPIO35.
Text Notes 9200 1250 0 90 ~ 18
STEPPER M4
Text Notes 9400 3800 0 60 ~ 12
DRV8825 STEP/DIR/ENABLE from MCP23017. VMOT separate. Rsense TBD after motor current is confirmed.
Text Notes 9200 4550 0 90 ~ 18
BLDC INTERFACE / ISOLATION
Text Notes 9400 5050 0 60 ~ 12
4 x TLP240A: Reverse L/R and Brake L/R. TLV9002 buffers throttle outputs.
Text Notes 5200 6400 0 90 ~ 18
ANALOG / SENSORS
Text Notes 5650 6900 0 60 ~ 12
MCP4725 brush DAC; INA228 external Kelvin shunt; OLED on shared I2C bus.
Text Notes 9200 7600 0 90 ~ 18
EXTERNAL CONNECTORS
Text Notes 9400 8000 0 60 ~ 12
RC, Hall L/R, OLED, RCWL, BLDC L/R, Actuator, Brush, Stepper and E-STOP connectors.
$EndSCHEMATC