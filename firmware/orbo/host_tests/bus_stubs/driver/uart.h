#pragma once
#include "esp_err.h"
typedef enum { UART_NUM_0, UART_NUM_1, UART_NUM_2, UART_NUM_MAX } uart_port_t;
esp_err_t uart_set_baudrate(uart_port_t port, unsigned baud);
