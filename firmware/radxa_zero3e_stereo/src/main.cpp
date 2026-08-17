#include "protocol.hpp"
#include "ptz.hpp"
#include "pwm_sysfs.hpp"
#include "video_pipeline.hpp"

#include <arpa/inet.h>
#include <atomic>
#include <chrono>
#include <csignal>
#include <fstream>
#include <iostream>
#include <string>

#include <sys/socket.h>
#include <unistd.h>

namespace {
std::atomic<bool> g_run{true};

struct Config {
    std::string server = "192.168.1.100";
    std::string bind_ip;
    std::string eth_if = "eth0";
    int video_port = 5004;
    int control_port = 6000;
    int telemetry_port = 6001;
    std::string camera = "/dev/video0";
    std::string ffmpeg = "ffmpeg";
    int width = 1280;
    int height = 480;
    int fps = 30;
    int bitrate_kbps = 2000;
    int maxrate_kbps = 3000;
    int gop = 15;
    std::string pan_chip;
    int pan_channel = 0;
    std::string tilt_chip;
    int tilt_channel = 0;
};

void onSignal(int) { g_run = false; }

void usage(const char* exe) {
    std::cout
        << "Usage: " << exe << " --server IP --pan-chip /sys/class/pwm/pwmchipX --tilt-chip /sys/class/pwm/pwmchipY [options]\n"
        << "  --bind-ip IP                 Bind outgoing RTP to Ethernet IP\n"
        << "  --eth-if eth0                Ethernet interface for link telemetry\n"
        << "  --camera /dev/video0         HBVCAM V4L2 node\n"
        << "  --ffmpeg /usr/local/bin/ffmpeg\n"
        << "  --video-port 5004 --control-port 6000 --telemetry-port 6001\n"
        << "  --width 1280 --height 480 --fps 30\n"
        << "  --bitrate 2000 --maxrate 3000 --gop 15\n"
        << "  --pan-channel 0 --tilt-channel 0\n";
}

bool parseArgs(int argc, char** argv, Config& c) {
    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        auto next = [&]() -> std::string {
            if (++i >= argc) return {};
            return argv[i];
        };
        if (a == "--server") c.server = next();
        else if (a == "--bind-ip") c.bind_ip = next();
        else if (a == "--eth-if") c.eth_if = next();
        else if (a == "--camera") c.camera = next();
        else if (a == "--ffmpeg") c.ffmpeg = next();
        else if (a == "--video-port") c.video_port = std::stoi(next());
        else if (a == "--control-port") c.control_port = std::stoi(next());
        else if (a == "--telemetry-port") c.telemetry_port = std::stoi(next());
        else if (a == "--width") c.width = std::stoi(next());
        else if (a == "--height") c.height = std::stoi(next());
        else if (a == "--fps") c.fps = std::stoi(next());
        else if (a == "--bitrate") c.bitrate_kbps = std::stoi(next());
        else if (a == "--maxrate") c.maxrate_kbps = std::stoi(next());
        else if (a == "--gop") c.gop = std::stoi(next());
        else if (a == "--pan-chip") c.pan_chip = next();
        else if (a == "--pan-channel") c.pan_channel = std::stoi(next());
        else if (a == "--tilt-chip") c.tilt_chip = next();
        else if (a == "--tilt-channel") c.tilt_channel = std::stoi(next());
        else if (a == "--help" || a == "-h") { usage(argv[0]); return false; }
        else { std::cerr << "Unknown option: " << a << "\n"; return false; }
    }
    return !c.server.empty() && !c.pan_chip.empty() && !c.tilt_chip.empty();
}

bool ethernetLink(const std::string& iface) {
    std::ifstream f("/sys/class/net/" + iface + "/carrier");
    int carrier = 0;
    return (f >> carrier) && carrier == 1;
}

std::uint32_t uptimeMs(const std::chrono::steady_clock::time_point& start) {
    return static_cast<std::uint32_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now() - start).count());
}

bool startVideo(VideoPipeline& v, const Config& c) {
    return v.start(c.ffmpeg, c.camera, c.server, c.video_port,
                   c.width, c.height, c.fps, c.bitrate_kbps,
                   c.maxrate_kbps, c.gop, c.bind_ip);
}
}

