package main

import (
	"bytes"
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

type gateway struct {
	cfg       config
	http      *http.Client
	onvif     *onvifClient
	esp       *espBridge
	start     time.Time
	rtpPort   int
	localIP   string
	videoMu   sync.Mutex
	videoCmd  *exec.Cmd
	restartCh chan struct{}
	lastDriveMu sync.Mutex
	lastDrive time.Time
	driveNonzero bool
	brushMu sync.Mutex
	brushSpin int16
	brushLift int16
	lastLift time.Time
	panCDeg int16
	tiltCDeg int16
}

type registerResponse struct { VideoIngestPort int `json:"video_ingest_port"` }

func main() {
	cfgPath := flag.String("config", "config.json", "path to JSON config")
	flag.Parse()
	cfg, err := loadConfig(*cfgPath)
	if err != nil { log.Fatal(err) }

	esp, err := openESPBridge(cfg.ESP32Serial, cfg.ESP32Baud)
	if err != nil {
		log.Printf("ESP32 serial unavailable (%v); continuing in camera-only mode", err)
		esp = &espBridge{}
	}
	defer esp.close()

	g := &gateway{
		cfg: cfg,
		http: &http.Client{Timeout: 4 * time.Second},
		onvif: newONVIF(cfg),
		esp: esp,
		start: time.Now(),
		restartCh: make(chan struct{}, 1),
	}
	g.localIP = g.detectLocalIP()

	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()

	for {
		if err := g.register(); err == nil { break }
		log.Printf("register failed, retry in 2s: %v", err)
		select { case <-ctx.Done(): return; case <-time.After(2 * time.Second): }
	}

	log.Printf("device_id=%s local_ip=%s RTP=%s:%d control=UDP/%d", cfg.DeviceID, g.localIP, cfg.ServerRTPHost, g.rtpPort, cfg.ControlListenPort)
	if g.onvif.enabled() { log.Printf("ONVIF PTZ: %s", cfg.ONVIFDeviceService) } else { log.Printf("ONVIF disabled") }
	if cfg.ESP32Serial != "" { log.Printf("ESP32: %s @ %d", cfg.ESP32Serial, cfg.ESP32Baud) }

	go g.controlLoop(ctx)
	go g.telemetryLoop(ctx)
	go g.watchdogLoop(ctx)
	go g.videoSupervisor(ctx)

	<-ctx.Done()
	_ = g.esp.emergencyStop()
	g.stopVideo()
}

func (g *gateway) detectLocalIP() string {
	host := g.cfg.ServerRTPHost
	if host == "" {
		if u, err := url.Parse(g.cfg.ServerHTTP); err == nil { host = u.Hostname() }
	}
	if host == "" { return "127.0.0.1" }
	c, err := net.Dial("udp", net.JoinHostPort(host, "9"))
	if err != nil { return "127.0.0.1" }
	defer c.Close()
	if a, ok := c.LocalAddr().(*net.UDPAddr); ok { return a.IP.String() }
	return "127.0.0.1"
}

func (g *gateway) register() error {
	payload := map[string]any{
		"name": g.cfg.DeviceName,
		"ip": g.localIP,
		"rtp_port": 5004,
		"ptz_port": g.cfg.ControlListenPort,
		"device_type": "orange_pi_ipcam",
	}
	b, _ := json.Marshal(payload)
	req, err := http.NewRequest(http.MethodPost, g.cfg.ServerHTTP+"/api/devices/"+url.PathEscape(g.cfg.DeviceID)+"/register", bytes.NewReader(b))
	if err != nil { return err }
	req.Header.Set("Content-Type", "application/json")
	resp, err := g.http.Do(req)
	if err != nil { return err }
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 { return fmt.Errorf("HTTP %d", resp.StatusCode) }
	var out registerResponse
	if err = json.NewDecoder(resp.Body).Decode(&out); err != nil { return err }
	if out.VideoIngestPort <= 0 { return fmt.Errorf("server did not return video_ingest_port") }
	g.rtpPort = out.VideoIngestPort
	if g.cfg.ServerRTPHost == "" {
		u, _ := url.Parse(g.cfg.ServerHTTP)
		g.cfg.ServerRTPHost = u.Hostname()
	}
	return nil
}

func (g *gateway) ffmpegArgs() []string {
	target := fmt.Sprintf("rtp://%s:%d?pkt_size=1200", g.cfg.ServerRTPHost, g.rtpPort)
	args := []string{"-hide_banner", "-loglevel", "warning", "-fflags", "nobuffer"}
	if strings.EqualFold(g.cfg.RTSPTransport, "tcp") || strings.EqualFold(g.cfg.RTSPTransport, "udp") {
		args = append(args, "-rtsp_transport", strings.ToLower(g.cfg.RTSPTransport))
	}
	args = append(args,
		"-i", g.cfg.RTSPURL,
		"-map", "0:v:0", "-an",
		"-c:v", "copy",
		"-bsf:v", "dump_extra=freq=keyframe",
		"-f", "rtp", target,
	)
	return args
}

func (g *gateway) startVideo(ctx context.Context) {
	g.videoMu.Lock()
	defer g.videoMu.Unlock()
	if g.videoCmd != nil && g.videoCmd.Process != nil { return }
	cmd := exec.CommandContext(ctx, g.cfg.FFmpeg, g.ffmpegArgs()...)
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		log.Printf("FFmpeg start failed: %v", err)
		return
	}
	g.videoCmd = cmd
	log.Printf("FFmpeg RTSP copy started pid=%d", cmd.Process.Pid)
	go func(c *exec.Cmd) {
		err := c.Wait()
		g.videoMu.Lock()
		if g.videoCmd == c { g.videoCmd = nil }
		g.videoMu.Unlock()
		if ctx.Err() == nil { log.Printf("FFmpeg exited: %v", err) }
	}(cmd)
}

