#pragma once
#include <stdbool.h>
#include <stdint.h>

enum { ORBO_OK=0, ORBO_DISARMED=1, ORBO_EXPIRED=2, ORBO_STALE=3,
       ORBO_BOUNDS=4, ORBO_FEEDBACK=5, ORBO_BUS=6, ORBO_CLOCK=7 };
typedef struct {
    uint64_t boot, epoch, session_sequence, wheel_sequence, flipper_sequence;
    uint64_t wheel_deadline_us, flipper_deadline_us, flipper_finish_us;
    uint64_t wheel_command_id, flipper_command_id;
    bool armed, wheels_enabled, clock_synced, flipper_active;
    uint16_t fault;
    double max_velocity, velocity[2];
    uint16_t pulse_min[2], pulse_max[2], pulse_rest[2], pulse[2];
} orbo_control_t;

void orbo_control_init(orbo_control_t *, uint64_t boot, double max_velocity);
void orbo_disarm(orbo_control_t *, uint16_t reason);
bool orbo_fresh(uint64_t issued_ms, uint32_t valid_ms, uint64_t now_ms);
bool orbo_arm(orbo_control_t *, uint64_t boot, uint64_t epoch, uint64_t seq,
              uint64_t issued_ms, uint32_t valid_ms, uint64_t now_ms,
              bool wheels, bool hardware_ready);
bool orbo_wheels(orbo_control_t *, uint64_t boot, uint64_t epoch, uint64_t seq,
                 uint64_t command, uint64_t issued_ms, uint32_t valid_ms,
                 uint64_t now_ms, uint64_t mono_us, const double velocity[2]);
bool orbo_flippers(orbo_control_t *, uint64_t boot, uint64_t epoch, uint64_t seq,
                   uint64_t command, uint64_t issued_ms, uint32_t valid_ms,
                   uint64_t now_ms, uint64_t mono_us, const uint16_t pulse[2],
                   uint32_t duration_ms);
// Called by the control task independently of the transport/executor.
// Returns true only when a PWM timer completed, not when observed motion completed.
bool orbo_tick(orbo_control_t *, uint64_t mono_us, bool feedback_fresh);
