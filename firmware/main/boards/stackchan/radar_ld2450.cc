#include "radar_ld2450.h"

#if CONFIG_RADAR_ENABLED

#include <cstring>
#include "config.h"
#include "driver/uart.h"
#include "esp_log.h"
#include "esp_timer.h"

#define TAG "RadarLd2450"

// LD2450 data frame (streamed ~10 Hz):
//   AA FF 03 00 | 3 target slots x 8 bytes | 55 CC   = 30 bytes total.
// Per-slot layout (little-endian): x(int16) y(int16) speed(int16)
// distance_resolution(uint16). An all-zero slot means "no target".
static constexpr int    kFrameLen = 30;
static constexpr size_t kAccSize  = 128;    // > 4 frames; scan residue stays < kFrameLen
static constexpr int    kRxBufBytes = 1024; // UART driver RX ring buffer

static inline uint32_t now_ms() {
    return (uint32_t)(esp_timer_get_time() / 1000);
}

// Reference decode: ESPHome's ld2450 component
// (esphome/components/ld2450/ld2450.cpp, decode_coordinate()/decode_speed()).
// CRITICAL: x / y / speed use the LD2450's signed-magnitude-style encoding,
// NOT two's complement — bit15 SET means positive, bit15 CLEAR means
// negative, with the magnitude in the low 15 bits. Replicated here exactly.
static inline int16_t DecodeSignedMagnitude(uint8_t lo, uint8_t hi) {
    int16_t v = (int16_t)((((uint16_t)(hi & 0x7F)) << 8) | lo);
    if ((hi & 0x80) == 0) {
        v = -v;
    }
    return v;
}

RadarLd2450& RadarLd2450::GetInstance() {
    static RadarLd2450 instance;
    return instance;
}

bool RadarLd2450::Init() {
    if (initialized_) return true;

    if (lock_ == nullptr) {
        lock_ = xSemaphoreCreateMutex();
        if (lock_ == nullptr) {
            ESP_LOGE(TAG, "failed to create snapshot mutex");
            return false;
        }
    }

    uart_config_t cfg = {};
    cfg.baud_rate  = RADAR_BAUDRATE;        // LD2450 factory default: 256000
    cfg.data_bits  = UART_DATA_8_BITS;      // 8N1
    cfg.parity     = UART_PARITY_DISABLE;
    cfg.stop_bits  = UART_STOP_BITS_1;
    cfg.flow_ctrl  = UART_HW_FLOWCTRL_DISABLE;
    cfg.source_clk = UART_SCLK_DEFAULT;

    esp_err_t err = uart_driver_install(RADAR_UART_NUM, kRxBufBytes, 0, 0, nullptr, 0);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "uart_driver_install failed: %s", esp_err_to_name(err));
        return false;
    }
    err = uart_param_config(RADAR_UART_NUM, &cfg);
    if (err == ESP_OK) {
        // Board TX (PORT_C_TX_PIN) -> module RX (only used if we ever send
        // config commands); module TX -> board RX (PORT_C_RX_PIN), which
        // carries the ~10 Hz data stream we decode below.
        err = uart_set_pin(RADAR_UART_NUM, PORT_C_TX_PIN, PORT_C_RX_PIN,
                           UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "uart config/pin failed: %s", esp_err_to_name(err));
        uart_driver_delete(RADAR_UART_NUM);
        return false;
    }

    initialized_ = true;

    if (task_ == nullptr) {
        BaseType_t ok = xTaskCreate(&RadarLd2450::ReaderTaskTrampoline, "radar_rx",
                                    3072, this, tskIDLE_PRIORITY + 1, &task_);
        if (ok != pdPASS) {
            ESP_LOGE(TAG, "failed to create radar_rx task");
            task_ = nullptr;
            initialized_ = false;
            uart_driver_delete(RADAR_UART_NUM);
            return false;
        }
    }
    ESP_LOGI(TAG, "LD2450 reader on Port C: UART%d rx=%d tx=%d @ %d 8N1",
             (int)RADAR_UART_NUM, (int)PORT_C_RX_PIN, (int)PORT_C_TX_PIN,
             (int)RADAR_BAUDRATE);
    return true;
}

