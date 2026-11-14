#pragma once
#include "tinyusb.h"
typedef enum { TINYUSB_CDC_ACM_0, TINYUSB_CDC_ACM_1 } tinyusb_cdcacm_itf_t;
#define TINYUSB_USBDEV_0 0
typedef struct {
    int usb_dev;
    tinyusb_cdcacm_itf_t cdc_port;
    size_t rx_unread_buf_sz;
    void *callback_rx, *callback_rx_wanted_char;
    void *callback_line_state_changed, *callback_line_coding_changed;
} tinyusb_config_cdcacm_t;
esp_err_t tusb_cdc_acm_init(const tinyusb_config_cdcacm_t *config);
esp_err_t tusb_cdc_acm_deinit(int port);
bool tusb_cdc_acm_initialized(tinyusb_cdcacm_itf_t port);
size_t tinyusb_cdcacm_write_queue(tinyusb_cdcacm_itf_t port, const uint8_t *buf, size_t len);
esp_err_t tinyusb_cdcacm_write_flush(tinyusb_cdcacm_itf_t port, uint32_t timeout);
esp_err_t tinyusb_cdcacm_read(tinyusb_cdcacm_itf_t port, uint8_t *buf, size_t len, size_t *received);
