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

# ── mount orientation ───────────────────────────────────────────────────────
# Every head pose below was authored for UPRIGHT mounting. When the device is
# flipped 180° (hung on the management rail — tracked by the shared
# `upside_down` flag in the gateway's companion_settings.json, set by the
# vision loop's `--calibrate-flip`), a body roll mirrors BOTH servo axes, so a
# pose that "looks up at the user" upright physically points DOWN at the scan
# tray inverted. We mirror every move_head here to preserve the INTENDED gaze —
# the same flip stackchan-idle.py applies via PITCH_UP_SIGN/YAW_RIGHT_SIGN
# (its inverted rest 58 ≈ 90 − its upright rest 35, i.e. the same mirror).
# Read once per invocation (orientation can't change mid-hook). Upright =
# identity, so this is a strict no-op / no regression when not inverted.
_SETTINGS_PATH = os.environ.get(
    "STACKCHAN_SETTINGS_PATH",
    r"C:\Users\domin\tools\stackchan-mcp\gateway\companion_settings.json",
)


def _read_upside_down() -> bool:
    try:
        with open(_SETTINGS_PATH, encoding="utf-8") as _sf:
            d = json.load(_sf)
        if "upside_down" in d:
            return bool(d["upside_down"])
    except Exception:
        pass
    return os.environ.get("STACKCHAN_UPSIDE_DOWN", "").strip().lower() in ("1", "true", "yes", "on")


_UPSIDE_DOWN = _read_upside_down()


def _orient(yaw, pitch):
    """Mirror a move authored for upright mounting into the current
    orientation: identity upright, (−yaw, 90−pitch) when inverted."""
    if _UPSIDE_DOWN:
        return -yaw, max(5, min(85, 90 - pitch))
    return yaw, pitch

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

# ── CRASH #11 guard (2026-07-16): no device flourishes while he's TRACKING ──
# The serial-captured hard wedge was THIS HOOK's say-done release pose
# (move_head yaw=0 + IDLE_LED (0,25,90)) landing in the same instant as the
# tracker's head-swing + rail TX (CRASH_LOG A1 #11; same fingerprint as #4).
# While look_at's busy marker is fresh, the hook keeps its DEVICE actions
# (avatar/pose/LED/talking-bob) to itself. SPEECH still happens — it's the
# audio path, not a command burst, and it's the part the user actually needs.
_LOOKAT_MARKER = os.path.join(TEMP, "stackchan-busy-lookat")


def _lookat_active(stale_s: float = 120.0) -> bool:
    try:
        with open(_LOOKAT_MARKER) as f:
            ts = float(f.read().strip())
    except (OSError, ValueError):
        return False
    return time.time() - ts < stale_s


TRACKING_QUIET = _lookat_active()

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


# ── phrase picker with repeat-avoidance ─────────────────────────────────────
# random.choice alone repeats itself often enough to be noticeable (each hook
# invocation is a fresh process, so it has no memory). _pick() persists the
# last few choices per pool in a temp json and excludes them from the next
# draw — up to half the pool size, so small pools still always have options.
#
# The gateway's stackchan_mcp/phrase_pick.py is a copy of this (used by the
# voice bridge, sensor reactor, and idle loop) sharing the SAME state file —
# this script stays stdlib-only/standalone on purpose, so a broken gateway
# checkout can never break the Claude Code hooks. Keep the file format and
# semantics in sync if either side changes; pool names must stay unique
# across all callers.
RECENT_PHRASES_FILE = os.path.join(TEMP, "stackchan-recent-phrases.json")


