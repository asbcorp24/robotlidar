#pragma once

#include <cstdint>

namespace stcam {

constexpr std::uint16_t kMagic = 0x5354;
constexpr std::uint8_t kVersion = 1;

enum PacketType : std::uint8_t {
    PTZ = 1,
    TELEMETRY = 2,
};

enum PtzFlags : std::uint16_t {
    CENTER = 1u << 0,
    REQUEST_IDR = 1u << 1,
};

#pragma pack(push, 1)
struct PtzPacket {
    std::uint16_t magic_be;
    std::uint8_t version;
    std::uint8_t type;
    std::uint32_t sequence_be;
    std::int16_t pan_cdeg_be;
    std::int16_t tilt_cdeg_be;
    std::uint16_t speed_cdeg_s_be;
    std::uint16_t flags_be;
};

struct TelemetryPacket {
    std::uint16_t magic_be;
    std::uint8_t version;
    std::uint8_t type;
    std::uint32_t sequence_be;
    std::int16_t pan_cdeg_be;
    std::int16_t tilt_cdeg_be;
    std::int8_t wifi_rssi_dbm;
    std::uint8_t video_running;
    std::uint16_t reserved_be;
    std::uint32_t uptime_ms_be;
};
#pragma pack(pop)

} // namespace stcam
