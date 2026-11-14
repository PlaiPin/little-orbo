#pragma once
#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"

typedef struct {
    double position[2], velocity[2];
    float voltage[2];
    uint64_t sample_us[2];
    uint32_t read_errors[2];
    uint8_t hardware_error[2];
    bool valid[2];
} orbo_feedback_t;
esp_err_t actuators_init(void);
esp_err_t wheels_prepare(bool enable);
esp_err_t wheels_set(const double velocity[2]);
void wheels_disable(void);
void wheels_sample(orbo_feedback_t *);
esp_err_t flippers_set(const uint16_t pulse[2]);
void flippers_disable(void);
