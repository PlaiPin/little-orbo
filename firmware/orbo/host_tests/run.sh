#!/usr/bin/env bash
set -euo pipefail
ORBO_FIRMWARE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ORBO_TEST_BUILD="$(mktemp -d "${TMPDIR:-/tmp}/orbo-host-tests.XXXXXX")"
trap 'rm -rf "$ORBO_TEST_BUILD"' EXIT
ORBO_CC="${CC:-cc}"
FLAGS=(-std=c11 -Wall -Wextra -Werror -fsanitize=address,undefined)
"$ORBO_CC" "${FLAGS[@]}" -I"$ORBO_FIRMWARE/main" \
  "$ORBO_FIRMWARE/main/control.c" "$ORBO_FIRMWARE/host_tests/test_control.c" \
  -o "$ORBO_TEST_BUILD/control"
"$ORBO_TEST_BUILD/control"
for profile in enabled disabled; do
  EXTRA=()
  if [ "$profile" = disabled ]; then EXTRA=(-DTEST_DISABLED); fi
  "$ORBO_CC" "${FLAGS[@]}" ${EXTRA[@]+"${EXTRA[@]}"} \
    -I"$ORBO_FIRMWARE/host_tests/flipper_stubs" \
    -I"$ORBO_FIRMWARE/host_tests/usb_stubs" -I"$ORBO_FIRMWARE/main" \
    "$ORBO_FIRMWARE/main/flippers.c" "$ORBO_FIRMWARE/host_tests/test_flippers.c" \
    -o "$ORBO_TEST_BUILD/flippers-$profile"
  "$ORBO_TEST_BUILD/flippers-$profile"
done
"$ORBO_CC" "${FLAGS[@]}" \
  -I"$ORBO_FIRMWARE/host_tests/bus_stubs" -I"$ORBO_FIRMWARE/host_tests/usb_stubs" \
  -I"$ORBO_FIRMWARE/main" -I"$ORBO_FIRMWARE/../../Software/Teleoperate/firmware/main" \
  "$ORBO_FIRMWARE/main/wheel_bus.c" "$ORBO_FIRMWARE/host_tests/test_wheel_bus.c" \
  -o "$ORBO_TEST_BUILD/wheel-bus"
"$ORBO_TEST_BUILD/wheel-bus"
for tick in 1000 10000; do
  "$ORBO_CC" "${FLAGS[@]}" -DTEST_TICK_US="$tick" \
    -I"$ORBO_FIRMWARE/host_tests/usb_stubs" \
    -I"$ORBO_FIRMWARE/components/esp_usbcdc_transport" \
    -I"$ORBO_FIRMWARE/components/esp_usbcdc_logging" \
    "$ORBO_FIRMWARE/components/esp_usbcdc_transport/esp_usbcdc_transport.c" \
    "$ORBO_FIRMWARE/components/esp_usbcdc_logging/esp_usbcdc_logging.c" \
    "$ORBO_FIRMWARE/host_tests/test_usbcdc.c" -o "$ORBO_TEST_BUILD/usbcdc-$tick"
  "$ORBO_TEST_BUILD/usbcdc-$tick"
  "$ORBO_CC" "${FLAGS[@]}" -DTEST_TICK_US="$tick" \
    -I"$ORBO_FIRMWARE/host_tests/usb_stubs" -I"$ORBO_FIRMWARE/main" \
    "$ORBO_FIRMWARE/main/usb_bootloader.c" \
    "$ORBO_FIRMWARE/host_tests/test_usb_bootloader.c" \
    -o "$ORBO_TEST_BUILD/usb-bootloader-$tick"
  "$ORBO_TEST_BUILD/usb-bootloader-$tick"
done
