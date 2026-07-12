#include "rail_driver.h"

#if CONFIG_RAIL_ENABLED

#include <cstring>
#include <cmath>
#include "esp_now.h"
#include "esp_wifi.h"
#include "esp_timer.h"
#include "esp_log.h"

#define TAG "RailDriver"

// The bench bridge (classic M5Stack Core) that owns the Roller485 + all safety.
static const uint8_t kBridgeMac[6] = {0x80, 0x7D, 0x3A, 0xDB, 0xDC, 0x08};

static inline uint32_t now_ms() {
    return (uint32_t)(esp_timer_get_time() / 1000);
}

RailDriver& RailDriver::GetInstance() {
    static RailDriver instance;
    return instance;
}

bool RailDriver::Init() {
    if (initialized_) return true;

    if (lock_ == nullptr) {
        lock_ = xSemaphoreCreateMutex();
        if (lock_ == nullptr) {
            ESP_LOGE(TAG, "failed to create status mutex");
            return false;
        }
    }

    esp_err_t err = esp_now_init();
    if (err != ESP_OK) {
        // Usually means WiFi isn't started yet — caller can retry later.
        ESP_LOGW(TAG, "esp_now_init failed: %s (retry once WiFi is up)", esp_err_to_name(err));
        return false;
    }

    err = esp_now_register_recv_cb(&RailDriver::OnRecvStatic);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "register_recv_cb failed: %s", esp_err_to_name(err));
        esp_now_deinit();
        return false;
    }

    // Peer on channel 0 = "current channel": ESP-NOW rides whatever channel the
    // station is associated on. The bridge must be pinned to that same channel.
    esp_now_peer_info_t peer = {};
    std::memcpy(peer.peer_addr, kBridgeMac, 6);
    peer.channel = 0;
    peer.ifidx   = WIFI_IF_STA;
    peer.encrypt = false;
    err = esp_now_add_peer(&peer);
    if (err != ESP_OK && err != ESP_ERR_ESPNOW_EXIST) {
        ESP_LOGE(TAG, "add_peer failed: %s", esp_err_to_name(err));
        esp_now_deinit();
        return false;
    }

    // Modem-sleep deafens ESP-NOW RX: with MIN/MAX_MODEM the radio only wakes
    // for DTIM beacons, so the bridge's status packets are silently dropped.
    // The PMIC/battery power-save path had set max_modem — force it off now
    // that ESP-NOW is up. (The heartbeat task re-asserts this periodically:
    // other firmware paths can silently restore power save.)
    esp_wifi_set_ps(WIFI_PS_NONE);

    initialized_ = true;
    ESP_LOGI(TAG, "ESP-NOW rail sender ready; bridge %02X:%02X:%02X:%02X:%02X:%02X, wifi ch %u",
             kBridgeMac[0], kBridgeMac[1], kBridgeMac[2], kBridgeMac[3], kBridgeMac[4], kBridgeMac[5],
             WifiChannel());

    // Heartbeat: ping the bridge at 4 Hz so its channel-scanner locks onto our
    // WiFi channel and stays locked (bridge link timeout is 10 s). Started once
    // per boot; Init() is idempotent so this can't run twice.
    if (hb_task_ == nullptr) {
        BaseType_t ok = xTaskCreate(&RailDriver::HeartbeatTaskTrampoline, "rail_hb",
                                    3072, this, tskIDLE_PRIORITY + 1, &hb_task_);
        if (ok != pdPASS) {
            ESP_LOGE(TAG, "failed to create rail_hb task (bridge may unlock after idle)");
            hb_task_ = nullptr;
        }
    }
    return true;
}

void RailDriver::HeartbeatTaskTrampoline(void* arg) {
    static_cast<RailDriver*>(arg)->HeartbeatLoop();
}

void RailDriver::HeartbeatLoop() {
    uint32_t ticks = 0;
    uint32_t last_fail_log_ms = 0;
    bool     fail_logged_once = false;
    for (;;) {
        // Re-assert WIFI_PS_NONE every ~40 ticks (~10 s): mdns discovery
        // restore and power-save transitions can silently put modem sleep
        // back, which deafens ESP-NOW RX (see Init()).
        if ((ticks++ % 40) == 0) {
            esp_wifi_set_ps(WIFI_PS_NONE);
        }
        // Quiet ping: no per-send logging at 4 Hz; failures at most every ~10 s.
        if (!Send(RCMD_PING, 0, /*quiet=*/true)) {
            uint32_t now = now_ms();
            if (!fail_logged_once || now - last_fail_log_ms >= 10000) {
                ESP_LOGW(TAG, "heartbeat ping not sending (wifi down or esp-now error)");
                last_fail_log_ms = now;
                fail_logged_once = true;
            }
        }
        vTaskDelay(pdMS_TO_TICKS(250));
    }
}

