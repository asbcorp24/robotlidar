#!/usr/bin/env bash
set -euo pipefail
sudo systemctl disable --now robotlidar-web.service 2>/dev/null || true
sudo rm -f /etc/systemd/system/robotlidar-web.service
sudo systemctl daemon-reload
echo "Сервис robotlidar-web удалён. Карты и маршруты сохранены."
