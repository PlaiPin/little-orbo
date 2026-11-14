#pragma once
#include <stdint.h>
#include "esp_err.h"
typedef int ledc_channel_t;
enum {LEDC_LOW_SPEED_MODE=0, LEDC_TIMER_14_BIT=14, LEDC_TIMER_0=0, LEDC_AUTO_CLK=0};
typedef struct {int speed_mode,duty_resolution,timer_num,freq_hz,clk_cfg;} ledc_timer_config_t;
typedef struct {int gpio_num,speed_mode,channel,timer_sel; uint32_t duty,hpoint;} ledc_channel_config_t;
esp_err_t ledc_timer_config(const ledc_timer_config_t *);
esp_err_t ledc_channel_config(const ledc_channel_config_t *);
esp_err_t ledc_set_duty(int,ledc_channel_t,uint32_t);
esp_err_t ledc_update_duty(int,ledc_channel_t);
esp_err_t ledc_stop(int,ledc_channel_t,int);
