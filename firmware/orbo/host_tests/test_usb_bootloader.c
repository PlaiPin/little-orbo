#include <assert.h>
#include <setjmp.h>
#include <stdio.h>
#include "usb_bootloader.h"
#include "tusb_cdc_acm.h"
#include "freertos/task.h"
#include "soc/rtc_cntl_reg.h"
#include "soc/usb_serial_jtag_reg.h"

static tusb_cdcacm_callback_t handler;
static int requests, detached, delay_ms;
static jmp_buf restart_point;
uint32_t test_registers[3];
static esp_err_t registration_result=ESP_OK;
esp_err_t tinyusb_cdcacm_register_callback(tinyusb_cdcacm_itf_t port,
    cdcacm_event_type_t event, tusb_cdcacm_callback_t callback) {
    assert(port==TINYUSB_CDC_ACM_1 && event==CDC_EVENT_LINE_CODING_CHANGED);
    handler=callback;
    return registration_result;
}
static void request(void) { requests++; }
bool tud_disconnect(void) { detached++; return true; }
void vTaskDelay(TickType_t ticks) {
    assert(detached==1);
    delay_ms += ticks*TEST_TICK_US/1000;
}
void esp_restart(void) {
    assert(detached==1 && delay_ms>=100);
    assert(test_registers[RTC_CNTL_OPTION1_REG]&RTC_CNTL_FORCE_DOWNLOAD_BOOT);
    assert(!(test_registers[RTC_CNTL_USB_CONF_REG]&RTC_CNTL_SW_USB_PHY_SEL));
    assert(test_registers[USB_SERIAL_JTAG_CONF0_REG]&USB_SERIAL_JTAG_USB_PAD_ENABLE);
    longjmp(restart_point,1);
}
int main(void) {
    assert(usb_bootloader_init(NULL)==ESP_ERR_INVALID_ARG);
    registration_result=ESP_FAIL;
    assert(usb_bootloader_init(request)==ESP_FAIL);
    registration_result=ESP_OK;
    assert(usb_bootloader_init(request)==ESP_OK);
    cdc_line_coding_t coding={.bit_rate=115200};
    cdcacm_event_t event={.type=CDC_EVENT_LINE_CODING_CHANGED,
        .line_coding_changed_data={.p_line_coding=&coding}};
    handler(1,&event); // Ordinary monitor opening must not reboot.
    coding.bit_rate=1200;
    handler(0,&event); // ROS CDC0 must not be a reboot interface.
    handler(1,NULL);
    event.type=CDC_EVENT_RX;
    handler(1,&event);
    assert(requests==0 && detached==0);
    event.type=CDC_EVENT_LINE_CODING_CHANGED;
    handler(1,&event);
    assert(requests==1 && detached==0); // USB callback only requests; no reset here.
    test_registers[0]=RTC_CNTL_SW_USB_PHY_SEL|RTC_CNTL_SW_HW_USB_PHY_SEL;
    if(setjmp(restart_point)==0)usb_bootloader_restart();
    puts("USB bootloader: trigger isolation, deferred reset, detach and ROM selection passed");
}
