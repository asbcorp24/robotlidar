#pragma once

#include <stddef.h>
#include <stdint.h>

typedef void (*platform_h264_nal_cb)(const uint8_t *nal, size_t len, int end_of_frame, void *user);
typedef void (*platform_ptz_rx_cb)(const uint8_t *data, size_t len, void *user);

int platform_init(void);
uint32_t platform_millis(void);
int platform_wifi_connect(void);
int platform_udp_video_open(const char *host, uint16_t port);
int platform_udp_control_open(uint16_t port, platform_ptz_rx_cb cb, void *user);
int platform_udp_send_video(const uint8_t *data, size_t len);
int platform_udp_send_telemetry(const uint8_t *data, size_t len);
int platform_servo_init(void);
void platform_servo_write_us(uint16_t pan_us, uint16_t tilt_us);
int platform_video_start(platform_h264_nal_cb cb, void *user);
void platform_video_request_idr(void);
int platform_video_fps(void);
uint32_t platform_video_bitrate(void);
uint32_t platform_video_frames(void);
uint32_t platform_video_dropped_frames(void);
int platform_wifi_rssi_dbm(void);
void platform_poll(void);
void platform_sleep_ms(uint32_t ms);
