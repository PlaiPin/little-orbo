#include "arm_motion.h"
#include <math.h>

bool arm_configure(arm_motion_t *s, const arm_calibration_t *c)
{
    if (s->enabled) return false;
    for (int i = 0; i < 2; i++) {
        if (c->low[i] < 500 || c->high[i] > 2500 ||
            c->low[i] >= c->center[i] || c->center[i] >= c->high[i] ||
            (c->sign[i] != 1 && c->sign[i] != -1)) return false;
    }
    s->calibration = *c;
    s->configured = true;
    return true;
}

bool arm_command(arm_motion_t *s, int mode, int tempo, int range, int left, int right, int64_t now)
{
    if (mode < ARM_OFF || mode > ARM_MANUAL || tempo < 20 || tempo > 150 ||
        range < 0 || range > 100 || left < -100 || left > 100 || right < -100 || right > 100)
        return false;
    if (mode && !s->configured) return false;
    if (mode && !s->enabled) {
        s->phase = 0;
        for (int i = 0; i < 2; i++) s->position[i] = s->calibration.center[i];
    }
    s->enabled = mode != ARM_OFF;
    s->mode = mode;
    s->tempo = tempo;
    s->range = range;
    s->manual[0] = left;
    s->manual[1] = right;
    s->last_command_ms = now;
    return true;
}

void arm_step(arm_motion_t *s, int64_t now, float dt)
{
    if (!s->enabled) return;
    if (now - s->last_command_ms >= 500) {
        s->enabled = false;
        s->mode = ARM_OFF;
        return;
    }
    /* Bound catch-up after scheduling delays; never jump to a distant target. */
    dt = fmaxf(0, fminf(dt, 0.04f));
    s->phase = fmodf(s->phase + dt * (s->tempo / 60.0f) * 3.14159265f, 6.28318531f);
    float wave = sinf(s->phase), v[2] = {0, 0};
    switch (s->mode) {
    case ARM_SWAY: v[0] = v[1] = wave; break;
    case ARM_PADDLE: v[0] = wave; v[1] = -wave; break;
    case ARM_WAVE: v[1] = 0.35f + 0.65f * wave; break;
    case ARM_HAPPY: v[0] = wave; v[1] = sinf(s->phase + 0.65f); break;
    case ARM_MANUAL: v[0] = s->manual[0] / 100.0f; v[1] = s->manual[1] / 100.0f; break;
    default: break;
    }
    for (int i = 0; i < 2; i++) {
        const arm_calibration_t *c = &s->calibration;
        float x = v[i] * s->range / 100.0f * c->sign[i];
        float target = c->center[i] + x * (x >= 0 ? c->high[i] - c->center[i] : c->center[i] - c->low[i]);
        float step = (100.0f + 2.0f * s->tempo) * dt;
        s->position[i] += fmaxf(-step, fminf(step, target - s->position[i]));
        s->position[i] = fmaxf(c->low[i], fminf(c->high[i], s->position[i]));
    }
}
