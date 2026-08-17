#pragma once

#include <cstdint>

class PtzController {
public:
    void configure(int pan_min_cdeg, int pan_max_cdeg,
                   int tilt_min_cdeg, int tilt_max_cdeg,
                   int pan_min_us, int pan_max_us,
                   int tilt_min_us, int tilt_max_us);
    void center();
    void setTarget(int pan_cdeg, int tilt_cdeg, int speed_cdeg_s);
    void update(std::uint32_t dt_ms);

    int panCdeg() const { return pan_cdeg_; }
    int tiltCdeg() const { return tilt_cdeg_; }
    int panPulseUs() const;
    int tiltPulseUs() const;

private:
    int pan_min_cdeg_ = -9000;
    int pan_max_cdeg_ = 9000;
    int tilt_min_cdeg_ = -4500;
    int tilt_max_cdeg_ = 4500;
    int pan_min_us_ = 700;
    int pan_max_us_ = 2300;
    int tilt_min_us_ = 900;
    int tilt_max_us_ = 2100;
    int pan_cdeg_ = 0;
    int tilt_cdeg_ = 0;
    int target_pan_cdeg_ = 0;
    int target_tilt_cdeg_ = 0;
    int speed_cdeg_s_ = 6000;
};
