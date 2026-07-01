#!/usr/bin/env python3
"""
stackchan-hook.py
Usage: python stackchan-hook.py <default_face> [mode]

  mode (optional):
    busy-start    -> PreToolUse: on the FIRST tool call of a new turn, set
                     the busy/concentrating face + speak a short busy line
                     once; subsequent calls in the same turn are silent
                     no-ops (tracked via a marker file) so this does NOT
                     flicker the face on every single tool call.
    busy-continue -> PostToolUse: on success, do nothing to the face (leaves
                     the busy face showing); on error, alert face + speech
                     (same error behaviour as say-on-error).
    say-on-error  -> speak a short failure phrase ONLY when the tool result
                     is an error; stays silent on success.
    urgent-say    -> Notification hook: speak an attention-grabbing prefix
                     line + the Notification `message`, with extra head
                     movement — for moments StackChan needs YOU (e.g. a
                     permission prompt), distinctly different from routine
                     busy/done chatter.
    say-done      -> speak a short, randomly-chosen completion phrase aloud
                     (used by the Stop hook, which has no message to read);
                     also clears the busy-turn marker.

Reads Claude Code hook data from stdin, detects errors, posts the right
expression to the stackchan-mcp daemon at http://127.0.0.1:8767/mcp.

The daemon's MCP endpoint is stateful Streamable HTTP, so each call here does
a full mini-handshake (initialize -> notifications/initialized -> tools/call)
using a fresh session, then lets the session expire.
"""
import sys
import os
import json
import random
import time
import urllib.request

GATEWAY_URL = "http://127.0.0.1:8767/mcp"

# Mark "activity now" so the ambient idle-fidget loop (stackchan-idle.py)
# holds still while Claude is actively working / speaking.
ACTIVITY_FILE = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", ".")), "stackchan-activity"
)
try:
    with open(ACTIVITY_FILE, "w") as _af:
        _af.write(str(time.time()))
except Exception:
    pass
face = sys.argv[1] if len(sys.argv) > 1 else "neutral"
mode = sys.argv[2] if len(sys.argv) > 2 else None
should_say = mode in ("say-done", "say-on-error", "urgent-say", "busy-start")

BUSY_MARKER = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", ".")), "stackchan-busy"
)

# Short, varied phrases for the Stop hook so it doesn't feel robotic.
DONE_PHRASES = [
    "All done!",
    "Finished.",
    "Task complete.",
    "Done and dusted.",
    "Ready when you are.",
    "That's done.",
]

# Short failure phrases for the PostToolUse hook (spoken only on error).
ERROR_PHRASES = [
    "That command failed.",
    "Uh oh, something went wrong.",
    "That didn't work.",
    "Error.",
]

# Busy/concentrating lines (Wheatley-flavoured), spoken ONCE per turn when
# the first tool call comes in — not every call, to avoid flicker/spam.
BUSY_PHRASES = [
    "Okay, I've just gotta concentrate.",
    "Hang on, hang on, working on it.",
    "Give me a second here.",
    "Right, let's have a look at this.",
]

# Attention-grabbing "I need you" lines, prefixed onto the Notification
# message so it reads as clearly different from busy/done chatter.
URGENT_PHRASES = [
    "Oi! Need a hand over here.",
    "Excuse me, bit of help please?",
    "Over here! Need you for a tic.",
]

# Funny Wheatley-flavoured easter eggs — occasionally swapped in for a
# plain DONE_PHRASES line (see EASTER_EGG_PROB below) so the done-chatter
# doesn't get stale.
EASTER_EGG_PHRASES = [
    "Brain damage's possible, but don't worry about it.",
    "I'm rambling out of fear now, just so you know.",
    "That was too aggressive — sorry, let's try that again.",
    "It's only a robot on a stick. Different one, though.",
    "I spy with my little eye... anyway. All done.",
]
EASTER_EGG_PROB = 0.15

message_to_say = None
skip_avatar = False
error_detected = False

if mode == "say-done":
    if random.random() < EASTER_EGG_PROB:
        message_to_say = random.choice(EASTER_EGG_PHRASES)
    else:
        message_to_say = random.choice(DONE_PHRASES)
    try:
        os.remove(BUSY_MARKER)
    except OSError:
        pass

if mode == "busy-start":
    if os.path.exists(BUSY_MARKER):
        skip_avatar = True   # already announced busy this turn — don't flicker
    else:
        message_to_say = random.choice(BUSY_PHRASES)
        try:
            with open(BUSY_MARKER, "w") as _bf:
                _bf.write(str(time.time()))
        except Exception:
            pass

