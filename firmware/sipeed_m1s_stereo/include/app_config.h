#pragma once

#include <stdint.h>

#define APP_VIDEO_WIDTH              1280
#define APP_VIDEO_HEIGHT             480
#define APP_VIDEO_FPS                30
#define APP_H264_BITRATE_BPS         2000000u
#define APP_H264_MAX_BITRATE_BPS     3000000u
#define APP_H264_GOP_FRAMES          15
#define APP_RTP_PAYLOAD_TYPE         96
#define APP_RTP_CLOCK_HZ             90000u
#define APP_RTP_MTU                  1200u
#define APP_VIDEO_DST_PORT           5004
#define APP_PTZ_LISTEN_PORT          6000
#define APP_TELEMETRY_DST_PORT       6001
#define APP_CONTROL_TIMEOUT_MS       1500u
#define APP_TELEMETRY_PERIOD_MS      250u

#define APP_PAN_MIN_CDEG             (-9000)
#define APP_PAN_MAX_CDEG             ( 9000)
#define APP_TILT_MIN_CDEG            (-4500)
#define APP_TILT_MAX_CDEG            ( 4500)
#define APP_PAN_CENTER_CDEG          0
#define APP_TILT_CENTER_CDEG         0

#define APP_SERVO_PAN_MIN_US         700
#define APP_SERVO_PAN_MAX_US         2300
#define APP_SERVO_TILT_MIN_US        900
#define APP_SERVO_TILT_MAX_US        2100
#define APP_SERVO_PERIOD_US          20000
#define APP_PTZ_DEFAULT_SPEED_CDEG_S 6000
