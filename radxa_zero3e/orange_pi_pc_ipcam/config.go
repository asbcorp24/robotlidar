package main

import (
	"encoding/json"
	"fmt"
	"os"
	"strings"
)

type config struct {
	DeviceID            string `json:"device_id"`
	DeviceName          string `json:"device_name"`
	ServerHTTP          string `json:"server_http"`
	ServerRTPHost       string `json:"server_rtp_host"`
	ControlListenPort   int    `json:"control_listen_port"`
	EthernetInterface   string `json:"ethernet_interface"`
	RTSPURL             string `json:"rtsp_url"`
	FFmpeg              string `json:"ffmpeg"`
	RTSPTransport       string `json:"rtsp_transport"`
	ReportedFPS         int64  `json:"reported_fps"`
	ReportedBitrateBPS  int64  `json:"reported_bitrate_bps"`
	ONVIFDeviceService  string `json:"onvif_device_service"`
	ONVIFUsername       string `json:"onvif_username"`
	ONVIFPassword       string `json:"onvif_password"`
	ONVIFProfileToken   string `json:"onvif_profile_token"`
	ONVIFPTZURL         string `json:"onvif_ptz_url"`
	ONVIFTimeoutMS      int    `json:"onvif_timeout_ms"`
	ESP32Serial         string `json:"esp32_serial"`
	ESP32Baud           int    `json:"esp32_baud"`
	DriveWatchdogMS     int    `json:"drive_watchdog_ms"`
	LiftWatchdogMS      int    `json:"lift_watchdog_ms"`
	TelemetryPeriodMS   int    `json:"telemetry_period_ms"`
}

func loadConfig(path string) (config, error) {
	var c config
	b, err := os.ReadFile(path)
	if err != nil {
		return c, err
	}
	if err = json.Unmarshal(b, &c); err != nil {
		return c, err
	}
	if c.DeviceName == "" { c.DeviceName = "Orange Pi IP camera gateway" }
	if c.ControlListenPort == 0 { c.ControlListenPort = 6000 }
	if c.EthernetInterface == "" { c.EthernetInterface = "eth0" }
	if c.FFmpeg == "" { c.FFmpeg = "ffmpeg" }
	if c.RTSPTransport == "" { c.RTSPTransport = "tcp" }
	if c.ONVIFTimeoutMS <= 0 { c.ONVIFTimeoutMS = 1500 }
	if c.ESP32Baud <= 0 { c.ESP32Baud = 115200 }
	if c.DriveWatchdogMS <= 0 { c.DriveWatchdogMS = 450 }
	if c.LiftWatchdogMS <= 0 { c.LiftWatchdogMS = 450 }
	if c.TelemetryPeriodMS <= 0 { c.TelemetryPeriodMS = 1000 }
	c.ServerHTTP = strings.TrimRight(c.ServerHTTP, "/")
	if c.DeviceID == "" || c.ServerHTTP == "" || c.RTSPURL == "" {
		return c, fmt.Errorf("device_id, server_http and rtsp_url are required")
	}
	return c, nil
}