func (g *gateway) stopVideo() {
	g.videoMu.Lock()
	cmd := g.videoCmd
	g.videoCmd = nil
	g.videoMu.Unlock()
	if cmd != nil && cmd.Process != nil { _ = cmd.Process.Signal(syscall.SIGTERM) }
}

func (g *gateway) videoRunning() bool {
	g.videoMu.Lock(); defer g.videoMu.Unlock()
	return g.videoCmd != nil && g.videoCmd.Process != nil
}

func (g *gateway) requestVideoRestart() {
	select { case g.restartCh <- struct{}{}: default: }
}

func (g *gateway) videoSupervisor(ctx context.Context) {
	g.startVideo(ctx)
	t := time.NewTicker(time.Second)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done(): return
		case <-g.restartCh:
			log.Printf("video restart requested")
			g.stopVideo(); time.Sleep(150 * time.Millisecond); g.startVideo(ctx)
		case <-t.C:
			if !g.videoRunning() { g.startVideo(ctx) }
		}
	}
}

func (g *gateway) controlLoop(ctx context.Context) {
	conn, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4zero, Port: g.cfg.ControlListenPort})
	if err != nil { log.Printf("control UDP failed: %v", err); return }
	defer conn.Close()
	_ = conn.SetReadBuffer(256 * 1024)
	buf := make([]byte, 512)
	for {
		_ = conn.SetReadDeadline(time.Now().Add(500 * time.Millisecond))
		n, peer, err := conn.ReadFromUDP(buf)
		if err != nil {
			if ne, ok := err.(net.Error); ok && ne.Timeout() { select { case <-ctx.Done(): return; default: continue } }
			return
		}
		p, err := parseControlPacket(buf[:n])
		if err != nil { continue }
		switch p.Type {
		case controlTypePTZ:
			speed := int16(p.Extra >> 16)
			flags := uint16(p.Extra & 0xffff)
			pan, tilt := p.Value1, p.Value2
			if flags&flagCenter != 0 { pan, tilt = 0, 0 }
			g.panCDeg, g.tiltCDeg = pan, tilt
			if g.onvif.enabled() {
				pn := float64(clamp(pan, -9000, 9000)) / 9000.0
				tn := float64(clamp(tilt, -4500, 4500)) / 4500.0
				sn := float64(speed) / 9000.0
				go func() { if err := g.onvif.absoluteMove(pn, tn, sn); err != nil { log.Printf("ONVIF PTZ: %v", err) } }()
			}
			if flags&flagRequestIDR != 0 { g.requestVideoRestart() }
			log.Printf("PTZ from %s pan=%.2f tilt=%.2f", peer.IP, float64(pan)/100, float64(tilt)/100)
		case controlTypeDrive:
			left, right := clamp(p.Value1, -1000, 1000), clamp(p.Value2, -1000, 1000)
			if err := g.esp.drive(left, right); err != nil { log.Printf("ESP32 DRV: %v", err) }
			g.lastDriveMu.Lock(); g.lastDrive = time.Now(); g.driveNonzero = left != 0 || right != 0; g.lastDriveMu.Unlock()
		case controlTypeBrush:
			spin, lift := clamp(p.Value1, -1000, 1000), clamp(p.Value2, -1000, 1000)
			if err := g.esp.aux(lift, spin); err != nil { log.Printf("ESP32 AUX: %v", err) }
			g.brushMu.Lock(); g.brushSpin, g.brushLift = spin, lift; if lift != 0 { g.lastLift = time.Now() }; g.brushMu.Unlock()
		}
	}
}

