// ESP-NOW sender for the Wheatley management rail.
//
// Wheatley (CoreS3) is the SENDER; a separate bridge MCU (classic M5Stack Core,
// MAC 80:7D:3A:DB:DC:08) owns the I2C wire to the Roller485 and ALL the safety
// (two-stage homing, soft limits, crash cutout, dead-man jog). This class just
// emits the shared RailCmdPacket over ESP-NOW and caches the RailStatusPacket the
// bridge streams back. No motion safety lives here — do not add any.
//
// Channel: ESP-NOW shares the radio with WiFi, so it can only run on the channel
// the station is currently associated on. We add the peer with channel 0 ("use
// current channel"), so sends automatically go out on Wheatley's live WiFi
// channel. The BRIDGE must be pinned to that same channel (a bridge-side change);
// WifiChannel() reports what that channel currently is.
//
// Enabled behind CONFIG_RAIL_ENABLED so the whole feature can be compiled out
// without disturbing avatar/vision/voice.
#pragma once

#include "sdkconfig.h"

#if CONFIG_RAIL_ENABLED

#include <cstdint>
#include "rail_espnow.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "esp_now.h"   // esp_now_recv_info_t (5.x recv-callback signature)

class RailDriver {
public:
    // Snapshot of the last status the bridge sent us, plus liveness metadata.
    struct Status {
        bool     linked;      // received a status packet within the last 3 s (fresh link)
        uint32_t age_ms;      // ms since the last status packet (UINT32_MAX if never)
        bool     homed;
        bool     crashed;
        bool     endstop;
        bool     moving;
        bool     power;       // 12V present on the Roller
        float    pos_mm;      // position from home
        int      rpm;
        float    vin;         // Roller input voltage
        uint16_t ack_seq;     // last command seq the bridge acknowledged
        uint16_t last_seq;    // last command seq we sent
        uint8_t  wifi_channel;// current STA channel (what the bridge must match)
    };

    static RailDriver& GetInstance();

    // One-time setup. Safe to call once WiFi (STA) has started; adds the bridge
    // as an ESP-NOW peer and registers the receive callback. Returns false if
    // ESP-NOW could not be initialised (e.g. WiFi not started yet) — callers may
    // retry. Idempotent.
    bool Init();
    bool initialized() const { return initialized_; }

    // Commands. Each returns true if the packet was handed to esp_now_send OK
    // (NOT that the bridge acted on it — check GetStatus() for that).
    bool Home();
    bool MoveMm(float mm);      // ABSOLUTE target from home (needs the bridge homed)
    bool NudgeMm(float mm);     // RELATIVE delta, clamped bridge-side to +/-100mm
    bool Stop();               // stop + hold (also aborts homing)
    bool Jog(int rpm);         // signed RPM, 0 = stop
    bool Ping();               // ask for a fresh status reply

    Status GetStatus();
    uint8_t WifiChannel();     // current associated STA channel, or 0 if not connected

private:
    RailDriver() = default;
    bool Send(uint8_t cmd, int32_t arg, bool quiet = false);
    static void OnRecvStatic(const esp_now_recv_info_t* info, const uint8_t* data, int len);
    void OnRecv(const uint8_t* src_mac, const uint8_t* data, int len);
    // Heartbeat: 4 Hz RCMD_PING so the bridge's channel-scanner locks onto us
    // and stays locked (its link timeout is 10 s). Started once by Init().
    static void HeartbeatTaskTrampoline(void* arg);
    void HeartbeatLoop();

    bool             initialized_ = false;
    SemaphoreHandle_t lock_ = nullptr;    // guards last_status_/last_rx_ms_
    RailStatusPacket last_status_{};      // most recent packet from the bridge
    bool             have_status_ = false;
    uint32_t         last_rx_ms_ = 0;     // esp_timer ms of last status
    uint16_t         seq_ = 0;            // outgoing command sequence
    TaskHandle_t     hb_task_ = nullptr;  // heartbeat task (never deleted)
};

// Registers the self.rail.* MCP tools (implemented in rail_mcp.cc). Call once
// from the board's RegisterMcpTools().
void RegisterRailMcpTools();

#endif  // CONFIG_RAIL_ENABLED
