#include "actuators.h"
#include "dxl_bus.h"
#include "driver/ledc.h"
#include "esp_timer.h"
#include <math.h>
#include <string.h>

static const uint8_t ids[2]={CONFIG_ORBO_LEFT_ID,CONFIG_ORBO_RIGHT_ID};
static const int signs[2]={CONFIG_ORBO_LEFT_SIGN,CONFIG_ORBO_RIGHT_SIGN};
static bool pwm_ready=false, wheel_torque=false;
static bool previous_valid[2]={false,false};
static double previous_position[2];
static uint64_t previous_time[2];
static const double TAU=6.283185307179586;
static esp_err_t byte_write(uint8_t id, uint16_t reg, uint8_t v){return dxl_write(id,reg,&v,1);}
static esp_err_t word_write(uint8_t id, uint16_t reg, int32_t v){
    uint32_t u=(uint32_t)v;
    uint8_t bytes[4]={u,u>>8,u>>16,u>>24};return dxl_write(id,reg,bytes,4);
}
static int32_t signed_word(const uint8_t *b){
    uint32_t u=(uint32_t)b[0]|((uint32_t)b[1]<<8)|((uint32_t)b[2]<<16)|((uint32_t)b[3]<<24);
    int32_t value;memcpy(&value,&u,4);return value;
}
esp_err_t actuators_init(void){
    esp_err_t result=dxl_bus_init(UART_NUM_1,CONFIG_ORBO_DXL_TX,CONFIG_ORBO_DXL_RX,CONFIG_ORBO_DXL_BAUD);
    if(result!=ESP_OK)return result;
    // Startup never enables wheel torque; legacy UDP and text-command code is not linked.
    wheels_disable();
#ifdef CONFIG_ORBO_FLIPPERS_CALIBRATED
    if(CONFIG_ORBO_FLIPPER_LEFT_GPIO<0 || CONFIG_ORBO_FLIPPER_RIGHT_GPIO<0 ||
       CONFIG_ORBO_FLIPPER_MIN_US<=0 || CONFIG_ORBO_FLIPPER_MAX_US>=20000 ||
       CONFIG_ORBO_FLIPPER_REST_US<CONFIG_ORBO_FLIPPER_MIN_US ||
       CONFIG_ORBO_FLIPPER_REST_US>CONFIG_ORBO_FLIPPER_MAX_US)return ESP_ERR_INVALID_ARG;
    ledc_timer_config_t timer={.speed_mode=LEDC_LOW_SPEED_MODE,.duty_resolution=LEDC_TIMER_14_BIT,
        .timer_num=LEDC_TIMER_0,.freq_hz=50,.clk_cfg=LEDC_AUTO_CLK};
    result=ledc_timer_config(&timer);if(result!=ESP_OK)return result;
    const int pins[2]={CONFIG_ORBO_FLIPPER_LEFT_GPIO,CONFIG_ORBO_FLIPPER_RIGHT_GPIO};
    for(int i=0;i<2;i++){
        ledc_channel_config_t channel={.gpio_num=pins[i],.speed_mode=LEDC_LOW_SPEED_MODE,
            .channel=(ledc_channel_t)i,.timer_sel=LEDC_TIMER_0,.duty=0,.hpoint=0};
        result=ledc_channel_config(&channel);if(result!=ESP_OK)return result;
    }
    pwm_ready=true;
#endif
    return ESP_OK;
}
esp_err_t wheels_prepare(bool enable){
    wheels_disable();
#ifndef CONFIG_ORBO_WHEELS_CALIBRATED
    if(enable)return ESP_ERR_INVALID_STATE;
#endif
    for(int i=0;i<2;i++){
        uint16_t model=0;uint8_t error=0;
        esp_err_t e=dxl_ping(ids[i],&model);
        if(e!=ESP_OK || model!=1060)return ESP_ERR_INVALID_RESPONSE;
        e=dxl_read(ids[i],70,&error,1);if(e!=ESP_OK || error)return ESP_FAIL;
        // No reboot/automatic hardware-error clearance. An owner must inspect faults.
        e=byte_write(ids[i],11,1);if(e!=ESP_OK)return e;
        e=byte_write(ids[i],10,0);if(e!=ESP_OK)return e; // Sign conversion occurs here, once.
        e=byte_write(ids[i],98,0);if(e!=ESP_OK)return e; // Explicit rearm clears bus watchdog latch.
        e=word_write(ids[i],104,0);if(e!=ESP_OK)return e;
        e=word_write(ids[i],108,CONFIG_ORBO_PROFILE_ACCEL);if(e!=ESP_OK)return e;
        e=byte_write(ids[i],98,5);if(e!=ESP_OK)return e; // 5 * 20ms; separate from command expiry.
        previous_valid[i]=false;
    }
    if(enable){
        for(int i=0;i<2;i++){
            esp_err_t e=byte_write(ids[i],64,1);
            if(e!=ESP_OK){wheels_disable();return e;}
        }
        wheel_torque=true;
    }
    return ESP_OK;
}
esp_err_t wheels_set(const double v[2]){
    uint8_t data[8];
    for(int i=0;i<2;i++){
        if(!isfinite(v[i]) || fabs(v[i])>CONFIG_ORBO_MAX_MRAD_S/1000.0)return ESP_ERR_INVALID_ARG;
        int32_t raw=(int32_t)llround(v[i]*signs[i]*60.0/(TAU*0.229));
        uint32_t u=(uint32_t)raw;
        for(int b=0;b<4;b++)data[i*4+b]=(uint8_t)(u>>(8*b));
    }
    // A successful broadcast never modifies feedback health/read-error counters.
    return dxl_sync_write(104,4,ids,2,data);
}
void wheels_disable(void){
    const double zero[2]={0,0};(void)wheels_set(zero);
    for(int i=0;i<2;i++)(void)byte_write(ids[i],64,0);
    wheel_torque=false;
}
void wheels_sample(orbo_feedback_t *state){
    for(int i=0;i<2;i++){
        uint8_t values[18],health[7],watchdog=0;
        state->valid[i]=false;
        esp_err_t e=dxl_read(ids[i],128,values,18);
        if(e==ESP_OK)e=dxl_read(ids[i],64,health,7);
        if(e==ESP_OK)e=dxl_read(ids[i],98,&watchdog,1);
        uint64_t now=esp_timer_get_time();
        if(e!=ESP_OK){state->read_errors[i]++;continue;}
        state->hardware_error[i]=health[6];
        if(health[6] || watchdog==255 || (wheel_torque && health[0]!=1)){
            state->read_errors[i]++;previous_valid[i]=false;continue;
        }
        double pos=signed_word(values+4)*(TAU/4096.0)*signs[i];
        double vel=signed_word(values)*(0.229*TAU/60.0)*signs[i];
        if(previous_valid[i] && fabs(pos-previous_position[i]) >
           CONFIG_ORBO_MAX_MRAD_S/1000.0*(now-previous_time[i])/1000000.0+0.1){
            // Encoder reset/discontinuity: retained invalid until explicit prepare/rearm.
            state->read_errors[i]++;continue;
        }
        state->position[i]=pos;state->velocity[i]=vel;
        state->voltage[i]=(values[16]|((uint16_t)values[17]<<8))/10.0f;
        state->sample_us[i]=now;state->valid[i]=true;
        previous_position[i]=pos;previous_time[i]=now;previous_valid[i]=true;
    }
}
esp_err_t flippers_set(const uint16_t p[2]){
#ifndef CONFIG_ORBO_FLIPPERS_CALIBRATED
    (void)p;return ESP_ERR_INVALID_STATE;
#else
    if(!pwm_ready)return ESP_ERR_INVALID_STATE;
    for(int i=0;i<2;i++){
        if(p[i]<CONFIG_ORBO_FLIPPER_MIN_US || p[i]>CONFIG_ORBO_FLIPPER_MAX_US)return ESP_ERR_INVALID_ARG;
        uint32_t duty=(uint32_t)p[i]*16384/20000;
        esp_err_t e=ledc_set_duty(LEDC_LOW_SPEED_MODE,(ledc_channel_t)i,duty);
        if(e==ESP_OK)e=ledc_update_duty(LEDC_LOW_SPEED_MODE,(ledc_channel_t)i);
        if(e!=ESP_OK)return e;
    }return ESP_OK;
#endif
}
void flippers_disable(void){
    if(pwm_ready)for(int i=0;i<2;i++)ledc_stop(LEDC_LOW_SPEED_MODE,(ledc_channel_t)i,0);
}
