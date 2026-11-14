# Firmware host regression checks

From the repository root, run:

```sh
firmware/orbo/host_tests/run.sh
```

The runner builds temporary executables with address and undefined-behavior
sanitizers and removes them when finished. It needs a C compiler (`CC`, or `cc`).

`test_control.c` checks admission, expiry, and recovery. `test_usbcdc.c` compiles
the actual transport and logging sources against the headers in `usb_stubs/`.
Its deterministic USB/scheduler mocks exercise:

- 100 reconnect cycles without reinstalling the driver or either CDC interface.
- Positive deadlines, zero-timeout polling, delayed/partial reads, and I/O errors.
- 1 ms and 10 ms scheduler ticks, including data arriving after a deadline.
- Partial writes, full queues, pending nonblocking flushes, and flush failures.
- Defined error codes, error clearing, invalid arguments, and closed interfaces.

The application installs TinyUSB and both CDC interfaces once. The micro-ROS
task logically opens/closes CDC0 while its physical interface remains available
to the host. An empty read timeout returns zero bytes with
no I/O error; a write with no queue capacity returns `ESP_USBCDC_ERR_WOULD_BLOCK`.
Queued writes do not prove remote delivery.

`test_usb_bootloader.c` compiles the production bootloader module with USB and
register mocks. It checks that ordinary logging and CDC0 do not request reset,
CDC1 at 1200 baud only queues a request, registration errors propagate, and reset
follows USB detachment and selection of ROM download mode. It does not exercise
motor-task concurrency or prove physical stopping.

With the ESP-IDF Python environment active, also run:

```sh
python firmware/orbo/host_tests/test_flash_firmware.py
```

These host-helper tests cover logging-port selection, already-in-ROM detection,
ambiguous-device rejection, and matching USB location across re-enumeration.

These tests do not simulate USB enumeration, endpoint interrupts, or real RTOS
scheduling. Rebuild and flash the ESP32, then check telemetry, absent-agent logs,
agent restart, and a physical power cycle to validate those paths.

Manual wheel publisher checks use the Pixi ROS overlay and mocked nodes (no
hardware access):

```sh
pixi run --locked bash -c 'source ros_ws/install/macos/local_setup.bash && python firmware/orbo/host_tests/test_control_wheels.py'
```

They cover key direction/timeout, fragmented terminal escape sequences, terminal
restoration after an exception, fresh message sequencing, explicit arm/stop,
invalid velocities, and refusal on stale feedback, reboot, disarm, clock-delivery
outliers or another command publisher. These checks do not qualify live keyboard
response or physical stopping latency.

Fault-reporting checks also verify that events from another boot/epoch are
ignored, the original fault survives cleanup stop acknowledgements, and a cleanup
stop is not mislabeled as the original failure.

`test_wheel_bus.c` exercises the 115200-baud profile's migration from 1 Mbps using
mock motors. It checks torque-off before EEPROM writes, verification after a
lost write reply, partial migration recovery, no repeated EEPROM writes, and
refusal for an unknown model, unavailable motor or torque that cannot be disabled.
The host baud is restored on failures. These checks do not establish signal
integrity on physical wiring.
