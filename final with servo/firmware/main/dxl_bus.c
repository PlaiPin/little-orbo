/*
 * DYNAMIXEL Protocol 2.0 on a single wire (see dxl_bus.h).
 *
 * Packet:  FF FF FD 00 | ID | LEN_L LEN_H | INST | PARAMS ... | CRC_L CRC_H
 *   LEN counts INST + PARAMS + CRC. Inside INST..PARAMS, FF FF FD is sent as FF FF FD FD.
 */
#include <string.h>
#include "dxl_bus.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define INST_PING       0x01
#define INST_READ       0x02
#define INST_WRITE      0x03
#define INST_REBOOT     0x08
#define INST_SYNC_WRITE 0x83
#define INST_STATUS     0x55

#define MAX_PARAMS      32
#define MAX_PACKET      (10 + 2 * MAX_PARAMS)
#define ECHO_WAIT_MS    5
#define REPLY_WAIT_MS   10

static uart_port_t s_port = UART_NUM_MAX;

static uint16_t crc16(const uint8_t *p, size_t n)
{
    uint16_t crc = 0;
    while (n--) {
        crc ^= (uint16_t)(*p++) << 8;
        for (int bit = 0; bit < 8; bit++) {
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x8005) : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

static bool is_stuff_point(const uint8_t *end)    /* the 3 bytes before `end` are FF FF FD */
{
    return end[-3] == 0xFF && end[-2] == 0xFF && end[-1] == 0xFD;
}

static size_t make_packet(uint8_t *pkt, uint8_t id, uint8_t inst, const uint8_t *params, size_t n)
{
    size_t i = 0;
    pkt[i++] = 0xFF;
    pkt[i++] = 0xFF;
    pkt[i++] = 0xFD;
    pkt[i++] = 0x00;
    pkt[i++] = id;
    pkt[i++] = 0;                           /* length, filled in below */
    pkt[i++] = 0;
    pkt[i++] = inst;
    for (size_t k = 0; k < n; k++) {
        pkt[i++] = params[k];
        if (is_stuff_point(pkt + i)) {
            pkt[i++] = 0xFD;
        }
    }
    uint16_t len = (uint16_t)(i - 7 + 2);
    pkt[5] = len & 0xFF;
    pkt[6] = len >> 8;
    uint16_t crc = crc16(pkt, i);
    pkt[i++] = crc & 0xFF;
    pkt[i++] = crc >> 8;
    return i;
}

/* Read exactly n bytes before the tick `deadline`. */
static bool read_until(uint8_t *buf, size_t n, TickType_t deadline)
{
    size_t got = 0;
    while (got < n) {
        TickType_t left = deadline - xTaskGetTickCount();
        if ((int32_t)left <= 0) {
            return false;
        }
        int r = uart_read_bytes(s_port, buf + got, n - got, left);
        if (r > 0) {
            got += (size_t)r;
        }
    }
    return true;
}

static esp_err_t read_status(uint8_t id, uint8_t *out, size_t out_len)
{
    TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(REPLY_WAIT_MS) + 1;
    uint8_t pkt[MAX_PACKET] = { 0xFF, 0xFF, 0xFD, 0x00 };
    uint32_t window = 0;

    while (window != 0xFFFFFD00) {          /* skip anything before the header */
        uint8_t b;
        if (!read_until(&b, 1, deadline)) {
            return ESP_ERR_TIMEOUT;
        }
        window = (window << 8) | b;
    }
    if (!read_until(pkt + 4, 3, deadline)) {
        return ESP_ERR_TIMEOUT;
    }
    size_t len = pkt[5] | (pkt[6] << 8);
    if (len < 4 || 7 + len > sizeof pkt) {
        return ESP_ERR_INVALID_SIZE;
    }
    if (!read_until(pkt + 7, len, deadline)) {
        return ESP_ERR_TIMEOUT;
    }
    size_t total = 7 + len;
    if (crc16(pkt, total - 2) != (pkt[total - 2] | (pkt[total - 1] << 8))) {
        return ESP_ERR_INVALID_CRC;
    }
    if (pkt[4] != id || pkt[7] != INST_STATUS) {
        return ESP_ERR_INVALID_SIZE;
    }
    /* remove byte stuffing from INST..PARAMS (in place) */
    size_t w = 8;
    for (size_t r = 8; r < total - 2; r++) {
        pkt[w++] = pkt[r];
        if (is_stuff_point(pkt + w) && r + 1 < total - 2 && pkt[r + 1] == 0xFD) {
            r++;
        }
    }
    uint8_t error = pkt[8];
    size_t n_params = w - 9;
    if (error & 0x7F) {                     /* bit 7 only flags a hardware error; checked elsewhere */
        return ESP_FAIL;
    }
    if (n_params != out_len) {
        return ESP_ERR_INVALID_SIZE;
    }
    if (out_len) {
        memcpy(out, pkt + 9, out_len);
    }
    return ESP_OK;
}

static esp_err_t transfer(uint8_t id, uint8_t inst, const uint8_t *params, size_t n,
                          bool expect_reply, uint8_t *out, size_t out_len)
{
    if (s_port == UART_NUM_MAX) {
        return ESP_ERR_INVALID_STATE;
    }
    if (n > MAX_PARAMS) {
        return ESP_ERR_INVALID_SIZE;
    }
    uint8_t tx[MAX_PACKET], echo[MAX_PACKET];
    size_t len = make_packet(tx, id, inst, params, n);

    uart_flush_input(s_port);
    uart_write_bytes(s_port, tx, len);
    uart_wait_tx_done(s_port, pdMS_TO_TICKS(10));
    /* TX and RX share the wire, so our packet comes straight back: check it and drop it */
    int got = uart_read_bytes(s_port, echo, len, pdMS_TO_TICKS(ECHO_WAIT_MS));
    if (got != (int)len || memcmp(echo, tx, len) != 0) {
        return ESP_ERR_INVALID_RESPONSE;
    }
    return expect_reply ? read_status(id, out, out_len) : ESP_OK;
}

esp_err_t dxl_bus_init(uart_port_t port, int tx_gpio, int rx_gpio, int baud)
{
    uart_config_t cfg = {
        .baud_rate = baud,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    esp_err_t e = uart_driver_install(port, 512, 0, 0, NULL, 0);
    if (e == ESP_OK) e = uart_param_config(port, &cfg);
    if (e == ESP_OK) e = uart_set_pin(port, tx_gpio, rx_gpio, UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    if (e != ESP_OK) {
        return e;
    }
    gpio_od_enable(tx_gpio);                /* release the shared wire when not sending */
    gpio_pullup_en(tx_gpio);
    gpio_pullup_en(rx_gpio);
    uart_set_rx_timeout(port, 2);           /* pass received bytes on quickly */
    s_port = port;
    return ESP_OK;
}

esp_err_t dxl_ping(uint8_t id, uint16_t *model)
{
    uint8_t r[3];
    esp_err_t e = transfer(id, INST_PING, NULL, 0, true, r, sizeof r);
    if (e == ESP_OK && model) {
        *model = r[0] | (r[1] << 8);
    }
    return e;
}

esp_err_t dxl_read(uint8_t id, uint16_t addr, uint8_t *out, uint16_t len)
{
    uint8_t p[4] = { addr & 0xFF, addr >> 8, len & 0xFF, len >> 8 };
    return transfer(id, INST_READ, p, sizeof p, true, out, len);
}

esp_err_t dxl_write(uint8_t id, uint16_t addr, const uint8_t *data, uint16_t len)
{
    uint8_t p[MAX_PARAMS];
    if (2 + (size_t)len > sizeof p) {
        return ESP_ERR_INVALID_SIZE;
    }
    p[0] = addr & 0xFF;
    p[1] = addr >> 8;
    memcpy(p + 2, data, len);
    return transfer(id, INST_WRITE, p, 2 + len, true, NULL, 0);
}

esp_err_t dxl_reboot(uint8_t id)
{
    return transfer(id, INST_REBOOT, NULL, 0, true, NULL, 0);
}

esp_err_t dxl_sync_write(uint16_t addr, uint16_t len, const uint8_t *ids, int count, const uint8_t *data)
{
    uint8_t p[MAX_PARAMS];
    size_t n = 4 + (size_t)count * (1 + len);
    if (n > sizeof p) {
        return ESP_ERR_INVALID_SIZE;
    }
    p[0] = addr & 0xFF;
    p[1] = addr >> 8;
    p[2] = len & 0xFF;
    p[3] = len >> 8;
    uint8_t *q = p + 4;
    for (int i = 0; i < count; i++) {
        *q++ = ids[i];
        memcpy(q, data + i * len, len);
        q += len;
    }
    return transfer(DXL_BROADCAST, INST_SYNC_WRITE, p, n, false, NULL, 0);
}
