package main

import (
	"encoding/binary"
	"errors"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/pion/rtp"
	"github.com/pion/webrtc/v4"
)

type rtpStream struct {
	DeviceID string
	Port     int
	Track    *webrtc.TrackLocalStaticRTP
	Conn     *net.UDPConn
	Packets  atomic.Uint64
	Bytes    atomic.Uint64
	LastMS   atomic.Int64
	Viewers  atomic.Int64
}

type webrtcOffer struct {
	SDP  string `json:"sdp"`
	Type string `json:"type"`
}

type ptzRequest struct {
	PanCDeg      int16  `json:"pan_cdeg"`
	TiltCDeg     int16  `json:"tilt_cdeg"`
	SpeedCDegSec uint16 `json:"speed_cdeg_s"`
	RequestIDR   bool   `json:"request_idr"`
}

func newRTPStream(deviceID string, port int) (*rtpStream, error) {
	conn, err := net.ListenUDP("udp", &net.UDPAddr{IP: net.IPv4zero, Port: port})
	if err != nil {
		return nil, err
	}
	_ = conn.SetReadBuffer(4 * 1024 * 1024)

	track, err := webrtc.NewTrackLocalStaticRTP(
		webrtc.RTPCodecCapability{
			MimeType:    webrtc.MimeTypeH264,
			ClockRate:   90000,
			SDPFmtpLine: "level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=42e01f",
		},
		"video",
		deviceID,
	)
	if err != nil {
		_ = conn.Close()
		return nil, err
	}

	st := &rtpStream{DeviceID: deviceID, Port: port, Track: track, Conn: conn}
	go st.readLoop()
	log.Printf("RTP ingest %s: udp://0.0.0.0:%d", deviceID, port)
	return st, nil
}

func (st *rtpStream) readLoop() {
	buf := make([]byte, 65535)
	for {
		n, _, err := st.Conn.ReadFromUDP(buf)
		if err != nil {
			if !errors.Is(err, net.ErrClosed) {
				log.Printf("RTP %s read error: %v", st.DeviceID, err)
			}
			return
		}

		var packet rtp.Packet
		if err := packet.Unmarshal(buf[:n]); err != nil {
			continue
		}
		if err := st.writeRTP(&packet, n); err != nil {
			log.Printf("WebRTC relay %s write error: %v", st.DeviceID, err)
		}
	}
}

// writeRTP is the common in-memory entry point into the Pion track. Legacy
// devices call it through the UDP RTP listener; pure-Go SRT ingest calls it
// directly after MPEG-TS/PES parsing and H.264 RTP packetization.
func (st *rtpStream) writeRTP(packet *rtp.Packet, wireBytes int) error {
	st.Packets.Add(1)
	if wireBytes <= 0 {
		wireBytes = packet.MarshalSize()
	}
	st.Bytes.Add(uint64(wireBytes))
	st.LastMS.Store(time.Now().UnixMilli())
	return st.Track.WriteRTP(packet)
}

func (st *rtpStream) videoOnline() bool {
	last := st.LastMS.Load()
	return last > 0 && time.Since(time.UnixMilli(last)) <= videoAfter
}

func (st *rtpStream) status() map[string]any {
	return map[string]any{
		"device_id":         st.DeviceID,
		"video_ingest_port": st.Port,
		"video_online":      st.videoOnline(),
		"video_packets":     st.Packets.Load(),
		"video_bytes":       st.Bytes.Load(),
		"video_last_seen":   st.LastMS.Load(),
		"viewers":           st.Viewers.Load(),
		"passthrough":       true,
	}
}

func (s *server) webrtc(w http.ResponseWriter, r *http.Request, id string) {
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
	if !d.stream.videoOnline() {
		writeError(w, http.StatusConflict, "Video stream is offline")
		return
	}

	var offer webrtcOffer
	if !decodeJSON(w, r, &offer) {
		return
	}
	if offer.SDP == "" {
		writeError(w, http.StatusBadRequest, "SDP offer required")
		return
	}
	answer, err := d.stream.answer(offer)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, answer)
}

