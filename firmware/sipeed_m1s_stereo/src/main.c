#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "app_config.h"
#include "platform_bl808.h"
#include "protocol.h"
#include "ptz.h"
#include "rtp_h264.h"

#ifndef APP_SERVER_HOST
#define APP_SERVER_HOST "192.168.1.100"
#endif

static ptz_state_t g_ptz;
static rtp_h264_t g_rtp;
static uint32_t g_last_loop_ms;
static uint32_t g_last_telem_ms;
static uint32_t g_last_ptz_ms;
static uint32_t g_telem_seq;

static uint16_t be16(uint16_t v){ return (uint16_t)((v>>8)|(v<<8)); }
static uint32_t be32(uint32_t v){ return ((v&0x000000ffu)<<24)|((v&0x0000ff00u)<<8)|((v&0x00ff0000u)>>8)|((v&0xff000000u)>>24); }
static int16_t sbe16(int16_t v){ return (int16_t)be16((uint16_t)v); }

static int rtp_send(const uint8_t *data,size_t len,void *user){ (void)user; return platform_udp_send_video(data,len); }

static void app_on_h264_nal(const uint8_t *nal,size_t len,int end_of_frame,void *user){
    (void)user;
    if(!nal || !len) return;
    /* platform adapter must strip Annex-B start codes before callback. */
    rtp_h264_send_nal(&g_rtp,nal,len,end_of_frame,rtp_send,0);
    if(end_of_frame) rtp_h264_next_frame(&g_rtp,APP_RTP_CLOCK_HZ,APP_VIDEO_FPS);
}

static void app_on_ptz_packet(const uint8_t *data,size_t len,void *user){
    (void)user;
    if(len!=sizeof(stcam_ptz_packet_t)) return;
    stcam_ptz_packet_t p;
    memcpy(&p,data,sizeof(p));
    if(be16(p.magic_be)!=STCAM_MAGIC || p.version!=STCAM_VERSION || p.type!=STCAM_PKT_PTZ) return;

    const uint16_t flags=be16(p.flags_be);
    const uint16_t speed=be16(p.speed_cdeg_s_be);
    if(flags & STCAM_PTZ_CENTER) ptz_center(&g_ptz);
    else ptz_set_target(&g_ptz,sbe16(p.pan_cdeg_be),sbe16(p.tilt_cdeg_be),speed);
    if(flags & STCAM_PTZ_REQUEST_IDR) platform_video_request_idr();
    g_last_ptz_ms=platform_millis();
}

static void send_telemetry(uint32_t now){
    stcam_telemetry_packet_t t;
    memset(&t,0,sizeof(t));
    t.magic_be=be16(STCAM_MAGIC);
    t.version=STCAM_VERSION;
    t.type=STCAM_PKT_TELEMETRY;
    t.sequence_be=be32(++g_telem_seq);
    t.pan_cdeg_be=sbe16((int16_t)g_ptz.pan_cdeg);
    t.tilt_cdeg_be=sbe16((int16_t)g_ptz.tilt_cdeg);
    t.wifi_rssi_dbm=(int8_t)platform_wifi_rssi_dbm();
    t.fps=(uint8_t)platform_video_fps();
    t.bitrate_bps_be=be32(platform_video_bitrate());
    t.frames_be=be32(platform_video_frames());
    t.dropped_frames_be=be32(platform_video_dropped_frames());
    t.uptime_ms_be=be32(now);
    platform_udp_send_telemetry((const uint8_t*)&t,sizeof(t));
}

int main(void){
    if(platform_init()<0) return 1;
    ptz_init(&g_ptz);
    rtp_h264_init(&g_rtp,0x4d315331u,APP_RTP_PAYLOAD_TYPE,APP_RTP_MTU);

    if(platform_servo_init()<0) return 2;
    platform_servo_write_us(ptz_pan_to_us(&g_ptz),ptz_tilt_to_us(&g_ptz));
    if(platform_wifi_connect()<0) return 3;
    if(platform_udp_video_open(APP_SERVER_HOST,APP_VIDEO_DST_PORT)<0) return 4;
    if(platform_udp_control_open(APP_PTZ_LISTEN_PORT,app_on_ptz_packet,0)<0) return 5;
    if(platform_video_start(app_on_h264_nal,0)<0) return 6;

    g_last_loop_ms=g_last_telem_ms=g_last_ptz_ms=platform_millis();
    for(;;){
        const uint32_t now=platform_millis();
        const uint32_t dt=now-g_last_loop_ms;
        g_last_loop_ms=now;
        ptz_update(&g_ptz,dt);
        platform_servo_write_us(ptz_pan_to_us(&g_ptz),ptz_tilt_to_us(&g_ptz));
        if(now-g_last_telem_ms>=APP_TELEMETRY_PERIOD_MS){ g_last_telem_ms=now; send_telemetry(now); }
        platform_poll();
        platform_sleep_ms(10);
    }
}
