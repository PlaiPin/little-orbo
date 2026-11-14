#pragma once
#include <stdint.h>
#include "esp_err.h"

// Initialize the configured bus. The 115200-baud profile can migrate known
// XL430s from the former 1 Mbps profile, with verified torque-off first.
esp_err_t wheel_bus_init(const uint8_t ids[2]);
