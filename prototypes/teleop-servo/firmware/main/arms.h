#pragma once
#include <stdbool.h>
#include <stddef.h>
#include "esp_err.h"
esp_err_t arms_init(void);
bool arms_command(const char *line, char *reply, size_t cap);
void arms_status(char *line, size_t cap);
