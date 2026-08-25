package main

import (
	"bytes"
	"crypto/rand"
	"encoding/binary"
	"fmt"
	"io"
	"log"
	"sync"
	"time"

	srt "github.com/datarhei/gosrt"
	"github.com/pion/rtp"
	"github.com/pion/rtp/codecs"
)

const (
	srtPortBase = 12000
	srtPortMax  = 12099
	tsPacketSize = 188
	h264RTPPayloadType = 96
	h264RTPMTU = 1188 // 1200-byte packet target minus the normal 12-byte RTP header.
)

// srtBridge accepts MPEG-TS/H.264 over SRT and feeds the existing Pion track
// directly in Go. There is no ffmpeg process, decode, or encode on the server.
type srtBridge struct {
	DeviceID string
	SRTPort  int
	stream   *rtpStream
	listener srt.Listener

	mu     sync.Mutex
	active srt.Conn
	closed bool
	once   sync.Once
}

func newSRTBridge(deviceID string, srtPort int, stream *rtpStream) (*srtBridge, error) {
	if stream == nil {
		return nil, fmt.Errorf("SRT ingest requires an RTP/WebRTC stream")
	}

	cfg := srt.DefaultConfig()
	cfg.TransmissionType = "live"
	cfg.TSBPDMode = true
	cfg.TooLatePacketDrop = true
	cfg.Latency = 200 * time.Millisecond
	cfg.ReceiverLatency = 200 * time.Millisecond
	cfg.PeerLatency = 200 * time.Millisecond
	cfg.PayloadSize = 1316
	cfg.PeerIdleTimeout = 5 * time.Second

	ln, err := srt.Listen("srt", fmt.Sprintf(":%d", srtPort), cfg)
	if err != nil {
		return nil, fmt.Errorf("listen SRT UDP/%d: %w", srtPort, err)
	}

	b := &srtBridge{
		DeviceID: deviceID,
		SRTPort:  srtPort,
		stream:   stream,
		listener: ln,
	}
	go b.acceptLoop()
	log.Printf("SRT ingest %s: srt://0.0.0.0:%d (pure Go MPEG-TS/H.264 -> Pion)", deviceID, srtPort)
	return b, nil
}

func (b *srtBridge) acceptLoop() {
	for {
		req, err := b.listener.Accept2()
		if err != nil {
			b.mu.Lock()
			closed := b.closed
			b.mu.Unlock()
			if !closed {
				log.Printf("SRT %s accept error: %v", b.DeviceID, err)
			}
			return
		}

		conn, err := req.Accept()
		if err != nil {
			log.Printf("SRT %s handshake accept error: %v", b.DeviceID, err)
			continue
		}

		b.mu.Lock()
		old := b.active
		b.active = conn
		b.mu.Unlock()
		if old != nil {
			_ = old.Close()
		}

		go b.handleConnection(conn)
	}
}

func (b *srtBridge) handleConnection(conn srt.Conn) {
	defer func() {
		_ = conn.Close()
		b.mu.Lock()
		if b.active == conn {
			b.active = nil
		}
		b.mu.Unlock()
	}()

	log.Printf("SRT %s publisher connected from %s", b.DeviceID, conn.RemoteAddr())
	demux := newTSH264Demux(b.stream)
	readBuf := make([]byte, 64*1024)
	pending := make([]byte, 0, 128*1024)

	for {
		n, err := conn.Read(readBuf)
		if n > 0 {
			pending = append(pending, readBuf[:n]...)
			pending = demux.consume(pending)
		}
		if err != nil {
			demux.flush()
			if err != io.EOF {
				log.Printf("SRT %s publisher ended: %v", b.DeviceID, err)
			} else {
				log.Printf("SRT %s publisher disconnected", b.DeviceID)
			}
			return
		}
	}
}

func (b *srtBridge) close() {
	b.once.Do(func() {
		b.mu.Lock()
		b.closed = true
		active := b.active
		b.active = nil
		b.mu.Unlock()
		if active != nil {
			_ = active.Close()
		}
		if b.listener != nil {
			b.listener.Close()
		}
	})
}

// tsH264Demux is intentionally small: Raspberry sends one H.264 video stream in
// MPEG-TS. It detects the video PES PID, reconstructs PES payloads, reads PTS,
// and packetizes the Annex-B H.264 elementary stream directly to RTP.
type tsH264Demux struct {
	stream *rtpStream

	videoPID int
	started  bool
	pes      []byte
	pts      uint64
	havePTS  bool

	payloader *codecs.H264Payloader
	seq       uint16
	ssrc      uint32
	lastTS    uint32
	lastStep  uint32
}

func newTSH264Demux(stream *rtpStream) *tsH264Demux {
	return &tsH264Demux{
		stream:    stream,
		videoPID:  -1,
		payloader: &codecs.H264Payloader{},
		seq:       randomUint16(),
		ssrc:      randomUint32(),
		lastStep:  3600, // 25 fps fallback when a PES has no PTS.
	}
}

