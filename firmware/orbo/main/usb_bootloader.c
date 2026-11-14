#include "usb_bootloader.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "soc/rtc_cntl_reg.h"
#include "soc/usb_serial_jtag_reg.h"
#include "tusb_cdc_acm.h"
#include "tusb.h"

static void (*request_download)(void);

static void line_coding(int itf, cdcacm_event_t *event) {
    if (itf == TINYUSB_CDC_ACM_1 && event &&
        event->type == CDC_EVENT_LINE_CODING_CHANGED &&
        event->line_coding_changed_data.p_line_coding &&
        event->line_coding_changed_data.p_line_coding->bit_rate == 1200 &&
        request_download) {
        request_download();
    }
}

esp_err_t usb_bootloader_init(void (*request)(void)) {
    if (!request) return ESP_ERR_INVALID_ARG;
    request_download = request;
    return tinyusb_cdcacm_register_callback(TINYUSB_CDC_ACM_1,
        CDC_EVENT_LINE_CODING_CHANGED, line_coding);
}

void usb_bootloader_restart(void) {
    // Leave TinyUSB allocated until reset: other tasks may still hold its state.
    // A real detach lets the host replace the composite device with ROM CDC.
    tud_disconnect();
    vTaskDelay(pdMS_TO_TICKS(100));
    // ESP32-S3: restore the internal PHY to hardware USB Serial/JTAG. These
    // registers and FORCE_DOWNLOAD_BOOT are from the locked IDF's SoC headers.
    REG_CLR_BIT(RTC_CNTL_USB_CONF_REG, RTC_CNTL_SW_HW_USB_PHY_SEL |
        RTC_CNTL_SW_USB_PHY_SEL | RTC_CNTL_USB_PAD_ENABLE);
    REG_CLR_BIT(USB_SERIAL_JTAG_CONF0_REG, USB_SERIAL_JTAG_PHY_SEL |
        USB_SERIAL_JTAG_DM_PULLUP | USB_SERIAL_JTAG_DM_PULLDOWN |
        USB_SERIAL_JTAG_DP_PULLDOWN | USB_SERIAL_JTAG_PAD_PULL_OVERRIDE);
    REG_SET_BIT(USB_SERIAL_JTAG_CONF0_REG,
        USB_SERIAL_JTAG_DP_PULLUP | USB_SERIAL_JTAG_USB_PAD_ENABLE);
    REG_SET_BIT(RTC_CNTL_OPTION1_REG, RTC_CNTL_FORCE_DOWNLOAD_BOOT);
    esp_restart();
}
