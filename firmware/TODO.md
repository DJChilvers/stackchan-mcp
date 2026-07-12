# Firmware TODO — items that need a rebuild + reflash

Not actionable via the gateway/MCP at runtime — batch these into the next
time the firmware actually gets rebuilt and flashed.

## FLASH-DAY CHECKLIST (added 2026-07-09 — read before plugging anything in)

**Ports — do not mix these up:** COM5 = **Wheatley (CoreS3)**, the board being
flashed today. COM7 = rail bridge (classic M5Stack Core) — do NOT reflash.
COM8 = Core Ink test-sender. Confirm with Device Manager before `idf.py flash`.

**Before flashing:**
- Prefer a plain `idf.py flash` over `erase-flash`. NVS holds the Wi-Fi
  credentials AND the `wifi/sleep_mode` checkbox (user set it OFF 2026-07-06).
  A full erase reverts sleep_mode to its compiled default (ON, unless this
  build changes the default per the "No sleep mode" section below) and forces
  Wi-Fi re-provisioning via the 192.168.4.1 AP page.
- **Screen language — DECIDED 2026-07-09: flip ja-JP → en-US in this build.**
  It's compile-time (`main/CMakeLists.txt` → `CONFIG_LANGUAGE_*` → `LANG_DIR`),
  so a rebuild-anyway makes the switch free — the "not worth a dedicated
  reflash" judgement from 2026-06-30 no longer applies. en-US is the only
  English locale; visually ≈ en-GB except the boot word "Initializing...".
  Set it BEFORE building — it's baked into the app binary, not NVS.
- ESP-NOW sender (section below): match Wheatley's associated **WiFi channel**
  or pin the AP; the bench bridge is currently pinned to channel 1.

**After flashing — verify in this order:**
1. Device reconnects to the gateway (`curl http://127.0.0.1:8767/status` →
   `esp32_connected: true`). On reconnect the gateway auto-repushes the matrix
   avatar and auto-disables torque release — check the daemon log for both.
   (First matrix push after a fresh boot always fits in PSRAM — no OOM risk.)
2. AP-config page: **sleep-mode checkbox is OFF** (or the new default is OFF).
3. `set_touch_sensor_enabled` no longer returns `Unknown tool` → the charging
   phantom-stroke workaround can now be wired gateway-side (see touch section).
4. `listen` tool responds instead of hanging 25s → re-add the voice-bridge
   follow-up-listening loop (reverted version is in git history; shorten the
   timeout per the section below).
5. If the ESP-NOW sender is in this build: command a HOME / NUDGE from
   Wheatley's MCP action and watch the bridge (MAC 80:7D:3A:DB:DC:08) move
   the motor + stream status back.
6. If the wake word made this build: "Hey Wheatley" from a metre away.

## Archive this build for Wheatley mk2 (added 2026-07-09)

The user intends to build a **second Wheatley (mk2)** once mk1 is finished, so
one unit can move into the house. Make today's build reproducible so mk2 is a
flash-only job, not a from-scratch rebuild:
- Save alongside the repo (or a tagged commit): the source **commit hash**,
  the generated **`sdkconfig`**, the **ESP-IDF version**, and the built
  artifacts (`bootloader.bin`, `partition-table.bin`, app `.bin` — or one
  `esptool.py merge_bin` image) with the flash offsets.
- Note the per-device bits mk2 will need decided later: its own MAC (matters
  if it ever gets ESP-NOW peers of its own), mDNS/device naming, and whether
  the gateway's `ESP32Manager` handles **two concurrent devices** or needs
  work (single-device assumptions likely — check before mk2 arrives). House
  unit will be on the same Wi-Fi/gateway.

## Touch sensor false-triggers on charging noise

The capacitive head-touch sensor misreads electrical noise from the
charging circuit as a continuous head-stroke, firing the built-in
touch-reaction wobble (`ServoWobbleStepAdvance` / `StartServoWobble` /
`HandleStroke` in `main/boards/stackchan/stackchan.cc`) roughly every 7-8
seconds while charging — confirmed 2026-07-01 by watching the gateway
daemon log (1623 phantom `touch/stroke` events in one session, consistent
~6.1-6.9s durations) and observing them stop immediately on unplugging the
charger. Likely the same root cause as an earlier-reported high-pitched
audio squeak that also only happens while charging — both look like
charging-circuit EMI coupling into sensitive analog inputs.

Current workaround (no firmware change): don't charge while interacting
with the device — charge separately, unplug before use.

