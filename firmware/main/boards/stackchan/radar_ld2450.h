// HLK-LD2450 24 GHz multi-target radar on Grove Port C (UART).
//
// The module streams tracking frames autonomously at ~10 Hz on its UART TX
// (256000 baud 8N1) — no request/response is needed for position data. This
// driver owns UART2 (UART0 = console, UART1 = SCS0009 servo bus), runs a
// FreeRTOS reader task that reassembles and validates frames, and keeps a
// mutex-guarded latest-snapshot + timestamp that the self.presence.read MCP
// tool reads. Pattern-matches rail_driver.{h,cc}: singleton, reader task,
// snapshot accessor.
//
// Graceful with NO module attached: the UART simply never yields a valid
// frame, the absence is logged ONCE (not repeatedly), and the MCP tool
// returns {ok:false, error:"no radar frames"}.
//
// Enabled behind CONFIG_RADAR_ENABLED so the whole feature can be compiled
// out without disturbing servo/avatar/rail.
#pragma once

#include "sdkconfig.h"

#if CONFIG_RADAR_ENABLED

#include <cstdint>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

class RadarLd2450 {
public:
    static constexpr int kMaxTargets = 3;   // fixed 3 target slots per frame

    struct Target {
        bool     valid;      // false = all-zero slot (no target tracked)
        int16_t  x_mm;       // lateral offset from boresight
        int16_t  y_mm;       // forward distance from the radar face
        int16_t  speed_cms;  // radial speed, sign per LD2450 convention
        uint16_t res_mm;     // distance resolution the module reports
    };

    struct Snapshot {
        bool     have_frame; // ever decoded a valid frame since boot
        uint32_t age_ms;     // ms since the last valid frame (UINT32_MAX if never)
        Target   targets[kMaxTargets];
    };

    static RadarLd2450& GetInstance();

    // One-time setup: installs the UART2 driver on the Port C pins and starts
    // the reader task. Returns false only on driver/task allocation failure
    // (NOT on module absence — that is detected passively). Idempotent.
    bool Init();
    bool initialized() const { return initialized_; }

    Snapshot GetSnapshot();

private:
    RadarLd2450() = default;
    static void ReaderTaskTrampoline(void* arg);
    void ReaderLoop();
    void ParseFrame(const uint8_t* frame);   // one 30-byte frame incl. header/tail

    bool              initialized_ = false;
    SemaphoreHandle_t lock_ = nullptr;       // guards targets_/have_frame_/last_rx_ms_
    Target            targets_[kMaxTargets] = {};
    bool              have_frame_ = false;
    uint32_t          last_rx_ms_ = 0;
    TaskHandle_t      task_ = nullptr;       // reader task (never deleted)
};

// Registers the self.presence.read MCP tool (implemented in radar_mcp.cc).
// Call once from the board's RegisterMcpTools().
void RegisterRadarMcpTools();

#endif  // CONFIG_RADAR_ENABLED
