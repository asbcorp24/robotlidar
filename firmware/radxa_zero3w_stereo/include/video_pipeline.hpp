#pragma once

#include <string>
#include <sys/types.h>

class VideoPipeline {
public:
    ~VideoPipeline();

    bool start(const std::string& ffmpeg,
               const std::string& device,
               const std::string& server_ip,
               int server_port,
               int width,
               int height,
               int fps,
               int bitrate_kbps,
               int maxrate_kbps,
               int gop);
    void stop();
    bool running();
    bool requestIdr();

private:
    pid_t pid_ = -1;
};
