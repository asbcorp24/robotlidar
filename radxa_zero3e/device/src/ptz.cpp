#include "ptz.hpp"
#include <algorithm>
#include <cstdint>

namespace {
int mapValue(int v, int in_min, int in_max, int out_min, int out_max) {
    v = std::clamp(v, in_min, in_max);
    const std::int64_t num = static_cast<std::int64_t>(v - in_min) * (out_max - out_min);
    return out_min + static_cast<int>(num / (in_max - in_min));
}
int stepTo(int current, int target, int step) {
    if (current < target) return std::min(current + step, target);
    if (current > target) return std::max(current - step, target);
    return current;
}
}

void PtzController::configure(int pan_min_cdeg, int pan_max_cdeg,
                              int tilt_min_cdeg, int tilt_max_cdeg,
                              int pan_min_us, int pan_max_us,
                              int tilt_min_us, int tilt_max_us) {
    pan_min_cdeg_ = pan_min_cdeg; pan_max_cdeg_ = pan_max_cdeg;
    tilt_min_cdeg_ = tilt_min_cdeg; tilt_max_cdeg_ = tilt_max_cdeg;
    pan_min_us_ = pan_min_us; pan_max_us_ = pan_max_us;
    tilt_min_us_ = tilt_min_us; tilt_max_us_ = tilt_max_us;
    center();
}
void PtzController::center() { target_pan_cdeg_ = 0; target_tilt_cdeg_ = 0; }
void PtzController::setTarget(int pan_cdeg, int tilt_cdeg, int speed_cdeg_s) {
    target_pan_cdeg_ = std::clamp(pan_cdeg, pan_min_cdeg_, pan_max_cdeg_);
    target_tilt_cdeg_ = std::clamp(tilt_cdeg, tilt_min_cdeg_, tilt_max_cdeg_);
    speed_cdeg_s_ = std::max(speed_cdeg_s, 100);
}
void PtzController::update(std::uint32_t dt_ms) {
    int step = static_cast<int>((static_cast<std::int64_t>(speed_cdeg_s_) * dt_ms) / 1000);
    step = std::max(step, 1);
    pan_cdeg_ = stepTo(pan_cdeg_, target_pan_cdeg_, step);
    tilt_cdeg_ = stepTo(tilt_cdeg_, target_tilt_cdeg_, step);
}
int PtzController::panPulseUs() const { return mapValue(pan_cdeg_, pan_min_cdeg_, pan_max_cdeg_, pan_min_us_, pan_max_us_); }
int PtzController::tiltPulseUs() const { return mapValue(tilt_cdeg_, tilt_min_cdeg_, tilt_max_cdeg_, tilt_min_us_, tilt_max_us_); }
