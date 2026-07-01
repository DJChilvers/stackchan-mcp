#!/usr/bin/env python3
"""
stackchan-led-chase.py — animated LED status chase for the 12-LED base ring.

Watches marker files written elsewhere:
    %TEMP%\\stackchan-busy-<session_id>  -> stackchan-hook.py (busy-start
                                             writes, say-done removes,
                                             one file per Claude Code
                                             session)                  -> amber chase
    %TEMP%\\stackchan-voice-thinking     -> stackchan-voice-bridge.py (set
                                             while transcribing + asking
                                             Claude)                    -> rainbow chase
    %TEMP%\\stackchan-needs-attention    -> stackchan-hook.py (urgent-say
                                             writes, cleared by the SAME
                                             session's next busy-start/
                                             say-done)                  -> priority red pulse

needs-attention takes PRIORITY over busy/thinking — 2026-07-01: with multiple
Claude Code sessions able to share one device, a session finishing or going
busy must not be able to silently erase another session's still-outstanding
"I need you" signal. Busy is now tracked per-session (glob stackchan-busy-*)
so one session's say-done can't wrongly clear a DIFFERENT session's busy
marker either.

While NONE of these are active, this script mostly leaves the LEDs alone
(stackchan-hook.py's static idle-blue / urgent-red), only periodically
re-asserting idle-blue — see IDLE_REPAINT_S below for why.

Run via stackchan-led-chase-start.vbs (hidden pythonw, same pattern as the
ambient idle-fidget loop). Single-instance locked.
"""
from __future__ import annotations
import colorsys
import glob
import json
import math
import os
import sys
import time
import urllib.request

# ── single-instance lock (same pattern as stackchan-idle.py) ───────────────
import atexit
import msvcrt

TEMP = os.environ.get("TEMP", os.environ.get("TMP", "."))
_LOCK_FILE = os.path.join(TEMP, "stackchan-led-chase.lock")
_lock_fh = None


def _acquire_lock() -> None:
    global _lock_fh
    try:
        _lock_fh = open(_LOCK_FILE, "a+b")
        msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        if _lock_fh:
            _lock_fh.close()
        sys.exit(0)  # another instance holds the lock — back off silently


atexit.register(lambda: _lock_fh.close() if _lock_fh else None)

GATEWAY_HTTP = "http://127.0.0.1:8767"
GATEWAY_MCP = GATEWAY_HTTP + "/mcp"
BUSY_MARKER_GLOB = os.path.join(TEMP, "stackchan-busy-*")
VOICE_THINKING_MARKER = os.path.join(TEMP, "stackchan-voice-thinking")
NEEDS_ATTENTION_MARKER = os.path.join(TEMP, "stackchan-needs-attention")

# A crashed owner process (e.g. a native segfault in the voice bridge's
# Whisper call) skips its own Python `finally` cleanup entirely, so the
# marker it wrote never gets removed — the chase would otherwise animate
# forever. Treat a marker older than its threshold as abandoned and ignore
# (and delete) it. Busy gets a generous window since long Claude Code turns
# are normal; voice interactions should always finish in well under a minute.
# Needs-attention gets the longest window — it's meant to persist until the
# user actually deals with it, but must still self-heal if orphaned.
BUSY_STALE_S = 30 * 60
THINKING_STALE_S = 90
NEEDS_ATTENTION_STALE_S = 60 * 60

NUM_LEDS = 12
STEP_S = 0.12  # ~8 fps — smooth enough for a chase, light on the WS link

# 2026-07-01: the firmware ITSELF autonomously drives the LEDs green while
# touch-to-talk is actively recording, and off when recording stops — this
# happens entirely on-device, with no marker file and no way for us to know
# it's happening. Normally that's harmless (if our own chase is already
# running, our next frame ~STEP_S later just paints over it) — but if the
# user taps to talk while genuinely idle (no busy/thinking/attention marker
# active) and then the recording TIMES OUT without ever completing (nothing
# said, or tapped again too slowly), firmware turns the LEDs off and nothing
# on our side ever gets triggered to restore idle-blue, since voice-bridge
# only hears about a recording that actually finished and got POSTed to it.
# Rather than trying to track the firmware's listening state directly (no
# marker exists for it), just periodically re-assert idle-blue while
# genuinely idle so any such gap self-heals within a bounded time.
IDLE_LED = (0, 25, 90)
IDLE_REPAINT_S = 4.0


def _marker_active(path: str, stale_s: float) -> bool:
    try:
        with open(path) as f:
            written_at = float(f.read().strip())
    except (OSError, ValueError):
        return False
    if time.time() - written_at > stale_s:
        try:
            os.remove(path)  # self-heal: don't keep re-checking a dead marker
        except OSError:
            pass
        return False
    return True


