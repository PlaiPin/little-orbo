/* PWM adapted from firmware/orbo/main/flippers.c. One task owns both channels. */
#include "sdkconfig.h"
#include "arms.h"
#include "arm_motion.h"
#include "driver/gpio.h"
#include "driver/ledc.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include <stdio.h>
#include <string.h>
#include <math.h>

static SemaphoreHandle_t lock;
static arm_motion_t motion;
static bool ready;
static int pulse[2];

static void pwm_off(void)
{
    for (int i = 0; i < 2; i++) {
        ledc_stop(LEDC_LOW_SPEED_MODE, (ledc_channel_t)i, 0);
        pulse[i] = 0;
    }
}

static void arm_task(void *unused)
{
    (void)unused;
    int64_t previous = esp_timer_get_time() / 1000;
    while (true) {
        int64_t now = esp_timer_get_time() / 1000;
        xSemaphoreTake(lock, portMAX_DELAY);
        arm_step(&motion, now, (now - previous) / 1000.0f);
        previous = now;
        if (!motion.enabled || !ready) {
            pwm_off();
        } else {
            for (int i = 0; i < 2; i++) {
                pulse[i] = (int)lroundf(motion.position[i]);
                esp_err_t e = ledc_set_duty(LEDC_LOW_SPEED_MODE, (ledc_channel_t)i,
                                           (uint32_t)pulse[i] * 16384 / 20000);
                if (e == ESP_OK) e = ledc_update_duty(LEDC_LOW_SPEED_MODE, (ledc_channel_t)i);
                if (e != ESP_OK) {
                    ready = false;
                    motion.enabled = false;
                    motion.mode = ARM_OFF;
                    pwm_off();
                    break;
                }
            }
        }
        xSemaphoreGive(lock);
        vTaskDelay(pdMS_TO_TICKS(20));
    }
}

esp_err_t arms_init(void)
{
    const int pins[2] = {CONFIG_TELEOP_ARM_LEFT_GPIO, CONFIG_TELEOP_ARM_RIGHT_GPIO};
    if (pins[0] == pins[1]) return ESP_ERR_INVALID_ARG;
    for (int i = 0; i < 2; i++) {
        if (!GPIO_IS_VALID_OUTPUT_GPIO(pins[i]) || pins[i] == 19 || pins[i] == 20 ||
            pins[i] == CONFIG_TELEOP_DXL_TX || pins[i] == CONFIG_TELEOP_DXL_RX)
            return ESP_ERR_INVALID_ARG;
    }
    lock = xSemaphoreCreateMutex();
    if (!lock) return ESP_ERR_NO_MEM;
    ledc_timer_config_t timer = {.speed_mode = LEDC_LOW_SPEED_MODE,
        .duty_resolution = LEDC_TIMER_14_BIT, .timer_num = LEDC_TIMER_0,
        .freq_hz = 50, .clk_cfg = LEDC_AUTO_CLK};
    esp_err_t e = ledc_timer_config(&timer);
    if (e != ESP_OK) return e;
    for (int i = 0; i < 2; i++) {
        ledc_channel_config_t channel = {.gpio_num = pins[i], .speed_mode = LEDC_LOW_SPEED_MODE,
            .channel = (ledc_channel_t)i, .timer_sel = LEDC_TIMER_0, .duty = 0, .hpoint = 0};
        e = ledc_channel_config(&channel);
        if (e != ESP_OK) {
            for (int j = 0; j < i; j++) ledc_stop(LEDC_LOW_SPEED_MODE, (ledc_channel_t)j, 0);
            return e;
        }
    }
    ready = true;
    if (xTaskCreate(arm_task, "arms", 3072, NULL, 5, NULL) != pdPASS) {
        ready = false;
        pwm_off();
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

bool arms_command(const char *line, char *reply, size_t cap)
{
    if (strncmp(line, "ARM", 3) != 0) return false;
    snprintf(reply, cap, "MSG Arm command rejected: check limits / disable arms before calibration");
    if (!lock) return true;
    xSemaphoreTake(lock, portMAX_DELAY);
    char extra;
    arm_calibration_t c;
    int mode, tempo, range, left, right;
    if (ready && sscanf(line, "ARMCFG %d %d %d %d %d %d %d %d %c",
            &c.low[0], &c.center[0], &c.high[0], &c.low[1], &c.center[1], &c.high[1],
            &c.sign[0], &c.sign[1], &extra) == 8 && arm_configure(&motion, &c)) {
        snprintf(reply, cap, "%s", line); /* Exact configuration acknowledgement. */
    } else if (ready && sscanf(line, "ARM %d %d %d %d %d %c", &mode, &tempo, &range,
            &left, &right, &extra) == 5 &&
            arm_command(&motion, mode, tempo, range, left, right, esp_timer_get_time() / 1000)) {
        reply[0] = 0;
    }
    xSemaphoreGive(lock);
    return true;
}

void arms_status(char *line, size_t cap)
{
    if (lock) xSemaphoreTake(lock, portMAX_DELAY);
    snprintf(line, cap, "ARMS %d %d %d %d %d %d", motion.enabled, motion.mode,
             pulse[0], pulse[1], motion.configured, ready);
    if (lock) xSemaphoreGive(lock);
}
