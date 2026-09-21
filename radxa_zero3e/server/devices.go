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
	ActiveCamera  *int64 `json:"active_camera"`
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
	case "control-ws":
		s.controlWebSocket(w, r, id)
	case "video-status":
		s.videoStatus(w, r, id)
	case "video-demand":
		s.videoDemand(w, r, id)
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
	case "camera":
		s.cameraSelect(w, r, id)
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
		d.ActiveCamera.Store(1)
		s.devices[id] = d
	} else {
		if d.ActiveCamera.Load() == 0 {
			d.ActiveCamera.Store(1)
		}
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
		"control_ws_path":   "/api/devices/" + id + "/control-ws",
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
	if req.ActiveCamera != nil && (*req.ActiveCamera == 1 || *req.ActiveCamera == 2) { d.ActiveCamera.Store(*req.ActiveCamera) }
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *server) videoStatus(w http.ResponseWriter, r *http.Request, id string) {
	if r.Method != http.MethodGet { methodNotAllowed(w); return }
	u, ok := s.requireUser(w, r); if !ok { return }
	d, ok := s.ownedDevice(w, u.ID, id); if !ok { return }
	status := d.stream.status()
	status["transport"] = d.Transport
	status["control_transport"] = controlTransportLabel(d)
	if d.SRTPort > 0 { status["srt_ingest_port"] = d.SRTPort }
	writeJSON(w, http.StatusOK, status)
}

type videoDemandRequest struct {
	SessionID string `json:"session_id"`
	Active    bool   `json:"active"`
}

func (s *server) videoDemand(w http.ResponseWriter, r *http.Request, id string) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}
	u, ok := s.requireUser(w, r)
	if !ok { return }
	d, ok := s.ownedDevice(w, u.ID, id)
	if !ok { return }

	var req videoDemandRequest
	if !decodeJSON(w, r, &req) { return }
	req.SessionID = strings.TrimSpace(req.SessionID)
	if req.SessionID == "" || len(req.SessionID) > 128 {
		writeError(w, http.StatusBadRequest, "session_id is required")
		return
	}

	now := time.Now()
	s.videoDemandM.Lock()
	sessions := s.videoDemand[id]
	if sessions == nil {
		sessions = make(map[string]time.Time)
		s.videoDemand[id] = sessions
	}
	wasActive := len(sessions) > 0
	if req.Active {
		sessions[req.SessionID] = now
	} else {
		delete(sessions, req.SessionID)
	}
	isActive := len(sessions) > 0
	viewers := len(sessions)
	if !isActive {
		delete(s.videoDemand, id)
	}
	s.videoDemandM.Unlock()

	if wasActive != isActive {
		value := int16(0)
		if isActive { value = 1 }
		if err := s.sendControl(d, controlTypeStream, value, 0); err != nil {
			writeError(w, http.StatusInternalServerError, err.Error())
			return
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "active": isActive, "viewers": viewers})
}

func (s *server) videoDemandJanitor() {
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	for range ticker.C {
		cutoff := time.Now().Add(-15 * time.Second)
		stopIDs := []string{}
		s.videoDemandM.Lock()
		for id, sessions := range s.videoDemand {
			for sid, seen := range sessions {
				if seen.Before(cutoff) {
					delete(sessions, sid)
				}
			}
			if len(sessions) == 0 {
				delete(s.videoDemand, id)
				stopIDs = append(stopIDs, id)
			}
		}
		s.videoDemandM.Unlock()

		for _, id := range stopIDs {
			s.videoDemandM.Lock()
			_, activeAgain := s.videoDemand[id]
			s.videoDemandM.Unlock()
			if activeAgain {
				continue
			}
			s.devicesM.RLock()
			d := s.devices[id]
			s.devicesM.RUnlock()
			if d != nil {
				_ = s.sendControl(d, controlTypeStream, 0, 0)
			}
		}
	}
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

func controlTransportLabel(d *device) string {
	if d.websocketControlConnected() {
		return "websocket"
	}
	return "udp-fallback"
}

func (d *device) publicJSON(alias string) map[string]any {
	return map[string]any{
		"id": d.ID, "device_id": d.ID, "device_type": d.DeviceType, "name": alias,
		"online": d.online(), "video_online": d.stream.videoOnline(),
		"streamType": "webrtc", "streamUrl": "/api/devices/" + d.ID + "/webrtc",
		"video_transport": d.Transport, "control_transport": controlTransportLabel(d),
		"pan": float64(d.PanCDeg.Load()) / 100.0, "tilt": float64(d.TiltCDeg.Load()) / 100.0,
		"fps": d.FPS.Load(), "bitrateKbps": d.Bitrate.Load() / 1000,
		"ethernet": linkLabel(d.LinkMbps.Load()), "uptimeSec": d.UptimeMS.Load() / 1000,
		"active_camera": maxInt64(d.ActiveCamera.Load(), 1),
		"video_packets": d.stream.Packets.Load(), "video_bytes": d.stream.Bytes.Load(), "viewers": d.stream.Viewers.Load(),
	}
}

func (d *device) runtimeJSON() map[string]any {
	return map[string]any{
		"device_id": d.ID, "device_type": d.DeviceType, "name": d.Name, "ip": d.IP,
		"video_ingest_port": d.RTPPort, "srt_ingest_port": d.SRTPort,
		"video_transport": d.Transport, "control_transport": controlTransportLabel(d),
		"ptz_port": d.PTZPort, "online": d.online(), "active_camera": maxInt64(d.ActiveCamera.Load(), 1),
	}
}

func offlineDeviceJSON(id, name string) map[string]any {
	return map[string]any{
		"id": id, "device_id": id, "device_type": "unknown", "name": name,
		"online": false, "video_online": false,
		"streamType": "webrtc", "streamUrl": "/api/devices/" + id + "/webrtc",
		"control_transport": "offline",
		"pan": 0, "tilt": 0, "fps": 0, "bitrateKbps": 0, "ethernet": "—", "uptimeSec": 0, "active_camera": 1,
	}
}

func linkLabel(v int64) string {
	if v <= 0 { return "—" }
	return fmt.Sprintf("%d Mbit/s", v)
}


func maxInt64(v, fallback int64) int64 {
	if v <= 0 {
		return fallback
	}
	return v
}
