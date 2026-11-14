#include <assert.h>
#include <stdbool.h>
#include <stdio.h>
#include "wheel_bus.h"
#include "dxl_bus.h"
#include "freertos/task.h"

static const uint8_t ids[2]={1,2};
static unsigned host_baud,baud[2],model[2],torque[2],writes[2];
static bool torque_stuck,lose_write_reply,drop_first_write,missing;
static void reset(unsigned left,unsigned right){
    baud[0]=left;baud[1]=right;
    for(int i=0;i<2;i++){model[i]=1060;torque[i]=1;writes[i]=0;}
    torque_stuck=lose_write_reply=drop_first_write=missing=false;
}
esp_err_t uart_set_baudrate(uart_port_t port,unsigned value){
    assert(port==UART_NUM_1);host_baud=value;return ESP_OK;
}
esp_err_t dxl_bus_init(uart_port_t port,int tx,int rx,int rate){
    assert(tx==43 && rx==44);return uart_set_baudrate(port,rate);
}
static bool reachable(uint8_t id){return !missing && host_baud==baud[id-1];}
esp_err_t dxl_ping(uint8_t id,uint16_t *out){
    if(!reachable(id))return ESP_ERR_TIMEOUT;
    *out=model[id-1];return ESP_OK;
}
esp_err_t dxl_read(uint8_t id,uint16_t address,uint8_t *out,uint16_t length){
    assert(length==1);
    if(!reachable(id))return ESP_ERR_TIMEOUT;
    if(address==8)*out=baud[id-1]==115200?2:3;
    else {assert(address==64);*out=torque[id-1];}
    return ESP_OK;
}
esp_err_t dxl_write(uint8_t id,uint16_t address,const uint8_t *data,uint16_t length){
    assert(length==1);
    if(!reachable(id))return ESP_ERR_TIMEOUT;
    if(address==64){assert(*data==0);if(!torque_stuck)torque[id-1]=0;return ESP_OK;}
    assert(address==8 && *data==2 && torque[id-1]==0 && model[id-1]==1060);
    writes[id-1]++;
    if(drop_first_write && writes[id-1]==1)return ESP_FAIL;
    baud[id-1]=115200;
    return lose_write_reply?ESP_ERR_TIMEOUT:ESP_OK;
}
void vTaskDelay(TickType_t ticks){assert(ticks>0);}
int main(void){
    reset(1000000,1000000);lose_write_reply=true;
    assert(wheel_bus_init(ids)==ESP_OK);
    assert(baud[0]==115200 && baud[1]==115200 && torque[0]==0 && torque[1]==0);
    assert(writes[0]==1 && writes[1]==1);
    // A second boot must not rewrite the EEPROM.
    assert(wheel_bus_init(ids)==ESP_OK);
    assert(writes[0]==1 && writes[1]==1);
    reset(115200,1000000);drop_first_write=true;
    assert(wheel_bus_init(ids)==ESP_OK);
    assert(writes[0]==0 && writes[1]==2);
    reset(1000000,1000000);torque_stuck=true;
    assert(wheel_bus_init(ids)!=ESP_OK && writes[0]==0 && writes[1]==0);
    assert(host_baud==115200);
    reset(1000000,1000000);model[0]=999;
    assert(wheel_bus_init(ids)!=ESP_OK && writes[0]==0 && writes[1]==0);
    assert(host_baud==115200);
    reset(1000000,1000000);missing=true;
    assert(wheel_bus_init(ids)!=ESP_OK && writes[0]==0 && writes[1]==0);
    assert(host_baud==115200);
    puts("Wheel bus: torque-off migration, verification, partial recovery and no repeated EEPROM writes passed");
}
