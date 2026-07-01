#!/usr/bin/env python3
"""
stackchan-led-chase.py — animated LED status chase for the 12-LED base ring.

Watches two marker files written elsewhere:
    %TEMP%\\stackchan-busy            -> stackchan-hook.py (busy-start writes,
                                          say-done removes)            -> amber chase
    %TEMP%\\stackchan-voice-thinking  -> stackchan-voice-bridge.py (set while
                                          transcribing + asking Claude) -> rainbow chase

While NEITHER marker exists, this script does nothing — it leaves the LEDs
exactly as stackchan-hook.py's static idle-blue / urgent-red left them. It
only takes over rendering while one of the two markers is present, and stops
writing the instant it disappears (the owning script is responsible for
setting the final static color, e.g. say-done sets idle blue).

Run via stackchan-led-chase-start.vbs (hidden pythonw, same pattern as the
ambient idle-fidget loop). Single-instance locked.
"""
from __future__ import annotations
import colorsys
import json
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
BUSY_MARKER = os.path.join(TEMP, "stackchan-busy")
VOICE_THINKING_MARKER = os.path.join(TEMP, "stackchan-voice-thinking")

# A crashed owner process (e.g. a native segfault in the voice bridge's
# Whisper call) skips its own Python `finally` cleanup entirely, so the
# marker it wrote never gets removed — the chase would otherwise animate
# forever. Treat a marker older than its threshold as abandoned and ignore
# (and delete) it. Busy gets a generous window since long Claude Code turns
# are normal; voice interactions should always finish in well under a minute.
BUSY_STALE_S = 30 * 60
THINKING_STALE_S = 90

NUM_LEDS = 12
STEP_S = 0.12  # ~8 fps — smooth enough for a chase, light on the WS link


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


def main():
    _acquire_lock()
    session = MCPSession(GATEWAY_MCP)
    have_session = False
    pos = 0
    hue = 0.0

    while True:
        time.sleep(STEP_S)

        busy = _marker_active(BUSY_MARKER, BUSY_STALE_S)
        thinking = _marker_active(VOICE_THINKING_MARKER, THINKING_STALE_S)
        if not (busy or thinking):
            have_session = False  # drop session while idle; re-init on resume
            continue

        if not device_connected():
            have_session = False
            continue

        try:
            if not have_session:
                session.init()
                have_session = True
            if thinking:
                hue = (hue + 30) % 360
                session.set_leds(rainbow_chase_frame(hue))
            else:
                pos = (pos + 1) % NUM_LEDS
                session.set_leds(amber_chase_frame(pos))
        except Exception:
            have_session = False  # re-init next tick


if __name__ == "__main__":
    main()
