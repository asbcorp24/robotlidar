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

cfg_value() {
  key="$1"
  [ -r "$CONFIG_FILE" ] || return 0
  sed -n 's/.*"'"$key"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$CONFIG_FILE" | head -n1
}

cfg_number() {
  key="$1"
  [ -r "$CONFIG_FILE" ] || return 0
  sed -n 's/.*"'"$key"'"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$CONFIG_FILE" | head -n1
}

ensure_connection() {
  iface="$1"
  mode="$2"
  canonical="RobotLiDAR-$iface"

  nmcli device set "$iface" managed yes >/dev/null 2>&1 || true
  ip link set "$iface" up >/dev/null 2>&1 || true

  active_uuid="$(nmcli -g GENERAL.CON-UUID device show "$iface" 2>/dev/null | head -n1)"
  case "$active_uuid" in ""|"--"|"(null)") active_uuid="";; esac

  conn_uuid="$active_uuid"
  if [ -z "$conn_uuid" ]; then
    conn_uuid="$(nmcli -t -f NAME,UUID,TYPE connection show 2>/dev/null | awk -F: -v n="$canonical" '$1==n && ($3=="802-3-ethernet" || $3=="ethernet"){print $2; exit}')"
  fi

  if [ -z "$conn_uuid" ]; then
    log "Creating Ethernet profile $canonical for $iface"
    nmcli connection add type ethernet ifname "$iface" con-name "$canonical" >/dev/null 2>&1 || true
    conn_uuid="$(nmcli -t -f NAME,UUID,TYPE connection show 2>/dev/null | awk -F: -v n="$canonical" '$1==n && ($3=="802-3-ethernet" || $3=="ethernet"){print $2; exit}')"
  fi

  [ -n "$conn_uuid" ] || return 0

  # Keep exactly one Ethernet profile eligible for automatic reconnect.
  # Old duplicate RobotLiDAR/ifupdown profiles otherwise may take over and
  # unexpectedly switch a statically configured device back to DHCP.
  nmcli -t -f UUID,TYPE connection show 2>/dev/null | while IFS=: read -r uuid typ; do
    case "$typ" in
      802-3-ethernet|ethernet)
        if [ "$uuid" = "$conn_uuid" ]; then
          nmcli connection modify uuid "$uuid" connection.autoconnect yes connection.autoconnect-priority 100 >/dev/null 2>&1 || true
        else
          nmcli connection modify uuid "$uuid" connection.autoconnect no >/dev/null 2>&1 || true
        fi
        ;;
    esac
  done

  if [ "$mode" = "dhcp" ]; then
    nmcli connection modify uuid "$conn_uuid" ipv4.method auto >/dev/null 2>&1 || true
  else
    ipaddr="$(cfg_value ip)"
    prefix="$(cfg_number prefix)"
    gateway="$(cfg_value gateway)"
    dns="$(cfg_value dns)"
    [ -n "$prefix" ] || prefix=24
    [ -n "$dns" ] || dns="8.8.8.8 1.1.1.1"
    if [ -n "$ipaddr" ]; then
      nmcli connection modify uuid "$conn_uuid" \
        ipv4.method manual \
        ipv4.addresses "$ipaddr/$prefix" \
        ipv4.gateway "$gateway" \
        ipv4.dns "$dns" \
        ipv4.ignore-auto-dns yes >/dev/null 2>&1 || true
    fi
  fi

  log "No IPv4 on $iface (carrier=UP, mode=$mode). Retrying NetworkManager connection UUID $conn_uuid"
  nmcli connection up uuid "$conn_uuid" ifname "$iface" >/dev/null 2>&1 || \
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
