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
#include "freertos/queue.h"
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

    // Commands. Each returns true if the packet was QUEUED for transmission OK
    // (NOT that it hit the air, and NOT that the bridge acted on it — verify
    // delivery via GetStatus().ack_seq >= the reply's last_seq, as ever). All
    // actual esp_now_send calls happen on the single rail_tx task — see TxLoop.
    bool Home();
    bool MoveMm(float mm);      // ABSOLUTE target from home (needs the bridge homed)
    bool NudgeMm(float mm);     // RELATIVE delta, clamped bridge-side to +/-100mm
    bool Stop();               // stop + hold (also aborts homing)
    bool Jog(int rpm);         // signed RPM, 0 = stop
    bool Ping();               // ask for a fresh status reply

    Status GetStatus();
    uint8_t WifiChannel();     // current associated STA channel, or 0 if not connected

private:
    // One queued outbound packet. seq is stamped at ENQUEUE time (under
    // seq_mux_) so the MCP tool reply's last_seq still equals the command it
    // just issued — the ack-verify harness contract (ack_seq >= last_seq).
    struct TxItem {
        RailCmdPacket pkt;
        bool          quiet;   // heartbeat pings: no per-send logging
    };

    RailDriver() = default;
    bool Send(uint8_t cmd, int32_t arg, bool quiet = false);
    static void OnRecvStatic(const esp_now_recv_info_t* info, const uint8_t* data, int len);
    void OnRecv(const uint8_t* src_mac, const uint8_t* data, int len);
    // TX ISOLATION (2026-07-16, the rail-follow lockup fix — firmware/TODO.md
    // "RailDriver TX isolation"): rail_tx is the ONLY code that ever calls
    // esp_now_send. Commands enqueue; the task drains the queue with a min gap
    // between sends, and fills idle 250 ms gaps with a quiet RCMD_PING so the
    // bridge's channel-scanner stays locked (its link timeout is 10 s; ANY
    // valid packet — command or ping — bumps its lastRxMs, so commands
    // themselves keep the link alive and pings only fill silence). Replaces
    // the old rail_hb task, which raced Send() from the MCP task context
    // (two unsynchronised esp_now_send callers + a shared ++seq_) — the
    // concurrency implicated in the hard lockups (CRASH_LOG A1 #4/#9).
    static void TxTaskTrampoline(void* arg);
    void TxLoop();
    bool TransmitNow(const TxItem& item);   // rail_tx task context ONLY

    bool             initialized_ = false;
    SemaphoreHandle_t lock_ = nullptr;    // guards last_status_/last_rx_ms_
    RailStatusPacket last_status_{};      // most recent packet from the bridge
    bool             have_status_ = false;
    uint32_t         last_rx_ms_ = 0;     // esp_timer ms of last status
    uint16_t         seq_ = 0;            // outgoing command sequence (seq_mux_)
    portMUX_TYPE     seq_mux_ = portMUX_INITIALIZER_UNLOCKED;
    QueueHandle_t    tx_q_ = nullptr;     // TxItem queue -> rail_tx task
    TaskHandle_t     tx_task_ = nullptr;  // the single TX task (never deleted)
};

// Registers the self.rail.* MCP tools (implemented in rail_mcp.cc). Call once
// from the board's RegisterMcpTools().
void RegisterRailMcpTools();

#endif  // CONFIG_RAIL_ENABLED
