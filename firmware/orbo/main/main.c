#include <string.h>
#include "actuators.h"
#include "control.h"
#include "usb_bootloader.h"
#include "esp_heap_caps.h"
#include "esp_system.h"
#include "esp_random.h"
#include "esp_task_wdt.h"
#include "esp_timer.h"
#include "esp_usbcdc_transport.h"
#include "esp_usbcdc_logging.h"
#include "tinyusb.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"
#include <rcl/rcl.h>
#include <rclc/rclc.h>
#include <rclc/executor.h>
#include <rmw_microros/rmw_microros.h>
#include <orbo_embedded_interfaces/msg/control_session.h>
#include <orbo_embedded_interfaces/msg/wheel_command.h>
#include <orbo_embedded_interfaces/msg/flipper_command.h>
#include <orbo_embedded_interfaces/msg/wheel_state.h>
#include <orbo_embedded_interfaces/msg/actuator_status.h>
#include <orbo_embedded_interfaces/msg/health.h>

static orbo_control_t control;
static orbo_feedback_t feedback;
static portMUX_TYPE state_lock=portMUX_INITIALIZER_UNLOCKED;
static QueueHandle_t session_queue,status_queue;
static uint64_t link_seen_us=0;
static TaskHandle_t control_task_handle,transport_task_handle;
static uint64_t status_seq=0,stop_generation=0;
static bool io_ready=false,flipper_io_ready=false;
static bool bootloader_requested=false; // Protected by state_lock; latched until reset.

static void request_bootloader(void){
    portENTER_CRITICAL(&state_lock);
    bootloader_requested=true;
    stop_generation++;
    orbo_disarm(&control,ORBO_DISARMED);
    portEXIT_CRITICAL(&state_lock);
}

