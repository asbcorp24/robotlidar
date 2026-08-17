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
               int gop,
               const std::string& bind_ip = {});
    void stop();
    bool running();

private:
    pid_t pid_ = -1;
};
