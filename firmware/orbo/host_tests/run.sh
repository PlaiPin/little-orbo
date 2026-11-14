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
