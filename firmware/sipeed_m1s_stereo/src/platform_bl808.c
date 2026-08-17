#include "platform_bl808.h"
#include "app_config.h"

#include <FreeRTOS.h>
#include <task.h>
#include <string.h>
#include <stdio.h>

#include <m1s_c906_xram_wifi.h>
#include <lwip/sockets.h>
#include <lwip/inet.h>

/*
 * Real M1s/BL808 platform layer for timing, Wi-Fi and UDP.
 *
 * VIDEO NOTE:
 * The official M1s SDK has working MIPI camera + MJPEG Wi-Fi examples, but it
 * does not ship a UVC HOST class for an external USB camera. Therefore the
 * HBVCAM UVC -> MJPEG decode -> H.264 part is isolated behind weak hooks below.
 * A UVC host driver can be added without changing RTP/PTZ/network code.
 */

#ifndef APP_WIFI_SSID
#define APP_WIFI_SSID "CHANGE_ME"
#endif
#ifndef APP_WIFI_PASSWORD
#define APP_WIFI_PASSWORD "CHANGE_ME"
#endif
#ifndef APP_SERVER_HOST
#define APP_SERVER_HOST "192.168.1.100"
#endif

static int g_video_fd = -1;
static int g_ctrl_fd = -1;
static int g_telem_fd = -1;
static struct sockaddr_in g_video_dst;
static struct sockaddr_in g_telem_dst;
static platform_ptz_rx_cb g_ptz_cb;
static void *g_ptz_user;

static volatile uint32_t g_frames;
static volatile uint32_t g_dropped;
static volatile uint32_t g_bitrate;
static volatile int g_fps;

/* Implemented by src/uvc_h264_bl808.c when a UVC host backend is available. */
__attribute__((weak)) int m1s_uvc_h264_start(platform_h264_nal_cb cb, void *user)
{
    (void)cb;
    (void)user;
    printf("ERR: UVC HOST backend is not available in stock M1s_BL808_SDK\r\n");
    return -1;
}

__attribute__((weak)) void m1s_uvc_h264_request_idr(void) {}

/* Board-specific servo backend. Kept as weak hooks so pin assignment can be
 * selected after checking the exact M1s carrier/dock wiring. */
__attribute__((weak)) int m1s_servo_hw_init(void) { return 0; }
__attribute__((weak)) void m1s_servo_hw_write_us(uint16_t pan_us, uint16_t tilt_us)
{
    (void)pan_us;
    (void)tilt_us;
}

static int make_udp_socket(void)
{
    int fd = lwip_socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    return fd;
}

static int set_destination(struct sockaddr_in *dst, const char *host, uint16_t port)
{
    memset(dst, 0, sizeof(*dst));
    dst->sin_family = AF_INET;
    dst->sin_port = htons(port);
    if (inet_aton(host, &dst->sin_addr) == 0) return -1;
    return 0;
}

int platform_init(void)
{
    g_video_fd = g_ctrl_fd = g_telem_fd = -1;
    g_frames = g_dropped = g_bitrate = 0;
    g_fps = 0;
    return 0;
}

uint32_t platform_millis(void)
{
    return (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS);
}

int platform_wifi_connect(void)
{
    printf("WiFi: init\r\n");
    m1s_xram_wifi_init();
    printf("WiFi: connect SSID=%s\r\n", APP_WIFI_SSID);
    m1s_xram_wifi_connect(APP_WIFI_SSID, APP_WIFI_PASSWORD);
    /* Sipeed API is asynchronous internally; give network stack time to obtain IP. */
    vTaskDelay(pdMS_TO_TICKS(2500));
    return 0;
}

int platform_udp_video_open(const char *host, uint16_t port)
{
    g_video_fd = make_udp_socket();
    g_telem_fd = make_udp_socket();
    if (g_video_fd < 0 || g_telem_fd < 0) return -1;
    if (set_destination(&g_video_dst, host, port) < 0) return -1;
    if (set_destination(&g_telem_dst, host, APP_TELEMETRY_DST_PORT) < 0) return -1;
    printf("Video RTP destination: %s:%u\r\n", host, (unsigned)port);
    return 0;
}

int platform_udp_control_open(uint16_t port, platform_ptz_rx_cb cb, void *user)
{
    struct sockaddr_in local;
    g_ptz_cb = cb;
    g_ptz_user = user;
    g_ctrl_fd = make_udp_socket();
    if (g_ctrl_fd < 0) return -1;

    memset(&local, 0, sizeof(local));
    local.sin_family = AF_INET;
    local.sin_addr.s_addr = PP_HTONL(INADDR_ANY);
    local.sin_port = htons(port);
    if (lwip_bind(g_ctrl_fd, (struct sockaddr *)&local, sizeof(local)) < 0) return -1;

    int flags = lwip_fcntl(g_ctrl_fd, F_GETFL, 0);
    if (flags >= 0) lwip_fcntl(g_ctrl_fd, F_SETFL, flags | O_NONBLOCK);
    printf("PTZ UDP listen: 0.0.0.0:%u\r\n", (unsigned)port);
    return 0;
}

int platform_udp_send_video(const uint8_t *data, size_t len)
{
    if (g_video_fd < 0) return -1;
    int rc = lwip_sendto(g_video_fd, data, len, 0,
                         (struct sockaddr *)&g_video_dst, sizeof(g_video_dst));
    return rc == (int)len ? 0 : -1;
}

int platform_udp_send_telemetry(const uint8_t *data, size_t len)
{
    if (g_telem_fd < 0) return -1;
    int rc = lwip_sendto(g_telem_fd, data, len, 0,
                         (struct sockaddr *)&g_telem_dst, sizeof(g_telem_dst));
    return rc == (int)len ? 0 : -1;
}

int platform_servo_init(void)
{
    return m1s_servo_hw_init();
}

void platform_servo_write_us(uint16_t pan_us, uint16_t tilt_us)
{
    m1s_servo_hw_write_us(pan_us, tilt_us);
}

int platform_video_start(platform_h264_nal_cb cb, void *user)
{
    return m1s_uvc_h264_start(cb, user);
}

void platform_video_request_idr(void)
{
    m1s_uvc_h264_request_idr();
}

int platform_video_fps(void) { return g_fps; }
uint32_t platform_video_bitrate(void) { return g_bitrate; }
uint32_t platform_video_frames(void) { return g_frames; }
uint32_t platform_video_dropped_frames(void) { return g_dropped; }

int platform_wifi_rssi_dbm(void)
{
    /* Stock m1s_xram wrapper does not expose RSSI in its small public API. */
    return -127;
}

void platform_poll(void)
{
    if (g_ctrl_fd >= 0 && g_ptz_cb) {
        uint8_t buf[128];
        struct sockaddr_in from;
        socklen_t from_len = sizeof(from);
        int n = lwip_recvfrom(g_ctrl_fd, buf, sizeof(buf), 0,
                              (struct sockaddr *)&from, &from_len);
        if (n > 0) g_ptz_cb(buf, (size_t)n, g_ptz_user);
    }
}

void platform_sleep_ms(uint32_t ms)
{
    vTaskDelay(pdMS_TO_TICKS(ms));
}
