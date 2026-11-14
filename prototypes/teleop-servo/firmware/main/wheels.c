#include <math.h>
#include <stdio.h>
#include <string.h>
#include "wheels.h"
#include "dxl_bus.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

/* XL430-W250 control table */
#define REG_DRIVE_MODE      10      /* 1 byte, bit 0 = reverse */
#define REG_OPERATING_MODE  11      /* 1 byte, 1 = velocity */
#define REG_TORQUE_ENABLE   64      /* 1 byte */
#define REG_HW_ERROR        70      /* 1 byte */
#define REG_GOAL_VELOCITY   104     /* 4 bytes, 0.229 rpm */
#define REG_PROFILE_ACCEL   108     /* 4 bytes, 214.577 rev/min^2, 0 = no ramp */
#define REG_PRESENT_VEL     128     /* 4 bytes, 0.229 rpm */
#define REG_PRESENT_VOLTS   144     /* 2 bytes, 0.1 V */

#define RPM_PER_UNIT        0.229f
#define VELOCITY_MODE       1

static const uint8_t ids[2] = { CONFIG_TELEOP_ID_LEFT, CONFIG_TELEOP_ID_RIGHT };
static const char *names[2] = { "Left", "Right" };

static esp_err_t write_u8(uint8_t id, uint16_t reg, uint8_t v)
{
    return dxl_write(id, reg, &v, 1);
}

static esp_err_t write_u32(uint8_t id, uint16_t reg, int32_t v)
{
    uint8_t b[4] = { v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF, (v >> 24) & 0xFF };
    return dxl_write(id, reg, b, 4);
}

static const char *bus_error(esp_err_t e)
{
    switch (e) {
    case ESP_ERR_TIMEOUT:          return "no reply (battery on? cable? ID / baud?)";
    case ESP_ERR_INVALID_RESPONSE: return "bus wiring fault (TX-RX jumper / level shifter)";
    case ESP_ERR_INVALID_CRC:      return "damaged reply";
    default:                       return esp_err_to_name(e);
    }
}

esp_err_t wheels_init(void)
{
    return dxl_bus_init(UART_NUM_1, CONFIG_TELEOP_DXL_TX, CONFIG_TELEOP_DXL_RX, CONFIG_TELEOP_DXL_BAUD);
}

/* Returns ESP_OK and appends what it did to `note`. */
static esp_err_t prepare(int i, bool torque_on, char *note, size_t cap)
{
    uint8_t id = ids[i], hw = 0, mode = 0, drive_mode = 0;
    uint16_t model = 0;
    esp_err_t e = dxl_ping(id, &model);
    if (e != ESP_OK) {
        snprintf(note, cap, "%s ID %d: %s", names[i], id, bus_error(e));
        return e;
    }
    if (dxl_read(id, REG_HW_ERROR, &hw, 1) == ESP_OK && hw) {
        dxl_reboot(id);                     /* clears the latched error (e.g. overload) */
        vTaskDelay(pdMS_TO_TICKS(1000));
    }
    e = write_u8(id, REG_TORQUE_ENABLE, 0); /* mode registers can only change with torque off */
    if (e == ESP_OK) e = dxl_read(id, REG_OPERATING_MODE, &mode, 1);
    if (e == ESP_OK && mode != VELOCITY_MODE) e = write_u8(id, REG_OPERATING_MODE, VELOCITY_MODE);
    if (e == ESP_OK) e = dxl_read(id, REG_DRIVE_MODE, &drive_mode, 1);
    if (e == ESP_OK) e = write_u32(id, REG_PROFILE_ACCEL, CONFIG_TELEOP_ACCEL);
    if (e == ESP_OK) e = write_u32(id, REG_GOAL_VELOCITY, 0);
    if (e == ESP_OK && torque_on) e = write_u8(id, REG_TORQUE_ENABLE, 1);
    if (e != ESP_OK) {
        snprintf(note, cap, "%s ID %d: set-up failed, %s", names[i], id, bus_error(e));
        return e;
    }
    snprintf(note, cap, "%s ID %d OK (model %u%s%s%s)", names[i], id, model,
             mode != VELOCITY_MODE ? ", set to velocity mode" : "",
             hw ? ", cleared a hardware error" : "",
             drive_mode & 1 ? ", reversed in motor" : "");
    return ESP_OK;
}

bool wheels_connect(bool torque_on, char *report, size_t cap)
{
    char note[2][96];
    bool ok = true;
    for (int i = 0; i < 2; i++) {
        ok &= prepare(i, torque_on, note[i], sizeof note[i]) == ESP_OK;
    }
    snprintf(report, cap, "%s; %s", note[0], note[1]);
    return ok;
}

esp_err_t wheels_drive(float left_rpm, float right_rpm)
{
    float rpm[2] = { left_rpm, right_rpm };
    uint8_t data[8];
    for (int i = 0; i < 2; i++) {
        float r = fmaxf(-CONFIG_TELEOP_MAX_RPM, fminf(CONFIG_TELEOP_MAX_RPM, rpm[i]));
        int32_t units = (int32_t)lroundf(r / RPM_PER_UNIT);
        for (int b = 0; b < 4; b++) {
            data[i * 4 + b] = (units >> (8 * b)) & 0xFF;
        }
    }
    return dxl_sync_write(REG_GOAL_VELOCITY, 4, ids, 2, data);
}

esp_err_t wheels_torque(bool on)
{
    esp_err_t e = ESP_OK;
    for (int i = 0; i < 2; i++) {
        esp_err_t r = write_u8(ids[i], REG_TORQUE_ENABLE, on ? 1 : 0);
        if (r != ESP_OK) {
            e = r;
        }
    }
    return e;
}

esp_err_t wheels_read_speed(float *left_rpm, float *right_rpm)
{
    float *out[2] = { left_rpm, right_rpm };
    for (int i = 0; i < 2; i++) {
        uint8_t b[4];
        esp_err_t e = dxl_read(ids[i], REG_PRESENT_VEL, b, 4);
        if (e != ESP_OK) {
            return e;
        }
        int32_t v = (int32_t)((uint32_t)b[0] | (uint32_t)b[1] << 8 | (uint32_t)b[2] << 16 | (uint32_t)b[3] << 24);
        *out[i] = v * RPM_PER_UNIT;
    }
    return ESP_OK;
}

esp_err_t wheels_read_volts(float *volts)
{
    uint8_t b[2];
    esp_err_t e = dxl_read(ids[0], REG_PRESENT_VOLTS, b, 2);
    if (e == ESP_OK) {
        *volts = (b[0] | (b[1] << 8)) / 10.0f;
    }
    return e;
}
