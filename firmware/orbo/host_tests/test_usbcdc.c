// Exercise the production callbacks against deterministic USB and scheduler mocks.
// This verifies callback contracts, not USB electrical behavior or RTOS timing.
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include "esp_usbcdc_transport.h"
#include "esp_usbcdc_logging.h"
#include "esp_timer.h"
#include "freertos/task.h"

static bool driver_installed, initialized[2], console_ready;
static int driver_installs, cdc_inits[2], cdc_deinits[2], reads, delays;
static int64_t now_us, ready_us;
static size_t tx_capacity = 512;
static esp_err_t read_result = ESP_OK, flush_result = ESP_OK;

esp_err_t tinyusb_driver_install(const tinyusb_config_t *config) {
    (void)config;
    driver_installs++;
    if (driver_installed) return ESP_ERR_INVALID_STATE;
    driver_installed = true;
    return ESP_OK;
}
esp_err_t tusb_cdc_acm_init(const tinyusb_config_cdcacm_t *config) {
    assert(driver_installed);
    int port = config->cdc_port;
    if (initialized[port]) return ESP_ERR_INVALID_STATE;
    initialized[port] = true;
    cdc_inits[port]++;
    return ESP_OK;
}
esp_err_t tusb_cdc_acm_deinit(int port) {
    assert(initialized[port]);
    initialized[port] = false;
    cdc_deinits[port]++;
    return ESP_OK;
}
bool tusb_cdc_acm_initialized(tinyusb_cdcacm_itf_t port) { return initialized[port]; }
esp_err_t esp_tusb_init_console(int port) {
    assert(port == 1 && initialized[1]);
    console_ready = true;
    return ESP_OK;
}
esp_err_t esp_tusb_deinit_console(int port) {
    assert(port == 1);
    console_ready = false;
    return ESP_OK;
}
size_t tinyusb_cdcacm_write_queue(tinyusb_cdcacm_itf_t port, const uint8_t *buf, size_t len) {
    (void)buf;
    assert(port == 0 && initialized[0]);
    return len < tx_capacity ? len : tx_capacity;
}
esp_err_t tinyusb_cdcacm_write_flush(tinyusb_cdcacm_itf_t port, uint32_t timeout) {
    assert(port == 0 && initialized[0] && timeout == 0);
    return flush_result;
}
esp_err_t tinyusb_cdcacm_read(tinyusb_cdcacm_itf_t port, uint8_t *buf, size_t len, size_t *received) {
    assert(port == 0 && initialized[0]);
    reads++;
    *received = 0;
    if (read_result != ESP_OK) return read_result;
    if (now_us >= ready_us) {
        *received = len < 3 ? len : 3;
        memset(buf, 0x5a, *received);
    }
    return ESP_OK;
}
int64_t esp_timer_get_time(void) { return now_us; }
void vTaskDelay(TickType_t ticks) {
    assert(ticks > 0);
    delays++;
    now_us += (int64_t)ticks * TEST_TICK_US;
}
static void no_data(void) {
    now_us = 0;
    ready_us = INT64_MAX;
    reads = delays = 0;
    read_result = ESP_OK;
}

