package main

import (
	"encoding/binary"
	"fmt"
	"net"
	"net/http"
)

const (
	controlTypeDrive = 2
	controlTypeBrush = 3
)

type driveRequest struct {
	Left  int16 `json:"left"`
	Right int16 `json:"right"`
}

type brushRequest struct {
	Spin int16 `json:"spin"`
	Lift int16 `json:"lift"`
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
