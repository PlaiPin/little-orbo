/*
 * Teleop firmware: takes drive commands from the PC app over the USB-C cable (wired) AND over
 * Wi-Fi (wireless) at the same time, so the app can switch between them without reflashing.
 *
 *   Wired:    the ESP32-S3's built-in USB serial port, one command per line.
 *   Wireless: the ESP32 creates its own Wi-Fi network (robot at 192.168.4.1) and takes one
 *             command per UDP packet.
 *
 * Commands and replies are the same on both (see teleop.h). Every reply goes out on both links;
 * the app only listens to the one it uses. USB output is written without waiting, so a closed
 * app never blocks the program (the watchdog then stops the wheels).
 */
#include <string.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "driver/usb_serial_jtag.h"
#include "esp_event.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "nvs_flash.h"
#include "lwip/sockets.h"
#include "teleop.h"

static int s_sock = -1;
static struct sockaddr_in s_pc;             /* last PC that sent a UDP packet */
static bool s_have_pc;

static void send_line(const char *line)
{
    if (usb_serial_jtag_is_connected()) {
        usb_serial_jtag_write_bytes(line, strlen(line), 0);
        usb_serial_jtag_write_bytes("\n", 1, 0);
    }
    if (s_have_pc) {
        sendto(s_sock, line, strlen(line), 0, (struct sockaddr *)&s_pc, sizeof s_pc);
    }
}

static void usb_start(void)
{
    usb_serial_jtag_driver_config_t cfg = USB_SERIAL_JTAG_DRIVER_CONFIG_DEFAULT();
    cfg.tx_buffer_size = 2048;
    cfg.rx_buffer_size = 512;
    usb_serial_jtag_driver_install(&cfg);
}

static void wifi_start(void)
{
    esp_err_t e = nvs_flash_init();
    if (e == ESP_ERR_NVS_NO_FREE_PAGES || e == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        nvs_flash_erase();
        nvs_flash_init();
    }
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_ap();
    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    esp_wifi_set_storage(WIFI_STORAGE_RAM);
    wifi_config_t ap = {
        .ap = {
            .ssid = CONFIG_TELEOP_WIFI_SSID,
            .ssid_len = strlen(CONFIG_TELEOP_WIFI_SSID),
            .password = CONFIG_TELEOP_WIFI_PASS,
            .channel = CONFIG_TELEOP_WIFI_CHANNEL,
            .max_connection = 2,
            .authmode = WIFI_AUTH_WPA2_PSK,
        },
    };
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap));
    ESP_ERROR_CHECK(esp_wifi_start());
    esp_wifi_set_ps(WIFI_PS_NONE);          /* lowest latency */

    s_sock = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    struct sockaddr_in addr = {
        .sin_family = AF_INET,
        .sin_port = htons(CONFIG_TELEOP_UDP_PORT),
        .sin_addr.s_addr = htonl(INADDR_ANY),
    };
    bind(s_sock, (struct sockaddr *)&addr, sizeof addr);
    struct timeval tv = { .tv_sec = 0, .tv_usec = 5000 };   /* also paces the main loop */
    setsockopt(s_sock, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof tv);
}

/* Collect USB bytes into lines; hand each complete line to teleop. */
static void poll_usb(void)
{
    static char line[96];
    static size_t len;
    uint8_t buf[64];
    int n;
    while ((n = usb_serial_jtag_read_bytes(buf, sizeof buf, 0)) > 0) {
        for (int i = 0; i < n; i++) {
            char c = (char)buf[i];
            if (c == '\n' || c == '\r') {
                if (len) {
                    line[len] = 0;
                    teleop_command(line);
                    len = 0;
                }
            } else if (len < sizeof line - 1) {
                line[len++] = c;
            }
        }
    }
}

/* Wait up to 5 ms for one UDP packet. */
static void poll_udp(void)
{
    char line[96];
    struct sockaddr_in from;
    socklen_t from_len = sizeof from;
    int n = recvfrom(s_sock, line, sizeof line - 1, 0, (struct sockaddr *)&from, &from_len);
    if (n <= 0) {
        return;
    }
    while (n > 0 && (line[n - 1] == '\n' || line[n - 1] == '\r')) {
        n--;
    }
    line[n] = 0;
    s_pc = from;
    s_have_pc = true;
    teleop_command(line);
}

void app_main(void)
{
    usb_start();
    wifi_start();
    teleop_start(send_line);
    send_line("MSG Wi-Fi \"" CONFIG_TELEOP_WIFI_SSID "\" (password \"" CONFIG_TELEOP_WIFI_PASS
              "\"), robot at 192.168.4.1. USB cable works too.");
    while (true) {
        poll_usb();
        poll_udp();
        teleop_update();
    }
}