bool RailDriver::Send(uint8_t cmd, int32_t arg, bool quiet) {
    if (!initialized_) {
        if (!quiet) ESP_LOGW(TAG, "Send(%u) before Init()", cmd);
        return false;
    }
    RailCmdPacket pkt{};
    pkt.magic = RAIL_MAGIC_CMD;
    pkt.cmd   = cmd;
    pkt.arg   = arg;
    pkt.seq   = ++seq_;
    esp_err_t err = esp_now_send(kBridgeMac, (const uint8_t*)&pkt, sizeof(pkt));
    if (err != ESP_OK) {
        // quiet (heartbeat) sends rate-limit their own failure logging.
        if (!quiet) ESP_LOGW(TAG, "esp_now_send cmd=%u arg=%ld failed: %s", cmd, (long)arg, esp_err_to_name(err));
        return false;
    }
    if (!quiet) ESP_LOGI(TAG, "TX cmd=%u arg=%ld seq=%u", cmd, (long)arg, pkt.seq);
    return true;
}

bool RailDriver::Home()          { return Send(RCMD_HOME, 0); }
bool RailDriver::MoveMm(float mm) { return Send(RCMD_MOVE_MM, (int32_t)lroundf(mm * 10.0f)); }
bool RailDriver::NudgeMm(float mm){ return Send(RCMD_NUDGE_MM, (int32_t)lroundf(mm * 10.0f)); }
bool RailDriver::Stop()          { return Send(RCMD_STOP, 0); }
bool RailDriver::Jog(int rpm)    { return Send(RCMD_JOG, rpm); }
bool RailDriver::Ping()          { return Send(RCMD_PING, 0); }

uint8_t RailDriver::WifiChannel() {
    wifi_ap_record_t ap{};
    if (esp_wifi_sta_get_ap_info(&ap) == ESP_OK) return ap.primary;
    return 0;
}

void RailDriver::OnRecvStatic(const esp_now_recv_info_t* info, const uint8_t* data, int len) {
    // WiFi-task context: keep this tiny.
    GetInstance().OnRecv(info ? info->src_addr : nullptr, data, len);
}

void RailDriver::OnRecv(const uint8_t* src_mac, const uint8_t* data, int len) {
    if (len != (int)sizeof(RailStatusPacket) || data == nullptr || data[0] != RAIL_MAGIC_STS) return;
    if (src_mac == nullptr || std::memcmp(src_mac, kBridgeMac, 6) != 0) return;   // only trust our bridge
    if (lock_ && xSemaphoreTake(lock_, pdMS_TO_TICKS(5)) == pdTRUE) {
        std::memcpy(&last_status_, data, sizeof(RailStatusPacket));
        have_status_ = true;
        last_rx_ms_  = now_ms();
        xSemaphoreGive(lock_);
    }
}

RailDriver::Status RailDriver::GetStatus() {
    Status s{};
    s.last_seq     = seq_;
    s.wifi_channel = WifiChannel();
    RailStatusPacket snap{};
    bool have = false;
    uint32_t rx_ms = 0;
    if (lock_ && xSemaphoreTake(lock_, pdMS_TO_TICKS(5)) == pdTRUE) {
        snap   = last_status_;
        have   = have_status_;
        rx_ms  = last_rx_ms_;
        xSemaphoreGive(lock_);
    }
    if (!have) {
        s.linked = false;
        s.age_ms = UINT32_MAX;
        return s;
    }
    s.age_ms  = now_ms() - rx_ms;
    // linked = FRESH, not "ever heard": the bridge streams status continuously
    // while in range, so anything older than 3 s means the link is down now.
    s.linked  = s.age_ms < 3000;
    s.homed   = snap.flags & RF_HOMED;
    s.crashed = snap.flags & RF_CRASHED;
    s.endstop = snap.flags & RF_ENDSTOP;
    s.moving  = snap.flags & RF_MOVING;
    s.power   = snap.flags & RF_POWER;
    s.pos_mm  = snap.pos_mm10 / 10.0f;
    s.rpm     = snap.rpm;
    s.vin     = snap.vin_cv / 100.0f;
    s.ack_seq = snap.ack_seq;
    return s;
}

#endif  // CONFIG_RAIL_ENABLED