void RadarLd2450::ReaderTaskTrampoline(void* arg) {
    static_cast<RadarLd2450*>(arg)->ReaderLoop();
}

void RadarLd2450::ReaderLoop() {
    uint8_t  acc[kAccSize];
    size_t   fill = 0;
    bool     absence_logged = false;
    bool     first_frame_logged = false;
    uint32_t start_ms = now_ms();

    for (;;) {
        int n = uart_read_bytes(RADAR_UART_NUM, acc + fill, kAccSize - fill,
                                pdMS_TO_TICKS(200));
        if (n > 0) fill += (size_t)n;

        // Scan for complete frames. Non-frame bytes are consumed one at a
        // time, so after the memmove the residue is always < kFrameLen and
        // the accumulator can never wedge full.
        size_t i = 0;
        while (fill - i >= (size_t)kFrameLen) {
            if (acc[i] == 0xAA && acc[i + 1] == 0xFF &&
                acc[i + 2] == 0x03 && acc[i + 3] == 0x00 &&
                acc[i + kFrameLen - 2] == 0x55 && acc[i + kFrameLen - 1] == 0xCC) {
                ParseFrame(acc + i);
                i += kFrameLen;
                if (!first_frame_logged) {
                    ESP_LOGI(TAG, "first valid LD2450 frame received");
                    first_frame_logged = true;
                }
            } else {
                ++i;
            }
        }
        if (i > 0) {
            std::memmove(acc, acc + i, fill - i);
            fill -= i;
        }

        // Module-absent detection: with nothing plugged into Port C no valid
        // frame ever arrives. Log the absence ONCE, not repeatedly — the
        // self.presence.read tool keeps reporting ok:false meanwhile, and if
        // a module is hot-plugged later the stream is picked up normally.
        if (!first_frame_logged && !absence_logged &&
            now_ms() - start_ms > 5000) {
            ESP_LOGW(TAG, "no LD2450 frames after 5 s (module not attached to "
                          "Port C?); self.presence.read will return ok:false");
            absence_logged = true;
        }
    }
}

void RadarLd2450::ParseFrame(const uint8_t* frame) {
    Target parsed[kMaxTargets];
    for (int t = 0; t < kMaxTargets; ++t) {
        const uint8_t* p = frame + 4 + t * 8;
        bool all_zero = true;
        for (int b = 0; b < 8; ++b) {
            if (p[b] != 0) { all_zero = false; break; }
        }
        if (all_zero) {              // all-zero slot = no target in this slot
            parsed[t] = Target{};
            continue;
        }
        parsed[t].valid     = true;
        parsed[t].x_mm      = DecodeSignedMagnitude(p[0], p[1]);
        parsed[t].y_mm      = DecodeSignedMagnitude(p[2], p[3]);
        parsed[t].speed_cms = DecodeSignedMagnitude(p[4], p[5]);
        // distance_resolution is a plain little-endian uint16 (no sign trick).
        parsed[t].res_mm    = (uint16_t)((uint16_t)p[6] | ((uint16_t)p[7] << 8));
    }
    if (lock_ && xSemaphoreTake(lock_, pdMS_TO_TICKS(5)) == pdTRUE) {
        std::memcpy(targets_, parsed, sizeof(targets_));
        have_frame_ = true;
        last_rx_ms_ = now_ms();
        xSemaphoreGive(lock_);
    }
}

RadarLd2450::Snapshot RadarLd2450::GetSnapshot() {
    Snapshot s{};
    s.age_ms = UINT32_MAX;
    if (lock_ && xSemaphoreTake(lock_, pdMS_TO_TICKS(5)) == pdTRUE) {
        s.have_frame = have_frame_;
        if (have_frame_) {
            s.age_ms = now_ms() - last_rx_ms_;
        }
        std::memcpy(s.targets, targets_, sizeof(s.targets));
        xSemaphoreGive(lock_);
    }
    return s;
}

#endif  // CONFIG_RADAR_ENABLED