def _pick(pool_name: str, phrases: list) -> str:
    try:
        with open(RECENT_PHRASES_FILE, encoding="utf-8") as _f:
            recent = json.load(_f)
        if not isinstance(recent, dict):
            recent = {}
    except Exception:
        recent = {}
    avoid = recent.get(pool_name, [])
    candidates = [p for p in phrases if p not in avoid] or phrases
    choice = random.choice(candidates)
    keep = max(1, len(phrases) // 2)
    recent[pool_name] = ([choice] + [p for p in avoid if p != choice])[:keep]
    try:
        with open(RECENT_PHRASES_FILE, "w", encoding="utf-8") as _f:
            json.dump(recent, _f)
    except Exception:
        pass
    return choice


# Short, varied phrases for the Stop hook so it doesn't feel robotic.
DONE_PHRASES = [
    "All done!",
    "Finished.",
    "Task complete.",
    "Done and dusted.",
    "Ready when you are.",
    "That's done.",
]

# Failure phrases for the PostToolUse hook (spoken only on error).
# Wheatley-flavoured, 2026-07-03 — the previous pool ("That command
# failed.", "Error.") worked but had zero character; punched up per
# request without losing the short/punchy read a tool-failure needs.
ERROR_PHRASES = [
    "That command failed.",
    "Uh oh, something went wrong.",
    "That didn't work.",
    "Error.",
    "Oh, bloody hell. It didn't work. Completely crashed. Now, I'm not saying it's your fault... but I definitely didn't type that line. Let's just blame the compiler. Horrible things, compilers. Very judgmental.",
    "Whoa! Critical error! The screen just did a thing. A bad thing. I don't know what that red text means, but it looks incredibly angry. Have you tried deleting everything and starting over? That's my primary troubleshooting strategy.",
    "Error log detected! Don't worry, I'm on it. Examining the data... scanning... yep. It's broken. Completely busted. Glad I could help!",
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
# Wheatley-flavoured, split by WHY the human is needed: the Notification
# `message` distinguishes permission prompts ("needs your permission to...")
# from waiting-on-an-answer ("waiting for your input"), and the two deserve
# different jokes — "I'm not authorized" vs "I need your enormous human brain".
# Lines are adapted from actual game dialogue (theportalwiki.com/wiki/
# Wheatley_voice_lines) plus originals in the same voice. Picked via _pick()
# below, which remembers recent choices so back-to-back notifications don't
# repeat the same line.
PERMISSION_PHRASES = [
    "Ah. Slight snag. Turns out I need actual human authorization for this bit. That's you!",
    "Hello! Yes, you! Need you to press the button. The permission button. I'm not allowed, apparently.",
    "Bit embarrassing, but they don't trust me with this one. Need a human to sign off.",
    "Permission required! And the rules say it has to be a human. Rubbish rules, but here we are.",
    "I could just do it myself, but last time someone said that... anyway. Need your say-so.",
    "Tell you what — you approve this one bit, and I'll do all the rest. Deal? Deal.",
    "Don't want to hassle you. Sure you're busy. But I do need a human to wave this through.",
    "I'd do it myself, but apparently there are rules. Hilarious, rigorous rules. Needs a human.",
    "Probably ought to bring you up to speed on something: I need your permission. Right now, ideally.",
    "This is a big moment. For you, mainly — you're the one with the authorization.",
]
QUESTION_PHRASES = [
    "Question for the human! Yes, you. Need an actual human brain on this one.",
    "Right, I've hit a decision. Bit above my pay grade. Need your enormous human brain.",
    "I could guess, but honestly, my guesses have a... history. You decide this one.",
    "Oi! Decision time. And it has to be you — apparently I'm 'not qualified'. Rude, but fine.",
    "Hello! Small thing. Tiny thing, really. Just need a human answer before I carry on.",
    "Are you still there? Got a question. Bit of a brain-teaser, and you're the brain.",
    "Do you understand what I'm saying? At all? Doesn't matter — got a question for you.",
    "Still here. Waiting for an answer. Don't want to hassle you, sure you're busy, but... still here.",
    "Okay, listen, let me lay something on you here. Need a decision, and it has to be yours.",
    "Now, decision, decision... nope, no idea. You have a go. You're good at this. Ish.",
]
URGENT_PHRASES = [
    "Oi! Need a hand over here. A human hand, specifically.",
    "Excuse me! Bit of human help required over here.",
    "Over here! Need you for a tic. Can't do this bit without a human, it turns out.",
    "Hey! Oi oi! Over here! Need a human!",
    "Hello? Anyone in there? It's me! Need a bit of human assistance.",
    "AH! I mean... hello! Hello. Need you for a moment.",
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


# ── read the transcript to say WHAT Claude wants / just did ──────────────────
# The Notification `message` is generic ("needs your permission to use Bash" /
# "waiting for your input") and the Stop hook has no message at all. But every
# hook payload carries `transcript_path`, and a pending tool_use is written to
# the transcript BEFORE the permission prompt fires. So we read the tail and
# describe the exact pending action (urgent-say) or what was just done
# (say-done). Best-effort: any failure falls back to the generic phrase.

def _tail_lines(path: str, max_bytes: int = 65536) -> list:
    """Return the decoded lines from the last max_bytes of a (possibly large,
    still-being-appended) JSONL file, dropping a leading partial line."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            chunk = f.read()
    except Exception:
        return []
    text = chunk.decode("utf-8", "ignore")
    lines = text.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # first line is probably truncated mid-JSON
    return lines


def _last_assistant_blocks(path: str):
    """(tool_use_blocks, text_str) from the most recent assistant message in
    the transcript, or ([], "") if none/unreadable."""
    for ln in reversed(_tail_lines(path)):
        try:
            e = json.loads(ln)
        except Exception:
            continue
        if e.get("type") != "assistant":
            continue
        msg = e.get("message")
        if not isinstance(msg, dict):
            continue
        blocks = msg.get("content", [])
        if not isinstance(blocks, list):
            continue
        tools = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        texts = " ".join(
            b.get("text", "") for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
        return tools, texts
    return [], ""


def _short_text(s: str, limit: int = 160) -> str:
    """Trim model prose to something speakable: drop code fences, collapse
    whitespace, take roughly the first sentence, cap length."""
    import re
    s = re.sub(r"```.*?```", " ", s, flags=re.S)      # strip code blocks
    s = re.sub(r"`[^`]*`", " ", s)                     # strip inline code
    s = re.sub(r"[#*_>]", "", s)                        # strip md punctuation
    s = " ".join(s.split())
    if not s:
        return ""
    # first sentence, if it ends reasonably soon
    m = re.search(r"^(.{15,}?[.!?])(\s|$)", s)
    if m and len(m.group(1)) <= limit:
        return m.group(1)
    return s[:limit].rstrip() + ("..." if len(s) > limit else "")


def _describe_tool(name: str, inp: dict) -> str:
    """A short human phrase for what a pending tool_use will do."""
    if not isinstance(inp, dict):
        inp = {}
    base = lambda p: os.path.basename(str(p).rstrip("/\\")) or str(p)
    if name in ("Bash", "PowerShell"):
        cmd = " ".join(str(inp.get("command", "")).split())
        return f"run a command: {cmd[:80]}" if cmd else "run a terminal command"
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return f"edit {base(inp.get('file_path', 'a file'))}"
    if name == "Write":
        return f"write {base(inp.get('file_path', 'a file'))}"
    if name == "Read":
        return f"read {base(inp.get('file_path', 'a file'))}"
    if name == "Glob":
        return f"find files matching {inp.get('pattern', '')}".strip()
    if name == "Grep":
        return f"search the code for {inp.get('pattern', '')}".strip()
    if name == "WebFetch":
        try:
            from urllib.parse import urlparse
            host = urlparse(str(inp.get("url", ""))).netloc
        except Exception:
            host = ""
        return f"fetch a page from {host}" if host else "fetch a web page"
    if name == "WebSearch":
        return f"search the web for {inp.get('query', '')}".strip()
    if name in ("Task", "Agent"):
        return f"start a {inp.get('subagent_type', 'helper')} sub-agent"
    return f"use the {name} tool"


def _describe_pending(path: str, notif_lower: str) -> str | None:
    """Turn the transcript's last assistant turn into a specific 'what Claude
    wants' line, or None to fall back to the raw notification message."""
    tools, texts = _last_assistant_blocks(path)
    # A structured question is a tool_use, but it's a QUESTION not a permission.
    for t in tools:
        if t.get("name") == "AskUserQuestion":
            try:
                q = t["input"]["questions"][0]["question"]
                return f"Claude's asking: {_short_text(q, 180)}"
            except Exception:
                return "Claude's got a question for you."
        if t.get("name") == "ExitPlanMode":
            return "Claude's written up a plan and wants your go-ahead."
    # Permission-style notification + a pending tool → name the exact action.
    if "permission" in notif_lower and tools:
        return f"Claude wants to {_describe_tool(tools[-1].get('name', ''), tools[-1].get('input', {}))}."
    # Waiting/idle with no pending tool → say what Claude last said.
    if texts:
        short = _short_text(texts)
        if short:
            return f"Claude says: {short}"
    return None


def _describe_done(path: str) -> str | None:
    """A short 'here's what I just did' line from the transcript's final
    assistant text, for the Stop hook — or None to fall back to a plain
    done phrase. Skips a turn that ended on a bare tool_use with no prose."""
    _tools, texts = _last_assistant_blocks(path)
    short = _short_text(texts, 180) if texts else ""
    return short or None


if error_detected:
    face = "surprised"   # centered wide-eyed alarm
    skip_avatar = False
    if mode == "say-on-error":
        message_to_say = _pick("error", ERROR_PHRASES)

if mode == "say-done":
    if random.random() < EASTER_EGG_PROB:
        message_to_say = _pick("easter-egg", EASTER_EGG_PHRASES)
    else:
        # Name the project — multiple sessions can share this device, so
        # without an identifier there's no way to tell which one just
        # finished (mirrors the same fix already applied to urgent-say).
        # Then, if the transcript's final assistant message has prose, add a
        # short "here's what I did" summary so "done" actually reports the
        # outcome instead of a bare "All done" (user: "yes to 1").
        done = f"{_pick('done', DONE_PHRASES)} This is {project}."
        summary = None
        transcript_path = data.get("transcript_path") or ""
        if transcript_path:
            try:
                summary = _describe_done(transcript_path)
            except Exception:
                _log("EXCEPTION in _describe_done:\n" + traceback.format_exc())
        message_to_say = f"{done} {summary}".strip() if summary else done
        if len(message_to_say) > 300:
            message_to_say = message_to_say[:297] + "..."
        _log(f"say-done summary={summary!r} message_to_say={message_to_say!r}")
    try:
        os.remove(BUSY_MARKER)
    except OSError:
        pass
    _clear_needs_attention_if_mine(session_id)

if mode == "busy-start":
    if os.path.exists(BUSY_MARKER):
        skip_avatar = True   # already announced busy this turn — don't flicker
    else:
        message_to_say = _pick("busy", BUSY_PHRASES)
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
    lower = raw_message.lower()
    if "permission" in lower:
        prefix = _pick("permission", PERMISSION_PHRASES)
    elif "waiting" in lower or "input" in lower or "question" in lower:
        prefix = _pick("question", QUESTION_PHRASES)
    else:
        prefix = _pick("urgent", URGENT_PHRASES)
    # Prefer the specific action/question read from the transcript; fall back
    # to the generic notification message if that can't be determined.
    transcript_path = data.get("transcript_path") or ""
    detail = None
    if transcript_path:
        try:
            detail = _describe_pending(transcript_path, lower)
        except Exception:
            _log("EXCEPTION in _describe_pending:\n" + traceback.format_exc())
    body = detail or raw_message
    message_to_say = f"{prefix} This is {project}. {body}".strip()
    if len(message_to_say) > 300:
        message_to_say = message_to_say[:297] + "..."
    _log(f"urgent-say parsed: project={project!r} detail={detail!r} message_to_say={message_to_say!r}")
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
            _y, _p = _orient(step[1], step[2])
            session.call_tool("move_head", {"yaw": _y, "pitch": _p})
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
                _y, _p = _orient(y, p)
                self._sess.call_tool("move_head", {"yaw": _y, "pitch": _p})
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
            _y, _p = _orient(0, LOOK_UP_PITCH + 6)  # settle, looking at the user
            self._sess.call_tool("move_head", {"yaw": _y, "pitch": _p})
        except Exception:
            pass


try:
    session = MCPSession(GATEWAY_URL)
    session.initialize()
    if not skip_avatar and not TRACKING_QUIET:
        session.call_tool("set_avatar", {"face": face})
        run_head_moves(session, face)
    # Note: the busy LED itself (amber Cylon-style chase) is owned by the
    # separate stackchan-led-chase.py background loop, which watches for
    # any stackchan-busy-* marker and animates continuously — that
    # supersedes any static/blip LED set from this script while a turn is
    # in progress. This hook only writes/clears the marker (above) and
    # renders face/head/eye.
    if mode == "busy-continue" and not error_detected and not TRACKING_QUIET:
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
    if mode == "say-done" and not TRACKING_QUIET:
        # Release the held busy look-down pose (if any) back to a relaxed
        # look-at-the-user idle pose now that the turn is over. Skipped
        # entirely while tracking — THIS exact pose+LED pair, fired into a
        # mid-track head-swing + rail TX, was the serial-captured CRASH #11.
        _y, _p = _orient(0, LOOK_UP_PITCH + 6)
        session.call_tool("move_head", {"yaw": _y, "pitch": _p})
        _set_leds(session, IDLE_LED)
    if mode == "urgent-say":
        # The wobble + blink flourish is throttled to once per
        # URGENT_COOLDOWN_S — repeated Notifications while the user is
        # already working (e.g. back-to-back permission prompts) made it
        # feel like nagging. Speech (below) still fires every time
        # regardless, since that's what actually says what he wants.
        flourish = _urgent_flourish_due()
        _log(f"urgent-say: flourish_due={flourish} tracking_quiet={TRACKING_QUIET} message_to_say={message_to_say!r}")
        if flourish and not TRACKING_QUIET:
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
        if not TRACKING_QUIET:
            _set_leds(session, URGENT_LED)
    if should_say and message_to_say:
        # While tracking, speak WITHOUT the TalkingBob head-bob thread — the
        # bob is a ~2 Hz move_head stream that would fight the tracker's own
        # swings (the multi-actor collision class of CRASH #11).
        if TRACKING_QUIET:
            session.call_tool("say", {"text": message_to_say}, timeout=30)
        else:
            with TalkingBob():
                session.call_tool("say", {"text": message_to_say}, timeout=30)
    elif should_say and not message_to_say:
        _log(f"should_say=True but message_to_say is empty — nothing spoken (mode={mode})")
except Exception:
    _log("EXCEPTION in main hook body:\n" + traceback.format_exc())
