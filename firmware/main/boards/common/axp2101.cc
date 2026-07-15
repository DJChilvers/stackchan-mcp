#include "axp2101.h"
#include "board.h"
#include "display.h"

#include <esp_log.h>

#define TAG "Axp2101"

Axp2101::Axp2101(i2c_master_bus_handle_t i2c_bus, uint8_t addr) : I2cDevice(i2c_bus, addr) {
}

int Axp2101::GetBatteryCurrentDirection() {
    return (ReadReg(0x01) & 0b01100000) >> 5;
}

bool Axp2101::IsCharging() {
    return GetBatteryCurrentDirection() == 1;
}

bool Axp2101::IsDischarging() {
    return GetBatteryCurrentDirection() == 2;
}

bool Axp2101::IsChargingDone() {
    uint8_t value = ReadReg(0x01);
    return (value & 0b00000111) == 0b00000100;
}

int Axp2101::GetBatteryLevel() {
    return ReadReg(0xA4);
}

float Axp2101::GetTemperature() {
    return ReadReg(0xA5);
}

void Axp2101::PowerOff() {
    DisableWatchdog();   // don't let the WDT re-power us right after a commanded off
    uint8_t value = ReadReg(0x10);
    value = value | 0x01;
    WriteReg(0x10, value);
}

// --- Hardware watchdog (regs confirmed from XPowersLib + AXP2101 datasheet) ---
// 0x18 bit0 = WDT enable. 0x19: bits5:4 = reset action (0b11 = full DCDC/LDO
// power-off+on = cold reboot), bits2:0 = timeout, bit3 = feed/clear strobe.
void Axp2101::ConfigWatchdog(uint8_t reset_cfg, uint8_t timeout) {
    uint8_t v = ReadReg(0x19);
    v &= ~0x37;                                      // clear bits 5:4 (action) + 2:0 (timeout)
    v |= ((reset_cfg & 0x3) << 4) | (timeout & 0x7);
    WriteReg(0x19, v);
}

void Axp2101::FeedWatchdog() {                       // pet: strobe bit 3 of 0x19
    WriteReg(0x19, ReadReg(0x19) | (1 << 3));
}

void Axp2101::EnableWatchdog() {                     // 0x18 bit0 = 1
    WriteReg(0x18, ReadReg(0x18) | 0x01);
}

void Axp2101::DisableWatchdog() {                    // 0x18 bit0 = 0
    WriteReg(0x18, ReadReg(0x18) & ~0x01);
}
