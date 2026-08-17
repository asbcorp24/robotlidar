#pragma once

#include <stdint.h>

#define STCAM_MAGIC 0x5354u
#define STCAM_VERSION 1u

enum stcam_packet_type {
    STCAM_PKT_PTZ = 1,
    STCAM_PKT_TELEMETRY = 2
};

enum stcam_ptz_flags {
    STCAM_PTZ_CENTER      = 1u << 0,
    STCAM_PTZ_REQUEST_IDR = 1u << 1
};

#pragma pack(push, 1)
typedef struct {
    uint16_t magic_be;
    uint8_t version;
    uint8_t type;
    uint32_t sequence_be;
    int16_t pan_cdeg_be;
    int16_t tilt_cdeg_be;
    uint16_t speed_cdeg_s_be;
    uint16_t flags_be;
} stcam_ptz_packet_t;

typedef struct {
    uint16_t magic_be;
    uint8_t version;
    uint8_t type;
    uint32_t sequence_be;
    int16_t pan_cdeg_be;
    int16_t tilt_cdeg_be;
    int8_t wifi_rssi_dbm;
    uint8_t fps;
    uint16_t reserved_be;
    uint32_t bitrate_bps_be;
    uint32_t frames_be;
    uint32_t dropped_frames_be;
    uint32_t uptime_ms_be;
} stcam_telemetry_packet_t;
#pragma pack(pop)
