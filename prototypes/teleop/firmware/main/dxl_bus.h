/*
 * DYNAMIXEL Protocol 2.0 on a single wire.
 *
 * Circuit: the ESP32's TX and RX pins are joined and go through a BSS138 level shifter to the
 * motors' DATA line. TX runs open-drain, so it lets go of the wire after sending and the motor
 * can answer on the same wire. RX hears our own packet first ("echo"); it is checked and dropped.
 *
 * Every call returns:
 *   ESP_OK                    done (and the motor's reply was good)
 *   ESP_ERR_INVALID_RESPONSE  our own packet did not come back (wiring / TX-RX jumper / level shifter)
 *   ESP_ERR_TIMEOUT           no reply from the motor (battery off, wrong ID or baud rate)
 *   ESP_ERR_INVALID_CRC       reply damaged
 *   ESP_ERR_INVALID_SIZE      reply has the wrong length or comes from the wrong motor
 *   ESP_FAIL                  the motor answered with an error (e.g. value out of range)
 */
#pragma once

#include <stdint.h>
#include "esp_err.h"
#include "driver/uart.h"

#define DXL_BROADCAST 0xFE

esp_err_t dxl_bus_init(uart_port_t port, int tx_gpio, int rx_gpio, int baud);
esp_err_t dxl_ping(uint8_t id, uint16_t *model);
esp_err_t dxl_read(uint8_t id, uint16_t addr, uint8_t *out, uint16_t len);
esp_err_t dxl_write(uint8_t id, uint16_t addr, const uint8_t *data, uint16_t len);
esp_err_t dxl_reboot(uint8_t id);
/* One packet for several motors; `data` holds `count` blocks of `len` bytes in the order of `ids`. No reply. */
esp_err_t dxl_sync_write(uint16_t addr, uint16_t len, const uint8_t *ids, int count, const uint8_t *data);
