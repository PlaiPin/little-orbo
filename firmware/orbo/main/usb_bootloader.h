#pragma once
#include "esp_err.h"

// CDC1 (logs) only: setting 1200 baud requests download mode. The callback
// must only latch the request; motor I/O and reset run in the motor task.
esp_err_t usb_bootloader_init(void (*request)(void));
void usb_bootloader_restart(void) __attribute__((noreturn));
