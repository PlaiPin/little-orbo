/*
 * Teleop logic, the same for the wired and the wireless firmware. main.c only moves text lines.
 *
 * PC -> robot (one command per line / packet)
 *   FIND                          answer "HERE <name>"
 *   DRIVE <left_rpm> <right_rpm>  wheel speeds. The PC sends this 20 times a second; it is also
 *                                 the keep-alive: nothing for CONFIG_TELEOP_WATCHDOG_MS -> wheels stop
 *   TORQUE <0|1>                  motors off (free wheels) / on
 * robot -> PC
 *   STATE <left_rpm> <right_rpm> <volts> <torque> <motors_ok>   10 times a second
 *   MSG <text>
 */
#pragma once

typedef void (*teleop_send_fn)(const char *line);    /* send one line (no newline) to the PC */

void teleop_start(teleop_send_fn send);
void teleop_command(const char *line);               /* one line received from the PC */
void teleop_update(void);                            /* call at least every 10 ms */
