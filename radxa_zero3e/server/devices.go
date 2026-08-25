package main

import (
	"database/sql"
	"fmt"
	"net/http"
	"strings"
	"time"
)

type registerRequest struct {
	Name           string `json:"name"`
	IP             string `json:"ip"`
	RTPPort        int    `json:"rtp_port"`
	PTZPort        int    `json:"ptz_port"`
	DeviceType     string `json:"device_type"`
	VideoTransport string `json:"video_transport"`
}

type telemetryRequest struct {
	FPS           *int64 `json:"fps"`
	BitrateBPS    *int64 `json:"bitrate_bps"`
	DroppedFrames *int64 `json:"dropped_frames"`
	UptimeMS      *int64 `json:"uptime_ms"`
	PanCDeg       *int64 `json:"pan_cdeg"`
	TiltCDeg      *int64 `json:"tilt_cdeg"`
	LinkMbps      *int64 `json:"link_mbps"`
}

func (s *server) listDevices(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		methodNotAllowed(w)
		return
	}
	u, ok := s.requireUser(w, r)
	if !ok {
		return
	}

	rows, err := s.db.Query(`SELECT device_id,alias FROM user_devices WHERE user_id=? ORDER BY created_at`, u.ID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	defer rows.Close()

	out := []map[string]any{}
	for rows.Next() {
		var id string
		var alias sql.NullString
		if err := rows.Scan(&id, &alias); err != nil {
			continue
		}
		name := id
		if alias.Valid && alias.String != "" {
			name = alias.String
		}
		s.devicesM.RLock()
		d := s.devices[id]
		s.devicesM.RUnlock()
		if d == nil {
			out = append(out, offlineDeviceJSON(id, name))
			continue
		}
		out = append(out, d.publicJSON(name))
	}
	writeJSON(w, http.StatusOK, map[string]any{"devices": out})
}

func (s *server) deviceAPI(w http.ResponseWriter, r *http.Request) {
	rest := strings.Trim(strings.TrimPrefix(r.URL.Path, "/api/devices/"), "/")
	parts := strings.Split(rest, "/")
	if len(parts) < 2 || parts[0] == "" {
		writeError(w, http.StatusNotFound, "Not found")
		return
	}
	id, action := parts[0], parts[1]

	switch action {
	case "register":
		s.registerDevice(w, r, id)
	case "telemetry":
		s.telemetry(w, r, id)
	case "video-status":
		s.videoStatus(w, r, id)
	case "webrtc":
		s.webrtc(w, r, id)
	case "ptz":
		s.ptz(w, r, id)
	case "center":
		s.center(w, r, id)
	case "request-idr":
		s.requestIDR(w, r, id)
	case "drive":
		s.drive(w, r, id)
	case "drive-stop":
		s.driveStop(w, r, id)
	case "brush":
		s.brush(w, r, id)
	default:
		writeError(w, http.StatusNotFound, "Not found")
	}
}

func (s *server) registerDevice(w http.ResponseWriter, r *http.Request, id string) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}
	var req registerRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	id = strings.TrimSpace(id)
	if id == "" || strings.TrimSpace(req.IP) == "" {
		writeError(w, http.StatusBadRequest, "device_id and ip are required")
		return
	}
	if req.PTZPort <= 0 {
		req.PTZPort = 6000
	}
	if strings.TrimSpace(req.DeviceType) == "" {
		req.DeviceType = "radxa_stereo"
	}
	transport := strings.ToLower(strings.TrimSpace(req.VideoTransport))
	if transport == "" {
		transport = "rtp"
	}
	if transport != "rtp" && transport != "srt" {
		writeError(w, http.StatusBadRequest, "video_transport must be rtp or srt")
		return
	}

	s.devicesM.Lock()
	d := s.devices[id]
	if d == nil {
		port := s.allocateVideoPortLocked()
		if port == 0 {
			s.devicesM.Unlock()
			writeError(w, http.StatusServiceUnavailable, "No RTP ports available")
			return
		}
		stream, err := newRTPStream(id, port)
		if err != nil {
			s.devicesM.Unlock()
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		d = &device{
			ID:         id,
			Name:       req.Name,
			DeviceType: req.DeviceType,
			IP:         req.IP,
			PTZPort:    req.PTZPort,
			RTPPort:    port,
			Transport:  transport,
			stream:     stream,
		}
		s.devices[id] = d
	} else {
		d.Name = req.Name
		d.DeviceType = req.DeviceType
		d.IP = req.IP
		d.PTZPort = req.PTZPort
		d.Transport = transport
	}

	if transport == "srt" && d.srt == nil {
		srtPort := s.allocateSRTPortLocked()
		if srtPort == 0 {
			s.devicesM.Unlock()
			writeError(w, http.StatusServiceUnavailable, "No SRT ports available")
			return
		}
		bridge, err := newSRTBridge(id, srtPort, d.stream)
		if err != nil {
			s.devicesM.Unlock()
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
		d.SRTPort = srtPort
		d.srt = bridge
	} else if transport != "srt" && d.srt != nil {
		d.srt.close()
		d.srt = nil
		d.SRTPort = 0
	}

	d.LastSeen.Store(time.Now().UnixMilli())
	resp := map[string]any{
		"ok":                true,
		"device":            d.runtimeJSON(),
		"video_ingest_port": d.RTPPort,
		"video_transport":   transport,
	}
	if transport == "srt" {
		resp["srt_ingest_port"] = d.SRTPort
		resp["srt_latency_ms"] = 200
	}
	s.devicesM.Unlock()

	writeJSON(w, http.StatusOK, resp)
}

func (s *server) telemetry(w http.ResponseWriter, r *http.Request, id string) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}
	var req telemetryRequest
	if !decodeJSON(w, r, &req) {
		return
	}

	s.devicesM.RLock()
	d := s.devices[id]
	s.devicesM.RUnlock()
	if d == nil {
		writeError(w, http.StatusNotFound, "Device not registered")
		return
	}

	d.LastSeen.Store(time.Now().UnixMilli())
	if req.FPS != nil { d.FPS.Store(*req.FPS) }
	if req.BitrateBPS != nil { d.Bitrate.Store(*req.BitrateBPS) }
	if req.DroppedFrames != nil { d.Dropped.Store(*req.DroppedFrames) }
	if req.UptimeMS != nil { d.UptimeMS.Store(*req.UptimeMS) }
	if req.PanCDeg != nil { d.PanCDeg.Store(*req.PanCDeg) }
	if req.TiltCDeg != nil { d.TiltCDeg.Store(*req.TiltCDeg) }
	if req.LinkMbps != nil { d.LinkMbps.Store(*req.LinkMbps) }
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *server) videoStatus(w http.ResponseWriter, r *http.Request, id string) {
	if r.Method != http.MethodGet { methodNotAllowed(w); return }
	u, ok := s.requireUser(w, r); if !ok { return }
	d, ok := s.ownedDevice(w, u.ID, id); if !ok { return }
	status := d.stream.status()
	status["transport"] = d.Transport
	if d.SRTPort > 0 { status["srt_ingest_port"] = d.SRTPort }
	writeJSON(w, http.StatusOK, status)
}

