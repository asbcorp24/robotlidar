#!/usr/bin/env bash
set -u

WIFI_IF="${ROBOTLIDAR_WIFI_IF:-wlan0}"
AP_CONN="${ROBOTLIDAR_AP_CONNECTION:-RobotLiDAR-Setup}"
AP_SSID="${ROBOTLIDAR_AP_SSID:-RobotLiDAR-Setup}"
AP_ADDR="${ROBOTLIDAR_AP_ADDR:-10.42.0.1/24}"
AP_IP="${AP_ADDR%/*}"
CONFIG_MARKER="/run/robotlidar-wifi-configuring"
CHECK_SEC=3

HOSTAPD_CONF="/run/robotlidar-hostapd.conf"
HOSTAPD_PID="/run/robotlidar-hostapd.pid"
DNSMASQ_CONF="/run/robotlidar-dnsmasq.conf"
DNSMASQ_PID="/run/robotlidar-dnsmasq.pid"

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

pid_alive() {
  local f="$1"
  [ -s "$f" ] || return 1
  kill -0 "$(cat "$f" 2>/dev/null)" 2>/dev/null
}

ap_is_active() {
  pid_alive "$HOSTAPD_PID"
}

disable_client_autoconnect() {
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

write_ap_configs() {
  cat > "$HOSTAPD_CONF" <<EOF
interface=$WIFI_IF
driver=nl80211
ssid=$AP_SSID
hw_mode=g
channel=6
auth_algs=1
ignore_broadcast_ssid=0
wmm_enabled=0
ieee80211n=0
EOF

  cat > "$DNSMASQ_CONF" <<EOF
interface=$WIFI_IF
bind-interfaces
# DHCP only. Disable DNS listener to avoid port 53 conflicts with the
# system resolver/NetworkManager on old Debian images.
port=0
dhcp-range=10.42.0.10,10.42.0.100,255.255.255.0,12h
dhcp-option=3,$AP_IP
# Android 14 requests DHCP option 114 (Captive-Portal). Point it directly
# at the local RobotLiDAR configuration UI so phones treat this as an
# intentional setup network instead of abandoning it for "no Internet".
dhcp-option=114,http://$AP_IP:8088/
log-dhcp
EOF
}

start_ap() {
  disable_client_autoconnect

  if ap_is_active; then
    iw dev "$WIFI_IF" set power_save off >/dev/null 2>&1 || true
    return 0
  fi

  command -v hostapd >/dev/null 2>&1 || { log "hostapd not installed"; return 1; }
  command -v dnsmasq >/dev/null 2>&1 || { log "dnsmasq not installed"; return 1; }

  # Do not use NetworkManager/wpa_supplicant for XR819 AP mode. On the
  # Orange Pi Zero 5.3.5+ image it emits invalid nl80211 attributes and
  # clients can be disconnected shortly after association.
  nm connection down "$AP_CONN" >/dev/null 2>&1 || true
  nm device disconnect "$WIFI_IF" >/dev/null 2>&1 || true
  nm device set "$WIFI_IF" managed no >/dev/null 2>&1 || true

  ip link set "$WIFI_IF" down >/dev/null 2>&1 || true
  iw dev "$WIFI_IF" set type __ap >/dev/null 2>&1 || true
  ip addr flush dev "$WIFI_IF" >/dev/null 2>&1 || true
  ip addr add "$AP_ADDR" dev "$WIFI_IF" >/dev/null 2>&1 || true
  ip link set "$WIFI_IF" up >/dev/null 2>&1 || return 1
  iw dev "$WIFI_IF" set power_save off >/dev/null 2>&1 || true

  write_ap_configs

  rm -f "$HOSTAPD_PID" "$DNSMASQ_PID"
  log "No Ethernet/Wi-Fi client. Starting XR819 hostapd AP '$AP_SSID' on channel 6 at http://$AP_IP:8088/"
  hostapd -B -P "$HOSTAPD_PID" "$HOSTAPD_CONF" >/dev/null 2>&1 || {
    log "hostapd failed to start"
    nm device set "$WIFI_IF" managed yes >/dev/null 2>&1 || true
    return 1
  }

  dnsmasq --conf-file="$DNSMASQ_CONF" --pid-file="$DNSMASQ_PID" >/dev/null 2>&1 || {
    log "dnsmasq failed to start"
    kill "$(cat "$HOSTAPD_PID" 2>/dev/null)" 2>/dev/null || true
    rm -f "$HOSTAPD_PID"
    nm device set "$WIFI_IF" managed yes >/dev/null 2>&1 || true
    return 1
  }
}

stop_ap() {
  local had_ap=0
  if pid_alive "$HOSTAPD_PID"; then
    had_ap=1
    kill "$(cat "$HOSTAPD_PID")" 2>/dev/null || true
  fi
  if pid_alive "$DNSMASQ_PID"; then
    had_ap=1
    kill "$(cat "$DNSMASQ_PID")" 2>/dev/null || true
  fi
  rm -f "$HOSTAPD_PID" "$DNSMASQ_PID" "$HOSTAPD_CONF" "$DNSMASQ_CONF"

  if [ "$had_ap" -eq 1 ]; then
    log "Stopping setup AP '$AP_SSID' and returning $WIFI_IF to NetworkManager"
    ip link set "$WIFI_IF" down >/dev/null 2>&1 || true
    ip addr flush dev "$WIFI_IF" >/dev/null 2>&1 || true
    iw dev "$WIFI_IF" set type managed >/dev/null 2>&1 || true
    nm device set "$WIFI_IF" managed yes >/dev/null 2>&1 || true
    nm radio wifi on >/dev/null 2>&1 || true
    ip link set "$WIFI_IF" up >/dev/null 2>&1 || true
  fi
}

trap 'stop_ap; exit 0' TERM INT

log "Fallback network watchdog started (wifi=$WIFI_IF, AP=$AP_SSID, backend=hostapd)"

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

  # Web UI creates this marker while switching from setup AP to a selected
  # client Wi-Fi network. Stop hostapd first and return wlan0 to NetworkManager.
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
