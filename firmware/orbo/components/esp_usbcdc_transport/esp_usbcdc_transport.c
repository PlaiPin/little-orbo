#include "esp_usbcdc_transport.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static bool transport_open;

// These callbacks run serially in the micro-ROS task. CDC1 belongs to logging.
static bool transport_port(const struct uxrCustomTransport *transport,
                           tinyusb_cdcacm_itf_t *port) {
    if (!transport || !transport->args) return false;
    *port = *(const tinyusb_cdcacm_itf_t *)transport->args;
    return *port == TINYUSB_CDC_ACM_0;
}

static void set_error(uint8_t *err, uint8_t value) {
    if (err) *err = value;
}

esp_err_t esp_usbcdc_transport_init(void) {
    if (tusb_cdc_acm_initialized(TINYUSB_CDC_ACM_0)) return ESP_OK;
    // Keep the physical interface alive even between XRCE discovery attempts.
    const tinyusb_config_cdcacm_t acm_cfg = {
        .usb_dev = TINYUSB_USBDEV_0,
        .cdc_port = TINYUSB_CDC_ACM_0,
    };
    return tusb_cdc_acm_init(&acm_cfg);
}

bool esp_usbcdc_open(struct uxrCustomTransport* transport) {
    tinyusb_cdcacm_itf_t port;
    if (!transport_port(transport, &port) || !tusb_cdc_acm_initialized(port)) return false;
    transport_open = true;
    return true;
}

bool esp_usbcdc_close(struct uxrCustomTransport* transport) {
    tinyusb_cdcacm_itf_t port;
    if (!transport_port(transport, &port)) return false;
    transport_open = false;
    return true;
}

size_t esp_usbcdc_write(struct uxrCustomTransport* transport, const uint8_t* buf, size_t len, uint8_t* err) {
    set_error(err, ESP_USBCDC_ERR_NONE);
    tinyusb_cdcacm_itf_t port;
    if (!transport_port(transport, &port) || (!buf && len)) {
        set_error(err, ESP_USBCDC_ERR_ARGUMENT);
        return 0;
    }
    if (!transport_open || !tusb_cdc_acm_initialized(port)) {
        set_error(err, ESP_USBCDC_ERR_IO);
        return 0;
    }
    if (!len) return 0;
    size_t tx_size = tinyusb_cdcacm_write_queue(port, buf, len);
    esp_err_t ret = tinyusb_cdcacm_write_flush(port, 0);
    // Queued bytes may still be in flight; nonblocking NOT_FINISHED is normal.
    if (ret != ESP_OK && ret != ESP_ERR_NOT_FINISHED) {
        set_error(err, ESP_USBCDC_ERR_IO);
        return 0;
    }
    // Partial writes are valid for XRCE stream framing. A full queue must not spin.
    if (!tx_size) set_error(err, ESP_USBCDC_ERR_WOULD_BLOCK);
    return tx_size;
}

size_t esp_usbcdc_read(struct uxrCustomTransport* transport, uint8_t* buf, size_t len, int timeout, uint8_t* err) {
    set_error(err, ESP_USBCDC_ERR_NONE);
    tinyusb_cdcacm_itf_t port;
    if (!transport_port(transport, &port) || (!buf && len) || timeout < 0) {
        set_error(err, ESP_USBCDC_ERR_ARGUMENT);
        return 0;
    }
    if (!transport_open || !tusb_cdc_acm_initialized(port)) {
        set_error(err, ESP_USBCDC_ERR_IO);
        return 0;
    }
    if (!len) return 0;
    const int64_t deadline = esp_timer_get_time() + (int64_t)timeout * 1000;
    do {
        size_t rx_size = 0;
        if (tinyusb_cdcacm_read(port, buf, len, &rx_size) != ESP_OK) {
            set_error(err, ESP_USBCDC_ERR_IO);
            return 0;
        }
        if (rx_size) return rx_size;
        if (esp_timer_get_time() >= deadline) break;
        // Yield to USB and idle tasks instead of busy-polling during discovery.
        vTaskDelay(1);
    } while (esp_timer_get_time() < deadline);
    // Ordinary timeout means no data, as in the XRCE POSIX serial transport.
    return 0;
}
