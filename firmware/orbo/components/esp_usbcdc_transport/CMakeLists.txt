idf_component_register(SRCS "esp_usbcdc_transport.c"
    INCLUDE_DIRS "."
    REQUIRES espressif__esp_tinyusb micro_ros_espidf_component # include <uxr/client/transport.h>
)