Possible real fixes, to weigh once doing a reflash anyway:
- **Likely the easy fix: the current firmware source ALREADY has
  `set_touch_sensor_enabled`** (`stackchan.cc`, exposed via the gateway's
  `stdio_server.py` as an MCP tool) — the device's currently-flashed
  firmware predates it, which is why calling it earlier this project
  returned `Unknown tool`. A reflash to the current codebase should let
  the gateway just call `set_touch_sensor_enabled(false)` while charging
  (e.g. driven off the same charging-state signal, if one exists/can be
  added) instead of needing any new threshold-tuning work at all. Check
  this first before doing anything more invasive.
- Fallback if that's not sufficient: raise the touch sensor's
  stroke-detection threshold and/or lengthen its debounce so
  charging-circuit noise doesn't cross it. Risk: makes legitimate strokes
  harder to trigger too — needs on-device tuning, not just a blind
  constant bump.
- Hardware-side (not firmware, but worth doing at the same time): try a
  different/higher-quality charging cable or adapter, or add a ferrite
  bead near the charge port, or check the touch sensor's grounding/
  shielding. Cheapest thing to try first, and rules the software fix in
  or out.

## English wake word ("Hey Wheatley")

Currently the wake-word model baked into `sdkconfig.defaults.esp32s3` is
`CONFIG_SR_WN_WN9_NIHAOXIAOZHI_TTS=y` — a fixed, pre-trained Chinese
acoustic model ("你好小智" / "Nihao Xiaozhi"), not overridden anywhere in
`main/boards/stackchan/config.json`. (Medium confidence this is what's
actually flashed — there's no generated `sdkconfig` checked in to confirm
the live binary matches the defaults file.)

ESP-IDF's speech-recognition component also exposes a `USE_CUSTOM_WAKE_WORD`
Kconfig option, which is MultiNet-based (matches phoneme text like
"hey wheatley" instead of requiring a fixed pre-trained acoustic model) —
but it isn't enabled for the stackchan board currently.

To get "Hey Wheatley" working would need:
- Swapping in an English MultiNet wake-word model (`USE_CUSTOM_WAKE_WORD`),
  trained/configured for the phrase "hey wheatley".
- An ESP-IDF rebuild + reflash — same rebuild window as the touch-sensor
  item above, so worth doing together.

No workaround needed for this one (voice control already works via
touch-to-talk + the PC-side voice bridge — see [[project_stackchan_voice_bridge]]
in memory) — this is a nice-to-have, not a bug.

## Tap-free follow-up listening (gateway `listen` MCP tool)

The gateway already exposes a `listen` tool (`stackchan_mcp/stt/orchestrator.py`
+ `stdio_server.py`) meant to put the device into listening mode remotely
(no physical tap) by sending `{"type":"listen","state":"stop"}` etc. over
the existing WebSocket — `main/application.cc` (~line 590) confirms this
wire message IS handled in the current firmware *source*. Tested live
2026-07-01 against the currently-flashed device: the call hung with zero
response for the full 25s client timeout — consistent with the same
source-vs-flashed-binary gap as `set_touch_sensor_enabled` above (the
running firmware predates this feature).

Wanted for: `stackchan-voice-bridge.py` speaking a reply that ends in "?"
(e.g. asking the user to clarify a garbled/ambiguous request, including the
take_photo clarification case) should be able to listen for a spoken
follow-up immediately rather than requiring another screen tap. The
voice-bridge-side code for this (conversational multi-turn loop, calls the
`listen` tool with `language="en"`) was written and then reverted 2026-07-01
specifically because of this gap — shipping it against the current firmware
would mean a ~20+ second silent hang after every question-ending reply,
which is worse than the current tap-required behavior. Once reflashed,
re-add the follow-up loop (git history has the reverted version) and give
it a much shorter timeout tuned to `duration_ms` rather than the generous
one used for testing.

Batch with the other two items above — same rebuild+reflash window.

## Management-rail motor drive — ESP-NOW sender to the bridge (UPDATED 2026-07-06)

**Architecture changed — this supersedes the old "firmware motor tool + /api/motor + app
buttons" plan.** The rail motor is NOT driven by Wheatley. It's driven by a **separate bridge
MCU** (currently a classic M5Stack Core) that owns the I2C wire to the Roller485 Lite. Wheatley's
firmware job is to be the **ESP-NOW SENDER** to that bridge. Spec:
`C:\Users\domin\Documents\StackChan\wheatley-rail-build-spec.md` §4. Chain:
`phone/MCP → WiFi → Wheatley (CoreS3) → ESP-NOW → bridge → I2C → Roller485`.

