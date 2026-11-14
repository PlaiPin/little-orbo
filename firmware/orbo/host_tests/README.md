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

These tests do not simulate USB enumeration, endpoint interrupts, or real RTOS
scheduling. Rebuild and flash the ESP32, then check telemetry, absent-agent logs,
agent restart, and a physical power cycle to validate those paths.
