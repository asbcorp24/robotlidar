# RobotLidar Controller Rev.A — KiCad engineering package

Назначение: единая интерфейсная плата RobotLidar без готовых логических модулей.

## Архитектура
- ESP32-WROOM-32E
- USB-C + CH340C для прошивки и USB Serial к Raspberry Pi
- MCP23017 (I2C GPIO expander)
- MCP4725 (щётка, аналоговый выход)
- DRV8825 (шаговый двигатель M4, STEP/DIR, токовая регулировка)
- 74LVC2G14 (формирование Hall LEFT/RIGHT после делителя 5V -> 3.3V)
- 4 x TLP240A для Reverse/Brake LEFT/RIGHT
- TLV9002 для буферизации/масштабирования аналоговых выходов LEFT/RIGHT
- AP2112K-3.3 для 3.3 V
- INA228 с Kelvin-разъёмом под внешний шунт
- разъёмы RC CH1..CH6, OLED, RCWL-1655, E-STOP, BLDC LEFT/RIGHT, actuator, brush, stepper

## Hall
LEFT Hall: 5V signal -> 10k series -> node -> 20k to GND -> 74LVC2G14 -> GPIO34  
RIGHT Hall: 5V signal -> 10k series -> node -> 20k to GND -> 74LVC2G14 -> GPIO35

GND контроллеров BLDC и логической платы должен иметь общий reference для Hall/Throttle,
кроме изолированных TLP240A контактов Reverse/Brake.

## M4
RC CH6 -> GPIO36 -> target 0..360 degrees.

На интегрированной плате вместо HW-095/L298N используется DRV8825:
- MCP23017 GPA3 -> STEP
- MCP23017 GPA4 -> DIR
- MCP23017 GPA5 -> ENABLE
- MCP23017 GPA6 -> резерв HOME

## Файлы
- `RobotLidar_Controller.kicad_pro` — проект KiCad
- `RobotLidar_Controller.kicad_pcb` — PCB Rev.A
- `RobotLidar_Controller.sch` — инженерная схема-заготовка
- `docs/pinmap.csv` — актуальная распиновка
- `docs/nets.csv` — карта соединений
- `production/BOM.csv` — BOM
- `production/PRE_FAB_CHECKLIST.txt` — список проверок перед производством

## Перед заказом платы
Это Rev.A engineering package. До финального Gerber обязательно подтвердить:
1. напряжение и номинальный ток шагового двигателя;
2. Rsense DRV8825;
3. максимальный ток INA228 и внешний шунт;
4. реальный диапазон аналогового входа газа BLDC;
5. точные серии и шаг разъёмов;
6. выполнить ERC/DRC и проверить Gerber.