static void status(uint64_t command,uint8_t state,uint16_t reason){
    orbo_embedded_interfaces__msg__ActuatorStatus msg={0};
    portENTER_CRITICAL(&state_lock);
    msg.boot_id=control.boot;msg.epoch=control.epoch;msg.sequence=++status_seq;
    portEXIT_CRITICAL(&state_lock);
    msg.command_id=command;msg.state=state;msg.reason=reason;
    msg.mono_us=esp_timer_get_time();msg.evidence=1;
    if(xQueueSend(status_queue,&msg,0)!=pdTRUE){
        portENTER_CRITICAL(&state_lock);orbo_disarm(&control,ORBO_BUS);portEXIT_CRITICAL(&state_lock);
    }
}
static void fault(uint16_t reason){
    portENTER_CRITICAL(&state_lock);stop_generation++;orbo_disarm(&control,reason);portEXIT_CRITICAL(&state_lock);
}
static void session_callback(const void *input){
    const orbo_embedded_interfaces__msg__ControlSession *msg=input;
    if(msg->operation==0){xQueueReset(session_queue);fault(ORBO_DISARMED);status(0,5,ORBO_DISARMED);return;}
    if(msg->operation!=1 || msg->interface_version!=1){status(0,4,ORBO_BOUNDS);return;}
    // One latest arm request; the motor task alone performs serial configuration.
    xQueueOverwrite(session_queue,msg);
}
static void wheel_callback(const void *input){
    const orbo_embedded_interfaces__msg__WheelCommand *msg=input;
    uint64_t now=rmw_uros_epoch_millis(),mono=esp_timer_get_time();
    portENTER_CRITICAL(&state_lock);
    bool accepted=orbo_wheels(&control,msg->boot_id,msg->epoch,msg->sequence,msg->command_id,
        msg->issued_epoch_ms,msg->valid_for_ms,now,mono,msg->velocity_rad_s);
    portEXIT_CRITICAL(&state_lock);
    // Wheel stream admission is observable in wheel_state.last_command_sequence.
    if(!accepted)status(msg->command_id,4,ORBO_STALE);
}
static void flipper_callback(const void *input){
    const orbo_embedded_interfaces__msg__FlipperCommand *msg=input;
    uint64_t now=rmw_uros_epoch_millis(),mono=esp_timer_get_time();
    portENTER_CRITICAL(&state_lock);
    bool accepted=orbo_flippers(&control,msg->boot_id,msg->epoch,msg->sequence,msg->command_id,
        msg->issued_epoch_ms,msg->valid_for_ms,now,mono,msg->pulse_us,msg->duration_ms);
    portEXIT_CRITICAL(&state_lock);
    status(msg->command_id,accepted?1:4,accepted?ORBO_OK:ORBO_STALE);
}
static void safety_task(void *unused){
    (void)unused;
    bool was_armed=false;
    for(;;){
        uint64_t now=esp_timer_get_time(),command;
        bool done,armed;uint16_t reason,pulses[2];
        portENTER_CRITICAL(&state_lock);
        if(control.armed && (now-link_seen_us>500000))orbo_disarm(&control,ORBO_BUS);
        bool fresh=feedback.valid[0] && feedback.valid[1] &&
            now-feedback.sample_us[0]<200000 && now-feedback.sample_us[1]<200000;
        done=orbo_tick(&control,now,fresh);command=control.flipper_command_id;
        armed=control.armed;reason=control.fault;
        memcpy(pulses,control.pulse,sizeof(pulses));
        portEXIT_CRITICAL(&state_lock);
        if(armed){
#ifdef CONFIG_ORBO_FLIPPERS_CALIBRATED
            if(flippers_set(pulses)!=ESP_OK){
                fault(ORBO_BUS);flippers_disable();
                done=false;armed=false;reason=ORBO_BUS;
            }
#endif
        }else flippers_disable();
        if(done)status(command,3,ORBO_OK); // PWM timing only; no encoder on the 9G servo.
        if(was_armed && !armed)status(command,6,reason);
        was_armed=armed;
        vTaskDelay(pdMS_TO_TICKS(5));
    }
}
static void motor_task(void *unused){
    (void)unused;
    esp_task_wdt_add(NULL);
    bool previously_enabled=false;
    for(;;){
        esp_task_wdt_reset();
        portENTER_CRITICAL(&state_lock);
        bool download=bootloader_requested;
        portEXIT_CRITICAL(&state_lock);
        if(download){
            xQueueReset(session_queue);
            // The motor task owns all bus access, including an in-flight arm.
            // No queued/new arm can pass the latched request below.
            if(io_ready)wheels_disable();
            flippers_disable();
            // Also allow the configured 100 ms motor bus watchdog to expire
            // if a stop/torque-off write failed. No more motor traffic is sent.
            vTaskDelay(pdMS_TO_TICKS(150));
            usb_bootloader_restart();
        }
        orbo_embedded_interfaces__msg__ControlSession request;
        if(xQueueReceive(session_queue,&request,0)==pdTRUE){
            portENTER_CRITICAL(&state_lock);
            uint64_t arm_generation=stop_generation;
            bool eligible=!bootloader_requested && !control.armed && request.boot_id==control.boot && request.epoch>control.epoch;
            portEXIT_CRITICAL(&state_lock);
            // Stationary flipper sessions do not require a powered wheel bus.
            bool prepared=eligible && flipper_io_ready && (!request.enable_wheels || io_ready);
            if(prepared && request.enable_wheels)prepared=wheels_prepare(true)==ESP_OK;
            if(prepared){
                orbo_feedback_t measured={0};
                if(request.enable_wheels){wheels_sample(&measured);prepared=measured.valid[0]&&measured.valid[1];}
                portENTER_CRITICAL(&state_lock);
                if(request.enable_wheels)feedback=measured;
                prepared=prepared && !bootloader_requested && arm_generation==stop_generation && orbo_arm(&control,request.boot_id,request.epoch,request.sequence,
                    request.issued_epoch_ms,request.valid_for_ms,rmw_uros_epoch_millis(),request.enable_wheels,prepared);
                portEXIT_CRITICAL(&state_lock);
            }
            if(!prepared)wheels_disable();
            status(0,prepared?1:4,prepared?ORBO_OK:ORBO_FEEDBACK);
        }
        portENTER_CRITICAL(&state_lock);
        bool enabled=control.armed && control.wheels_enabled;
        double velocity[2]={control.velocity[0],control.velocity[1]};
        portEXIT_CRITICAL(&state_lock);
        if(enabled){if(wheels_set(velocity)!=ESP_OK)fault(ORBO_BUS);}
        else if(previously_enabled)wheels_disable();
        previously_enabled=enabled;
        orbo_feedback_t measured;
        portENTER_CRITICAL(&state_lock);measured=feedback;portEXIT_CRITICAL(&state_lock);
        if(io_ready)wheels_sample(&measured);
        portENTER_CRITICAL(&state_lock);feedback=measured;portEXIT_CRITICAL(&state_lock);
        // An expiry during bounded bus reads is handled before any new setpoint.
        portENTER_CRITICAL(&state_lock);enabled=control.armed && control.wheels_enabled;portEXIT_CRITICAL(&state_lock);
        if(previously_enabled && !enabled){wheels_disable();previously_enabled=false;}
        vTaskDelay(pdMS_TO_TICKS(1));
    }
}

