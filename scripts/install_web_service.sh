#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
DEFAULT_WORKSPACE="$(cd "$REPO_DIR/../.." && pwd)"
WORKSPACE="${1:-$DEFAULT_WORKSPACE}"
SERVICE_NAME="robotlidar-web.service"
TEMPLATE="$REPO_DIR/systemd/robotlidar-web.service.template"
TARGET="/etc/systemd/system/$SERVICE_NAME"
USER_NAME="${SUDO_USER:-$USER}"
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
USER_GROUP="$(id -gn "$USER_NAME")"

if [[ ! -f "$WORKSPACE/install/setup.bash" ]]; then
  echo "Не найден $WORKSPACE/install/setup.bash"
  echo "Сначала соберите workspace: colcon build --symlink-install"
  exit 1
fi

if [[ ! -f "$TEMPLATE" ]]; then
  echo "Не найден шаблон systemd: $TEMPLATE"
  exit 1
fi

sudo apt update
sudo apt install -y python3-fastapi python3-uvicorn python3-pil

# Разрешения на USB-лидар, I2C и GPIO. Отсутствующие группы пропускаются.
for DEVICE_GROUP in dialout i2c gpio; do
  if getent group "$DEVICE_GROUP" >/dev/null; then
    sudo usermod -aG "$DEVICE_GROUP" "$USER_NAME"
  fi
done

TEMP_FILE="$(mktemp)"
sed \
  -e "s|@USER@|$USER_NAME|g" \
  -e "s|@GROUP@|$USER_GROUP|g" \
  -e "s|@HOME@|$USER_HOME|g" \
  -e "s|@WORKSPACE@|$WORKSPACE|g" \
  "$TEMPLATE" > "$TEMP_FILE"

sudo install -m 0644 "$TEMP_FILE" "$TARGET"
rm -f "$TEMP_FILE"

mkdir -p "$USER_HOME/robotlidar_data/maps" \
         "$USER_HOME/robotlidar_data/routes" \
         "$USER_HOME/robotlidar_data/config"
sudo chown -R "$USER_NAME:$USER_GROUP" "$USER_HOME/robotlidar_data"

sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE_NAME"

echo
echo "RobotLidar Web установлен и запущен."
echo "Статус: sudo systemctl status $SERVICE_NAME"
echo "Журнал: journalctl -u $SERVICE_NAME -f"
echo "Панель: http://<IP_RASPBERRY_PI>:8080"
echo "После добавления пользователя в группы dialout/i2c/gpio может потребоваться перезагрузка."
