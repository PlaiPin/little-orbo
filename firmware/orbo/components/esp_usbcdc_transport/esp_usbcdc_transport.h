#ifndef ESP_USBCDC_TRANSPORT_H
#define ESP_USBCDC_TRANSPORT_H

#include <uxr/client/transport.h>

#include "tinyusb.h"
#include "tusb_cdc_acm.h"

#if defined(CONFIG_IDF_TARGET_ESP32S2) || defined(CONFIG_IDF_TARGET_ESP32S3)

#ifdef __cplusplus
extern "C"
{
#endif

enum {
    ESP_USBCDC_ERR_NONE = 0,
    ESP_USBCDC_ERR_IO = 1,
    ESP_USBCDC_ERR_ARGUMENT = 2,
    ESP_USBCDC_ERR_WOULD_BLOCK = 3,
};

// app_main installs TinyUSB, then initializes CDC0 and CDC1 once per boot.
// The single micro-ROS task logically opens/closes CDC0 without deinitializing it.
esp_err_t esp_usbcdc_transport_init(void);
bool esp_usbcdc_open(struct uxrCustomTransport* transport);
bool esp_usbcdc_close(struct uxrCustomTransport* transport);
// Writes return the queued byte count, not proof of delivery to the agent.
size_t esp_usbcdc_write(struct uxrCustomTransport* transport, const uint8_t* buf, size_t len, uint8_t* err);
// Timeout is milliseconds (0 polls once). Empty reads yield one RTOS tick;
// scheduler/tick granularity can delay return beyond the deadline. Timeout is
// not an I/O error. Each call resets err; a NULL error pointer is permitted.
size_t esp_usbcdc_read(struct uxrCustomTransport* transport, uint8_t* buf, size_t len, int timeout, uint8_t* err);

#ifdef __cplusplus
}
#endif

#else
#error "This transport is only supported on ESP32-S2 or ESP32-S3 targets"
#endif // CONFIG_IDF_TARGET_ESP32S2 || CONFIG_IDF_TARGET_ESP32S3

#endif // ESP_USBCDC_TRANSPORT_H
