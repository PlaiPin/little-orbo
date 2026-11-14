#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <math.h>
#include "arms.h"
#include "teleop.h"
#include "wheels.h"
#include "esp_timer.h"
#include "dxl_bus.h"

#define DRIVE_PERIOD_MS     20      /* goal speeds to the motors, 50 Hz */
#define STATE_PERIOD_MS     100     /* STATE to the PC, 10 Hz */
#define VOLTS_PERIOD_MS     1000
#define RETRY_PERIOD_MS     2000    /* look for missing motors again */
#define MAX_BUS_FAILS       10      /* failures in a row before the motors count as lost */

static teleop_send_fn s_send;
static bool s_motors_ok, s_torque = true;
static float s_goal[2], s_speed[2], s_volts;
static int64_t s_goal_ms, s_next_drive, s_next_state, s_next_volts, s_next_retry;
static int s_fails;
static char s_last_report[200];

static int64_t now_ms(void)
{
    return esp_timer_get_time() / 1000;
}

static void say(const char *fmt, ...)
{
    char line[220] = "MSG ";
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line + 4, sizeof line - 4, fmt, ap);
    va_end(ap);
    s_send(line);
}

static void try_motors(void)
{
    char report[200];
    s_motors_ok = wheels_connect(s_torque, report, sizeof report);
    s_fails = 0;
    if (s_motors_ok) {
        say("Motors ready: %s", report);
    } else if (strcmp(report, s_last_report) != 0) {   /* say it once, keep retrying quietly */
        say("Motors not ready: %s. Retrying every %d s", report, RETRY_PERIOD_MS / 1000);
    }
    strlcpy(s_last_report, s_motors_ok ? "" : report, sizeof s_last_report);
}

static void bus_result(esp_err_t e)
{
    s_fails = e == ESP_OK ? 0 : s_fails + 1;
    if (s_fails >= MAX_BUS_FAILS) {
        s_motors_ok = false;
        s_speed[0] = s_speed[1] = 0;
        s_next_retry = now_ms() + RETRY_PERIOD_MS;
        say("Lost the motors (%s)", esp_err_to_name(e));
    }
}

void teleop_start(teleop_send_fn send)
{
    s_send = send;
    esp_err_t arm_error = arms_init();
    if (arm_error != ESP_OK) say("Arms unavailable: %s", esp_err_to_name(arm_error));
    esp_err_t e = wheels_init();
    if (e != ESP_OK) {
        say("Motor bus set-up failed: %s", esp_err_to_name(e));
    }
    try_motors();
    s_next_retry = now_ms() + RETRY_PERIOD_MS;
}

/* Diagnostic: ping every ID at every common baud rate, report who answers. */
static void scan_bus(void)
{
    static const int bauds[] = { 57600, 115200, 1000000, 2000000, 3000000, 4000000 };
    int found = 0;
    say("SCAN: pinging IDs 0-252 at 6 baud rates (about 30 s) ...");
    for (int b = 0; b < 6; b++) {
        uart_set_baudrate(UART_NUM_1, bauds[b]);
        int echo_faults = 0;
        for (int id = 0; id <= 252; id++) {
            uint16_t model = 0;
            esp_err_t e = dxl_ping(id, &model);
            if (e == ESP_OK) {
                say("SCAN: FOUND ID %d at %d baud, model %u", id, bauds[b], model);
                found++;
            } else if (e == ESP_ERR_INVALID_RESPONSE) {
                echo_faults++;
            } else if (e != ESP_ERR_TIMEOUT) {
                say("SCAN: ID %d at %d baud: %s", id, bauds[b], esp_err_to_name(e));
            }
        }
        say("SCAN: %d baud done%s", bauds[b], echo_faults ? " (echo faults seen)" : "");
    }
    uart_set_baudrate(UART_NUM_1, CONFIG_TELEOP_DXL_BAUD);
    say("SCAN: finished, %d motor(s) found", found);
}

void teleop_command(const char *line)
{
    char reply[160];
    if (arms_command(line, reply, sizeof reply)) {
        if (reply[0]) s_send(reply);
        return;
    }
    if (strcmp(line, "SCAN") == 0) {
        /* A scan blocks wheel updates: stop and remove torque first. */
        s_goal[0] = s_goal[1] = 0;
        wheels_drive(0, 0);
        wheels_torque(false);
        s_torque = false;
        scan_bus();
        return;
    }
    float l, r;
    int on;
    char extra;
    if (strcmp(line, "FIND") == 0) {
        s_send("HERE " CONFIG_TELEOP_NAME);
        if (s_motors_ok) {
            say("%s here. Motors ready", CONFIG_TELEOP_NAME);
        } else {
            say("%s here. Motors NOT ready: %s", CONFIG_TELEOP_NAME, s_last_report);
        }
    } else if (sscanf(line, "DRIVE %f %f %c", &l, &r, &extra) == 2 && isfinite(l) && isfinite(r)) {
        s_goal[0] = l;
        s_goal[1] = r;
        s_goal_ms = now_ms();
    } else if (sscanf(line, "TORQUE %d", &on) == 1) {
        if (s_torque != (on != 0)) {
            s_torque = on != 0;
            say(s_torque ? "Torque on" : "Torque off: wheels turn freely");
        }
        if (s_motors_ok) {
            bus_result(wheels_torque(s_torque));
        }
    }
}

void teleop_update(void)
{
    int64_t now = now_ms();

    if (now - s_goal_ms > CONFIG_TELEOP_WATCHDOG_MS && (s_goal[0] != 0 || s_goal[1] != 0)) {
        s_goal[0] = s_goal[1] = 0;
        say("No commands from the PC - stopping");
    }

    if (!s_motors_ok) {
        if (now >= s_next_retry) {
            s_next_retry = now + RETRY_PERIOD_MS;
            try_motors();
        }
    } else {
        if (now >= s_next_drive) {
            s_next_drive = now + DRIVE_PERIOD_MS;
            bus_result(wheels_drive(s_goal[0], s_goal[1]));
        }
        if (s_motors_ok && now >= s_next_volts) {
            s_next_volts = now + VOLTS_PERIOD_MS;
            bus_result(wheels_read_volts(&s_volts));
        }
    }

    if (now >= s_next_state) {
        s_next_state = now + STATE_PERIOD_MS;
        if (s_motors_ok) {
            bus_result(wheels_read_speed(&s_speed[0], &s_speed[1]));
        }
        char line[80];
        snprintf(line, sizeof line, "STATE %.1f %.1f %.1f %d %d", s_speed[0], s_speed[1],
                 s_motors_ok ? s_volts : 0.0f, s_torque, s_motors_ok);
        s_send(line);
        arms_status(line, sizeof line);
        s_send(line);
    }
}
