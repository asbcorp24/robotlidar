#include "ptz.h"
#include "app_config.h"

static int32_t clamp32(int32_t v,int32_t lo,int32_t hi){return v<lo?lo:(v>hi?hi:v);} 
static int32_t step_to(int32_t cur,int32_t dst,int32_t step){
    if(cur<dst){cur+=step; if(cur>dst)cur=dst;}
    else if(cur>dst){cur-=step; if(cur<dst)cur=dst;}
    return cur;
}
static uint16_t map_us(int32_t v,int32_t lo,int32_t hi,uint16_t us_lo,uint16_t us_hi){
    v=clamp32(v,lo,hi);
    return (uint16_t)(us_lo + ((int64_t)(v-lo)*(us_hi-us_lo))/(hi-lo));
}
void ptz_init(ptz_state_t *s){
    s->pan_cdeg=APP_PAN_CENTER_CDEG; s->tilt_cdeg=APP_TILT_CENTER_CDEG;
    s->target_pan_cdeg=s->pan_cdeg; s->target_tilt_cdeg=s->tilt_cdeg;
    s->speed_cdeg_s=APP_PTZ_DEFAULT_SPEED_CDEG_S;
}
void ptz_set_target(ptz_state_t *s,int32_t pan,int32_t tilt,uint16_t speed){
    s->target_pan_cdeg=clamp32(pan,APP_PAN_MIN_CDEG,APP_PAN_MAX_CDEG);
    s->target_tilt_cdeg=clamp32(tilt,APP_TILT_MIN_CDEG,APP_TILT_MAX_CDEG);
    s->speed_cdeg_s=speed?speed:APP_PTZ_DEFAULT_SPEED_CDEG_S;
}
void ptz_center(ptz_state_t *s){ptz_set_target(s,APP_PAN_CENTER_CDEG,APP_TILT_CENTER_CDEG,s->speed_cdeg_s);} 
void ptz_update(ptz_state_t *s,uint32_t dt_ms){
    int32_t step=(int32_t)(((uint32_t)s->speed_cdeg_s*dt_ms)/1000u); if(step<1)step=1;
    s->pan_cdeg=step_to(s->pan_cdeg,s->target_pan_cdeg,step);
    s->tilt_cdeg=step_to(s->tilt_cdeg,s->target_tilt_cdeg,step);
}
uint16_t ptz_pan_to_us(const ptz_state_t *s){return map_us(s->pan_cdeg,APP_PAN_MIN_CDEG,APP_PAN_MAX_CDEG,APP_SERVO_PAN_MIN_US,APP_SERVO_PAN_MAX_US);} 
uint16_t ptz_tilt_to_us(const ptz_state_t *s){return map_us(s->tilt_cdeg,APP_TILT_MIN_CDEG,APP_TILT_MAX_CDEG,APP_SERVO_TILT_MIN_US,APP_SERVO_TILT_MAX_US);} 
