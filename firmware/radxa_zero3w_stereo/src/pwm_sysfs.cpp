#include "pwm_sysfs.hpp"

#include <chrono>
#include <filesystem>
#include <fstream>
#include <thread>

PwmChannel::~PwmChannel() {
    disable();
}

bool PwmChannel::writeFile(const std::string& path, const std::string& value) const {
    std::ofstream f(path);
    if (!f) return false;
    f << value;
    return static_cast<bool>(f);
}

bool PwmChannel::open(const std::string& pwmchip_path, int channel, int period_ns) {
    chip_ = pwmchip_path;
    channel_ = channel;
    period_ns_ = period_ns;
    pwm_ = chip_ + "/pwm" + std::to_string(channel_);

    if (!std::filesystem::exists(pwm_)) {
        if (!writeFile(chip_ + "/export", std::to_string(channel_))) return false;
        for (int i = 0; i < 50 && !std::filesystem::exists(pwm_); ++i)
            std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    if (!std::filesystem::exists(pwm_)) return false;

    writeFile(pwm_ + "/enable", "0");
    if (!writeFile(pwm_ + "/period", std::to_string(period_ns_))) return false;
    if (!writeFile(pwm_ + "/duty_cycle", "1500000")) return false;
    if (!writeFile(pwm_ + "/enable", "1")) return false;

    ready_ = true;
    return true;
}

bool PwmChannel::setPulseUs(int pulse_us) {
    if (!ready_) return false;
    const int duty_ns = pulse_us * 1000;
    if (duty_ns <= 0 || duty_ns >= period_ns_) return false;
    return writeFile(pwm_ + "/duty_cycle", std::to_string(duty_ns));
}

void PwmChannel::disable() {
    if (!ready_) return;
    writeFile(pwm_ + "/enable", "0");
    ready_ = false;
}
