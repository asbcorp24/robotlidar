#include "rtp_h264.h"

#include <string.h>

static void put16(uint8_t *p, uint16_t v) { p[0]=(uint8_t)(v>>8); p[1]=(uint8_t)v; }
static void put32(uint8_t *p, uint32_t v) { p[0]=(uint8_t)(v>>24); p[1]=(uint8_t)(v>>16); p[2]=(uint8_t)(v>>8); p[3]=(uint8_t)v; }

static size_t make_header(uint8_t *p, const rtp_h264_t *r, int marker) {
    p[0]=0x80;
    p[1]=(uint8_t)((marker?0x80:0x00) | (r->payload_type & 0x7f));
    put16(p+2, r->sequence);
    put32(p+4, r->timestamp);
    put32(p+8, r->ssrc);
    return 12;
}

void rtp_h264_init(rtp_h264_t *r, uint32_t ssrc, uint8_t payload_type, uint16_t mtu) {
    memset(r,0,sizeof(*r));
    r->ssrc=ssrc; r->payload_type=payload_type; r->mtu=mtu;
}

int rtp_h264_send_nal(rtp_h264_t *r, const uint8_t *nal, size_t n,
                      int marker, rtp_send_fn send_fn, void *user) {
    uint8_t packet[1500];
    if (!r || !nal || n < 1 || !send_fn || r->mtu > sizeof(packet) || r->mtu < 64) return -1;

    if (n + 12 <= r->mtu) {
        size_t h=make_header(packet,r,marker);
        memcpy(packet+h,nal,n);
        int rc=send_fn(packet,h+n,user);
        r->sequence++;
        return rc;
    }

    const uint8_t hdr=nal[0];
    const uint8_t fu_indicator=(uint8_t)((hdr & 0xe0) | 28);
    const uint8_t nal_type=(uint8_t)(hdr & 0x1f);
    const size_t max_chunk=r->mtu-14;
    size_t off=1;
    int start=1;
    while (off<n) {
        size_t chunk=n-off;
        if (chunk>max_chunk) chunk=max_chunk;
        const int end=(off+chunk)==n;
        size_t h=make_header(packet,r,marker && end);
        packet[h++]=fu_indicator;
        packet[h++]=(uint8_t)((start?0x80:0) | (end?0x40:0) | nal_type);
        memcpy(packet+h,nal+off,chunk);
        int rc=send_fn(packet,h+chunk,user);
        r->sequence++;
        if (rc<0) return rc;
        off+=chunk;
        start=0;
    }
    return 0;
}

void rtp_h264_next_frame(rtp_h264_t *r, uint32_t clock_hz, uint32_t fps) {
    if (r && fps) r->timestamp += clock_hz / fps;
}
