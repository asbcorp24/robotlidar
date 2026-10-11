package main

import (
	"encoding/binary"
	"io"
	"fmt"
	"net"
	"net/http"
	"strings"
)

const (
	controlTypeDrive  = 2
	controlTypeBrush  = 3
	controlTypeCamera = 4
	controlTypeStream = 5
)

type driveRequest struct {
	Left  int16 `json:"left"`
	Right int16 `json:"right"`
}

type brushRequest struct {
	Spin int16 `json:"spin"`
	Lift int16 `json:"lift"`
}

type cameraRequest struct {
	Camera int `json:"camera"`
}

const (
	talkAudioVersion   = 1
	talkAudioFlagStart = 1
	talkAudioFlagStop  = 2
	talkAudioRate      = 8000
	talkAudioMaxBytes  = 32768
	talkClipMaxBytes   = 4 << 20
	talkClipChunkBytes = 28 * 1024
	talkClipVersion    = 1
	talkClipFlagStart  = 1
	talkClipFlagEnd    = 2
)

func buildTalkAudioPacket(flags byte, pcm []byte) []byte {
	packet := make([]byte, 8+len(pcm))
	copy(packet[0:4], []byte("RLAU"))
	packet[4] = talkAudioVersion
	packet[5] = flags
	binary.BigEndian.PutUint16(packet[6:8], talkAudioRate)
	copy(packet[8:], pcm)
	return packet
}

func buildTalkClipPacket(flags, mime byte, total int, payload []byte) []byte {
	packet := make([]byte, 12+len(payload))
	copy(packet[0:4], []byte("RLCL"))
	packet[4] = talkClipVersion
	packet[5] = flags
	packet[6] = mime
	packet[7] = 0
	binary.BigEndian.PutUint32(packet[8:12], uint32(total))
	copy(packet[12:], payload)
	return packet
}

func talkClipMimeCode(contentType string) byte {
	contentType = strings.ToLower(strings.TrimSpace(strings.Split(contentType, ";")[0]))
	switch contentType {
	case "audio/ogg", "application/ogg":
		return 2
	case "audio/wav", "audio/x-wav":
		return 3
	case "audio/mp4", "audio/m4a":
		return 4
	default:
		return 1 // WebM/Opus is the normal Chrome/Edge path.
	}
}

func cameraOnlyDevice(d *device) bool {
	if d == nil {
		return false
	}
	t := strings.ToLower(strings.TrimSpace(d.DeviceType))
	return strings.HasPrefix(t, "orange_pi_zero_camera") ||
		t == "orange_pi_ipcam" ||
		t == "raspberry_robotlidar" ||
		strings.HasPrefix(t, "raspberry_pi_zero") ||
		t == "rpz" ||
		strings.HasPrefix(t, "rpz_")
}

func rejectTractorControlForCameraOnly(w http.ResponseWriter, d *device) bool {
	if !cameraOnlyDevice(d) {
		return false
	}
	writeError(w, http.StatusForbidden, "Tractor control is unavailable for this camera-only device")
	return true
}

func (s *server) cameraSelect(w http.ResponseWriter, r *http.Request, id string) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}
	u, ok := s.requireUser(w, r)
	if !ok {
		return
	}
	d, ok := s.ownedDevice(w, u.ID, id)
	if !ok {
		return
	}
	var req cameraRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.Camera != 1 && req.Camera != 2 {
		writeError(w, http.StatusBadRequest, "camera must be 1 or 2")
		return
	}
	if err := s.sendControl(d, controlTypeCamera, int16(req.Camera), 0); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	d.ActiveCamera.Store(int64(req.Camera))
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "camera": req.Camera})
}