func (s *server) ownedDevice(w http.ResponseWriter, userID int64, id string) (*device, bool) {
	var n int
	if err := s.db.QueryRow(`SELECT COUNT(*) FROM user_devices WHERE user_id=? AND device_id=?`, userID, id).Scan(&n); err != nil || n == 0 {
		writeError(w, http.StatusForbidden, "This tractor is not linked to your account")
		return nil, false
	}
	s.devicesM.RLock(); d := s.devices[id]; s.devicesM.RUnlock()
	if d == nil {
		writeError(w, http.StatusConflict, "Tractor is linked but currently offline")
		return nil, false
	}
	return d, true
}

func (s *server) allocateVideoPortLocked() int {
	used := make(map[int]bool, len(s.devices))
	for _, d := range s.devices { used[d.RTPPort] = true }
	for p := videoPortBase; p < srtPortBase; p++ { if !used[p] { return p } }
	return 0
}

func (s *server) allocateSRTPortLocked() int {
	used := make(map[int]bool, len(s.devices))
	for _, d := range s.devices { if d.SRTPort > 0 { used[d.SRTPort] = true } }
	for p := srtPortBase; p <= srtPortMax; p++ { if !used[p] { return p } }
	return 0
}

func (d *device) online() bool {
	last := d.LastSeen.Load()
	return last > 0 && time.Since(time.UnixMilli(last)) <= offlineAfter
}

func (d *device) publicJSON(alias string) map[string]any {
	return map[string]any{
		"id": d.ID, "device_id": d.ID, "device_type": d.DeviceType, "name": alias,
		"online": d.online(), "video_online": d.stream.videoOnline(),
		"streamType": "webrtc", "streamUrl": "/api/devices/" + d.ID + "/webrtc",
		"video_transport": d.Transport,
		"pan": float64(d.PanCDeg.Load()) / 100.0, "tilt": float64(d.TiltCDeg.Load()) / 100.0,
		"fps": d.FPS.Load(), "bitrateKbps": d.Bitrate.Load() / 1000,
		"ethernet": linkLabel(d.LinkMbps.Load()), "uptimeSec": d.UptimeMS.Load() / 1000,
		"video_packets": d.stream.Packets.Load(), "video_bytes": d.stream.Bytes.Load(), "viewers": d.stream.Viewers.Load(),
	}
}

func (d *device) runtimeJSON() map[string]any {
	return map[string]any{
		"device_id": d.ID, "device_type": d.DeviceType, "name": d.Name, "ip": d.IP,
		"video_ingest_port": d.RTPPort, "srt_ingest_port": d.SRTPort,
		"video_transport": d.Transport, "ptz_port": d.PTZPort, "online": d.online(),
	}
}

func offlineDeviceJSON(id, name string) map[string]any {
	return map[string]any{
		"id": id, "device_id": id, "device_type": "unknown", "name": name,
		"online": false, "video_online": false,
		"streamType": "webrtc", "streamUrl": "/api/devices/" + id + "/webrtc",
		"pan": 0, "tilt": 0, "fps": 0, "bitrateKbps": 0, "ethernet": "—", "uptimeSec": 0,
	}
}

func linkLabel(v int64) string {
	if v <= 0 { return "—" }
	return fmt.Sprintf("%d Mbit/s", v)
}
