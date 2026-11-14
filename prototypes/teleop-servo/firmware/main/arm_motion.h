#pragma once
#include <stdbool.h>
#include <stdint.h>

enum { ARM_OFF, ARM_REST, ARM_SWAY, ARM_PADDLE, ARM_WAVE, ARM_HAPPY, ARM_MANUAL };
typedef struct {
    int low[2], center[2], high[2], sign[2];
} arm_calibration_t;
typedef struct {
    arm_calibration_t calibration;
    bool configured, enabled;
    int mode, tempo, range, manual[2];
    int64_t last_command_ms;
    float phase, position[2];
} arm_motion_t;
bool arm_configure(arm_motion_t *s, const arm_calibration_t *c);
bool arm_command(arm_motion_t *s, int mode, int tempo, int range, int left, int right, int64_t now);
void arm_step(arm_motion_t *s, int64_t now, float dt);
