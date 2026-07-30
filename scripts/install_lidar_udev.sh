#!/usr/bin/env bash
set -euo pipefail

DEVICE="${1:-/dev/ttyUSB0}"
RULE_FILE="/etc/udev/rules.d/99-ldrobot-stl19p.rules"

if [[ ! -e "$DEVICE" ]]; then
  echo "Устройство не найдено: $DEVICE"
  echo "Подключите LD_CONTROL_SPEED_BOARD_V1.0 и проверьте:"
  echo "  ls -l /dev/ttyUSB* /dev/ttyACM* 2>/dev/null"
  exit 1
fi

PROPERTIES="$(udevadm info --query=property --name="$DEVICE")"
VENDOR_ID="$(printf '%s\n' "$PROPERTIES" | sed -n 's/^ID_VENDOR_ID=//p' | head -n1)"
MODEL_ID="$(printf '%s\n' "$PROPERTIES" | sed -n 's/^ID_MODEL_ID=//p' | head -n1)"
SERIAL_SHORT="$(printf '%s\n' "$PROPERTIES" | sed -n 's/^ID_SERIAL_SHORT=//p' | head -n1)"

if [[ -z "$VENDOR_ID" || -z "$MODEL_ID" ]]; then
  echo "Не удалось определить USB VID/PID для $DEVICE"
  echo "$PROPERTIES"
  exit 1
fi

RULE="SUBSYSTEM==\"tty\", ATTRS{idVendor}==\"$VENDOR_ID\", ATTRS{idProduct}==\"$MODEL_ID\""
if [[ -n "$SERIAL_SHORT" ]]; then
  RULE+=", ATTRS{serial}==\"$SERIAL_SHORT\""
fi
RULE+=", SYMLINK+=\"ldlidar\", GROUP=\"dialout\", MODE=\"0660\""

printf '%s\n' "$RULE" | sudo tee "$RULE_FILE" >/dev/null
sudo udevadm control --reload-rules
sudo udevadm trigger --subsystem-match=tty

for _ in {1..20}; do
  if [[ -e /dev/ldlidar ]]; then
    break
  fi
  sleep 0.1
done

echo "Правило установлено: $RULE_FILE"
echo "USB VID:PID: $VENDOR_ID:$MODEL_ID"
if [[ -n "$SERIAL_SHORT" ]]; then
  echo "Серийный номер: $SERIAL_SHORT"
fi

if [[ -e /dev/ldlidar ]]; then
  echo "Лидар доступен как: /dev/ldlidar -> $(readlink -f /dev/ldlidar)"
else
  echo "Переподключите USB-плату и проверьте: ls -l /dev/ldlidar"
fi
