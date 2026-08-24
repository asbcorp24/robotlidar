package main

import (
	"fmt"
	"log"
	"os"
	"sync"

	"golang.org/x/sys/unix"
)

type espBridge struct {
	mu      sync.Mutex
	f       *os.File
	seq     uint32
	armed   bool
}

func openESPBridge(path string, baud int) (*espBridge, error) {
	if path == "" {
		return &espBridge{}, nil
	}
	fd, err := unix.Open(path, unix.O_RDWR|unix.O_NOCTTY|unix.O_NONBLOCK, 0)
	if err != nil { return nil, err }
	if err = unix.SetNonblock(fd, false); err != nil { unix.Close(fd); return nil, err }
	t, err := unix.IoctlGetTermios(fd, unix.TCGETS)
	if err != nil { unix.Close(fd); return nil, err }
	rate, err := baudConst(baud)
	if err != nil { unix.Close(fd); return nil, err }
	t.Iflag = 0
	t.Oflag = 0
	t.Lflag = 0
	t.Cflag = unix.CS8 | unix.CLOCAL | unix.CREAD | rate
	t.Cc[unix.VMIN] = 0
	t.Cc[unix.VTIME] = 1
	if err = unix.IoctlSetTermios(fd, unix.TCSETS, t); err != nil { unix.Close(fd); return nil, err }
	return &espBridge{f: os.NewFile(uintptr(fd), path)}, nil
}

func baudConst(baud int) (uint32, error) {
	switch baud {
	case 9600: return unix.B9600, nil
	case 19200: return unix.B19200, nil
	case 38400: return unix.B38400, nil
	case 57600: return unix.B57600, nil
	case 115200: return unix.B115200, nil
	default: return 0, fmt.Errorf("unsupported baud %d", baud)
	}
}

func (e *espBridge) close() { if e != nil && e.f != nil { _ = e.f.Close() } }

func xorChecksum(s string) byte {
	var v byte
	for i := 0; i < len(s); i++ { v ^= s[i] }
	return v
}

func (e *espBridge) sendBody(body string) error {
	if e == nil || e.f == nil {
		log.Printf("ESP32 disabled: %s", body)
		return nil
	}
	line := fmt.Sprintf("%s*%02X\n", body, xorChecksum(body))
	_, err := e.f.WriteString(line)
	return err
}

func (e *espBridge) nextSeq() uint32 { e.seq++; return e.seq }

func (e *espBridge) ensureArmed() error {
	if e.armed { return nil }
	s := e.nextSeq()
	if err := e.sendBody(fmt.Sprintf("ARM,%d,1", s)); err != nil { return err }
	e.armed = true
	return nil
}

func (e *espBridge) drive(left, right int16) error {
	e.mu.Lock(); defer e.mu.Unlock()
	if left != 0 || right != 0 {
		if err := e.ensureArmed(); err != nil { return err }
	}
	s := e.nextSeq()
	return e.sendBody(fmt.Sprintf("DRV,%d,%d,%d", s, left, right))
}

func (e *espBridge) aux(lift, spin int16) error {
	e.mu.Lock(); defer e.mu.Unlock()
	if lift != 0 || spin != 0 {
		if err := e.ensureArmed(); err != nil { return err }
	}
	// Current ESP32 brush controller is speed-only (0..1000). Until a physical
	// reverse line is added to that hardware, negative spin is treated as the
	// same magnitude. Lift maps directly to AUX actuator -1/0/+1.
	brush := int(spin)
	if brush < 0 { brush = -brush }
	if brush > 1000 { brush = 1000 }
	act := 0
	if lift > 0 { act = 1 } else if lift < 0 { act = -1 }
	s := e.nextSeq()
	return e.sendBody(fmt.Sprintf("AUX,%d,%d,%d", s, act, brush))
}

func (e *espBridge) emergencyStop() error {
	e.mu.Lock(); defer e.mu.Unlock()
	s := e.nextSeq()
	err := e.sendBody(fmt.Sprintf("STOP,%d", s))
	e.armed = false
	return err
}
