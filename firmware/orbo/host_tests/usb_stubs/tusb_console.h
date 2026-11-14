#pragma once
#include "esp_err.h"
esp_err_t esp_tusb_init_console(int port);
esp_err_t esp_tusb_deinit_console(int port);
