#pragma once

#include <string>

class PwmChannel {
public:
    PwmChannel() = default;
    ~PwmChannel();

    bool open(const std::string& pwmchip_path, int channel, int period_ns = 20000000);
    bool setPulseUs(int pulse_us);
    void disable();
    bool valid() const { return ready_; }

private:
    bool writeFile(const std::string& path, const std::string& value) const;

    std::string chip_;
    std::string pwm_;
    int channel_ = -1;
    int period_ns_ = 20000000;
    bool ready_ = false;
};