// consume parses as many complete 188-byte TS packets as possible and returns
// the unconsumed tail. It also resynchronizes after arbitrary SRT read boundaries.
func (d *tsH264Demux) consume(buf []byte) []byte {
	for {
		if len(buf) < tsPacketSize {
			return buf
		}
		if buf[0] != 0x47 {
			idx := bytes.IndexByte(buf[1:], 0x47)
			if idx < 0 {
				if len(buf) > tsPacketSize {
					return append([]byte(nil), buf[len(buf)-tsPacketSize+1:]...)
				}
				return buf
			}
			buf = buf[idx+1:]
			continue
		}
		// When possible, verify the next sync byte before trusting alignment.
		if len(buf) >= tsPacketSize*2 && buf[tsPacketSize] != 0x47 {
			buf = buf[1:]
			continue
		}
		d.handlePacket(buf[:tsPacketSize])
		buf = buf[tsPacketSize:]
	}
}

func (d *tsH264Demux) handlePacket(p []byte) {
	if len(p) != tsPacketSize || p[0] != 0x47 || p[1]&0x80 != 0 {
		return
	}
	pid := int(p[1]&0x1f)<<8 | int(p[2])
	pusi := p[1]&0x40 != 0
	afc := (p[3] >> 4) & 0x03
	if afc == 0 || afc == 2 { // reserved or adaptation-only: no payload
		return
	}

	off := 4
	if afc == 3 {
		if off >= len(p) {
			return
		}
		off += 1 + int(p[4])
	}
	if off >= len(p) {
		return
	}
	payload := p[off:]

	// We only send one H.264 video stream from Raspberry. Detect its PID from a
	// video PES start code instead of doing a full PSI/PAT/PMT dependency chain.
	if d.videoPID < 0 {
		if pusi && isVideoPES(payload) {
			d.videoPID = pid
			d.startPES(payload)
			log.Printf("SRT H.264 MPEG-TS detected video PID %d", pid)
		}
		return
	}
	if pid != d.videoPID {
		return
	}

	if pusi {
		d.flush()
		if isVideoPES(payload) {
			d.startPES(payload)
		} else {
			d.started = false
		}
		return
	}
	if d.started {
		d.pes = append(d.pes, payload...)
		if len(d.pes) > 16*1024*1024 {
			// Corrupt/missing PES boundary protection.
			d.pes = d.pes[:0]
			d.started = false
		}
	}
}

func isVideoPES(payload []byte) bool {
	return len(payload) >= 9 &&
		payload[0] == 0x00 && payload[1] == 0x00 && payload[2] == 0x01 &&
		payload[3] >= 0xe0 && payload[3] <= 0xef
}

func (d *tsH264Demux) startPES(payload []byte) {
	if !isVideoPES(payload) {
		d.started = false
		return
	}
	headerLen := int(payload[8])
	dataOff := 9 + headerLen
	if dataOff > len(payload) {
		d.started = false
		return
	}

	d.havePTS = false
	if payload[7]&0x80 != 0 && len(payload) >= 14 {
		d.pts = parsePTS(payload[9:14])
		d.havePTS = true
	}
	d.pes = append(d.pes[:0], payload[dataOff:]...)
	d.started = true
}

func parsePTS(b []byte) uint64 {
	if len(b) < 5 {
		return 0
	}
	return (uint64((b[0]>>1)&0x07) << 30) |
		(uint64(b[1]) << 22) |
		(uint64((b[2]>>1)&0x7f) << 15) |
		(uint64(b[3]) << 7) |
		uint64((b[4]>>1)&0x7f)
}

func (d *tsH264Demux) flush() {
	if !d.started || len(d.pes) == 0 {
		d.pes = d.pes[:0]
		d.started = false
		return
	}

	var ts uint32
	if d.havePTS {
		ts = uint32(d.pts)
		if d.lastTS != 0 {
			step := ts - d.lastTS
			if step > 0 && step < 90000 {
				d.lastStep = step
			}
		}
	} else if d.lastTS != 0 {
		ts = d.lastTS + d.lastStep
	} else {
		ts = randomUint32()
	}

	payloads := d.payloader.Payload(h264RTPMTU, d.pes)
	for i, payload := range payloads {
		packet := &rtp.Packet{
			Header: rtp.Header{
				Version:        2,
				Marker:         i == len(payloads)-1,
				PayloadType:    h264RTPPayloadType,
				SequenceNumber: d.seq,
				Timestamp:      ts,
				SSRC:           d.ssrc,
			},
			Payload: payload,
		}
		d.seq++
		if err := d.stream.writeRTP(packet, len(payload)+12); err != nil {
			log.Printf("SRT/WebRTC relay %s write error: %v", d.stream.DeviceID, err)
			break
		}
	}

	if len(payloads) > 0 {
		d.lastTS = ts
	}
	d.pes = d.pes[:0]
	d.started = false
}

func randomUint16() uint16 {
	var b [2]byte
	if _, err := rand.Read(b[:]); err == nil {
		return binary.BigEndian.Uint16(b[:])
	}
	return uint16(time.Now().UnixNano())
}

func randomUint32() uint32 {
	var b [4]byte
	if _, err := rand.Read(b[:]); err == nil {
		return binary.BigEndian.Uint32(b[:])
	}
	return uint32(time.Now().UnixNano())
}
