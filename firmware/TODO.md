# Firmware TODO — items that need a rebuild + reflash

Not actionable via the gateway/MCP at runtime — batch these into the next
time the firmware actually gets rebuilt and flashed.

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

## Management-rail motor drive (companion controller)

The Wheatley companion app (Android, `companion-android/` — talks to the gateway over the
LAN) has a Control screen with **left/right drive controls for the management-rail motor(s)**
Dominic is adding. There is **no firmware motor tool today** — the only actuators exposed are
the servo head (`self.robot.set_head_angles`), the LEDs, and the camera. The motor L/R buttons
are wired in the app but disabled ("firmware pending"), and the gateway's companion API already
exposes a `POST /api/motor {direction, speed}` endpoint stubbed to **501 Not Implemented** until
this lands.

Next flash: add a motor-drive tool (e.g. `self.rail.drive(direction, speed)` /
`self.motor.set(...)`) wired to whatever motor driver / Grove port the rail hardware ends up
using (PWM / H-bridge). Then map it in `stdio_server.py`'s `tool_map` like the other device
tools, and flip the companion `/api/motor` handler + the app's motor buttons from stub to live.

- Confirm the motor driver wiring / port once the rail hardware is chosen.
- No workaround (feature doesn't exist yet); nice-to-have, not a bug.

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
