#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "sdkconfig.h"
#include "esp_err.h"
typedef struct {
    const void *device_descriptor, *string_descriptor, *configuration_descriptor;
    bool external_phy;
} tinyusb_config_t;
esp_err_t tinyusb_driver_install(const tinyusb_config_t *config);
