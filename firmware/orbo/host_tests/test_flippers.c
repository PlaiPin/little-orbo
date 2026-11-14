#include "actuators.h"
#include "driver/ledc.h"
#include <assert.h>
#include <stdio.h>

static int timers,channels,writes,updates,stops;
static int fail_channel=-1,fail_update=-1;
static uint32_t duties[2];
esp_err_t ledc_timer_config(const ledc_timer_config_t *t){
    assert(t->freq_hz==50 && t->duty_resolution==14);timers++;return ESP_OK;
}
esp_err_t ledc_channel_config(const ledc_channel_config_t *c){
    assert(c->gpio_num==(c->channel==0?10:21));assert(c->duty==0);
    channels++;return c->channel==fail_channel?ESP_FAIL:ESP_OK;
}
esp_err_t ledc_set_duty(int mode,ledc_channel_t c,uint32_t duty){
    (void)mode;duties[c]=duty;writes++;return ESP_OK;
}
esp_err_t ledc_update_duty(int mode,ledc_channel_t c){
    (void)mode;updates++;return c==fail_update?ESP_FAIL:ESP_OK;
}
esp_err_t ledc_stop(int mode,ledc_channel_t c,int idle){
    (void)mode;(void)c;assert(idle==0);stops++;return ESP_OK;
}
int main(void){
    uint16_t pulses[2]={1400,1600};
    assert(flippers_set(pulses)==ESP_ERR_INVALID_STATE);
#ifdef TEST_DISABLED
    assert(flippers_init()==ESP_OK);
    assert(flippers_set(pulses)==ESP_ERR_INVALID_STATE);
    flippers_disable();assert(timers==0 && channels==0 && writes==0 && stops==0);
#else
    fail_channel=1;
    assert(flippers_init()==ESP_FAIL && stops==1);
    assert(flippers_set(pulses)==ESP_ERR_INVALID_STATE);
    fail_channel=-1;
    assert(flippers_init()==ESP_OK && channels==4 && timers==2);
    assert(writes==0); // Initialization never drives the servos.
    assert(flippers_set(pulses)==ESP_OK);
    assert(duties[0]==1146 && duties[1]==1310 && updates==2);
    pulses[1]=1700;
    assert(flippers_set(pulses)==ESP_ERR_INVALID_ARG && writes==2);
    pulses[1]=1500;fail_update=1;
    assert(flippers_set(pulses)==ESP_FAIL && stops==3);
    fail_update=-1;
    assert(flippers_set(pulses)==ESP_OK); // Re-enable after output disable.
    flippers_disable();assert(stops==5);
#endif
    puts("flipper GPIO/PWM, bounds and failure assertions passed (host, no hardware)");
}
