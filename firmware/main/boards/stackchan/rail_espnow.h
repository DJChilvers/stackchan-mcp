// Shared ESP-NOW protocol for the Wheatley rail.
// Copy this file verbatim into the sender sketch so both ends agree.
#pragma once
#include <stdint.h>

static const uint8_t RAIL_MAGIC_CMD = 0xA5;   // Wheatley -> bridge
static const uint8_t RAIL_MAGIC_STS = 0x5A;   // bridge  -> Wheatley
static const uint8_t RAIL_CHANNEL   = 1;       // both ends pinned here for the bench

enum RailCmd : uint8_t {
    RCMD_PING     = 0,   // no-op, just asks for a status reply
    RCMD_HOME     = 1,   // run the two-stage homing routine
    RCMD_MOVE_MM  = 2,   // arg = ABSOLUTE target from home, in mm x10 (0.1mm)
    RCMD_NUDGE_MM = 3,   // arg = RELATIVE delta, in mm x10 (signed)
    RCMD_STOP     = 4,   // stop + hold (also aborts homing)
    RCMD_JOG      = 5,   // arg = signed RPM (0 = stop)
};

struct __attribute__((packed)) RailCmdPacket {
    uint8_t  magic;      // RAIL_MAGIC_CMD
    uint8_t  cmd;        // RailCmd
    int32_t  arg;        // command argument
    uint16_t seq;        // sender sequence number (echoed back in status.ack_seq)
};

enum RailFlag : uint8_t {
    RF_HOMED   = 1 << 0,
    RF_CRASHED = 1 << 1,
    RF_ENDSTOP = 1 << 2,   // limit switch currently pressed
    RF_MOVING  = 1 << 3,
    RF_POWER   = 1 << 4,   // 12V present on the Roller
};

struct __attribute__((packed)) RailStatusPacket {
    uint8_t  magic;      // RAIL_MAGIC_STS
    uint8_t  flags;      // RailFlag bits
    int32_t  pos_mm10;   // current position from home, mm x10
    int16_t  rpm;        // actual RPM
    uint16_t vin_cv;     // input voltage in centivolts (1216 = 12.16 V)
    uint16_t ack_seq;    // last command seq processed
};