if mode == "busy-continue":
    # Success case: leave whatever face is already showing (the busy face
    # set by busy-start) instead of resetting on every single tool call —
    # that per-call flicker was the original problem with always-on hooks.
    skip_avatar = True

try:
    raw = sys.stdin.read().lstrip("﻿")
    if raw.strip():
        data = json.loads(raw)
        # Detect tool errors in PostToolUse
        resp = data.get("tool_response", {})
        if isinstance(resp, dict) and resp.get("is_error"):
            error_detected = True
        elif isinstance(resp, list):
            for item in resp:
                if isinstance(item, dict) and item.get("is_error"):
                    error_detected = True
                    break
        if error_detected:
            face = "surprised"   # centered wide-eyed alarm
            skip_avatar = False
            if mode == "say-on-error":
                message_to_say = random.choice(ERROR_PHRASES)
        # Extract Notification message for speech
        if mode == "urgent-say":
            raw_message = (data.get("message") or "").strip()
            prefix = random.choice(URGENT_PHRASES)
            message_to_say = f"{prefix} {raw_message}".strip()
            if len(message_to_say) > 200:
                message_to_say = message_to_say[:197] + "..."
except Exception:
    pass


class MCPSession:
    def __init__(self, url, timeout=10):
        self.url = url
        self.timeout = timeout
        self.session_id = None
        self._next_id = 1

    def _post(self, payload, timeout=None):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers=headers,
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=timeout or self.timeout)
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        body = resp.read()
        return json.loads(body) if body.strip() else None

    def initialize(self):
        self._post({
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "stackchan-hook", "version": "1.0"},
            },
        })
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, name, arguments, timeout=None):
        call_id = self._next_id
        self._next_id += 1
        return self._post(
            {
                "jsonrpc": "2.0",
                "id": call_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            },
            timeout=timeout,
        )


# Characterful head movement (Wheatley) for meaningful moments only — NOT on
# every Bash tool call, to spare the servos. Each entry is a sequence of
# ("move", yaw, pitch) and ("sleep", seconds) steps. Neutral is ~yaw 0,
# pitch 45; ranges are yaw -90..90, pitch 5..85. Kept small + gentle, with
# pauses between reversals (firmware dislikes large rapid reversals).
# Pitch convention: LOWER pitch = look UP (toward the user, who sits above
# the desk bot), HIGHER pitch = look DOWN. Neutral ~45.
LOOK_DOWN_PITCH = 60   # busy/concentrating — head down at the "work"
LOOK_UP_PITCH = 34     # idle/need-you — looking up, engaged with the user

# Persistent status LED colours (currently hidden inside the case, but
# harmless to send — ready for when the new shell exposes them). Distinct
# hues so idle/need-you never look ambiguous at a glance. The busy state
# itself is an amber chase animated by stackchan-led-chase.py, not a color
# set here — see the BUSY_MARKER comment below.
IDLE_LED = (0, 25, 90)      # dim blue — resting/idle
URGENT_LED = (255, 0, 0)    # bright red flash — needs you


def _set_leds(session, rgb):
    try:
        session.call_tool("set_all_leds", {"r": rgb[0], "g": rgb[1], "b": rgb[2]})
    except Exception:
        pass

HEAD_MOVES = {
    # "sad" is the busy-hook's face arg (see settings.json busy-start /
    # busy-continue) — only busy-start actually calls run_head_moves (the
    # skip_avatar gate keeps busy-continue from repeating this every tool
    # call), so this is a single held look-down pose for the whole turn,
    # NOT a flinch-and-return.
    "sad": [("move", -4, LOOK_DOWN_PITCH)],
    "surprised": [("move", 0, LOOK_UP_PITCH)],             # snap to attention, looking up
    "urgent": [("move", 0, LOOK_UP_PITCH - 4), ("sleep", 0.22),   # insistent "look at
               ("move", -9, LOOK_UP_PITCH), ("sleep", 0.18),      # me!" wobble, layered
               ("move", 9, LOOK_UP_PITCH), ("sleep", 0.18),       # on top of the plain
               ("move", 0, LOOK_UP_PITCH)],                       # "surprised" snap
}


def run_head_moves(session, face_name):
    seq = HEAD_MOVES.get(face_name)
    if not seq:
        return
    for step in seq:
        if step[0] == "move":
            session.call_tool("move_head", {"yaw": step[1], "pitch": step[2]})
        elif step[0] == "sleep":
            time.sleep(step[1])


