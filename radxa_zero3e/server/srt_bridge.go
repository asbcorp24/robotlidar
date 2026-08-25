package main

import (
	"fmt"
	"io"
	"log"
	"os/exec"
	"strconv"
	"sync"
	"time"
)

const (
	srtPortBase = 12000
	srtPortMax  = 12099
)

// srtBridge accepts MPEG-TS/H.264 over SRT and remuxes it to the existing
// localhost RTP ingest. Both sides use stream copy: there is no decode/encode.
type srtBridge struct {
	DeviceID string
	SRTPort  int
	RTPPort  int

	stop chan struct{}
	done chan struct{}
	once sync.Once
}

func newSRTBridge(deviceID string, srtPort, rtpPort int) (*srtBridge, error) {
	if _, err := exec.LookPath("ffmpeg"); err != nil {
		return nil, fmt.Errorf("SRT ingest requires ffmpeg on server: %w", err)
	}
	b := &srtBridge{
		DeviceID: deviceID,
		SRTPort:  srtPort,
		RTPPort:  rtpPort,
		stop:     make(chan struct{}),
		done:     make(chan struct{}),
	}
	go b.run()
	return b, nil
}

func (b *srtBridge) run() {
	defer close(b.done)
	for {
		select {
		case <-b.stop:
			return
		default:
		}

		input := "srt://0.0.0.0:" + strconv.Itoa(b.SRTPort) + "?mode=listener&transtype=live&latency=200000"
		output := "rtp://127.0.0.1:" + strconv.Itoa(b.RTPPort) + "?pkt_size=1200"
		cmd := exec.Command(
			"ffmpeg",
			"-hide_banner", "-loglevel", "warning",
			"-fflags", "nobuffer",
			"-i", input,
			"-map", "0:v:0", "-an",
			"-c:v", "copy",
			"-bsf:v", "dump_extra=freq=keyframe",
			"-f", "rtp", output,
		)
		cmd.Stdout = io.Discard
		cmd.Stderr = io.Discard
		log.Printf("SRT ingest %s: srt://0.0.0.0:%d -> RTP 127.0.0.1:%d (copy)", b.DeviceID, b.SRTPort, b.RTPPort)
		err := cmd.Run()

		select {
		case <-b.stop:
			return
		default:
		}
		if err != nil {
			log.Printf("SRT %s ffmpeg exited: %v; restarting listener", b.DeviceID, err)
		} else {
			log.Printf("SRT %s session ended; waiting for reconnect", b.DeviceID)
		}

		select {
		case <-b.stop:
			return
		case <-time.After(500 * time.Millisecond):
		}
	}
}

func (b *srtBridge) close() {
	b.once.Do(func() { close(b.stop) })
}
