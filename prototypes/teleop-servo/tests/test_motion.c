#include "arm_motion.h"
#include <assert.h>
#include <math.h>
#include <stdio.h>

int main(void)
{
    arm_motion_t s = {0};
    arm_calibration_t c = {{1400, 1420}, {1500, 1510}, {1600, 1620}, {1, -1}};
    assert(!arm_command(&s, ARM_SWAY, 50, 35, 0, 0, 0));
    assert(arm_configure(&s, &c));
    arm_calibration_t bad = c;
    bad.high[1] = 1500;
    assert(!arm_configure(&s, &bad));
    assert(s.calibration.high[1] == 1620); /* Rejection is atomic. */
    for (int mode = ARM_REST; mode <= ARM_MANUAL; mode++) {
        assert(arm_command(&s, mode, 150, 100, 100, -100, 0));
        assert(!arm_configure(&s, &c));
        for (int t = 20; t <= 10000; t += 20) {
            float before[2] = {s.position[0], s.position[1]};
            assert(arm_command(&s, mode, 150, 100, 100, -100, t));
            arm_step(&s, t, 0.02f);
            for (int i = 0; i < 2; i++) {
                assert(s.position[i] >= c.low[i] && s.position[i] <= c.high[i]);
                assert(fabsf(s.position[i] - before[i]) <= 8.01f);
            }
        }
        arm_step(&s, 10499, 0.02f);
        assert(s.enabled);
        arm_step(&s, 10500, 0.02f);
        assert(!s.enabled && s.mode == ARM_OFF);
    }
    assert(!arm_command(&s, ARM_MANUAL, 50, 101, 0, 0, 0));
    assert(!arm_command(&s, ARM_MANUAL, 50, 50, -101, 0, 0));
    assert(!arm_command(&s, 7, 50, 50, 0, 0, 0));
    assert(arm_command(&s, ARM_MANUAL, 50, 100, 100, 100, 0));
    for (int t = 20; t <= 400; t += 20) arm_step(&s, t, 0.02f);
    assert(s.position[0] > c.center[0] && s.position[1] < c.center[1]);
    assert(arm_command(&s, ARM_OFF, 50, 0, 0, 0, 401));
    assert(!s.enabled);
    puts("PASS: all patterns bounded, slew limit, calibration validation, reversal, watchdog, stop");
    return 0;
}