class TalkingBob:
    """Gently bob/sway the head while speaking — Wheatley is hugely animated
    when he talks. Runs on its own MCP session in a background thread so it
    moves *during* the (blocking) say() call, then settles. Centre pitch is
    biased slightly up (~43) so he 'looks at you' across the desk while
    nattering. Small amplitude + ~0.6s cadence keeps it gentle and keeps WS
    traffic light alongside the audio stream."""

    def __init__(self):
        self._stop = __import__("threading").Event()
        self._sess = MCPSession(GATEWAY_URL)
        self._thread = None

    def _loop(self):
        try:
            self._sess.initialize()
        except Exception:
            return
        center_p = LOOK_UP_PITCH + 6   # looking up at the user while chatting
        last_y = 0
        while not self._stop.is_set():
            try:
                # big, animated, gesticulating motion — the deliberate contrast
                # to the small idle fidget. Wider yaw swings + bigger pitch bob,
                # with the occasional emphatic lean.
                if random.random() < 0.25:
                    y = random.choice([random.randint(-26, -14),
                                       random.randint(14, 26)])  # emphatic lean
                    p = center_p + random.randint(-7, 9)
                else:
                    y = max(-20, min(20, last_y + random.randint(-12, 12)))
                    p = center_p + random.randint(-6, 8)
                last_y = y
                self._sess.call_tool("move_head", {"yaw": y, "pitch": p})
            except Exception:
                pass
            self._stop.wait(random.uniform(0.4, 0.7))

    def __enter__(self):
        th = __import__("threading").Thread(target=self._loop, daemon=True)
        self._thread = th
        th.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)
        try:
            self._sess.call_tool("move_head", {"yaw": 0, "pitch": LOOK_UP_PITCH + 6})  # settle, looking up
        except Exception:
            pass


try:
    session = MCPSession(GATEWAY_URL)
    session.initialize()
    if not skip_avatar:
        session.call_tool("set_avatar", {"face": face})
        run_head_moves(session, face)
    # Note: the busy LED itself (amber Cylon-style chase) is owned by the
    # separate stackchan-led-chase.py background loop, which watches for
    # BUSY_MARKER and animates continuously — that supersedes any static/
    # blip LED set from this script while a turn is in progress. This hook
    # only writes/clears the marker (above) and renders face/head/eye.
    if mode == "busy-continue" and not error_detected:
        # Eye does a fast upward flutter into the top lid on each completed
        # tool call ("Neo learning kung fu" download-look) via
        # set_mouth_sequence — a firmware-local step queue, so this is one
        # network call regardless of step count. mouth_e/mouth_u are
        # repurposed for this (see wheatley_avatar.py) — firmware's own
        # lip-sync auto-cycle never touches those two slots, so it can't
        # collide with real speech. set_mouth_sequence returns immediately;
        # sleep the queued duration before restoring the steady busy face,
        # or the restore would interrupt the in-flight sequence early.
        flutter_steps = [
            {"shape": "e", "duration_ms": 70},
            {"shape": "u", "duration_ms": 60},
            {"shape": "e", "duration_ms": 70},
            {"shape": "u", "duration_ms": 60},
        ]
        try:
            session.call_tool("set_mouth_sequence", {"steps": flutter_steps})
        except Exception:
            pass
        time.sleep(sum(s["duration_ms"] for s in flutter_steps) / 1000)
        try:
            session.call_tool("set_avatar", {"face": "sad"})
        except Exception:
            pass
    if mode == "say-done":
        # Release the held busy look-down pose (if any) back to a relaxed
        # looking-up idle pose now that the turn is over.
        session.call_tool("move_head", {"yaw": 0, "pitch": LOOK_UP_PITCH + 6})
        _set_leds(session, IDLE_LED)
    if mode == "urgent-say":
        run_head_moves(session, "urgent")
        # Blink red a few times to catch the eye, then HOLD solid red —
        # do not revert to idle blue here. A notification means work is
        # genuinely paused waiting on the user; reverting to "calm" would
        # misrepresent "still waiting on you" as "all clear". Stays red
        # until the next busy-start/say-done changes it.
        for _ in range(3):
            _set_leds(session, URGENT_LED)
            time.sleep(0.18)
            _set_leds(session, (0, 0, 0))
            time.sleep(0.12)
        _set_leds(session, URGENT_LED)
    if should_say and message_to_say:
        with TalkingBob():
            session.call_tool("say", {"text": message_to_say}, timeout=30)
except Exception:
    pass
