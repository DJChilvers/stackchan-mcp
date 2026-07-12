// self.rail.* MCP tools — thin glue over RailDriver (ESP-NOW sender to the bridge).
// Kept separate from stackchan.cc to keep that file small; auto-globbed into the
// board sources. ALL motion safety lives on the bridge MCU, not here.
#include "rail_driver.h"

#if CONFIG_RAIL_ENABLED

#include "mcp_server.h"
#include <cJSON.h>
#include <cstdint>

void RegisterRailMcpTools() {
    auto& mcp_server = McpServer::GetInstance();

    auto rail_status_json = [](const RailDriver::Status& st) -> cJSON* {
        cJSON* r = cJSON_CreateObject();
        cJSON_AddBoolToObject(r, "linked", st.linked);
        if (st.age_ms == UINT32_MAX) cJSON_AddNullToObject(r, "status_age_ms");
        else cJSON_AddNumberToObject(r, "status_age_ms", (double)st.age_ms);
        cJSON_AddBoolToObject(r, "homed", st.homed);
        cJSON_AddBoolToObject(r, "crashed", st.crashed);
        cJSON_AddBoolToObject(r, "endstop", st.endstop);
        cJSON_AddBoolToObject(r, "moving", st.moving);
        cJSON_AddBoolToObject(r, "power_12v", st.power);
        cJSON_AddNumberToObject(r, "pos_mm", st.pos_mm);
        cJSON_AddNumberToObject(r, "rpm", st.rpm);
        cJSON_AddNumberToObject(r, "vin", st.vin);
        cJSON_AddNumberToObject(r, "ack_seq", st.ack_seq);
        cJSON_AddNumberToObject(r, "last_seq", st.last_seq);
        cJSON_AddNumberToObject(r, "wifi_channel", st.wifi_channel);
        return r;
    };

    mcp_server.AddTool(
        "self.rail.home",
        "Home the management rail: the bridge runs its two-stage homing onto the "
        "limit switch and zeroes position. Required once per bridge power-up before "
        "absolute moves. Non-blocking + deliberate/slow — poll self.rail.status for "
        "homed=true. Returns {ok, sent, status}.",
        PropertyList(),
        [rail_status_json](const PropertyList&) -> ReturnValue {
            RailDriver& rail = RailDriver::GetInstance();
            bool ready = rail.Init();
            bool sent = ready && rail.Home();
            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "ok", sent);
            cJSON_AddBoolToObject(root, "sent", sent);
            if (!ready) cJSON_AddStringToObject(root, "error", "esp-now not ready (is wifi up?)");
            cJSON_AddItemToObject(root, "status", rail_status_json(rail.GetStatus()));
            return root;
        });

    mcp_server.AddTool(
        "self.rail.move_mm",
        "Move the carriage to an ABSOLUTE position from home, in millimetres "
        "(0 = home/dock end .. 896 = far soft limit). Requires the rail to be homed "
        "first (the bridge rejects the move otherwise). Non-blocking — poll "
        "self.rail.status for moving/pos_mm. Returns {ok, sent, target_mm, status}.",
        PropertyList({Property("mm", kPropertyTypeInteger, 0, 0, 896)}),
        [rail_status_json](const PropertyList& props) -> ReturnValue {
            int mm = props["mm"].value<int>();
            RailDriver& rail = RailDriver::GetInstance();
            bool ready = rail.Init();
            bool sent = ready && rail.MoveMm((float)mm);
            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "ok", sent);
            cJSON_AddBoolToObject(root, "sent", sent);
            cJSON_AddNumberToObject(root, "target_mm", mm);
            if (!ready) cJSON_AddStringToObject(root, "error", "esp-now not ready (is wifi up?)");
            cJSON_AddItemToObject(root, "status", rail_status_json(rail.GetStatus()));
            return root;
        });

    mcp_server.AddTool(
        "self.rail.nudge_mm",
        "Move the carriage a RELATIVE distance in millimetres (signed, -100..100; "
        "positive = away from home). Works without homing; clamped bridge-side to the "
        "soft limits once homed. Returns {ok, sent, delta_mm, status}.",
        PropertyList({Property("mm", kPropertyTypeInteger, 0, -100, 100)}),
        [rail_status_json](const PropertyList& props) -> ReturnValue {
            int mm = props["mm"].value<int>();
            RailDriver& rail = RailDriver::GetInstance();
            bool ready = rail.Init();
            bool sent = ready && rail.NudgeMm((float)mm);
            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "ok", sent);
            cJSON_AddBoolToObject(root, "sent", sent);
            cJSON_AddNumberToObject(root, "delta_mm", mm);
            if (!ready) cJSON_AddStringToObject(root, "error", "esp-now not ready (is wifi up?)");
            cJSON_AddItemToObject(root, "status", rail_status_json(rail.GetStatus()));
            return root;
        });

    mcp_server.AddTool(
        "self.rail.stop",
        "Immediately stop the carriage and hold position (also aborts an in-progress "
        "home). Returns {ok, sent, status}.",
        PropertyList(),
        [rail_status_json](const PropertyList&) -> ReturnValue {
            RailDriver& rail = RailDriver::GetInstance();
            bool ready = rail.Init();
            bool sent = ready && rail.Stop();
            cJSON* root = cJSON_CreateObject();
            cJSON_AddBoolToObject(root, "ok", sent);
            cJSON_AddBoolToObject(root, "sent", sent);
            if (!ready) cJSON_AddStringToObject(root, "error", "esp-now not ready (is wifi up?)");
            cJSON_AddItemToObject(root, "status", rail_status_json(rail.GetStatus()));
            return root;
        });

    mcp_server.AddTool(
        "self.rail.status",
        "Read the latest rail status the bridge streamed back: linked, status_age_ms, "
        "homed, crashed, endstop, moving, power_12v, pos_mm, rpm, vin, ack_seq/last_seq, "
        "wifi_channel. Sends a ping to refresh. linked=false means no fresh status in "
        "the last 3s — the ESP-NOW link is down right now (bridge unpowered or on a "
        "different channel — the bridge must be pinned to wifi_channel).",
        PropertyList(),
        [rail_status_json](const PropertyList&) -> ReturnValue {
            RailDriver& rail = RailDriver::GetInstance();
            rail.Init();
            rail.Ping();
            return rail_status_json(rail.GetStatus());
        });
}

#endif  // CONFIG_RAIL_ENABLED