int main(void) {
    tinyusb_config_t config = {0};
    assert(tinyusb_driver_install(&config) == ESP_OK); // app_main owns the driver.
    assert(esp_usbcdc_transport_init() == ESP_OK);
    assert(esp_usbcdc_transport_init() == ESP_OK);
    assert(esp_usbcdc_logging_init() == ESP_OK);
    assert(driver_installs == 1);
    tinyusb_cdcacm_itf_t port = TINYUSB_CDC_ACM_0;
    struct uxrCustomTransport transport = {.args = &port};
    for (int i = 0; i < 100; i++) {
        assert(esp_usbcdc_open(&transport));
        assert(esp_usbcdc_open(&transport)); // No duplicate CDC allocation.
        assert(esp_usbcdc_close(&transport));
        assert(esp_usbcdc_close(&transport));
        assert(console_ready && initialized[0] && initialized[1]);
    }
    assert(driver_installs == 1 && cdc_inits[0] == 1 && cdc_deinits[0] == 0);
    assert(cdc_inits[1] == 1 && cdc_deinits[1] == 0);
    assert(esp_usbcdc_open(&transport));

    uint8_t buf[16], err = 0xff;
    no_data();
    assert(esp_usbcdc_read(&transport, buf, sizeof(buf), 5, &err) == 0);
    assert(err == 0 && delays > 0 && reads <= delays + 1);
    assert(now_us >= 5000 && now_us < 5000 + TEST_TICK_US);

    no_data(); err = 0xff;
    assert(esp_usbcdc_read(&transport, buf, sizeof(buf), 0, &err) == 0);
    assert(err == 0 && reads == 1 && delays == 0);

    no_data(); ready_us = 2000; err = 0xff;
    assert(esp_usbcdc_read(&transport, buf, sizeof(buf), 20, &err) == 3);
    assert(err == 0 && now_us >= 2000 && now_us < 2000 + TEST_TICK_US);
    assert(delays > 0 && buf[0] == 0x5a);

    no_data(); ready_us = 6000; err = 0xff;
    assert(esp_usbcdc_read(&transport, buf, sizeof(buf), 5, &err) == 0);
    assert(err == 0); // Do not accept data after the deadline, even on a coarse tick.

    no_data(); ready_us = 0; err = 0xff;
    assert(esp_usbcdc_read(&transport, buf, 1, 0, &err) == 1);
    assert(err == 0 && delays == 0);
    no_data(); read_result = ESP_FAIL; err = 0;
    assert(esp_usbcdc_read(&transport, buf, sizeof(buf), 20, &err) == 0);
    assert(err == ESP_USBCDC_ERR_IO && reads == 1 && delays == 0);
    no_data(); ready_us = 0;
    assert(esp_usbcdc_read(&transport, buf, 1, 0, &err) == 1 && err == ESP_USBCDC_ERR_NONE);

    flush_result = ESP_ERR_NOT_FINISHED; err = 0xff;
    assert(esp_usbcdc_write(&transport, buf, sizeof(buf), &err) == sizeof(buf));
    assert(err == 0); // Queued transfer still in flight is not a failure.
    tx_capacity = 3; err = 0xff;
    assert(esp_usbcdc_write(&transport, buf, sizeof(buf), &err) == 3);
    assert(err == 0); // Stream framing can retry the remaining bytes.
    tx_capacity = 0; err = 0;
    assert(esp_usbcdc_write(&transport, buf, sizeof(buf), &err) == 0);
    assert(err == ESP_USBCDC_ERR_WOULD_BLOCK);
    tx_capacity = 512; flush_result = ESP_FAIL; err = 0;
    assert(esp_usbcdc_write(&transport, buf, sizeof(buf), &err) == 0);
    assert(err == ESP_USBCDC_ERR_IO);
    flush_result = ESP_OK; err = 0xff;
    assert(esp_usbcdc_write(&transport, buf, sizeof(buf), &err) == sizeof(buf));
    assert(err == 0); // Errors are cleared by the next successful operation.

    err = 0xff;
    assert(esp_usbcdc_read(&transport, NULL, 0, 0, &err) == 0 && err == 0);
    err = 0xff;
    assert(esp_usbcdc_write(&transport, NULL, 0, &err) == 0 && err == 0);
    err = 0;
    assert(esp_usbcdc_read(&transport, buf, 1, -1, &err) == 0 && err == ESP_USBCDC_ERR_ARGUMENT);
    err = 0;
    assert(esp_usbcdc_read(NULL, buf, 1, 0, &err) == 0 && err == ESP_USBCDC_ERR_ARGUMENT);
    err = 0;
    assert(esp_usbcdc_write(&transport, NULL, 1, &err) == 0 && err == ESP_USBCDC_ERR_ARGUMENT);
    assert(esp_usbcdc_close(&transport));
    err = 0;
    assert(esp_usbcdc_read(&transport, buf, 1, 0, &err) == 0 && err == ESP_USBCDC_ERR_IO);
    err = 0;
    assert(esp_usbcdc_write(&transport, buf, 1, &err) == 0 && err == ESP_USBCDC_ERR_IO);
    port = TINYUSB_CDC_ACM_1;
    assert(!esp_usbcdc_open(&transport) && !esp_usbcdc_close(&transport));
    assert(console_ready && initialized[1]);
    assert(!esp_usbcdc_open(NULL) && !esp_usbcdc_close(NULL));
    assert(esp_usbcdc_read(NULL, buf, 1, 0, NULL) == 0);
    printf("USB callback lifecycle/timeout/error assertions passed (mock tick %d us, no hardware)\n", TEST_TICK_US);
}
