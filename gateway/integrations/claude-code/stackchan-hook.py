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
                     also clears this session's busy-turn marker.

Reads Claude Code hook data from stdin, detects errors, posts the right
expression to the stackchan-mcp daemon at http://127.0.0.1:8767/mcp.

The daemon's MCP endpoint is stateful Streamable HTTP, so each call here does
a full mini-handshake (initialize -> notifications/initialized -> tools/call)
using a fresh session, then lets the session expire.

MULTI-SESSION NOTE (2026-07-01): multiple Claude Code sessions/projects can
share this one physical device and hook script. The busy marker is scoped
per session_id (stackchan-busy-<id>) so one session finishing its turn can't
wrongly clear another session's still-active busy state — earlier this was
a single shared file and any session's say-done erased everyone's busy
indicator. There's also a stackchan-needs-attention marker (see below) that
gives "needs you" priority over any session's busy state, instead of
whichever hook fires last silently overwriting the other's signal.
"""
import sys
import os
import json
import random
import time
import traceback
import urllib.request

GATEWAY_URL = "http://127.0.0.1:8767/mcp"
TEMP = os.environ.get("TEMP", os.environ.get("TMP", "."))

# This script previously swallowed every exception silently with no record of
# what mode/payload it ran with — made it impossible to tell why a given
# notification did (or didn't) speak. Append-only log, one line per event.
HOOK_LOG = os.path.join(TEMP, "stackchan-hook.log")


def _log(line: str) -> None:
    try:
        with open(HOOK_LOG, "a", encoding="utf-8") as _lf:
            _lf.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except Exception:
        pass


# Mark "activity now" so the ambient idle-fidget loop (stackchan-idle.py)
# holds still while Claude is actively working / speaking.
ACTIVITY_FILE = os.path.join(TEMP, "stackchan-activity")
try:
    with open(ACTIVITY_FILE, "w") as _af:
        _af.write(str(time.time()))
except Exception:
    pass

face = sys.argv[1] if len(sys.argv) > 1 else "neutral"
mode = sys.argv[2] if len(sys.argv) > 2 else None
should_say = mode in ("say-done", "say-on-error", "urgent-say", "busy-start")

# ── read + parse stdin ONCE, early — session_id/cwd/project must be known
# BEFORE the busy-marker logic below runs, since the marker is now per-session
# (see module docstring). Previously this parsing happened after that logic,
# which was fine when the marker was a single global file. ─────────────────
data = {}
error_detected = False
try:
    raw = sys.stdin.read().lstrip("﻿")
    _log(f"invoked mode={mode} face={face} raw_len={len(raw)} raw_preview={raw[:300]!r}")
    if raw.strip():
        data = json.loads(raw)
    else:
        _log("stdin was empty/whitespace-only — nothing to parse")
except Exception:
    _log("EXCEPTION parsing stdin:\n" + traceback.format_exc())

session_id = data.get("session_id") or "unknown-session"
_cwd = (data.get("cwd") or "").rstrip("/\\")
project = os.path.basename(_cwd) if _cwd else "a project"

message_to_say = None
skip_avatar = False

# Detect tool errors in PostToolUse
resp = data.get("tool_response", {})
if isinstance(resp, dict) and resp.get("is_error"):
    error_detected = True
elif isinstance(resp, list):
    for item in resp:
        if isinstance(item, dict) and item.get("is_error"):
            error_detected = True
            break

BUSY_MARKER = os.path.join(TEMP, f"stackchan-busy-{session_id}")

# Rate-limit the urgent head-wobble + red-blink flourish — user reported it's
# annoying when several Notifications fire close together while they're
# already working (permission prompts etc. can repeat quickly). The physical
# motion is what's distracting; the SPOKEN message is what's actually useful
# ("what does he want"), so only the wobble/blink gets throttled — speech
# still fires on every notification, unconditionally, below.
URGENT_MARKER = os.path.join(TEMP, "stackchan-urgent-last")
URGENT_COOLDOWN_S = 30.0


def _urgent_flourish_due() -> bool:
    try:
        with open(URGENT_MARKER) as _f:
            last = float(_f.read().strip())
    except Exception:
        last = 0.0
    return (time.time() - last) >= URGENT_COOLDOWN_S


def _mark_urgent_flourish() -> None:
    try:
        with open(URGENT_MARKER, "w") as _f:
            _f.write(str(time.time()))
    except Exception:
        pass


# ── "needs attention" priority marker ───────────────────────────────────────
# One session finishing (say-done) or going busy (busy-start) must NOT be
# able to silently erase another session's still-outstanding "I need you"
# signal — that was possible when everything shared one busy marker and the
# LED chase just watched it. This is a single marker (not per-session: only
# one physical LED, so "someone needs you" is the signal, not which one —
# the SPOKEN message already names the project). stackchan-led-chase.py gives
# this priority over any busy chase. Cleared only when the SAME session_id
# that raised it fires busy-start or say-done again — i.e. once the user has
# actually gone back and engaged with that session.
NEEDS_ATTENTION_MARKER = os.path.join(TEMP, "stackchan-needs-attention")


def _write_needs_attention(sid: str, proj: str) -> None:
    try:
        with open(NEEDS_ATTENTION_MARKER, "w", encoding="utf-8") as _f:
            json.dump({"session_id": sid, "project": proj, "ts": time.time()}, _f)
    except Exception:
        pass


def _clear_needs_attention_if_mine(sid: str) -> None:
    try:
        with open(NEEDS_ATTENTION_MARKER, encoding="utf-8") as _f:
            d = json.load(_f)
        if d.get("session_id") == sid:
            os.remove(NEEDS_ATTENTION_MARKER)
    except Exception:
        pass


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

if error_detected:
    face = "surprised"   # centered wide-eyed alarm
    skip_avatar = False
    if mode == "say-on-error":
        message_to_say = random.choice(ERROR_PHRASES)

if mode == "say-done":
    if random.random() < EASTER_EGG_PROB:
        message_to_say = random.choice(EASTER_EGG_PHRASES)
    else:
        # Name the project — multiple sessions can share this device, so
        # without an identifier there's no way to tell which one just
        # finished (mirrors the same fix already applied to urgent-say).
        message_to_say = f"{random.choice(DONE_PHRASES)} This is {project}."
    try:
        os.remove(BUSY_MARKER)
    except OSError:
        pass
    _clear_needs_attention_if_mine(session_id)

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
    _clear_needs_attention_if_mine(session_id)

if mode == "busy-continue":
    # Success case: leave whatever face is already showing (the busy face
    # set by busy-start) instead of resetting on every single tool call —
    # that per-call flicker was the original problem with always-on hooks.
    skip_avatar = True

# Extract Notification message for speech. Multiple Claude Code
# sessions/projects can share this one physical hook, so without an
# identifier the user has no way to tell which one is calling —
# cwd is present on every Notification payload, so name the project.
if mode == "urgent-say":
    raw_message = (data.get("message") or "").strip()
    prefix = random.choice(URGENT_PHRASES)
    message_to_say = f"{prefix} This is {project}. {raw_message}".strip()
    if len(message_to_say) > 200:
        message_to_say = message_to_say[:197] + "..."
    _log(f"urgent-say parsed: project={project!r} message_to_say={message_to_say!r}")
    _write_needs_attention(session_id, project)


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
# Pitch convention: HIGHER pitch = look UP, LOWER pitch = look DOWN. Neutral
# ~45. (2026-07-01: this was previously documented backwards — "lower = up"
# — and every constant below was set accordingly, so busy/idle read as the
# OPPOSITE of intended. Confirmed via a clean isolated test: pitch=5 [the
# servo minimum] physically looked DOWN, pitch=85 [the maximum] looked UP.
# Fixed by swapping the two values below; every other reference in this
# file uses these constants BY NAME, so nothing else needed to change.)
LOOK_DOWN_PITCH = 34   # busy/concentrating — head down at the "work"
LOOK_UP_PITCH = 60     # idle/need-you — looking up, engaged with the user

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
    # any stackchan-busy-* marker and animates continuously — that
    # supersedes any static/blip LED set from this script while a turn is
    # in progress. This hook only writes/clears the marker (above) and
    # renders face/head/eye.
    if mode == "busy-continue" and not error_detected:
        # Eye does a fast upward flutter into the top lid on each completed
        # tool call ("Neo learning kung fu" download-look) via
        # set_mouth_sequence — a firmware-local step queue, so this is one
        # network call regardless of step count. mouth_e is repurposed for
        # this (see wheatley_avatar.py) — firmware's own lip-sync auto-cycle
        # never touches it, so it can't collide with real speech.
        # set_mouth_sequence returns immediately; sleep the queued duration
        # before restoring the steady busy face, or the restore would
        # interrupt the in-flight sequence early.
        #
        # 2026-07-01: this used to alternate mouth_e/mouth_u (both "up" —
        # see wheatley_avatar.py's old comment). mouth_u is now a genuine
        # "look down" cue instead of a second up-glance, so the flutter
        # alternates mouth_e with mouth_closed instead — still a fluttering
        # roll-up-and-settle motion, just using only the up slot.
        flutter_steps = [
            {"shape": "e", "duration_ms": 70},
            {"shape": "closed", "duration_ms": 60},
            {"shape": "e", "duration_ms": 70},
            {"shape": "closed", "duration_ms": 60},
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
        # The wobble + blink flourish is throttled to once per
        # URGENT_COOLDOWN_S — repeated Notifications while the user is
        # already working (e.g. back-to-back permission prompts) made it
        # feel like nagging. Speech (below) still fires every time
        # regardless, since that's what actually says what he wants.
        flourish = _urgent_flourish_due()
        _log(f"urgent-say: flourish_due={flourish} message_to_say={message_to_say!r}")
        if flourish:
            run_head_moves(session, "urgent")
            # Blink red a few times to catch the eye, then HOLD solid red —
            # do not revert to idle blue here. A notification means work is
            # genuinely paused waiting on the user; reverting to "calm" would
            # misrepresent "still waiting on you" as "all clear". Stays red
            # until the SAME session's next busy-start/say-done clears the
            # needs-attention marker (see _clear_needs_attention_if_mine) —
            # NOT superseded by some other session going busy in the
            # meantime, since stackchan-led-chase.py now gives this priority.
            for _ in range(3):
                _set_leds(session, URGENT_LED)
                time.sleep(0.18)
                _set_leds(session, (0, 0, 0))
                time.sleep(0.12)
            _mark_urgent_flourish()
        _set_leds(session, URGENT_LED)
    if should_say and message_to_say:
        with TalkingBob():
            session.call_tool("say", {"text": message_to_say}, timeout=30)
    elif should_say and not message_to_say:
        _log(f"should_say=True but message_to_say is empty — nothing spoken (mode={mode})")
except Exception:
    _log("EXCEPTION in main hook body:\n" + traceback.format_exc())
