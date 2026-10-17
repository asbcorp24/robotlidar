# RobotLidar Controller Rev.A — KiCad engineering package

Назначение: единая интерфейсная плата RobotLidar без готовых логических модулей.

## Архитектура
- ESP32-WROOM-32E
- USB-C + CH340C для прошивки и USB Serial к Raspberry Pi
- MCP23017 (I2C GPIO expander)
- MCP4725 (щётка, аналоговый выход)
- DRV8825 (шаговый двигатель M4, STEP/DIR, токовая регулировка)
- 74LVC2G14 (формирование Hall LEFT/RIGHT после делителя 5V -> 3.3V)
- 2 x TLP240A для Reverse LEFT/RIGHT; Brake не используется
- TLV9002 для буферизации/масштабирования аналоговых выходов LEFT/RIGHT
- AP2112K-3.3 для 3.3 V
- INA228 с Kelvin-разъёмом под внешний шунт
- разъёмы RC CH1..CH5, SBUS 8/B, OLED, RCWL-1655, E-STOP, BLDC LEFT/RIGHT, actuator, brush, stepper

## Hall
LEFT Hall: 5V signal -> 10k series -> node -> 20k to GND -> 74LVC2G14 -> GPIO34  
RIGHT Hall: 5V signal -> 10k series -> node -> 20k to GND -> 74LVC2G14 -> GPIO35

GND контроллеров BLDC и логической платы должен иметь общий reference для Hall/Throttle,
кроме изолированных TLP240A контактов Reverse/Brake.

## SBUS CH8 / M4
MC8RE-V2 порт 8/B SBUS -> К155ЛА3 -> GPIO36 ESP32/UART2 RX -> CH8 -> target 0..360 degrees.

К155ЛА3 используется как инвертор:
- pin 1 + pin 2 -> SBUS 8/B
- pin 3 -> 1k -> GPIO36
- pin 14 -> +5V
- pin 7 -> GND

Обычный PWM CH6 к GPIO36 в этой версии не подключается.

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


## Brush
- RC CH4 -> GPIO5
- GPIO12 -> BRUSH REVERSE
- MCP4725 0x60 -> аналоговая скорость
- GPIO15 свободен; BRUSH BRAKE не используется


## Track Brake
Начиная с прошивки v1.9.6 отдельные Brake LEFT/RIGHT не используются.
GPIO18 и GPIO19 свободны. Остановка выполняется снятием газа, а смена направления —
через 0 В на throttle, паузу, переключение Reverse и повторную паузу.
