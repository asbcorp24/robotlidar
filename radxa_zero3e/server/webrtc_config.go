package main

import (
	"fmt"
	"os"
	"strconv"
	"strings"

	"github.com/pion/webrtc/v4"
)

const (
	defaultWebRTCUDPMin = 40000
	defaultWebRTCUDPMax = 40100
)

// newWebRTCPeerConnection creates a Pion PeerConnection with a predictable
// UDP port range and optional TURN relay configured from environment variables.
//
// Supported environment variables:
//   WEBRTC_UDP_MIN=40000
//   WEBRTC_UDP_MAX=40100
//   TURN_URL=turn:tele.example.org:3478
//   TURN_USERNAME=robotlidar
//   TURN_PASSWORD=secret
//
// STUN_URL remains configured by media.go for backwards compatibility.
func newWebRTCPeerConnection(cfg webrtc.Configuration) (*webrtc.PeerConnection, error) {
	portMin, err := envUint16("WEBRTC_UDP_MIN", defaultWebRTCUDPMin)
	if err != nil {
		return nil, err
	}
	portMax, err := envUint16("WEBRTC_UDP_MAX", defaultWebRTCUDPMax)
	if err != nil {
		return nil, err
	}
	if portMax < portMin {
		return nil, fmt.Errorf("WEBRTC_UDP_MAX (%d) must be >= WEBRTC_UDP_MIN (%d)", portMax, portMin)
	}

	settingEngine := webrtc.SettingEngine{}
	if err := settingEngine.SetEphemeralUDPPortRange(portMin, portMax); err != nil {
		return nil, fmt.Errorf("configure WebRTC UDP range %d-%d: %w", portMin, portMax, err)
	}

	if turnURLs := splitEnvList(os.Getenv("TURN_URL")); len(turnURLs) > 0 {
		cfg.ICEServers = append(cfg.ICEServers, webrtc.ICEServer{
			URLs:       turnURLs,
			Username:   strings.TrimSpace(os.Getenv("TURN_USERNAME")),
			Credential: os.Getenv("TURN_PASSWORD"),
		})
	}

	api := webrtc.NewAPI(webrtc.WithSettingEngine(settingEngine))
	return api.NewPeerConnection(cfg)
}

func envUint16(key string, def uint16) (uint16, error) {
	raw := strings.TrimSpace(os.Getenv(key))
	if raw == "" {
		return def, nil
	}
	v, err := strconv.ParseUint(raw, 10, 16)
	if err != nil || v == 0 {
		return 0, fmt.Errorf("%s must be an integer in range 1..65535", key)
	}
	return uint16(v), nil
}

func splitEnvList(raw string) []string {
	parts := strings.Split(raw, ",")
	out := make([]string, 0, len(parts))
	for _, p := range parts {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}
