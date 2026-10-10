#!/usr/bin/env bash
set -u

WIFI_IF="${ROBOTLIDAR_WIFI_IF:-wlan0}"
AP_CONN="${ROBOTLIDAR_AP_CONNECTION:-RobotLiDAR-Setup}"
AP_SSID="${ROBOTLIDAR_AP_SSID:-RobotLiDAR-Setup}"
AP_ADDR="${ROBOTLIDAR_AP_ADDR:-10.42.0.1/24}"
CONFIG_MARKER="/run/robotlidar-wifi-configuring"
CHECK_SEC=3

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

nm() {
  nmcli "$@"
}

wifi_exists() {
  [ -d "/sys/class/net/$WIFI_IF" ]
}

ethernet_has_ipv4() {
  local ifc
  for ifc in /sys/class/net/eth* /sys/class/net/en*; do
    [ -e "$ifc" ] || continue
    ifc="$(basename "$ifc")"
    # A configured/static IPv4 address can remain present even with the cable
    # unplugged. Require physical carrier as well, otherwise setup AP would
    # never start on an offline Ethernet interface.
    [ "$(cat "/sys/class/net/$ifc/carrier" 2>/dev/null || echo 0)" = "1" ] || continue
    if ip -4 -o addr show dev "$ifc" scope global 2>/dev/null | grep -q ' inet '; then
      return 0
    fi
  done
  return 1
}

active_wifi_connection() {
  nm -g GENERAL.CONNECTION device show "$WIFI_IF" 2>/dev/null | head -n1
}

wifi_client_connected() {
  local c
  c="$(active_wifi_connection)"
  [ -n "$c" ] && [ "$c" != "--" ] && [ "$c" != "(null)" ] && [ "$c" != "$AP_CONN" ]
}

ap_is_active() {
  [ "$(active_wifi_connection)" = "$AP_CONN" ]
}

disable_client_autoconnect() {
  # While setup AP is active, prevent NetworkManager from stealing wlan0
  # for a previously saved client profile. Client mode is entered only
  # explicitly from the web UI.
  local line name typ
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    typ="${line##*:}"
    name="${line%:*}"
    [ "$name" = "$AP_CONN" ] && continue
    case "$typ" in
      802-11-wireless|wifi)
        nm connection modify "$name" connection.autoconnect no >/dev/null 2>&1 || true
        ;;
    esac
  done < <(nm -t -f NAME,TYPE connection show 2>/dev/null)
}

ensure_ap_profile() {
  if ! nm -t -f NAME connection show 2>/dev/null | grep -Fxq "$AP_CONN"; then
    log "Creating open setup access point profile: $AP_SSID"
    nm connection add type wifi ifname "$WIFI_IF" con-name "$AP_CONN" ssid "$AP_SSID" >/dev/null || return 1
  fi

  nm connection modify "$AP_CONN"     connection.autoconnect no     connection.autoconnect-priority 999     connection.interface-name "$WIFI_IF"     802-11-wireless.mode ap     802-11-wireless.band bg     ipv4.method shared     ipv4.addresses "$AP_ADDR"     ipv6.method ignore >/dev/null || return 1
}

start_ap() {
  # Keep setup AP stable: once fallback mode starts, saved client profiles
  # are not allowed to auto-activate and take wlan0 away from the phone.
  disable_client_autoconnect
  if ap_is_active; then
    # NetworkManager/driver may re-enable power save after activation.
    # Re-assert it on every watchdog pass while setup AP is active.
    iw dev "$WIFI_IF" set power_save off >/dev/null 2>&1 || true
    return 0
  fi
  ensure_ap_profile || return 1
  nm radio wifi on >/dev/null 2>&1 || true
  nm device set "$WIFI_IF" managed yes >/dev/null 2>&1 || true
  ip link set "$WIFI_IF" up >/dev/null 2>&1 || true
  log "No Ethernet/Wi-Fi client. Starting LOCKED OPEN AP '$AP_SSID' at http://10.42.0.1:8088/"
  nm connection up "$AP_CONN" ifname "$WIFI_IF" >/dev/null || return 1

  # XR819 can be unstable as an access point with Wi-Fi power saving enabled.
  # Disable it every time the setup AP is activated.
  iw dev "$WIFI_IF" set power_save off >/dev/null 2>&1 || true
}

stop_ap() {
  if ap_is_active; then
    log "Network connectivity available. Stopping setup AP '$AP_SSID'"
    nm connection down "$AP_CONN" >/dev/null 2>&1 || true
  fi
}

trap 'stop_ap; exit 0' TERM INT

log "Fallback network watchdog started (wifi=$WIFI_IF, AP=$AP_SSID)"

while true; do
  if ! command -v nmcli >/dev/null 2>&1; then
    log "nmcli not found; retrying"
    sleep 10
    continue
  fi

  if ! wifi_exists; then
    log "Wi-Fi interface $WIFI_IF not found; retrying"
    sleep 10
    continue
  fi

  # The web UI creates this marker while it is intentionally switching wlan0
  # from the setup AP to a user-selected client network.
  if [ -e "$CONFIG_MARKER" ]; then
    stop_ap
    sleep "$CHECK_SEC"
    continue
  fi

  if ethernet_has_ipv4 || wifi_client_connected; then
    stop_ap
  else
    start_ap || log "Failed to start setup AP; will retry"
  fi

  sleep "$CHECK_SEC"
done