func (st *rtpStream) answer(offer webrtcOffer) (map[string]any, error) {
	cfg := webrtc.Configuration{}
	if stun := strings.TrimSpace(os.Getenv("STUN_URL")); stun != "" {
		cfg.ICEServers = []webrtc.ICEServer{{URLs: []string{stun}}}
	}

	pc, err := newWebRTCPeerConnection(cfg)
	if err != nil {
		return nil, err
	}
	sender, err := pc.AddTrack(st.Track)
	if err != nil {
		_ = pc.Close()
		return nil, err
	}

	st.Viewers.Add(1)
	var closeOnce sync.Once
	closePeer := func() {
		closeOnce.Do(func() {
			st.Viewers.Add(-1)
			_ = pc.Close()
		})
	}

	go func() {
		for {
			if _, _, err := sender.ReadRTCP(); err != nil {
				return
			}
		}
	}()

	pc.OnConnectionStateChange(func(state webrtc.PeerConnectionState) {
		if state == webrtc.PeerConnectionStateFailed ||
			state == webrtc.PeerConnectionStateClosed ||
			state == webrtc.PeerConnectionStateDisconnected {
			closePeer()
		}
	})

	if err = pc.SetRemoteDescription(webrtc.SessionDescription{Type: webrtc.SDPTypeOffer, SDP: offer.SDP}); err != nil {
		closePeer()
		return nil, err
	}
	answer, err := pc.CreateAnswer(nil)
	if err != nil {
		closePeer()
		return nil, err
	}
	gatherComplete := webrtc.GatheringCompletePromise(pc)
	if err = pc.SetLocalDescription(answer); err != nil {
		closePeer()
		return nil, err
	}
	<-gatherComplete

	local := pc.LocalDescription()
	if local == nil {
		closePeer()
		return nil, errors.New("missing local description")
	}
	return map[string]any{"sdp": local.SDP, "type": local.Type.String()}, nil
}

func (s *server) ptz(w http.ResponseWriter, r *http.Request, id string) {
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

	var req ptzRequest
	if !decodeJSON(w, r, &req) {
		return
	}
	if req.SpeedCDegSec == 0 {
		req.SpeedCDegSec = 6000
	}
	flags := uint16(0)
	if req.RequestIDR {
		flags |= flagRequestIDR
	}
	if err := s.sendPTZ(d, req.PanCDeg, req.TiltCDeg, req.SpeedCDegSec, flags); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *server) center(w http.ResponseWriter, r *http.Request, id string) {
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
	if err := s.sendPTZ(d, 0, 0, 6000, flagCenter); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *server) requestIDR(w http.ResponseWriter, r *http.Request, id string) {
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
	if err := s.sendPTZ(d, 0, 0, 6000, flagRequestIDR); err != nil {
		writeError(w, http.StatusInternalServerError, err.Error())
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *server) sendPTZ(d *device, pan, tilt int16, speed, flags uint16) error {
	seq := s.seq.Add(1)
	packet := make([]byte, 16)
	binary.BigEndian.PutUint16(packet[0:2], ptzMagic)
	packet[2] = ptzVersion
	packet[3] = ptzType
	binary.BigEndian.PutUint32(packet[4:8], seq)
	binary.BigEndian.PutUint16(packet[8:10], uint16(pan))
	binary.BigEndian.PutUint16(packet[10:12], uint16(tilt))
	binary.BigEndian.PutUint16(packet[12:14], speed)
	binary.BigEndian.PutUint16(packet[14:16], flags)

	ip := net.ParseIP(d.IP)
	if ip == nil {
		return fmt.Errorf("invalid device ip: %s", d.IP)
	}
	_, err := s.ptzConn.WriteToUDP(packet, &net.UDPAddr{IP: ip, Port: d.PTZPort})
	return err
}
