#include "control.h"
#include <math.h>
#include <string.h>

void orbo_control_init(orbo_control_t *s, uint64_t boot, double limit) {
    memset(s, 0, sizeof(*s));
    s->boot = boot;
    s->max_velocity = limit;
    s->fault = ORBO_DISARMED;
}
void orbo_disarm(orbo_control_t *s, uint16_t reason) {
    s->armed = false;
    s->wheels_enabled = false;
    s->flipper_active = false;
    s->fault = reason;
    for (int i=0;i<2;i++) {s->velocity[i]=0; s->pulse[i]=s->pulse_rest[i];}
}
bool orbo_fresh(uint64_t issued, uint32_t valid, uint64_t now) {
    // No future timestamps accepted: clock alignment must be qualified first.
    return valid > 0 && valid <= 500 && issued <= now && now-issued < valid;
}
bool orbo_arm(orbo_control_t *s, uint64_t boot, uint64_t epoch, uint64_t seq,
              uint64_t issued, uint32_t valid, uint64_t now, bool wheels, bool ready) {
    if (s->armed || boot != s->boot || epoch <= s->epoch || seq == 0 || !ready ||
        !s->clock_synced || !orbo_fresh(issued, valid, now) || !isfinite(s->max_velocity) || s->max_velocity <= 0)
        return false;
    s->epoch=epoch; s->session_sequence=seq; s->wheel_sequence=0; s->flipper_sequence=0;
    s->wheel_deadline_us=0; s->flipper_deadline_us=0;
    s->armed=true; s->wheels_enabled=wheels; s->fault=ORBO_OK;
    return true;
}
bool orbo_wheels(orbo_control_t *s, uint64_t boot, uint64_t epoch, uint64_t seq,
                 uint64_t command, uint64_t issued, uint32_t valid, uint64_t now,
                 uint64_t mono, const double v[2]) {
    if (!s->armed || !s->wheels_enabled || !s->clock_synced || boot != s->boot || epoch != s->epoch || seq <= s->wheel_sequence ||
        !orbo_fresh(issued, valid, now)) return false;
    for (int i=0;i<2;i++) if (!isfinite(v[i]) || fabs(v[i]) > s->max_velocity) return false;
    s->wheel_sequence=seq; s->wheel_command_id=command;
    s->wheel_deadline_us=mono+(valid-(now-issued))*1000;
    for (int i=0;i<2;i++) s->velocity[i]=v[i];
    return true;
}
bool orbo_flippers(orbo_control_t *s, uint64_t boot, uint64_t epoch, uint64_t seq,
                   uint64_t command, uint64_t issued, uint32_t valid, uint64_t now,
                   uint64_t mono, const uint16_t pulse[2], uint32_t duration) {
    if (!s->armed || !s->clock_synced || s->flipper_active || boot != s->boot || epoch != s->epoch || seq <= s->flipper_sequence ||
        !orbo_fresh(issued, valid, now) || duration == 0 || duration > 5000) return false;
    for (int i=0;i<2;i++) if (s->pulse_min[i] == 0 || s->pulse_max[i] <= s->pulse_min[i] || pulse[i] < s->pulse_min[i] || pulse[i] > s->pulse_max[i]) return false;
    s->flipper_sequence=seq; s->flipper_command_id=command; s->flipper_active=true;
    // Validity bounds admission, duration bounds the non-queued PWM action locally.
    s->flipper_finish_us=mono+(uint64_t)duration*1000;
    s->flipper_deadline_us=s->flipper_finish_us+100000;
    for (int i=0;i<2;i++) s->pulse[i]=pulse[i];
    return true;
}
bool orbo_tick(orbo_control_t *s, uint64_t mono, bool feedback) {
    if (!s->armed) return false;
    if (!s->clock_synced) {orbo_disarm(s, ORBO_CLOCK); return false;}
    if (s->wheels_enabled && !feedback) {orbo_disarm(s, ORBO_FEEDBACK); return false;}
    if (s->wheels_enabled && s->wheel_deadline_us && mono >= s->wheel_deadline_us) {
        orbo_disarm(s, ORBO_EXPIRED); return false;
    }
    if (s->flipper_active && mono >= s->flipper_deadline_us) {orbo_disarm(s, ORBO_EXPIRED);return false;}
    if (s->flipper_active && mono >= s->flipper_finish_us) {
        s->flipper_active=false;
        for (int i=0;i<2;i++) s->pulse[i]=s->pulse_rest[i];
        return true;
    }
    return false;
}
