#pragma once
#include <stdint.h>
extern uint32_t test_registers[3];
#define REG_CLR_BIT(r, b) (test_registers[r] &= ~(b))
#define REG_SET_BIT(r, b) (test_registers[r] |= (b))
#define RTC_CNTL_USB_CONF_REG 0
#define RTC_CNTL_OPTION1_REG 1
#define RTC_CNTL_SW_HW_USB_PHY_SEL (1u << 20)
#define RTC_CNTL_SW_USB_PHY_SEL (1u << 19)
#define RTC_CNTL_USB_PAD_ENABLE (1u << 18)
#define RTC_CNTL_FORCE_DOWNLOAD_BOOT 1u
