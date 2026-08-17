#include "platform_bl808.h"

/*
 * BL808/M1s hardware adapter.
 *
 * This file is intentionally the only SDK-specific layer. Wire the real
 * Sipeed/Bouffalo APIs here during board bring-up:
 *   - Wi-Fi STA + lwIP UDP sockets
 *   - CherryUSB UVC host, HBVCAM-4M2214HD-2, MJPEG 1280x480@30
 *   - MJPEG decode -> raw frame
 *   - BL808 HW H.264 encoder, 2 Mbps, GOP 15, no B frames
 *   - 50 Hz PWM for PAN/TILT
 *
 * The rest of the project does not depend on the exact SDK revision.
 */

static platform_ptz_rx_cb g_ptz_cb;
static void *g_ptz_user;
static platform_h264_nal_cb g_h264_cb;
static void *g_h264_user;

int platform_init(void){ return 0; }
uint32_t platform_millis(void){ return 0; }
int platform_wifi_connect(void){ return -1; }
int platform_udp_video_open(const char *host,uint16_t port){ (void)host;(void)port; return -1; }
int platform_udp_control_open(uint16_t port,platform_ptz_rx_cb cb,void *user){ g_ptz_cb=cb;g_ptz_user=user;(void)g_ptz_cb;(void)g_ptz_user;(void)port;return -1; }
int platform_udp_send_video(const uint8_t *data,size_t len){ (void)data;(void)len; return -1; }
int platform_udp_send_telemetry(const uint8_t *data,size_t len){ (void)data;(void)len; return -1; }
int platform_servo_init(void){ return -1; }
void platform_servo_write_us(uint16_t pan_us,uint16_t tilt_us){ (void)pan_us;(void)tilt_us; }
int platform_video_start(platform_h264_nal_cb cb,void *user){ g_h264_cb=cb;g_h264_user=user;(void)g_h264_cb;(void)g_h264_user;return -1; }
void platform_video_request_idr(void){}
int platform_video_fps(void){ return 0; }
uint32_t platform_video_bitrate(void){ return 0; }
uint32_t platform_video_frames(void){ return 0; }
uint32_t platform_video_dropped_frames(void){ return 0; }
int platform_wifi_rssi_dbm(void){ return -127; }
void platform_poll(void){}
void platform_sleep_ms(uint32_t ms){ (void)ms; }
