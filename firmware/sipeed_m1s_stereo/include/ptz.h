#pragma once

#include <stdint.h>

typedef struct {
    int32_t pan_cdeg;
    int32_t tilt_cdeg;
    int32_t target_pan_cdeg;
    int32_t target_tilt_cdeg;
    uint16_t speed_cdeg_s;
} ptz_state_t;

void ptz_init(ptz_state_t *s);
void ptz_set_target(ptz_state_t *s, int32_t pan_cdeg, int32_t tilt_cdeg, uint16_t speed_cdeg_s);
void ptz_center(ptz_state_t *s);
void ptz_update(ptz_state_t *s, uint32_t dt_ms);
uint16_t ptz_pan_to_us(const ptz_state_t *s);
uint16_t ptz_tilt_to_us(const ptz_state_t *s);
