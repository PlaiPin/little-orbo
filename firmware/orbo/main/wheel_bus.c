#include "wheel_bus.h"
#include "dxl_bus.h"
#include "sdkconfig.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#if CONFIG_ORBO_DXL_BAUD == 115200
static esp_err_t ensure_baud(uint8_t id) {
    // Detect a previously completed/partial migration before writing EEPROM.
    // Never change an unknown model, ID, or motor with torque still enabled.
    for (int attempt=0; attempt<3; ++attempt) {
        uint16_t model=0;
        esp_err_t result=uart_set_baudrate(UART_NUM_1,CONFIG_ORBO_DXL_BAUD);
        if(result!=ESP_OK)return result;
        result=dxl_ping(id,&model);
        if(result==ESP_OK){
            if(model!=1060)return ESP_ERR_INVALID_RESPONSE;
            uint8_t baud=0;
            result=dxl_read(id,8,&baud,1);
            if(result==ESP_OK && baud==2)return ESP_OK;
            if(result==ESP_OK)return ESP_ERR_INVALID_RESPONSE;
            continue;
        }
        result=uart_set_baudrate(UART_NUM_1,1000000);
        if(result!=ESP_OK)return result;
        result=dxl_ping(id,&model);
        if(result!=ESP_OK)continue;
        if(model!=1060)return ESP_ERR_INVALID_RESPONSE;
        uint8_t torque=1,off=0,baud=2;
        result=dxl_write(id,64,&off,1);
        if(result!=ESP_OK)continue;
        result=dxl_read(id,64,&torque,1);
        if(result!=ESP_OK || torque!=0)continue;
        // The status reply may use either baud around the change. Verify by
        // ping + EEPROM read at the target rate, not by trusting this write.
        (void)dxl_write(id,8,&baud,1);
        vTaskDelay(pdMS_TO_TICKS(20));
        ESP_LOGI("wheel_bus","id=%u requested 115200 baud with torque off",id);
    }
    // Include verification after the last possible EEPROM write.
    esp_err_t result=uart_set_baudrate(UART_NUM_1,CONFIG_ORBO_DXL_BAUD);
    if(result!=ESP_OK)return result;
    uint16_t model=0;uint8_t baud=0;
    result=dxl_ping(id,&model);
    if(result!=ESP_OK || model!=1060)return ESP_ERR_INVALID_RESPONSE;
    result=dxl_read(id,8,&baud,1);
    return result==ESP_OK && baud==2 ? ESP_OK : ESP_ERR_INVALID_RESPONSE;
}
#endif

esp_err_t wheel_bus_init(const uint8_t ids[2]) {
    esp_err_t result=dxl_bus_init(UART_NUM_1,CONFIG_ORBO_DXL_TX,
                                CONFIG_ORBO_DXL_RX,CONFIG_ORBO_DXL_BAUD);
    if(result!=ESP_OK)return result;
#if CONFIG_ORBO_DXL_BAUD == 115200
    for(int i=0;i<2;i++){
        result=ensure_baud(ids[i]);
        // Leave the host at the configured rate even on an incomplete migration.
        esp_err_t restored=uart_set_baudrate(UART_NUM_1,CONFIG_ORBO_DXL_BAUD);
        if(result!=ESP_OK)return result;
        if(restored!=ESP_OK)return restored;
    }
    ESP_LOGI("wheel_bus","ids=%u,%u verified at 115200 baud",ids[0],ids[1]);
#else
    (void)ids;
#endif
    return ESP_OK;
}
