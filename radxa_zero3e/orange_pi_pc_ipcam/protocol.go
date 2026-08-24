package main

import (
	"encoding/binary"
	"fmt"
)

const (
	controlMagic      = 0x5354
	controlVersion    = 1
	controlTypePTZ    = 1
	controlTypeDrive  = 2
	controlTypeBrush  = 3
	flagCenter        = 1 << 0
	flagRequestIDR    = 1 << 1
)

type controlPacket struct {
	Type   byte
	Seq    uint32
	Value1 int16
	Value2 int16
	Extra  uint32
}

func parseControlPacket(b []byte) (controlPacket, error) {
	var p controlPacket
	if len(b) != 16 {
		return p, fmt.Errorf("control packet size %d, expected 16", len(b))
	}
	if binary.BigEndian.Uint16(b[0:2]) != controlMagic || b[2] != controlVersion {
		return p, fmt.Errorf("invalid control header")
	}
	p.Type = b[3]
	p.Seq = binary.BigEndian.Uint32(b[4:8])
	p.Value1 = int16(binary.BigEndian.Uint16(b[8:10]))
	p.Value2 = int16(binary.BigEndian.Uint16(b[10:12]))
	p.Extra = binary.BigEndian.Uint32(b[12:16])
	return p, nil
}

func clamp(v, lo, hi int16) int16 {
	if v < lo { return lo }
	if v > hi { return hi }
	return v
}
