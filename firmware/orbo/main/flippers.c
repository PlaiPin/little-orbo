#include "sdkconfig.h"
#include "actuators.h"
#include "driver/gpio.h"
#include "driver/ledc.h"

static bool pwm_ready=false;

esp_err_t flippers_init(void){
#ifdef CONFIG_ORBO_FLIPPERS_CALIBRATED
    const int pins[2]={CONFIG_ORBO_FLIPPER_LEFT_GPIO,CONFIG_ORBO_FLIPPER_RIGHT_GPIO};
    if(pins[0]==pins[1] || CONFIG_ORBO_FLIPPER_MIN_US<=0 ||
       CONFIG_ORBO_FLIPPER_MAX_US<=CONFIG_ORBO_FLIPPER_MIN_US ||
       CONFIG_ORBO_FLIPPER_MAX_US>=20000 ||
       CONFIG_ORBO_FLIPPER_REST_US<CONFIG_ORBO_FLIPPER_MIN_US ||
       CONFIG_ORBO_FLIPPER_REST_US>CONFIG_ORBO_FLIPPER_MAX_US)return ESP_ERR_INVALID_ARG;
    for(int i=0;i<2;i++){
        if(!GPIO_IS_VALID_OUTPUT_GPIO(pins[i]) || pins[i]==19 || pins[i]==20 ||
           pins[i]==CONFIG_ORBO_DXL_TX || pins[i]==CONFIG_ORBO_DXL_RX)
            return ESP_ERR_INVALID_ARG;
    }
    ledc_timer_config_t timer={.speed_mode=LEDC_LOW_SPEED_MODE,.duty_resolution=LEDC_TIMER_14_BIT,
        .timer_num=LEDC_TIMER_0,.freq_hz=50,.clk_cfg=LEDC_AUTO_CLK};
    esp_err_t result=ledc_timer_config(&timer);
    if(result!=ESP_OK)return result;
    for(int i=0;i<2;i++){
        ledc_channel_config_t channel={.gpio_num=pins[i],.speed_mode=LEDC_LOW_SPEED_MODE,
            .channel=(ledc_channel_t)i,.timer_sel=LEDC_TIMER_0,.duty=0,.hpoint=0};
        result=ledc_channel_config(&channel);
        if(result!=ESP_OK){
            for(int j=0;j<i;j++)ledc_stop(LEDC_LOW_SPEED_MODE,(ledc_channel_t)j,0);
            return result;
        }
    }
    pwm_ready=true;
#endif
    return ESP_OK;
}

esp_err_t flippers_set(const uint16_t pulse[2]){
#ifndef CONFIG_ORBO_FLIPPERS_CALIBRATED
    (void)pulse;return ESP_ERR_INVALID_STATE;
#else
    if(!pwm_ready)return ESP_ERR_INVALID_STATE;
    // Validate both targets before changing either output.
    for(int i=0;i<2;i++){
        if(pulse[i]<CONFIG_ORBO_FLIPPER_MIN_US || pulse[i]>CONFIG_ORBO_FLIPPER_MAX_US)
            return ESP_ERR_INVALID_ARG;
    }
    for(int i=0;i<2;i++){
        uint32_t duty=(uint32_t)pulse[i]*16384/20000;
        esp_err_t result=ledc_set_duty(LEDC_LOW_SPEED_MODE,(ledc_channel_t)i,duty);
        if(result==ESP_OK)result=ledc_update_duty(LEDC_LOW_SPEED_MODE,(ledc_channel_t)i);
        if(result!=ESP_OK){flippers_disable();return result;}
    }
    return ESP_OK;
#endif
}

void flippers_disable(void){
    if(pwm_ready)for(int i=0;i<2;i++)ledc_stop(LEDC_LOW_SPEED_MODE,(ledc_channel_t)i,0);
}
