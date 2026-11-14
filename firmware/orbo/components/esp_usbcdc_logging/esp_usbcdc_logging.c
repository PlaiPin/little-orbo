#include "esp_usbcdc_logging.h"

// Initialize CDC1 after app_main installs the shared TinyUSB driver.
esp_err_t esp_usbcdc_logging_init(void)
{
    tinyusb_config_cdcacm_t acm_config = {
        .usb_dev = TINYUSB_USBDEV_0,
        .cdc_port = TINYUSB_CDC_ACM_1,
        .callback_rx = NULL,
        .callback_rx_wanted_char = NULL,
        .callback_line_state_changed = NULL,
        .callback_line_coding_changed = NULL,
    };

    esp_err_t ret = tusb_cdc_acm_init(&acm_config);

    if (ret != ESP_OK) {
        return ret;
    }

    ret = esp_tusb_init_console(TINYUSB_CDC_ACM_1);

    return ret;
}

// Deinitialize USB-CDC logging
esp_err_t esp_usbcdc_logging_deinit(void)
{
    esp_err_t ret = esp_tusb_deinit_console(TINYUSB_CDC_ACM_1);

    return ret;
}
