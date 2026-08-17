#pragma once

#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint16_t sequence;
    uint32_t timestamp;
    uint32_t ssrc;
    uint8_t payload_type;
    uint16_t mtu;
} rtp_h264_t;

typedef int (*rtp_send_fn)(const uint8_t *data, size_t len, void *user);

void rtp_h264_init(rtp_h264_t *rtp, uint32_t ssrc, uint8_t payload_type, uint16_t mtu);
int rtp_h264_send_nal(rtp_h264_t *rtp, const uint8_t *nal, size_t nal_len,
                      int marker, rtp_send_fn send_fn, void *user);
void rtp_h264_next_frame(rtp_h264_t *rtp, uint32_t clock_hz, uint32_t fps);
