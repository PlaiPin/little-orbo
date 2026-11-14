# Validation — November 14, 2026

- ESP-IDF 6.1 build completed successfully for ESP32-S3.
- Flashed COM25 successfully; esptool verified the written image hash.
- App image: 818,464 bytes; 47% of the app partition remains free.
- `firmware/build/teleop.bin` SHA-256:
  `02F6923C98AF402EAF99E18BB003AB8408C8731907F552172D22101AA4C4B326`.
- All **9 UI regression tests passed**, including focus-loss stop, connection-loss
  stop, configuration acknowledgement, malformed limits, combined wheel/arm command
  generation, disconnect, and offscreen rendering.
- The desktop UI was launched and its new controls were inspected. Automated clicks
  in that live window were unavailable because the desktop tool could not obtain its
  input geometry; live transport verification used the protocol test below.
- `tests/check_board.py --port COM25` **passed**.
- `tests/check_board.py --udp 192.168.4.1` **passed** after refreshing the Windows Wi-Fi
  scan and reconnecting the saved PetBot profile.
- Both transports verified discovery, wheel readiness, PWM-driver readiness,
  acknowledgement of arm limits, rejection of invalid limits/modes/travel/trailing
  command text, and disabled arm outputs.
- Live wheel status after upload: `STATE 0.0 0.0 12.4 1 1` (zero speeds, 12.4 V,
  wheel torque on, both motors ready).
- Live final arm status: `ARMS 0 0 0 0 1 1` (off, zero PWM, configuration received,
  PWM driver ready). Test-only limits in RAM were 1450/1500/1550 us, signs +1/−1.
  Applying these limits did not enable PWM or establish physical calibration.
- USB and Wi-Fi observations are saved locally in `tests/build/board-usb.json` and
  `tests/build/board-wifi.json` (generated files are git-ignored).

## Remaining physical verification

No nonzero wheel motion or servo PWM was commanded during these hardware checks.
The mounted arm center, travel clearance, movement direction and actual PWM waveform
have not been physically measured. Follow the first-calibration instructions in
README.md before enabling hands. No claim of completed physical calibration is made.

The native C motion-engine test compiled, but Windows Smart App Control blocked its
execution. It is included as `tests/test_motion.c` for execution on a host that permits
locally compiled tests. No Windows protection settings were modified. Firmware
watchdog and slew behavior are implemented and covered by that test source, but those
C assertions were not executed during this session.

## Review scope and preserved behavior

Reviewed the Wi-Fi/USB keyboard teleoperation path and the existing PWM flipper code,
using the user's running Software/Teleoperate tree as the copy source. The broader
ROS, simulation and CAD projects were outside this integration scope.

The running wheel driver, motor IDs, UART pins, 1 Mbps bus rate, AP credentials,
wheel acceleration, and existing PC wheel preferences are preserved. The source
reference and original little-orbo folders were not edited. This copy additionally
stops wheels/removes wheel torque before the inherited blocking SCAN diagnostic,
rejects non-finite drive inputs, stops both outputs on focus loss, and keeps servo
timing/watchdog processing in its own task.
