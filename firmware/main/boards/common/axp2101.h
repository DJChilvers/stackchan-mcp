#ifndef __AXP2101_H__
#define __AXP2101_H__

#include "i2c_device.h"

class Axp2101 : public I2cDevice {
public:
    Axp2101(i2c_master_bus_handle_t i2c_bus, uint8_t addr);
    bool IsCharging();
    bool IsDischarging();
    bool IsChargingDone();
    int GetBatteryLevel();
    float GetTemperature();
    void PowerOff();

    // Hardware watchdog — auto-recovers a firmware lockup by power-cycling the PMIC
    // rails (battery-independent). See Documents/StackChan/PMIC_WATCHDOG_PLAN.md.
    void ConfigWatchdog(uint8_t reset_cfg, uint8_t timeout); // reset_cfg 0..3 (0b11=full power-cycle); timeout 0..7 (0b101=32s)
    void EnableWatchdog();
    void DisableWatchdog();
    void FeedWatchdog();

private:
    int GetBatteryCurrentDirection();
};

#endif
