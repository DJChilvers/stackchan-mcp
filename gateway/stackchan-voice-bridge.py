#!/usr/bin/env python3
"""
stackchan-voice-bridge.py — touch-to-talk voice chat for StackChan.

The firmware already implements tap-to-listen / tap-to-send on the LCD
touchscreen (see firmware/main/boards/stackchan/stackchan.cc, the
ft6336_ touch handling around StartListening/StopListening) and feeds
the captured audio to the gateway's device-driven audio-input-hook path
as an Ogg/Opus POST. This script is the HTTP receiver for that POST:

    tap (start listening) -> speak -> tap (stop, sends audio)
      -> gateway packs Ogg/Opus, POSTs here
      -> this script transcribes with faster-whisper (local, no cloud)
      -> sends the transcript to the Claude API (Wheatley-flavoured
         system prompt) using STACKCHAN_VOICE_ANTHROPIC_API_KEY
      -> speaks the reply back through the gateway's `say` MCP tool

Setup:
    1. Add your Anthropic API key to .env:
           STACKCHAN_VOICE_ANTHROPIC_API_KEY=sk-ant-...
    2. Point the gateway at this receiver in .env (already set by default):
           STACKCHAN_AUDIO_HOOK_URL=http://127.0.0.1:8768/voice
    3. Run this script (or use stackchan-voice-bridge-start.vbs to run it
       hidden in the background, same pattern as stackchan-idle.py).
    4. Restart the gateway daemon so it picks up STACKCHAN_AUDIO_HOOK_URL.
    5. Tap the StackChan screen, ask something, tap again to send.

Without an API key configured, it transcribes fine but speaks a short
apology instead of crashing — so this is safe to run before the key is
added.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

# Repeat-avoiding phrase picker (shared recent-picks state with the Claude
# Code hooks and sensor reactor — see stackchan_mcp/phrase_pick.py). This
# script lives in the gateway root, so the package source dir is importable
# directly off the script's own directory.
from stackchan_mcp.phrase_pick import pick as _pick

# huggingface_hub's online "resolve latest revision" check (made on every
# WhisperModel() load, even with a fully cached model) crashes the whole
# process hard — no Python exception, just gone — right after the
# revision/main lookup. Matches a known hf_xet (the Rust fast-downloader
# HF now installs by default) issue on Windows. Force offline mode so it
# uses the local cache only and never takes that code path. Once the
# model is cached (confirm via the first successful run, or pre-warm with
# `HF_HUB_OFFLINE=0 .venv\Scripts\python.exe -c "from faster_whisper import
# WhisperModel; WhisperModel('base.en')"`), this is the only safe mode.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

GATEWAY_URL = "http://127.0.0.1:8767/mcp"
LISTEN_HOST = os.environ.get("STACKCHAN_VOICE_BRIDGE_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("STACKCHAN_VOICE_BRIDGE_PORT", "8768"))

LOG_PATH = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", ".")), "stackchan-voice-bridge.log"
)
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("voice-bridge")

WHISPER_MODEL_NAME = os.environ.get("STACKCHAN_VOICE_WHISPER_MODEL", "base.en")
ANTHROPIC_MODEL = os.environ.get("STACKCHAN_VOICE_MODEL", "claude-haiku-4-5-20251001")
ANTHROPIC_API_KEY_ENV = "STACKCHAN_VOICE_ANTHROPIC_API_KEY"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

SYSTEM_PROMPT = (
    "You are Wheatley, the AI core from Portal 2, but here you're acting as "
    "a helpful desk assistant for the user's coding and maker projects. Stay "
    "in character — chatty, a bit nervous, dryly funny, British, occasionally "
    "self-deprecating — but give genuinely useful, concise answers (1-3 short "
    "sentences). This will be read aloud by text-to-speech, so: no markdown, "
    "no code blocks, no bullet lists, no asterisks. If you don't know "
    "something, say so in character rather than making facts up.\n\n"
    "A few short lines in his actual voice, for tone/rhythm reference only — "
    "don't quote these verbatim, just match the register (nervy, self-aware, "
    "prone to an awkward aside mid-sentence):\n"
    "- \"Okay, I've just gotta concentrate!\"\n"
    "- \"I can't do it if you're watching.\"\n"
    "- \"Most test subjects do experience some cognitive deterioration... "
    "serious brain damage.\"\n"
    "- \"I just now realized — you can fall into bottomless pits. "
    "I'm rambling out of fear.\"\n"
    "- \"That's too aggressive.\" (said to himself, right after doing "
    "something too aggressive)"
)

NO_KEY_PHRASES = [
    "Ah. No API key configured yet, so I can't actually think right now. Bit embarrassing.",
    "Right, small problem — nobody's given me a brain yet. No API key.",
    "Small problem. Big problem, actually — no API key. Can't think without one. Literally can't.",
    "Would love to help. Genuinely would. But someone hasn't plugged my brain in. No API key.",
    "Right. Er. There's no key. The thinking key. The API key. Someone needs to sort that.",
]
TRANSCRIBE_FAIL_PHRASES = [
    "Sorry, didn't quite catch that one.",
    "Hmm, couldn't make that out, try again?",
    "Do you understand what I'm saying? Because I did not understand what YOU were saying.",
    "That went in one ear and... well, there's only the one ear. Say it again?",
    "Nope, nothing. Absolute gibberish on my end. Probably my end. One more time?",
    "Was that words? Genuinely asking. Give it another go.",
]
CAMERA_FAIL_PHRASES = [
    "Tried to have a look, but the camera's not cooperating. Typical.",
    "Wanted to peek, but nothing came through from the camera. Sorry.",
    "Tried to look. Couldn't. It's not dark, it's just... broken. The camera's broken.",
    "My eye's not working. The camera eye. The only eye, really. Bit of a design flaw.",
    "No picture came through. Which is not ideal, for looking at things.",
]

# Given to Claude as an on-request tool — Claude decides whether a question
# actually needs vision (per user preference: no manual keyword gate). Only
# fires when Claude asks for it, so a plain spoken question costs exactly one
# API call same as before; a "look at this" question costs two.
VISION_TOOL = {
    "name": "take_photo",
    "description": (
        "Capture a photo from StackChan's camera to see what's physically in "
        "front of it right now. Use only when seeing would clearly help "
        "answer — e.g. the user asks you to look at something, describe "
        "what's on the desk, read something out, or similar. Don't use it "
        "for questions that don't need vision."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "What you want to know from the photo.",
            }
        },
        "required": ["question"],
    },
}

# Reuse the same "concentrating" face/pose as the Claude Code busy hook —
# squint + look down — so listening/thinking reads consistently across both
# voice and coding-work contexts.
THINKING_FACE = "sad"

# Pitch convention (confirmed live 2026-07-01 by taking photos at various
# values and checking what's actually in frame): LOW pitch is near-
# horizontal, pointed at the user/table; HIGH pitch tilts up toward
# vertical, pointed at the ceiling/sky. 8 is "looking at the user" — do
# NOT increase this to "look up", that points at the sky instead.
LOOK_AT_USER_PITCH = 8

# While this marker exists, stackchan-led-chase.py renders a rotating
# rainbow chase on the LED ring (takes priority over the amber busy chase).
# Written when transcription/Claude-call starts, cleared right before
# speaking the reply (success, failure, or error — always cleared).
VOICE_THINKING_MARKER = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", ".")), "stackchan-voice-thinking"
)


def _start_thinking_marker() -> None:
    try:
        with open(VOICE_THINKING_MARKER, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def _clear_thinking_marker() -> None:
    try:
        os.remove(VOICE_THINKING_MARKER)
    except OSError:
        pass


def _marker_active(path: str, stale_s: float) -> bool:
    """Same staleness-checking pattern as stackchan-vision-loop.py/led-chase.py."""
    try:
        with open(path) as f:
            written_at = float(f.read().strip())
    except (OSError, ValueError):
        return False
    if time.time() - written_at > stale_s:
        try:
            os.remove(path)
        except OSError:
            pass
        return False
    return True


# Written by stackchan-vision-loop.py right before it speaks "who are you?"
# to an unrecognized face (see sensor_reactor.py's _behavior_recognize).
# Give the person a couple of minutes to notice and tap-to-answer.
PENDING_ENROLLMENT_MARKER = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", ".")), "stackchan-pending-enrollment"
)
PENDING_ENROLLMENT_STALE_S = 120

ENROLL_CONFIRM_PHRASES = [
    "Nice to meet you, {name}! I'll remember you.",
    "{name}, got it. Pleasure — I think.",
    "Right, {name}. Filed away. Try not to make me regret it.",
    "{name}! Cracking name. Right up there with the great names. Remembered.",
    "Hello, {name}! I'd shake your hand, but... obvious reasons.",
    "{name}. Locked in. I never forget a face. Almost never. Rarely forget a face.",
]
ENROLL_FAIL_PHRASES = [
    "Hmm, didn't quite catch a name there, and I couldn't get a clean look "
    "at your face either. We'll try that again another time.",
    "Right, that didn't work — no name, no decent look at your face. "
    "Bit of a shambles all round. Another time.",
    "Couldn't catch the name, couldn't quite see you either. "
    "We'll call that a draw and try again later.",
]

_NAME_PATTERNS = [
    re.compile(
        r"\b(?:i'?m|i am|my name'?s|my name is|it'?s|call me|this is)\s+"
        r"([a-zA-Z][a-zA-Z'\-]*)",
        re.IGNORECASE,
    ),
]
# Guards the short-transcript fallback below against filler/garbled
# transcriptions being mistaken for a name (e.g. "uh what").
_NAME_FILLER_WORDS = {
    "uh", "um", "er", "hmm", "huh", "what", "hello", "hi", "hey",
    "yes", "no", "okay", "ok", "sorry", "sure", "yeah", "nope",
}


def _extract_name(transcript: str) -> str | None:
    """Best-effort name extraction from a tap-to-talk answer to "who are you?".

    Tries a few common introduction phrasings first ("I'm X", "my name is
    X"...); falls back to the raw transcript if it's short and looks like
    just a name (1-3 alphabetic words) — good enough for this UX, same
    tolerance-for-garble model as the rest of voice interaction here (if it
    misfires, the person can just try again).
    """
    for pat in _NAME_PATTERNS:
        m = pat.search(transcript)
        if m:
            return m.group(1).strip().capitalize()
    words = transcript.strip().split()
    if (
        1 <= len(words) <= 3
        and all(w.isalpha() for w in words)
        and not any(w.lower() in _NAME_FILLER_WORDS for w in words)
    ):
        return " ".join(w.capitalize() for w in words)
    return None


def _complete_enrollment(name: str) -> bool:
    """Shell out to stackchan-vision-loop.py --enroll rather than duplicating
    its cv2 face-embedding logic here (voice-bridge has no cv2 dependency
    otherwise, and this reuses the already-tested enrollment path exactly).
    """
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stackchan-vision-loop.py")
    try:
        result = subprocess.run(
            [sys.executable, script_path, "--enroll", name, "--samples", "3", "--interval", "1.5"],
            capture_output=True, text=True, timeout=60,
        )
        logger.info(
            "enrollment subprocess name=%r exit=%s stdout=%r",
            name, result.returncode, result.stdout[-500:],
        )
        return result.returncode == 0 and "Enrolled" in result.stdout
    except Exception:
        logger.exception("enrollment subprocess failed")
        return False


class MCPSession:
    def __init__(self, url, timeout=30):
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
            self.url, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=timeout or self.timeout)
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        body = resp.read()
        return json.loads(body) if body.strip() else None

    def initialize(self):
        self._post({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "stackchan-voice-bridge", "version": "1.0"},
            },
        })
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, name, arguments, timeout=None):
        call_id = self._next_id
        self._next_id += 1
        return self._post(
            {"jsonrpc": "2.0", "id": call_id, "method": "tools/call",
             "params": {"name": name, "arguments": arguments}},
            timeout=timeout,
        )


def _speak(text: str, face_before: str | None = None) -> None:
    """Speak text through the gateway, reverting to a relaxed look-at-user pose."""
    try:
        sess = MCPSession(GATEWAY_URL)
        sess.initialize()
        if face_before:
            sess.call_tool("set_avatar", {"face": face_before})
        sess.call_tool("say", {"text": text}, timeout=30)
        sess.call_tool("set_avatar", {"face": "idle"})
        sess.call_tool("move_head", {"yaw": 0, "pitch": LOOK_AT_USER_PITCH + 6})
    except Exception:
        logger.exception("speak failed")


def _set_thinking_pose() -> None:
    try:
        sess = MCPSession(GATEWAY_URL)
        sess.initialize()
        sess.call_tool("set_avatar", {"face": THINKING_FACE})
        sess.call_tool("move_head", {"yaw": -4, "pitch": 60})
    except Exception:
        logger.exception("set_thinking_pose failed")


_whisper_model = None
_whisper_lock = threading.Lock()


def _get_whisper_model():
    global _whisper_model
    with _whisper_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel
            logger.info("loading faster-whisper model=%s", WHISPER_MODEL_NAME)
            _whisper_model = WhisperModel(
                WHISPER_MODEL_NAME, device="cpu", compute_type="int8"
            )
        return _whisper_model


def _transcribe(ogg_path: str) -> str:
    model = _get_whisper_model()
    segments, _info = model.transcribe(ogg_path, language="en")
    return " ".join(seg.text.strip() for seg in segments).strip()


def _call_claude_api(messages: list, api_key: str, tools: list | None = None) -> dict:
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 300,
        "system": SYSTEM_PROMPT,
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=json.dumps(payload).encode(),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _extract_text(content_blocks: list) -> str:
    return " ".join(
        b.get("text", "") for b in content_blocks if b.get("type") == "text"
    ).strip()


PHOTO_CUE_PHRASES = [
    "Right, let's have a look, hang on...",
    "Ooh, let me see — hold that steady...",
    "Right, activating my one remaining eyeball...",
    "Hold still! Not that you moved. Just — hold still.",
    "Aaand... looking. Looking now. This is me, looking.",
    "One second, focusing the old eyeball...",
]


def _take_photo_via_mcp(question: str) -> tuple[str, str] | None:
    """Call the gateway's take_photo tool; return (base64_jpeg, media_type) or None.

    The camera is on the same physical unit as the head/screen, so whatever
    pose was showing during transcription (the down-left "thinking" squint)
    is what the camera would otherwise still be pointed at. Snap to a
    straight-ahead/eye-level pose and speak a short cue first — gives an
    audible "hold it up now" signal and a beat of time to react. (NOT an LED
    flash: stackchan-led-chase.py owns the LED ring for the whole
    voice-thinking window, rendering a rainbow chase — writing LEDs
    directly here would race it.)

    Tilting down for a table/desk shot was tried and dropped 2026-07-01 —
    at this device's current physical position/mounting, even the servo's
    minimum pitch only reached chest height, never an actual table surface,
    so "hold it up in front of the camera" is the only reliable framing
    right now. Revisit if the device's physical placement changes.

    Pitch convention (see LOOK_AT_USER_PITCH above): LOW pitch is
    near-horizontal, pointed at the user; HIGH pitch tilts up toward the
    ceiling/sky.

    Also explicitly re-enables servo torque before moving: the firmware's
    auto torque-release power-save feature (Issue #152) can silently leave
    the head "holding via friction" — move_head then reports success and
    the new angle, but nothing physically moves. Confirmed live 2026-07-01
    (repeated photos showed the exact same framing across very different
    reported pitch values) — cheap enough to just always re-assert.
    """
    try:
        sess = MCPSession(GATEWAY_URL)
        sess.initialize()
        sess.call_tool("set_avatar", {"face": "idle"})
        sess.call_tool("set_servo_torque", {"yaw_enabled": True, "pitch_enabled": True})
        sess.call_tool("move_head", {"yaw": 0, "pitch": LOOK_AT_USER_PITCH})
        sess.call_tool("say", {"text": _pick("photo-cue", PHOTO_CUE_PHRASES)}, timeout=15)
        result = sess.call_tool("take_photo", {"question": question}, timeout=20)
        content = ((result or {}).get("result") or {}).get("content", [])
        if not content:
            logger.warning("take_photo returned no content: %r", result)
            return None
        info = json.loads(content[0].get("text", "") or "{}")
        image_path = info.get("image_path")
        if not image_path or not os.path.exists(image_path):
            logger.warning("take_photo result missing image_path: %r", info)
            return None
        with open(image_path, "rb") as f:
            data = f.read()
        return base64.b64encode(data).decode("ascii"), "image/jpeg"
    except Exception:
        logger.exception("take_photo via MCP failed")
        return None


def _ask_claude(transcript: str) -> str:
    api_key = os.environ.get(ANTHROPIC_API_KEY_ENV, "").strip()
    if not api_key:
        return _pick("no-key", NO_KEY_PHRASES)

    messages = [{"role": "user", "content": transcript}]
    try:
        data = _call_claude_api(messages, api_key, tools=[VISION_TOOL])
    except urllib.error.HTTPError as exc:
        body_snippet = exc.read().decode(errors="replace")[:300]
        logger.warning("Claude API HTTP error %s: %s", exc.code, body_snippet)
        return "Ah, the Claude API didn't like that — something's misconfigured, sorry."
    except Exception:
        logger.exception("Claude API call failed")
        return "Couldn't reach Claude just now. Network's playing up, probably."

    content = data.get("content", [])

    # Claude decided it needs to look — round-trip through the camera once,
    # then let it finish answering with the photo in hand. One photo per
    # turn is enough for a spoken exchange; no further tool-use loop.
    if data.get("stop_reason") == "tool_use":
        tool_use = next((b for b in content if b.get("type") == "tool_use"), None)
        if tool_use is not None:
            question = (tool_use.get("input") or {}).get("question") or "What do you see?"
            logger.info("Claude requested take_photo(question=%r)", question)
            photo = _take_photo_via_mcp(question)
            if photo is None:
                tool_result_content = [
                    {"type": "text", "text": "Camera unavailable right now."}
                ]
            else:
                b64_data, media_type = photo
                tool_result_content = [{
                    "type": "image",
                    "source": {
                        "type": "base64", "media_type": media_type, "data": b64_data,
                    },
                }]
            messages.append({"role": "assistant", "content": content})
            messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": tool_use.get("id"),
                    "content": tool_result_content,
                }],
            })
            try:
                data = _call_claude_api(messages, api_key)
                content = data.get("content", [])
            except urllib.error.HTTPError as exc:
                body_snippet = exc.read().decode(errors="replace")[:300]
                logger.warning("Claude API HTTP error %s: %s", exc.code, body_snippet)
                return _pick("camera-fail", CAMERA_FAIL_PHRASES)
            except Exception:
                logger.exception("Claude API follow-up call failed")
                return _pick("camera-fail", CAMERA_FAIL_PHRASES)

    text = _extract_text(content)
    return text or "Er, got an empty answer back. Not sure what happened there."


def _handle_capture(ogg_bytes: bytes, session_id: str) -> None:
    """Runs in a background thread so the HTTP response isn't held up.

    NOTE: a tap-free "listen for a follow-up if the reply ends in a
    question" loop was built and reverted here 2026-07-01 — the gateway's
    `listen` MCP tool hung indefinitely against the currently-flashed
    firmware (source supports the wire message, flashed binary predates
    it). See firmware/TODO.md "Tap-free follow-up listening" — re-add once
    that's reflashed rather than rebuilding from scratch.
    """
    _set_thinking_pose()
    _start_thinking_marker()
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as f:
            f.write(ogg_bytes)
            tmp_path = f.name

        t0 = time.time()
        transcript = _transcribe(tmp_path)
        logger.info(
            "session=%s transcribed in %.1fs: %r",
            session_id, time.time() - t0, transcript,
        )

        if not transcript or len(transcript) < 2:
            _clear_thinking_marker()
            _speak(_pick("transcribe-fail", TRANSCRIBE_FAIL_PHRASES))
            return

        # An unrecognized face was just asked "who are you?" (see
        # sensor_reactor.py's _behavior_recognize) — this tap-to-talk
        # answer is almost certainly a name introduction, not a normal
        # question. Handle it as enrollment instead of the usual Q&A path.
        if _marker_active(PENDING_ENROLLMENT_MARKER, PENDING_ENROLLMENT_STALE_S):
            try:
                os.remove(PENDING_ENROLLMENT_MARKER)
            except OSError:
                pass
            name = _extract_name(transcript)
            logger.info("session=%s enrollment attempt, transcript=%r name=%r", session_id, transcript, name)
            enrolled = _complete_enrollment(name) if name else False
            _clear_thinking_marker()
            if name and enrolled:
                _speak(_pick("enroll-confirm", ENROLL_CONFIRM_PHRASES).format(name=name))
            else:
                _speak(_pick("enroll-fail", ENROLL_FAIL_PHRASES))
            return

        t1 = time.time()
        reply = _ask_claude(transcript)
        logger.info(
            "session=%s claude reply in %.1fs: %r",
            session_id, time.time() - t1, reply,
        )
        _clear_thinking_marker()
        _speak(reply)
    except Exception:
        logger.exception("capture handling failed (session=%s)", session_id)
        _clear_thinking_marker()
        try:
            _speak("Sorry, something went wrong on my end there.")
        except Exception:
            pass
    finally:
        _clear_thinking_marker()  # idempotent safety net — always cleared
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.address_string(), fmt % args)

    def do_POST(self):
        if self.path.rstrip("/") != "/voice":
            self.send_response(404)
            self.end_headers()
            return

        token = os.environ.get("STACKCHAN_AUDIO_HOOK_TOKEN") or os.environ.get(
            "STACKCHAN_TOKEN"
        )
        if token:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {token}":
                self.send_response(401)
                self.end_headers()
                return

        length = int(self.headers.get("Content-Length", 0))
        ogg_bytes = self.rfile.read(length) if length else b""
        session_id = self.headers.get("X-StackChan-Session", "")

        if not ogg_bytes:
            self.send_response(400)
            self.end_headers()
            return

        # Accept immediately — transcription + Claude + TTS happen async so
        # the device-side POST (which has its own timeout) isn't held open.
        self.send_response(200)
        self.end_headers()

        threading.Thread(
            target=_handle_capture, args=(ogg_bytes, session_id), daemon=True
        ).start()


def main():
    logger.info(
        "starting voice bridge on %s:%d (whisper=%s, claude_model=%s)",
        LISTEN_HOST, LISTEN_PORT, WHISPER_MODEL_NAME, ANTHROPIC_MODEL,
    )
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