def _any_busy() -> bool:
    """True if ANY Claude Code session has an active (non-stale) busy
    marker. One marker file per session_id — see module docstring."""
    any_active = False
    for path in glob.glob(BUSY_MARKER_GLOB):
        if _marker_active(path, BUSY_STALE_S):
            any_active = True
        # _marker_active already self-heals (deletes) stale ones as it goes
    return any_active


def _needs_attention_active() -> bool:
    try:
        with open(NEEDS_ATTENTION_MARKER, encoding="utf-8") as f:
            d = json.load(f)
        written_at = float(d.get("ts", 0))
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if time.time() - written_at > NEEDS_ATTENTION_STALE_S:
        try:
            os.remove(NEEDS_ATTENTION_MARKER)
        except OSError:
            pass
        return False
    return True


class MCPSession:
    def __init__(self, url):
        self.url = url
        self.session_id = None

    def _post(self, payload, timeout=5):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=timeout)
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        body = resp.read()
        return json.loads(body) if body.strip() else None

    def init(self):
        self.session_id = None
        self._post({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "stackchan-led-chase", "version": "1.0"},
            },
        })
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def set_leds(self, colors):
        self._post({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "set_leds", "arguments": {"colors": colors}},
        })


def device_connected() -> bool:
    try:
        with urllib.request.urlopen(GATEWAY_HTTP + "/status", timeout=3) as r:
            return bool(json.load(r).get("esp32_connected"))
    except Exception:
        return False


def amber_chase_frame(pos: int) -> list[list[int]]:
    """Cylon-style scanner: bright head + fading two-pixel tail, rest off."""
    colors = [[0, 0, 0] for _ in range(NUM_LEDS)]
    colors[pos % NUM_LEDS] = [200, 90, 0]
    colors[(pos - 1) % NUM_LEDS] = [90, 40, 0]
    colors[(pos - 2) % NUM_LEDS] = [30, 15, 0]
    return colors


def rainbow_chase_frame(base_hue_deg: float) -> list[list[int]]:
    """Full ring, each LED offset by its position, slowly rotating."""
    colors = []
    for i in range(NUM_LEDS):
        hue = ((base_hue_deg + i * (360 / NUM_LEDS)) % 360) / 360.0
        r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
        colors.append([int(r * 255), int(g * 255), int(b * 255)])
    return colors


def attention_pulse_frame(phase: float) -> list[list[int]]:
    """Full ring, gentle red breathing pulse — deliberately NOT a chase
    (chase motion reads as "busy/active"; a slow breathing pulse reads as
    "waiting, come deal with me"). phase 0..1 maps to one full breath."""
    level = 0.35 + 0.65 * (0.5 - 0.5 * math.cos(phase * 2 * math.pi))
    r = int(255 * level)
    return [[r, 0, 0] for _ in range(NUM_LEDS)]


def main():
    _acquire_lock()
    session = MCPSession(GATEWAY_MCP)
    have_session = False
    pos = 0
    hue = 0.0
    pulse_phase = 0.0
    last_idle_repaint = 0.0

    while True:
        time.sleep(STEP_S)

        # Priority order: needs-attention > voice-thinking > busy. A session
        # going busy or another session's chase must not be able to bury the
        # "someone needs you" signal — see module docstring.
        attention = _needs_attention_active()
        thinking = _marker_active(VOICE_THINKING_MARKER, THINKING_STALE_S)
        busy = _any_busy()
        active = attention or thinking or busy

        # Idle: only wake up to repaint every IDLE_REPAINT_S (see constant
        # above for why this exists — self-heals any state the firmware's
        # autonomous touch-to-talk LEDs left behind).
        if not active and (time.time() - last_idle_repaint) < IDLE_REPAINT_S:
            have_session = False  # drop session between repaints; re-init on resume
            continue

        if not device_connected():
            have_session = False
            continue

        try:
            if not have_session:
                session.init()
                have_session = True
            if attention:
                pulse_phase = (pulse_phase + STEP_S / 2.2) % 1.0  # ~2.2s per breath
                session.set_leds(attention_pulse_frame(pulse_phase))
            elif thinking:
                hue = (hue + 30) % 360
                session.set_leds(rainbow_chase_frame(hue))
            elif busy:
                pos = (pos + 1) % NUM_LEDS
                session.set_leds(amber_chase_frame(pos))
            else:
                session.set_leds([list(IDLE_LED)] * NUM_LEDS)
                last_idle_repaint = time.time()
        except Exception:
            have_session = False  # re-init next tick


if __name__ == "__main__":
    main()