int main(int argc, char** argv) {
    Config cfg;
    if (!parseArgs(argc, argv, cfg)) {
        usage(argv[0]);
        return 2;
    }

    std::signal(SIGINT, onSignal);
    std::signal(SIGTERM, onSignal);

    PwmChannel pan_pwm, tilt_pwm;
    if (!pan_pwm.open(cfg.pan_chip, cfg.pan_channel)) {
        std::cerr << "PAN PWM init failed: " << cfg.pan_chip << " channel " << cfg.pan_channel << "\n";
        return 3;
    }
    if (!tilt_pwm.open(cfg.tilt_chip, cfg.tilt_channel)) {
        std::cerr << "TILT PWM init failed: " << cfg.tilt_chip << " channel " << cfg.tilt_channel << "\n";
        return 4;
    }

    PtzController ptz;
    ptz.configure(-9000, 9000, -4500, 4500, 700, 2300, 900, 2100);
    pan_pwm.setPulseUs(ptz.panPulseUs());
    tilt_pwm.setPulseUs(ptz.tiltPulseUs());

    const int sock = ::socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) { perror("socket"); return 5; }

    sockaddr_in local{};
    local.sin_family = AF_INET;
    local.sin_addr.s_addr = htonl(INADDR_ANY);
    local.sin_port = htons(static_cast<std::uint16_t>(cfg.control_port));
    if (bind(sock, reinterpret_cast<sockaddr*>(&local), sizeof(local)) < 0) {
        perror("bind"); close(sock); return 6;
    }

    timeval tv{};
    tv.tv_sec = 0;
    tv.tv_usec = 10000;
    setsockopt(sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

    sockaddr_in telemetry_dst{};
    telemetry_dst.sin_family = AF_INET;
    telemetry_dst.sin_port = htons(static_cast<std::uint16_t>(cfg.telemetry_port));
    if (inet_pton(AF_INET, cfg.server.c_str(), &telemetry_dst.sin_addr) != 1) {
        std::cerr << "Invalid server IP: " << cfg.server << "\n";
        close(sock); return 7;
    }

    VideoPipeline video;
    if (!startVideo(video, cfg)) {
        std::cerr << "Video pipeline start failed\n";
        close(sock); return 8;
    }

    std::cout << "Radxa ZERO 3E stereo node started\n"
              << "Ethernet interface: " << cfg.eth_if << "\n"
              << "Camera: " << cfg.camera << " " << cfg.width << "x" << cfg.height << "@" << cfg.fps << " MJPEG\n"
              << "Video: RTP/H264 -> " << cfg.server << ':' << cfg.video_port << "\n"
              << "PTZ listen: UDP :" << cfg.control_port << "\n";

    const auto start = std::chrono::steady_clock::now();
    auto last_loop = start;
    auto last_telem = start;
    auto last_video_check = start;
    std::uint32_t telem_seq = 0;

    while (g_run) {
        stcam::PtzPacket p{};
        sockaddr_in src{};
        socklen_t src_len = sizeof(src);
        const ssize_t n = recvfrom(sock, &p, sizeof(p), 0, reinterpret_cast<sockaddr*>(&src), &src_len);
        if (n == static_cast<ssize_t>(sizeof(p)) &&
            ntohs(p.magic_be) == stcam::kMagic && p.version == stcam::kVersion && p.type == stcam::PTZ) {
            const int pan = static_cast<std::int16_t>(ntohs(static_cast<std::uint16_t>(p.pan_cdeg_be)));
            const int tilt = static_cast<std::int16_t>(ntohs(static_cast<std::uint16_t>(p.tilt_cdeg_be)));
            const int speed = ntohs(p.speed_cdeg_s_be);
            const std::uint16_t flags = ntohs(p.flags_be);
            if (flags & stcam::CENTER) ptz.center();
            else ptz.setTarget(pan, tilt, speed);
            if (flags & stcam::REQUEST_IDR) {
                video.stop();
                startVideo(video, cfg);
            }
        }

        const auto now = std::chrono::steady_clock::now();
        const auto dt = std::chrono::duration_cast<std::chrono::milliseconds>(now - last_loop).count();
        if (dt > 0) {
            ptz.update(static_cast<std::uint32_t>(dt));
            pan_pwm.setPulseUs(ptz.panPulseUs());
            tilt_pwm.setPulseUs(ptz.tiltPulseUs());
            last_loop = now;
        }

        if (now - last_video_check >= std::chrono::seconds(1)) {
            last_video_check = now;
            if (!video.running()) {
                std::cerr << "video process exited, restarting\n";
                startVideo(video, cfg);
            }
        }

        if (now - last_telem >= std::chrono::milliseconds(250)) {
            last_telem = now;
            stcam::TelemetryPacket t{};
            t.magic_be = htons(stcam::kMagic);
            t.version = stcam::kVersion;
            t.type = stcam::TELEMETRY;
            t.sequence_be = htonl(++telem_seq);
            t.pan_cdeg_be = static_cast<std::int16_t>(htons(static_cast<std::uint16_t>(ptz.panCdeg())));
            t.tilt_cdeg_be = static_cast<std::int16_t>(htons(static_cast<std::uint16_t>(ptz.tiltCdeg())));
            t.ethernet_link = ethernetLink(cfg.eth_if) ? 1 : 0;
            t.video_running = video.running() ? 1 : 0;
            t.uptime_ms_be = htonl(uptimeMs(start));
            sendto(sock, &t, sizeof(t), 0, reinterpret_cast<sockaddr*>(&telemetry_dst), sizeof(telemetry_dst));
        }
    }

    video.stop();
    close(sock);
    return 0;
}
