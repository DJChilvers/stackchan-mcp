// self.presence.read MCP tool — thin glue over RadarLd2450 (HLK-LD2450 on
// Grove Port C). Kept separate from stackchan.cc to keep that file small
// (rail_mcp.cc pattern); auto-globbed into the board sources.
#include "radar_ld2450.h"

#if CONFIG_RADAR_ENABLED

#include "mcp_server.h"
#include <cJSON.h>
#include <cstdint>
#include <math.h>

#ifndef M_PI
#define M_PI 3.14159265358979323846   // same fallback as boards/common/afsk_demod.cc
#endif

void RegisterRadarMcpTools() {
    auto& mcp_server = McpServer::GetInstance();

    mcp_server.AddTool(
        "self.presence.read",
        "Read the latest HLK-LD2450 radar presence snapshot (Grove Port C, "
        "streams ~10 Hz). Returns {ok, age_ms, targets:[{x_mm, y_mm, "
        "distance_mm, angle_deg, speed_cms}]} with up to 3 tracked people. "
        "x_mm = lateral offset, y_mm = distance straight out from the radar "
        "face, angle_deg = atan2(x, y) so 0 = straight ahead and the sign "
        "follows x (verify handedness empirically on first bring-up), "
        "speed_cms = radial speed. Empty targets = nobody present. age_ms is "
        "the snapshot staleness (normally under ~200 ms). ok:false with error "
        "\"no radar frames\" means no module has spoken on Port C yet.",
        PropertyList(),
        [](const PropertyList&) -> ReturnValue {
            RadarLd2450& radar = RadarLd2450::GetInstance();
            radar.Init();   // idempotent; normally already started by the board ctor
            RadarLd2450::Snapshot snap = radar.GetSnapshot();
            cJSON* root = cJSON_CreateObject();
            if (!snap.have_frame) {
                // No module attached (or it has never produced a valid frame).
                cJSON_AddBoolToObject(root, "ok", false);
                cJSON_AddStringToObject(root, "error", "no radar frames");
                return root;
            }
            cJSON_AddBoolToObject(root, "ok", true);
            cJSON_AddNumberToObject(root, "age_ms", (double)snap.age_ms);
            cJSON* targets = cJSON_CreateArray();
            for (int i = 0; i < RadarLd2450::kMaxTargets; ++i) {
                const RadarLd2450::Target& t = snap.targets[i];
                if (!t.valid) continue;
                float x = (float)t.x_mm;
                float y = (float)t.y_mm;
                cJSON* o = cJSON_CreateObject();
                cJSON_AddNumberToObject(o, "x_mm", t.x_mm);
                cJSON_AddNumberToObject(o, "y_mm", t.y_mm);
                cJSON_AddNumberToObject(o, "distance_mm",
                                        (double)lroundf(sqrtf(x * x + y * y)));
                // atan2 convention with 0 deg = straight ahead (boresight):
                // note the argument order is (x, y), NOT the usual (y, x).
                cJSON_AddNumberToObject(o, "angle_deg",
                                        (double)(atan2f(x, y) * 180.0f / (float)M_PI));
                cJSON_AddNumberToObject(o, "speed_cms", t.speed_cms);
                cJSON_AddItemToArray(targets, o);
            }
            cJSON_AddItemToObject(root, "targets", targets);
            return root;
        });
}

#endif  // CONFIG_RADAR_ENABLED
