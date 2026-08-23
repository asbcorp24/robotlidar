package main

import (
	"encoding/json"
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

type stream struct {
	DeviceID string
	Port     int
	Track    *webrtc.TrackLocalStaticRTP
	Conn     *net.UDPConn
	Packets  atomic.Uint64
	Bytes    atomic.Uint64
	LastMS   atomic.Int64
	Viewers  atomic.Int64
}

type relay struct {
	mu      sync.RWMutex
	streams map[string]*stream
}

type streamRequest struct {
	RTPPort int `json:"rtp_port"`
}

type offerRequest struct {
	SDP  string `json:"sdp"`
	Type string `json:"type"`
}

type answerResponse struct {
	SDP  string `json:"sdp"`
	Type string `json:"type"`
}

type statusResponse struct {
	DeviceID   string `json:"device_id"`
	RTPPort    int    `json:"rtp_port"`
	Packets    uint64 `json:"packets"`
	Bytes      uint64 `json:"bytes"`
	LastSeenMS int64  `json:"last_seen_ms"`
	VideoOnline bool  `json:"video_online"`
	Viewers    int64  `json:"viewers"`
}

func newRelay() *relay {
	return &relay{streams: map[string]*stream{}}
}

func (r *relay) ensureStream(deviceID string, port int) (*stream, error) {
	r.mu.Lock()
	defer r.mu.Unlock()

	if s, ok := r.streams[deviceID]; ok {
		if s.Port != port {
			return nil, fmt.Errorf("stream %s already uses RTP port %d", deviceID, s.Port)
		}
		return s, nil
	}

	addr := &net.UDPAddr{IP: net.IPv4zero, Port: port}
	conn, err := net.ListenUDP("udp", addr)
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

	s := &stream{DeviceID: deviceID, Port: port, Track: track, Conn: conn}
	r.streams[deviceID] = s
	go s.readLoop()
	log.Printf("RTP ingest %s: udp://0.0.0.0:%d", deviceID, port)
	return s, nil
}

func (s *stream) readLoop() {
	buf := make([]byte, 65535)
	for {
		n, _, err := s.Conn.ReadFromUDP(buf)
		if err != nil {
			if !errors.Is(err, net.ErrClosed) {
				log.Printf("RTP %s read error: %v", s.DeviceID, err)
			}
			return
		}

		var packet rtp.Packet
		if err = packet.Unmarshal(buf[:n]); err != nil {
			continue
		}
		s.Packets.Add(1)
		s.Bytes.Add(uint64(n))
		s.LastMS.Store(time.Now().UnixMilli())

		// TrackLocalStaticRTP keeps the encoded H.264 payload unchanged and only
		// rewrites WebRTC transport-specific SSRC/payload type for each subscriber.
		if err = s.Track.WriteRTP(&packet); err != nil {
			log.Printf("WebRTC relay %s write error: %v", s.DeviceID, err)
		}
	}
}

func (s *stream) status() statusResponse {
	last := s.LastMS.Load()
	online := last > 0 && time.Now().UnixMilli()-last <= 3000
	return statusResponse{
		DeviceID: s.DeviceID,
		RTPPort: s.Port,
		Packets: s.Packets.Load(),
		Bytes: s.Bytes.Load(),
		LastSeenMS: last,
		VideoOnline: online,
		Viewers: s.Viewers.Load(),
	}
}

func (r *relay) get(deviceID string) (*stream, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	s, ok := r.streams[deviceID]
	return s, ok
}

func (r *relay) allStatus() map[string]statusResponse {
	r.mu.RLock()
	defer r.mu.RUnlock()
	out := make(map[string]statusResponse, len(r.streams))
	for id, s := range r.streams {
		out[id] = s.status()
	}
	return out
}

func peerConfig() webrtc.Configuration {
	cfg := webrtc.Configuration{}
	if stun := strings.TrimSpace(os.Getenv("STUN_URL")); stun != "" {
		cfg.ICEServers = []webrtc.ICEServer{{URLs: []string{stun}}}
	}
	return cfg
}

func (s *stream) answer(offer offerRequest) (answerResponse, error) {
	pc, err := webrtc.NewPeerConnection(peerConfig())
	if err != nil {
		return answerResponse{}, err
	}

	sender, err := pc.AddTrack(s.Track)
	if err != nil {
		_ = pc.Close()
		return answerResponse{}, err
	}

	s.Viewers.Add(1)
	var closeOnce sync.Once
	closePeer := func() {
		closeOnce.Do(func() {
			s.Viewers.Add(-1)
			_ = pc.Close()
		})
	}

	go func() {
		for {
			packets, _, readErr := sender.ReadRTCP()
			if readErr != nil {
				return
			}
			// NACK/PLI/FIR are consumed by Pion's interceptors. The upstream Radxa
			// produces an IDR every short GOP, so no decode/re-encode path is needed.
			_ = packets
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
		return answerResponse{}, err
	}

	answer, err := pc.CreateAnswer(nil)
	if err != nil {
		closePeer()
		return answerResponse{}, err
	}
	gatherComplete := webrtc.GatheringCompletePromise(pc)
	if err = pc.SetLocalDescription(answer); err != nil {
		closePeer()
		return answerResponse{}, err
	}
	<-gatherComplete

	local := pc.LocalDescription()
	if local == nil {
		closePeer()
		return answerResponse{}, errors.New("missing local description")
	}
	return answerResponse{SDP: local.SDP, Type: local.Type.String()}, nil
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func main() {
	r := newRelay()
	mux := http.NewServeMux()

	mux.HandleFunc("/health", func(w http.ResponseWriter, req *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
	})

	mux.HandleFunc("/api/streams", func(w http.ResponseWriter, req *http.Request) {
		if req.Method != http.MethodGet {
			writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"streams": r.allStatus()})
	})

	mux.HandleFunc("/api/streams/", func(w http.ResponseWriter, req *http.Request) {
		path := strings.Trim(strings.TrimPrefix(req.URL.Path, "/api/streams/"), "/")
		parts := strings.Split(path, "/")
		if len(parts) == 0 || parts[0] == "" {
			writeJSON(w, http.StatusBadRequest, map[string]string{"error": "device id required"})
			return
		}
		deviceID := parts[0]

		if len(parts) == 1 && req.Method == http.MethodPost {
			var body streamRequest
			if err := json.NewDecoder(req.Body).Decode(&body); err != nil || body.RTPPort <= 0 || body.RTPPort > 65535 {
				writeJSON(w, http.StatusBadRequest, map[string]string{"error": "valid rtp_port required"})
				return
			}
			s, err := r.ensureStream(deviceID, body.RTPPort)
			if err != nil {
				writeJSON(w, http.StatusConflict, map[string]string{"error": err.Error()})
				return
			}
			writeJSON(w, http.StatusOK, s.status())
			return
		}

		s, ok := r.get(deviceID)
		if !ok {
			writeJSON(w, http.StatusNotFound, map[string]string{"error": "stream not found"})
			return
		}

		if len(parts) == 2 && parts[1] == "status" && req.Method == http.MethodGet {
			writeJSON(w, http.StatusOK, s.status())
			return
		}
		if len(parts) == 2 && parts[1] == "webrtc" && req.Method == http.MethodPost {
			var offer offerRequest
			if err := json.NewDecoder(req.Body).Decode(&offer); err != nil || offer.SDP == "" {
				writeJSON(w, http.StatusBadRequest, map[string]string{"error": "valid SDP offer required"})
				return
			}
			answer, err := s.answer(offer)
			if err != nil {
				writeJSON(w, http.StatusInternalServerError, map[string]string{"error": err.Error()})
				return
			}
			writeJSON(w, http.StatusOK, answer)
			return
		}

		writeJSON(w, http.StatusNotFound, map[string]string{"error": "not found"})
	})

	listen := os.Getenv("RELAY_LISTEN")
	if listen == "" {
		listen = "127.0.0.1:8090"
	}
	log.Printf("RobotLiDAR Pion H264 relay listening on http://%s", listen)
	log.Printf("Pion WebRTC H264 passthrough: RTP payload is not decoded or re-encoded")
	if err := http.ListenAndServe(listen, mux); err != nil {
		log.Fatal(err)
	}
}
