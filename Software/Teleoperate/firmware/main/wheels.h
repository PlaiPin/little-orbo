/*
 * The two wheel motors (DYNAMIXEL XL430-W250-T, velocity mode).
 * Speeds are in rpm, positive = the motor's forward direction. Mirrored motors are
 * handled by the PC app's "Reverse" boxes (or by the motor's own Drive Mode bit).
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"

esp_err_t wheels_init(void);                              /* set up the bus (once) */
/* Find and prepare both motors. Writes a one-line report; true if both are ready. */
bool wheels_connect(bool torque_on, char *report, size_t cap);
esp_err_t wheels_drive(float left_rpm, float right_rpm);
esp_err_t wheels_torque(bool on);
esp_err_t wheels_read_speed(float *left_rpm, float *right_rpm);
esp_err_t wheels_read_volts(float *volts);
