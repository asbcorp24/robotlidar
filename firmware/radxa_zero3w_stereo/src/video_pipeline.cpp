#include "video_pipeline.hpp"

#include <csignal>
#include <cstdlib>
#include <iostream>
#include <string>
#include <sys/wait.h>
#include <unistd.h>
#include <vector>

VideoPipeline::~VideoPipeline() {
    stop();
}

bool VideoPipeline::start(const std::string& ffmpeg,
                          const std::string& device,
                          const std::string& server_ip,
                          int server_port,
                          int width,
                          int height,
                          int fps,
                          int bitrate_kbps,
                          int maxrate_kbps,
                          int gop) {
    stop();

    const std::string size = std::to_string(width) + "x" + std::to_string(height);
    const std::string fr = std::to_string(fps);
    const std::string br = std::to_string(bitrate_kbps) + "k";
    const std::string mr = std::to_string(maxrate_kbps) + "k";
    const std::string g = std::to_string(gop);
    const std::string url = "rtp://" + server_ip + ":" + std::to_string(server_port) + "?pkt_size=1200";

    std::vector<std::string> args = {
        ffmpeg,
        "-hide_banner", "-loglevel", "warning",
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-f", "v4l2",
        "-input_format", "mjpeg",
        "-video_size", size,
        "-framerate", fr,
        "-i", device,
        "-an",
        "-pix_fmt", "nv12",
        "-c:v", "h264_rkmpp",
        "-rc_mode", "CBR",
        "-b:v", br,
        "-maxrate", mr,
        "-g", g,
        "-bf", "0",
        "-profile:v", "main",
        "-f", "rtp",
        url
    };

    pid_ = fork();
    if (pid_ < 0) return false;
    if (pid_ == 0) {
        std::vector<char*> argv;
        argv.reserve(args.size() + 1);
        for (auto& s : args) argv.push_back(s.data());
        argv.push_back(nullptr);
        execvp(argv[0], argv.data());
        _exit(127);
    }

    std::cerr << "video: started ffmpeg pid=" << pid_ << " -> " << url << "\n";
    return true;
}

void VideoPipeline::stop() {
    if (pid_ <= 0) return;
    kill(pid_, SIGTERM);
    for (int i = 0; i < 20; ++i) {
        int status = 0;
        const pid_t r = waitpid(pid_, &status, WNOHANG);
        if (r == pid_) {
            pid_ = -1;
            return;
        }
        usleep(50000);
    }
    kill(pid_, SIGKILL);
    waitpid(pid_, nullptr, 0);
    pid_ = -1;
}

bool VideoPipeline::running() {
    if (pid_ <= 0) return false;
    int status = 0;
    const pid_t r = waitpid(pid_, &status, WNOHANG);
    if (r == 0) return true;
    pid_ = -1;
    return false;
}

bool VideoPipeline::requestIdr() {
    // ffmpeg CLI has no portable per-frame IDR signal. Restart gives an immediate new stream/IDR.
    // The main loop currently treats REQUEST_IDR as a pipeline restart request.
    return pid_ > 0;
}
