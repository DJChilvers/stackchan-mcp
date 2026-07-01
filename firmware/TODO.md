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
