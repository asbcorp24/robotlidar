#!/usr/bin/env bash
set -u

CONFIG_FILE="${ORANGE_PI_CAMERA_CONFIG:-/etc/robotlidar/orange-pi-zero-camera.json}"
CHECK_SEC="${ROBOTLIDAR_ETH_CHECK_SEC:-5}"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

find_eth() {
  if [ -r "$CONFIG_FILE" ]; then
    iface="$(sed -n 's/.*"interface"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG_FILE" | head -n1)"
    case "$iface" in
      eth*|en*) [ -d "/sys/class/net/$iface" ] && { echo "$iface"; return; } ;;
    esac
  fi
  for p in /sys/class/net/eth* /sys/class/net/en*; do
    [ -d "$p" ] || continue
    basename "$p"
    return
  done
}

has_ipv4() {
  ip -4 -o addr show dev "$1" scope global 2>/dev/null | grep -q ' inet '
}

carrier_up() {
  [ "$(cat "/sys/class/net/$1/carrier" 2>/dev/null || echo 0)" = "1" ]
}

network_mode() {
  if [ -r "$CONFIG_FILE" ]; then
    mode="$(sed -n 's/.*"mode"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG_FILE" | head -n1)"
    [ -n "$mode" ] && { echo "$mode"; return; }
  fi
  echo dhcp
}

ensure_connection() {
  iface="$1"
  mode="$2"

  nmcli device set "$iface" managed yes >/dev/null 2>&1 || true
  ip link set "$iface" up >/dev/null 2>&1 || true

  conn="$(nmcli -g GENERAL.CONNECTION device show "$iface" 2>/dev/null | head -n1)"
  case "$conn" in ""|"--"|"(null)") conn="";; esac

  if [ -z "$conn" ]; then
    conn="$(nmcli -t -f NAME,TYPE connection show 2>/dev/null | awk -F: '$2=="802-3-ethernet" || $2=="ethernet"{print $1; exit}')"
  fi

  if [ -z "$conn" ]; then
    conn="RobotLiDAR-$iface"
    log "Creating Ethernet profile $conn for $iface"
    nmcli connection add type ethernet ifname "$iface" con-name "$conn" >/dev/null 2>&1 || true
  fi

  nmcli connection modify "$conn" connection.autoconnect yes >/dev/null 2>&1 || true

  if [ "$mode" = "dhcp" ]; then
    nmcli connection modify "$conn" ipv4.method auto >/dev/null 2>&1 || true
  fi

  log "No IPv4 on $iface (carrier=UP, mode=$mode). Retrying NetworkManager connection '$conn'"
  nmcli connection up "$conn" ifname "$iface" >/dev/null 2>&1 || \
    nmcli device connect "$iface" >/dev/null 2>&1 || true
}

log "Ethernet watchdog started; retry interval ${CHECK_SEC}s"

while true; do
  iface="$(find_eth)"
  if [ -z "${iface:-}" ]; then
    sleep "$CHECK_SEC"
    continue
  fi

  if has_ipv4 "$iface"; then
    sleep "$CHECK_SEC"
    continue
  fi

  if carrier_up "$iface"; then
    ensure_connection "$iface" "$(network_mode)"
  fi

  sleep "$CHECK_SEC"
done