static void cleanup_result(rcl_ret_t result){if(result!=RCL_RET_OK)rcl_reset_error();}
#define CHECK(call) do{if((call)!=RCL_RET_OK){fault(ORBO_BUS);goto cleanup;}}while(0)
static void transport_task(void *unused){
    (void)unused;
    static tinyusb_cdcacm_itf_t cdc=TINYUSB_CDC_ACM_0;
    uint64_t sequence=0; // Monotonic within a boot, including session reconnects.
    if(rmw_uros_set_custom_transport(true,&cdc,esp_usbcdc_open,esp_usbcdc_close,esp_usbcdc_write,esp_usbcdc_read)!=RMW_RET_OK){fault(ORBO_BUS);vTaskDelete(NULL);return;}
    for(;;){
        // Discovery, synchronization and reconnection never arm actuators.
        if(rmw_uros_ping_agent(100,1)!=RMW_RET_OK){vTaskDelay(pdMS_TO_TICKS(200));continue;}
        rcl_allocator_t allocator=rcl_get_default_allocator();
        rclc_support_t support={0};rcl_node_t node=rcl_get_zero_initialized_node();
        rcl_init_options_t options=rcl_get_zero_initialized_init_options();
        rclc_executor_t executor=rclc_executor_get_zero_initialized_executor();
        rcl_publisher_t pubs[3]={rcl_get_zero_initialized_publisher(),rcl_get_zero_initialized_publisher(),rcl_get_zero_initialized_publisher()};
        rcl_subscription_t subs[3]={rcl_get_zero_initialized_subscription(),rcl_get_zero_initialized_subscription(),rcl_get_zero_initialized_subscription()};
        bool options_ok=false,support_ok=false,node_ok=false,executor_ok=false;
        int pub_count=0,sub_count=0;
        CHECK(rcl_init_options_init(&options,allocator));options_ok=true;
        CHECK(rcl_init_options_set_domain_id(&options,CONFIG_ORBO_ROS_DOMAIN_ID));
        CHECK(rclc_support_init_with_options(&support,0,NULL,&options,&allocator));support_ok=true;
        CHECK(rclc_node_init_default(&node,"orbo_mcu","/orbo",&support));node_ok=true;
        CHECK(rclc_publisher_init_best_effort(&pubs[0],&node,ROSIDL_GET_MSG_TYPE_SUPPORT(orbo_embedded_interfaces,msg,WheelState),"embedded/wheel_state"));pub_count++;
        CHECK(rclc_publisher_init_best_effort(&pubs[1],&node,ROSIDL_GET_MSG_TYPE_SUPPORT(orbo_embedded_interfaces,msg,ActuatorStatus),"embedded/actuator_status"));pub_count++;
        CHECK(rclc_publisher_init_best_effort(&pubs[2],&node,ROSIDL_GET_MSG_TYPE_SUPPORT(orbo_embedded_interfaces,msg,Health),"embedded/health"));pub_count++;
        CHECK(rclc_subscription_init_default(&subs[0],&node,ROSIDL_GET_MSG_TYPE_SUPPORT(orbo_embedded_interfaces,msg,ControlSession),"embedded/control_session"));sub_count++;
        CHECK(rclc_subscription_init_default(&subs[1],&node,ROSIDL_GET_MSG_TYPE_SUPPORT(orbo_embedded_interfaces,msg,WheelCommand),"embedded/wheel_command"));sub_count++;
        CHECK(rclc_subscription_init_default(&subs[2],&node,ROSIDL_GET_MSG_TYPE_SUPPORT(orbo_embedded_interfaces,msg,FlipperCommand),"embedded/flipper_command"));sub_count++;
        CHECK(rclc_executor_init(&executor,&support.context,3,&allocator));executor_ok=true;
        orbo_embedded_interfaces__msg__ControlSession session={0};
        orbo_embedded_interfaces__msg__WheelCommand wheel={0};
        orbo_embedded_interfaces__msg__FlipperCommand flipper={0};
        CHECK(rclc_executor_add_subscription(&executor,&subs[0],&session,session_callback,ON_NEW_DATA));
        CHECK(rclc_executor_add_subscription(&executor,&subs[1],&wheel,wheel_callback,ON_NEW_DATA));
        CHECK(rclc_executor_add_subscription(&executor,&subs[2],&flipper,flipper_callback,ON_NEW_DATA));
        if(rmw_uros_sync_session(1000)!=RMW_RET_OK)goto cleanup;
        portENTER_CRITICAL(&state_lock);control.clock_synced=true;link_seen_us=esp_timer_get_time();portEXIT_CRITICAL(&state_lock);
        uint64_t last_ping=0;
        for(;;){
            rcl_ret_t spin=rclc_executor_spin_some(&executor,RCL_MS_TO_NS(2));
            if(spin!=RCL_RET_OK && spin!=RCL_RET_TIMEOUT)goto cleanup;
            uint64_t now=esp_timer_get_time();
            if(now-last_ping>200000){
                if(rmw_uros_ping_agent(50,1)!=RMW_RET_OK)goto cleanup;
                portENTER_CRITICAL(&state_lock);link_seen_us=esp_timer_get_time();portEXIT_CRITICAL(&state_lock);
                last_ping=now;
            }
            orbo_embedded_interfaces__msg__WheelState sample={0};
            orbo_embedded_interfaces__msg__Health health={0};
            portENTER_CRITICAL(&state_lock);
            sample.boot_id=control.boot;sample.epoch=control.epoch;sample.armed=control.armed;
            sample.last_command_sequence=control.wheel_sequence;
            memcpy(sample.position_rad,feedback.position,sizeof(feedback.position));
            memcpy(sample.velocity_rad_s,feedback.velocity,sizeof(feedback.velocity));
            memcpy(sample.voltage,feedback.voltage,sizeof(feedback.voltage));
            memcpy(sample.valid,feedback.valid,sizeof(feedback.valid));
            memcpy(sample.motor_sample_mono_us,feedback.sample_us,sizeof(feedback.sample_us));
            memcpy(sample.read_errors,feedback.read_errors,sizeof(feedback.read_errors));
            memcpy(sample.hardware_error,feedback.hardware_error,sizeof(feedback.hardware_error));
            health.interface_version=1;health.boot_id=control.boot;health.epoch=control.epoch;
            health.armed=control.armed;health.clock_synced=control.clock_synced;health.fault=control.fault;
            // Stamp the completed snapshot, not the time before a blocking agent ping.
            // Every copied motor reading must precede this enclosing sample timestamp.
            sample.sample_mono_us=esp_timer_get_time();
            portEXIT_CRITICAL(&state_lock);
            sample.sequence=++sequence;sample.epoch_ms=rmw_uros_epoch_millis();
            CHECK(rcl_publish(&pubs[0],&sample,NULL));
            orbo_embedded_interfaces__msg__ActuatorStatus event;
            while(xQueueReceive(status_queue,&event,0)==pdTRUE)CHECK(rcl_publish(&pubs[1],&event,NULL));
            if(sequence%10==0){
                health.mono_us=sample.sample_mono_us;health.epoch_ms=sample.epoch_ms;
                health.free_heap=esp_get_free_heap_size();health.minimum_free_heap=esp_get_minimum_free_heap_size();
                health.control_stack_watermark=uxTaskGetStackHighWaterMark(control_task_handle);
                health.transport_stack_watermark=uxTaskGetStackHighWaterMark(NULL);
                CHECK(rcl_publish(&pubs[2],&health,NULL));
            }
            vTaskDelay(pdMS_TO_TICKS(18));
        }
cleanup:
        fault(ORBO_BUS);
        portENTER_CRITICAL(&state_lock);control.clock_synced=false;portEXIT_CRITICAL(&state_lock);
        if(support_ok)rmw_uros_set_context_entity_destroy_session_timeout(rcl_context_get_rmw_context(&support.context),0);
        if(executor_ok)(void)rclc_executor_fini(&executor);
        for(int i=0;i<sub_count;i++)cleanup_result(rcl_subscription_fini(&subs[i],&node));
        for(int i=0;i<pub_count;i++)cleanup_result(rcl_publisher_fini(&pubs[i],&node));
        if(node_ok)cleanup_result(rcl_node_fini(&node));
        if(support_ok)(void)rclc_support_fini(&support);
        if(options_ok)cleanup_result(rcl_init_options_fini(&options));
        rcl_reset_error();
        vTaskDelay(pdMS_TO_TICKS(200));
    }
}
void app_main(void){
    // Driver/PHY lifetime is the boot lifetime, independent of XRCE sessions.
    const tinyusb_config_t usb_config={.external_phy=false};
    ESP_ERROR_CHECK(tinyusb_driver_install(&usb_config));
    ESP_ERROR_CHECK(esp_usbcdc_transport_init());
    ESP_ERROR_CHECK(esp_usbcdc_logging_init());
    uint64_t boot=((uint64_t)esp_random()<<32)|esp_random();
    orbo_control_init(&control,boot,CONFIG_ORBO_MAX_MRAD_S/1000.0);
#ifdef CONFIG_ORBO_FLIPPERS_CALIBRATED
    for(int i=0;i<2;i++){
        control.pulse_min[i]=CONFIG_ORBO_FLIPPER_MIN_US;control.pulse_max[i]=CONFIG_ORBO_FLIPPER_MAX_US;
        control.pulse_rest[i]=CONFIG_ORBO_FLIPPER_REST_US;control.pulse[i]=control.pulse_rest[i];
    }
#endif
    session_queue=xQueueCreate(1,sizeof(orbo_embedded_interfaces__msg__ControlSession));
    status_queue=xQueueCreate(16,sizeof(orbo_embedded_interfaces__msg__ActuatorStatus));
    configASSERT(session_queue && status_queue);
    io_ready=actuators_init()==ESP_OK;
    flipper_io_ready=flippers_init()==ESP_OK;
    ESP_ERROR_CHECK(usb_bootloader_init(request_bootloader));
    configASSERT(xTaskCreate(motor_task,"motor_io",6144,NULL,8,&control_task_handle)==pdPASS);
    configASSERT(xTaskCreate(safety_task,"expiry_pwm",3072,NULL,12,NULL)==pdPASS);
    configASSERT(xTaskCreate(transport_task,"micro_ros",16384,NULL,5,&transport_task_handle)==pdPASS);
}
