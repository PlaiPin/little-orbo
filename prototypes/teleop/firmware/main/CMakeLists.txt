idf_component_register(SRCS "main.c" "teleop.c" "wheels.c" "dxl_bus.c"
                       INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_driver_uart esp_driver_gpio esp_driver_usb_serial_jtag esp_timer
                                     esp_wifi esp_netif esp_event nvs_flash lwip)
