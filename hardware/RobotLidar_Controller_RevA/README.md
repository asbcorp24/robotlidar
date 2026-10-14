# RobotLidar Controller Rev.A

Компонентная версия основной платы RobotLidar.

Состав:
- ESP32-WROOM-32E
- MCP23017
- MCP4725
- DRV8825
- 74LVC2G14 Hall conditioning
- TLV9002 throttle buffers
- 4x TLP240A Reverse/Brake isolation
- AP2112K-3.3
- INA228 with external Kelvin shunt
- CH340C USB-UART

CH6/GPIO36 задаёт положение M4 0..360°. В интегрированной версии M4 переводится с временного HW-095 на DRV8825 STEP/DIR.

**Rev.A engineering package:** перед производством обязательно проверить ERC/DRC, Rsense шаговика, диапазон газа BLDC, внешний шунт INA228 и выбранные разъёмы.