The bridge firmware is built + bench-validated (motor control, trapezoidal smoothing, two-stage
repeatable homing on an M5 Limit switch, current+stall crash detection, and an **ESP-NOW receiver**):
`C:\Users\domin\Documents\StackChan\core2-rail-controller\`. Bridge MAC = **80:7D:3A:DB:DC:08**.

**What to add to Wheatley firmware in the next reflash — an ESP-NOW sender:**
- Wire protocol: `RailCmdPacket` / `RailStatusPacket` in
  `C:\Users\domin\Documents\StackChan\core2-rail-controller\rail_espnow.h` (copy verbatim).
  Commands: HOME / MOVE_MM (abs, mm×10) / NUDGE_MM (rel) / STOP / JOG / PING. Status back:
  pos_mm, rpm, vin, flags (homed / crashed / endstop / moving / power).
- **Channel gotcha (spec §4):** ESP-NOW + WiFi share the radio and MUST be on the same channel.
  Read Wheatley's associated WiFi channel at startup and set ESP-NOW to it (or pin the AP channel).
  Mismatch = the link silently does nothing. (Bench test currently pins both to channel 1.)
- Peer = the bridge MAC above. Send commands; receive + surface status/faults (crash, not-homed).
- Expose as **MCP actions** ("move to X mm", "go to dock", "move to left/right end", "home") so
  the rail is commandable from MCP/phone — this is the spec's Wheatley task list §8.
- A standalone **test-sender** already validates the bridge end-to-end:
  `C:\Users\domin\Documents\StackChan\rail-sender-test\` — port its logic into the firmware.

The old `POST /api/motor` 501 stub + companion app L/R buttons are DROPPED as the drive path
(the bridge owns drive). If still wanted, the app could relay to Wheatley's ESP-NOW MCP action
instead — optional, not required.

## Live camera stream (companion "live" view)

The companion app's Camera screen shows face-recognition results "live", but the firmware only
supports **on-demand single-JPEG capture** (`self.camera.take_photo`). So v1 polls snapshots
roughly every ~1.5 s — usable, but not smooth video. A genuine live view would need a firmware
streaming path:

- **MJPEG over an HTTP endpoint on the device**, or
- **continuous frame push over the existing WebSocket** to the gateway, which the companion API
  would then relay to the app (e.g. over `/ws/live`).

Nice-to-have; the snapshot-polling fallback works fine until then. Batch with a reflash.

## Batch-with opportunities (already known, worth doing in the same reflash window)

Not new, but if the firmware is being rebuilt anyway for the items above, these are the highest-
value extras (both documented in memory):

- **On-screen directional eye-gaze** — new directional optic frames + a trigger tool, so the eye
  can glance left/right/up/down on screen independently of the head servos. Would let the
  companion Control screen (and the idle loop) do a real eye-slide instead of faking it with a
  head flick.
- **PSRAM free-before-fetch for matrix avatar sets** (`avatar_set_fetcher.cc`) — free the old
  buffer before allocating the incoming one, so a matrix avatar set can be replaced without a
  power-cycle (currently OOMs because both 3.45 MB buffers must coexist).
- **Default auto torque-release OFF** (Issue #152 feature). The gateway disables it on every
  reconnect (`_disable_auto_torque_release()` in `esp32_client.py`), which works but is
  load-bearing — a device power-cycle with the gateway down leaves servos silently limp.
  Flipping the firmware default to OFF makes the gateway call a belt-and-braces no-op.

## No sleep mode — keep it off across the reflash (added 2026-07-06, source-verified)

Sleep mode was the cause of Wheatley silently dropping off the gateway
after ~20 min without interaction and staying frozen/blind until a screen
tap. The user turned it OFF via the AP-config checkbox on 2026-07-06 (he's
on USB power, so there's no battery reason to sleep). Source-level facts
(explored 2026-07-06):

- **Setting:** NVS key `sleep_mode` (u8) in the `wifi` namespace. Written
  only by the AP-config page (`components/78__esp-wifi-connect/
  wifi_configuration_ap.cc` ~L814-819, checkbox in
  `assets/wifi_configuration.html` ~L338). **Default is TRUE/enabled**
  (~L244). There is NO MCP tool or WebSocket message to change it at
  runtime — the gateway cannot manage it.
- **Mechanism:** StackChan uses `PowerSaveTimer(-1, 60, 300)`
  (`main/boards/stackchan/stackchan.cc` ~L2117-2131,
  `main/boards/common/power_save_timer.cc`): after **60s** idle it enters
  power-save (wake-word off, codec input off, CPU throttled with
  auto-light-sleep, display dimmed to 10), after **300s** it requests
  shutdown. Both durations are hardcoded constructor params, not
  NVS-configurable. The timer only starts at all if `sleep_mode` is true
  (power_save_timer.cc ~L32-36) — which is why the checkbox fix works.
- **What resets the idle counter:** LCD tap (via `StartListening` →
  `SetPowerSaveLevel(PERFORMANCE)`, stackchan.cc ~L2358), audio open/
  close, listening/speaking transitions. **Head-touch (Si12T) taps/strokes
  do NOT** — they only emit gateway events (stackchan.cc ~L3890/3903).
  Gateway WS traffic doesn't either, which matches the observed drop 23
  min after the last physical stroke despite constant touch polling.
- **Wake tap = tap-to-talk, inseparably:** the same LCD touch that wakes
  from power-save IS the `StartListening()` call — waking him this way
  always starts an audio capture → Claude API chat. There is no
  swallowed-wake-tap path in current firmware, and no network wake path.

For the next flash:
- **Change the default to OFF** for this board (wifi_configuration_ap.cc
  ~L244) — or simply don't instantiate the PowerSaveTimer in stackchan.cc —
  so a factory/NVS reset can't silently reintroduce disappearing-Wheatley.
  Then verify the checkbox state first thing after flashing.
- If sleep is ever wanted again (battery use): swallow the wake tap
  (wake without starting listening), and make head-touch events count as
  activity/wake sources — they currently don't.

## Expose the ambient light sensor (LTR-553ALS) — real lux for "lights out" (added 2026-07-06)

Nothing currently reads the LTR-553ALS (checked live 2026-07-03), so the
gateway's lights-out reaction approximates darkness from the camera's mean
frame brightness. That approximation false-triggered repeatedly ("it's
dark" lines in a fully lit room — auto-exposure dips, hand near the lens,
idle wander pointing at a dark corner); tightened gateway-side 2026-07-06
(absolute-darkness threshold + two consecutive dark frames in
stackchan-vision-loop.py `_check_lights_out`), but a real sensor makes the
whole guessing game unnecessary.

Add an MCP tool (e.g. `self.light.read` → lux, mirroring the IMU item
below — it's on the same internal I2C bus that's deliberately not exposed
via `self.i2c.*`). Then rewire stackchan-vision-loop.py's lights-out check
to prefer lux when the tool exists (keep the camera fallback for older
firmware). Batch with the reflash items above.

## Expose the IMU (BMI270/BMM150) for orientation auto-detect

The CoreS3's on-board IMU sits on the internal I2C bus and is deliberately
NOT exposed through MCP tools (see `main/boards/stackchan/stackchan.cc` ~L507:
"Direct on-board ICs only; not exposed through self.i2c.* MCP tools").

The companion app now has a **manual** "mounted upside-down" toggle
(`/api/orientation` in `stackchan_mcp/companion_server.py`) so the gateway
rotates the camera 180° and mirrors head yaw/pitch when Wheatley is hung
inverted (e.g. on the management rail, looking down at the scan tray).

To make that **automatic**, add a firmware tool (e.g. `self.imu.read` →
accel x/y/z, or a derived `orientation`/`pitch`/`roll`) that reads the
BMI270 gravity vector. The gateway could then infer upright vs inverted
from the sign of gravity-Z and set the flag itself — no manual toggle, and
it works with nothing in the camera's view.

**Interim (no reflash needed), added 2026-07-06:** orientation is now
auto-detectable from the **camera** instead, via the scan tray's four
DICT_4X4_50 ArUco corner codes (IDs 0-3, printed upright). Run
`python stackchan-vision-loop.py --calibrate-flip` (`_calibrate_flip` there):
it sweeps a couple of pitches until the tray markers are in view, reads their
rotation (~0° = upright, ~180° = inverted — the raw camera image genuinely
rotates with the body, confirmed live), and writes the `upside_down` flag in
companion_settings.json. The vision loop rotates frames 180° off that same
flag so YuNet can detect faces while inverted, and stackchan-idle.py derives
its servo signs (PITCH_UP_SIGN/YAW_RIGHT_SIGN) from it. The IMU tool would
still be strictly better (no tray required, instant, works mid-air while
being remounted), so keep it on this list — but the ArUco path removes the
urgency. Batch the IMU tool with the other reflash items.
