#include "control.h"
#include <assert.h>
#include <math.h>
#include <stdio.h>

static orbo_control_t ready(void) {
    orbo_control_t s; orbo_control_init(&s, 42, 2.0);
    s.clock_synced=true;
    for(int i=0;i<2;i++){s.pulse_min[i]=1100;s.pulse_max[i]=1800;s.pulse_rest[i]=1450;}
    assert(orbo_arm(&s,42,1,1,1000,100,1000,true,true));
    return s;
}
int main(void) {
    orbo_control_t s=ready(); double v[2]={1,-1}; uint16_t p[2]={1500,1500};
    assert(!orbo_arm(&s,42,2,2,1000,100,1000,true,true));
    assert(!orbo_wheels(&s,41,1,1,1,1000,100,1000,0,v));
    assert(!orbo_wheels(&s,42,0,1,1,1000,100,1000,0,v));
    assert(!orbo_wheels(&s,42,1,1,1,900,100,1000,0,v));
    assert(!orbo_wheels(&s,42,1,1,1,1001,100,1000,0,v));
    v[0]=NAN; assert(!orbo_wheels(&s,42,1,1,1,1000,100,1000,0,v));
    v[0]=3; assert(!orbo_wheels(&s,42,1,1,1,1000,100,1000,0,v));
    v[0]=1; assert(orbo_wheels(&s,42,1,1,1,1000,100,1050,0,v));
    assert(!orbo_wheels(&s,42,1,1,1,1000,100,1050,0,v));
    assert(s.wheel_deadline_us==50000); // Original age is deducted.
    orbo_tick(&s,50000,true); assert(!s.armed && s.velocity[0]==0 && s.fault==ORBO_EXPIRED);
    assert(!orbo_arm(&s,42,1,2,1100,100,1100,true,true));
    assert(orbo_arm(&s,42,2,2,1100,100,1100,true,true));
    orbo_tick(&s,60000,false); assert(!s.armed && s.fault==ORBO_FEEDBACK);
    s=ready(); assert(orbo_flippers(&s,42,1,1,5,1000,100,1000,0,p,1000));
    assert(!orbo_tick(&s,999999,true)); assert(orbo_tick(&s,1000000,true));
    assert(s.pulse[0]==1450 && !s.flipper_active);
    s=ready(); p[0]=2000; assert(!orbo_flippers(&s,42,1,1,5,1000,100,1000,0,p,1000));
    s=ready(); s.clock_synced=false; orbo_tick(&s,1,true);assert(!s.armed);
    // A flipper-only session needs no wheel feedback and rejects wheel commands.
    s=ready();orbo_disarm(&s,ORBO_DISARMED);
    assert(orbo_arm(&s,42,2,2,1000,100,1000,false,true));
    assert(!orbo_wheels(&s,42,2,1,1,1000,100,1000,0,v));
    p[0]=1500;
    assert(orbo_flippers(&s,42,2,1,5,1000,100,1000,0,p,500));
    assert(!orbo_flippers(&s,42,2,2,6,1000,100,1000,0,p,500));
    assert(!orbo_tick(&s,499999,false) && s.armed);
    assert(orbo_tick(&s,500000,false) && s.pulse[0]==1450);
    assert(!orbo_flippers(&s,42,2,1,5,1000,100,1000,0,p,500));
    assert(orbo_flippers(&s,42,2,2,6,1000,100,1000,500000,p,500));
    orbo_tick(&s,1100000,false);assert(!s.armed && s.fault==ORBO_EXPIRED);
    s=ready();s.pulse_min[0]=0;
    assert(!orbo_flippers(&s,42,1,1,5,1000,100,1000,0,p,500));
    // Both command streams share one arm, but retain independent sequences.
    s=ready();p[0]=1500;
    assert(orbo_flippers(&s,42,1,1,8,1000,100,1000,0,p,300));
    for(int tick=0;tick<=8;tick++){
        uint64_t ms=1000+(uint64_t)tick*50,us=(uint64_t)tick*50000;
        assert(orbo_wheels(&s,42,1,(uint64_t)tick+1,9,ms,100,ms,us,v));
        assert(orbo_tick(&s,us,true)==(tick==6));
        assert(s.armed && s.wheels_enabled && s.velocity[0]==v[0]);
    }
    assert(!s.flipper_active && s.pulse[0]==1450 && s.wheel_sequence==9);
    assert(orbo_flippers(&s,42,1,2,10,1400,100,1400,400000,p,500));
    assert(!orbo_tick(&s,500000,true)); // Wheel expiry cancels the unfinished gesture.
    assert(!s.armed && !s.flipper_active && s.velocity[0]==0 && s.fault==ORBO_EXPIRED);
    puts("embedded admission/expiry/recovery assertions passed (host, no hardware)");
}