func (s *server) talkback(w http.ResponseWriter, r *http.Request, id string) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}
	u, ok := s.requireUser(w, r)
	if !ok {
		return
	}
	d, ok := s.ownedDevice(w, u.ID, id)
	if !ok {
		return
	}
	if !d.Talkback {
		writeError(w, http.StatusConflict, "Talkback is not enabled on this camera")
		return
	}
	if !d.websocketControlConnected() {
		writeError(w, http.StatusServiceUnavailable, "Camera control WebSocket is offline")
		return
	}

	action := strings.ToLower(strings.TrimSpace(r.URL.Query().Get("action")))
	switch action {
	case "clip":
		body, err := io.ReadAll(io.LimitReader(r.Body, talkClipMaxBytes+1))
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		if len(body) == 0 || len(body) > talkClipMaxBytes {
			writeError(w, http.StatusBadRequest, "Audio clip must be non-empty and <= 4 MiB")
			return
		}
		mime := talkClipMimeCode(r.Header.Get("Content-Type"))
		for off := 0; off < len(body); off += talkClipChunkBytes {
			end := off + talkClipChunkBytes
			if end > len(body) {
				end = len(body)
			}
			flags := byte(0)
			if off == 0 {
				flags |= talkClipFlagStart
			}
			if end == len(body) {
				flags |= talkClipFlagEnd
			}
			if err := d.sendControlWebSocket(buildTalkClipPacket(flags, mime, len(body), body[off:end])); err != nil {
				writeError(w, http.StatusBadGateway, err.Error())
				return
			}
		}
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "state": "queued", "bytes": len(body)})
		return
	case "start":
		if err := d.sendControlWebSocket(buildTalkAudioPacket(talkAudioFlagStart, nil)); err != nil {
			writeError(w, http.StatusBadGateway, err.Error())
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "state": "talking"})
		return
	case "stop":
		if err := d.sendControlWebSocket(buildTalkAudioPacket(talkAudioFlagStop, nil)); err != nil {
			writeError(w, http.StatusBadGateway, err.Error())
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "state": "stopped"})
		return
	case "data", "":
		body, err := io.ReadAll(io.LimitReader(r.Body, talkAudioMaxBytes+1))
		if err != nil {
			writeError(w, http.StatusBadRequest, err.Error())
			return
		}
		if len(body) == 0 || len(body) > talkAudioMaxBytes || len(body)%2 != 0 {
			writeError(w, http.StatusBadRequest, "PCM chunk must be non-empty S16LE and <= 32768 bytes")
			return
		}
		if err := d.sendControlWebSocket(buildTalkAudioPacket(0, body)); err != nil {
			writeError(w, http.StatusBadGateway, err.Error())
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"ok": true, "bytes": len(body)})
		return
	default:
		writeError(w, http.StatusBadRequest, "action must be clip, start, data or stop")
		return
	}
}

func (s *server) drive(w http.ResponseWriter, r *http.Request, id string) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}
	u, ok := s.requireUser(w, r)
	if !ok {
		return
	}
	d, ok := s.ownedDevice(w, u.ID, id)
	if !ok {
		return
	}
	if rejectTractorControlForCameraOnly(w, d) {
		return
	}

	var req driveRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.Left < -1000 || req.Left > 1000 || req.Right < -1000 || req.Right > 1000 {
		writeError(w, http.StatusBadRequest, "left/right must be in range -1000..1000")
		return
	}
	if err := s.sendControl(d, controlTypeDrive, req.Left, req.Right); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "left": req.Left, "right": req.Right})
}

func (s *server) driveStop(w http.ResponseWriter, r *http.Request, id string) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}
	u, ok := s.requireUser(w, r)
	if !ok {
		return
	}
	d, ok := s.ownedDevice(w, u.ID, id)
	if !ok {
		return
	}
	if rejectTractorControlForCameraOnly(w, d) {
		return
	}
	if err := s.sendControl(d, controlTypeDrive, 0, 0); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *server) brush(w http.ResponseWriter, r *http.Request, id string) {
	if r.Method != http.MethodPost {
		methodNotAllowed(w)
		return
	}
	u, ok := s.requireUser(w, r)
	if !ok {
		return
	}
	d, ok := s.ownedDevice(w, u.ID, id)
	if !ok {
		return
	}
	if rejectTractorControlForCameraOnly(w, d) {
		return
	}

	var req brushRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.Spin < -1000 || req.Spin > 1000 || req.Lift < -1000 || req.Lift > 1000 {
		writeError(w, http.StatusBadRequest, "spin/lift must be in range -1000..1000")
		return
	}
	if err := s.sendControl(d, controlTypeBrush, req.Spin, req.Lift); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "spin": req.Spin, "lift": req.Lift})
}

// Control packet format stays identical for UDP and WebSocket:
// magic:u16, version:u8, type:u8, seq:u32, value1:i16, value2:i16, speed:u16, flags:u16.
func (s *server) buildControlPacket(packetType byte, value1, value2 int16, speed, flags uint16) []byte {
	seq := s.seq.Add(1)
	packet := make([]byte, 16)
	binary.BigEndian.PutUint16(packet[0:2], ptzMagic)
	packet[2] = ptzVersion
	packet[3] = packetType
	binary.BigEndian.PutUint32(packet[4:8], seq)
	binary.BigEndian.PutUint16(packet[8:10], uint16(value1))
	binary.BigEndian.PutUint16(packet[10:12], uint16(value2))
	binary.BigEndian.PutUint16(packet[12:14], speed)
	binary.BigEndian.PutUint16(packet[14:16], flags)
	return packet
}

func (s *server) sendDevicePacket(d *device, packet []byte) error {
	if d.websocketControlConnected() {
		if err := d.sendControlWebSocket(packet); err == nil {
			return nil
		}
	}

	ip := net.ParseIP(d.IP)
	if ip == nil {
		return fmt.Errorf("control websocket unavailable and invalid device ip: %s", d.IP)
	}
	_, err := s.ptzConn.WriteToUDP(packet, &net.UDPAddr{IP: ip, Port: d.PTZPort})
	if err != nil {
		return fmt.Errorf("control websocket unavailable; UDP fallback failed: %w", err)
	}
	return nil
}

func (s *server) sendControl(d *device, packetType byte, value1, value2 int16) error {
	return s.sendDevicePacket(d, s.buildControlPacket(packetType, value1, value2, 0, 0))
}