func (g *gateway) watchdogLoop(ctx context.Context) {
	t := time.NewTicker(50 * time.Millisecond)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done(): return
		case <-t.C:
			now := time.Now()
			g.lastDriveMu.Lock()
			if g.driveNonzero && !g.lastDrive.IsZero() && now.Sub(g.lastDrive) > time.Duration(g.cfg.DriveWatchdogMS)*time.Millisecond {
				log.Printf("drive watchdog -> STOP")
				_ = g.esp.emergencyStop()
				g.driveNonzero = false
			}
			g.lastDriveMu.Unlock()

			g.brushMu.Lock()
			if g.brushLift != 0 && !g.lastLift.IsZero() && now.Sub(g.lastLift) > time.Duration(g.cfg.LiftWatchdogMS)*time.Millisecond {
				log.Printf("brush lift watchdog -> lift STOP")
				g.brushLift = 0
				_ = g.esp.aux(0, g.brushSpin)
			}
			g.brushMu.Unlock()
		}
	}
}

func (g *gateway) telemetryLoop(ctx context.Context) {
	period := time.Duration(g.cfg.TelemetryPeriodMS) * time.Millisecond
	t := time.NewTicker(period)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done(): return
		case <-t.C: g.sendTelemetry()
		}
	}
}

func (g *gateway) sendTelemetry() {
	fps, bitrate := int64(0), int64(0)
	if g.videoRunning() { fps, bitrate = g.cfg.ReportedFPS, g.cfg.ReportedBitrateBPS }
	payload := map[string]any{
		"fps": fps,
		"bitrate_bps": bitrate,
		"dropped_frames": 0,
		"uptime_ms": time.Since(g.start).Milliseconds(),
		"pan_cdeg": g.panCDeg,
		"tilt_cdeg": g.tiltCDeg,
		"link_mbps": ethernetSpeed(g.cfg.EthernetInterface),
	}
	b, _ := json.Marshal(payload)
	req, err := http.NewRequest(http.MethodPost, g.cfg.ServerHTTP+"/api/devices/"+url.PathEscape(g.cfg.DeviceID)+"/telemetry", bytes.NewReader(b))
	if err != nil { return }
	req.Header.Set("Content-Type", "application/json")
	resp, err := g.http.Do(req)
	if err != nil { log.Printf("telemetry: %v", err); return }
	_ = resp.Body.Close()
	if resp.StatusCode == http.StatusNotFound {
		if err = g.register(); err != nil { log.Printf("re-register: %v", err) }
	}
}

func ethernetSpeed(iface string) int64 {
	b, err := os.ReadFile("/sys/class/net/"+iface+"/speed")
	if err != nil { return 0 }
	v, _ := strconv.ParseInt(strings.TrimSpace(string(b)), 10, 64)
	return v
}
