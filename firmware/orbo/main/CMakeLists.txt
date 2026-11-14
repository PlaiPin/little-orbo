idf_component_register(SRCS "main.c" "control.c" "actuators.c" "usb_bootloader.c"
  "../../../Software/Teleoperate/firmware/main/dxl_bus.c"
  INCLUDE_DIRS "." "../../../Software/Teleoperate/firmware/main"
  REQUIRES micro_ros_espidf_component esp_usbcdc_transport esp_usbcdc_logging esp_driver_uart esp_driver_gpio esp_driver_ledc esp_timer)
