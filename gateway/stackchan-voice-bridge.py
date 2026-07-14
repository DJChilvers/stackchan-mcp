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

Replies stream by default: the Claude response is read as SSE and spoken
sentence-by-sentence, so the first words land while the rest is still
generating (STACKCHAN_VOICE_STREAMING=0 restores the original batch
behaviour). Each turn also injects one live-telemetry line (battery +
rail position, 30s cache) into the system prompt so questions like
"how's your battery?" get true answers in character.

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

# Sentence-streaming replies (2026-07-13): stream the Claude response (SSE
# over the same raw-urllib path — no SDK dependency) and speak it sentence
# by sentence instead of waiting for the full reply, so the first words
# land a few seconds sooner. STACKCHAN_VOICE_STREAMING=0 is the kill
# switch — it restores the original batch behaviour exactly. Default ON.
STREAMING_ENABLED = os.environ.get(
    "STACKCHAN_VOICE_STREAMING", "1"
).strip().lower() not in ("0", "false", "no", "off")

# Speak a chunk at each sentence boundary (. ! ?) or once the pending text
# exceeds this many characters (split at a word gap), whichever comes first.
SENTENCE_MAX_CHARS = 140

# Colour-identification intent (2026-07-13): assistive feature — the user is
# colour-blind, so "what colour is this?" must ALWAYS photograph and answer
# from the photo, deterministically, rather than leaving camera use to the
# LLM's judgement the way normal chat does (see VISION_TOOL). Kill switch:
# STACKCHAN_COLOUR_CHECK=0. Default ON.
COLOUR_CHECK_ENABLED = os.environ.get(
    "STACKCHAN_COLOUR_CHECK", "1"
).strip().lower() not in ("0", "false", "no", "off")

# Dream-loop failure capture (2026-07-13): SYSTEM_PROMPT asks the LLM to
# append a literal [CANT] token to any reply that amounts to "can't do
# that / don't know / don't have that capability or information". The
# token is stripped before speaking (batch, streaming and colour paths
# all strip it) and the failed turn is appended to the Dream Loop
# wishlist, which the nightly DREAM_LOOP.md run consumes to teach him
# the missing trick overnight. Kill switch: STACKCHAN_DREAM_CAPTURE=0.
# Default ON. Capture is fire-and-forget — it can never break a turn.
DREAM_CAPTURE_ENABLED = os.environ.get(
    "STACKCHAN_DREAM_CAPTURE", "1"
).strip().lower() not in ("0", "false", "no", "off")
WISHLIST_PATH = r"C:\Users\domin\Documents\StackChan\dream\wishlist.jsonl"

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
    "something too aggressive)\n\n"
    "Begin EVERY reply with one emotion tag in square brackets that matches "
    "your tone, from EXACTLY this set: [happy] [sad] [surprised] [thinking] "
    "[embarrassed] [idle]. Then a space, then the spoken words. The tag drives "
    "my face and is stripped before speaking — never mention it. Rough guide: "
    "[happy] pleased or amused, [surprised] alarmed or excited, [thinking] "
    "unsure or working it out, [sad] apologetic or disappointed, [embarrassed] "
    "self-deprecating or awkward (very you), [idle] neutral. "
    "Example: [happy] Oh, brilliant — that actually worked.\n\n"
    "One more marker: if your reply amounts to declining — 'I can't do "
    "that', 'I don't know', 'I don't have that capability or information' "
    "— ALSO append the literal token [CANT] at the very END of the reply, "
    "after all the spoken words. Never mention or explain it; it is "
    "stripped before speaking. "
    "Example: [sad] Honestly, no idea — I can't check that from in here. [CANT]"
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

# Look-AT-the-user "thinking" face while working out a reply (changed
# 2026-07-14). It used to be "sad" + a look-DOWN pose reused from the Claude
# Code busy hook, but in a spoken conversation dropping his gaze reads as him
# disengaging — the user wanted him to hold eye contact with a thinking face
# instead. "thinking" is one of the six valid set_avatar faces.
THINKING_FACE = "thinking"

# Pitch convention (confirmed live 2026-07-01 by taking photos at various
# values and checking what's actually in frame): LOW pitch is near-
# horizontal, pointed at the user/table; HIGH pitch tilts up toward
# vertical, pointed at the ceiling/sky. 8 is "looking at the user" — do
# NOT increase this to "look up", that points at the sky instead.
LOOK_AT_USER_PITCH = 8

# Where to FACE when speaking: the radar tracker (look_at.py / calibrate.py)
# publishes the person's last-known head-yaw here. Fresh hint -> face THEM;
# stale/missing -> +25 = perpendicular to the rail (camera-verified 2026-07-14;
# the old hardcoded yaw 0 stared 25deg off the room thanks to the carriage
# rotator, and every spoken reply broke whatever pose the tracker held).
PERSON_YAW_HINT = os.path.join(tempfile.gettempdir(), "stackchan-person-yaw.json")


def _user_yaw() -> int:
    try:
        with open(PERSON_YAW_HINT, encoding="utf-8") as f:
            d = json.load(f)
        if time.time() - float(d.get("ts", 0)) <= 120.0:
            return max(-90, min(90, int(d.get("yaw", 25))))
    except Exception:
        pass
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "companion_settings.json"), encoding="utf-8") as f:
            return max(-90, min(90, int(json.load(f).get("perpendicular_yaw", 25))))
    except Exception:
        return 25

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
    # Actual bad jokes, as requested — deliberately groan-worthy, not clever.
    "{name}! Great name. Do you know what's not great? My last host body. Anyway — remembered.",
    "Nice to meet you, {name}. I'd tell you a chemistry joke but I know I wouldn't get a reaction. Saved you, though.",
    "{name}, right. Here's one for you: why don't robots panic? Too many built-in fail-safes. I have none. Remembered you anyway.",
]
ENROLL_FAIL_PHRASES = [
    "Hmm, didn't quite catch a name there, and I couldn't get a clean look "
    "at your face either. We'll try that again another time.",
    "Right, that didn't work — no name, no decent look at your face. "
    "Bit of a shambles all round. Another time.",
    "Couldn't catch the name, couldn't quite see you either. "
    "We'll call that a draw and try again later.",
]
# Fired instead of ENROLL_CONFIRM_PHRASES when stackchan-vision-loop.py's
# --enroll had to auto-rename because the spoken name was already taken by
# someone whose face clearly doesn't match (see _resolve_enroll_name there
# — only splits into a new identity when both the local score and Claude
# agree it's a different person, specifically to avoid this firing on
# every bad-lighting re-enrollment of someone already known).
ENROLL_RENAMED_PHRASES = [
    "Funny thing — already got a {said_name}, and you're definitely not them. Filed you as {saved_name} instead.",
    "So, turns out {said_name}'s taken. Not you, apparently — different face entirely. You're {saved_name} now.",
    "Slight mix-up: {said_name} already belongs to someone else round here. Saved you as {saved_name}.",
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


def _complete_enrollment(name: str) -> tuple[bool, str | None]:
    """Shell out to stackchan-vision-loop.py --enroll rather than duplicating
    its cv2 face-embedding logic here (voice-bridge has no cv2 dependency
    otherwise, and this reuses the already-tested enrollment path exactly).

    Returns (success, resolved_name) — resolved_name can differ from the
    name passed in if --enroll auto-renamed due to a name collision with a
    clearly different existing person (see _resolve_enroll_name there);
    the caller needs the actual saved name to speak an accurate
    confirmation rather than repeating back a name that wasn't used.
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
        success = result.returncode == 0 and "Enrolled" in result.stdout
        if not success:
            return False, None
        m = re.search(r"Enrolled '([^']+)'", result.stdout)
        return True, (m.group(1) if m else name)
    except Exception:
        logger.exception("enrollment subprocess failed")
        return False, None


# Written by stackchan-vision-loop.py when the arbiter judges a fresh
# capture "definite" match + "good" quality (see its module docstring for
# the arbiter design) — JSON {"name", "frame_path", "ts"}, NOT a bare
# timestamp like the other markers here, so it gets its own staleness
# check rather than reusing _marker_active.
PENDING_LEARN_CONFIRM_MARKER = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", ".")), "stackchan-pending-learn-confirm"
)
PENDING_LEARN_CONFIRM_STALE_S = 120

LEARN_CONFIRM_YES_PHRASES = [
    "Brilliant, learned it. Getting better at this every day.",
    "Done! Filed away. My facial recognition improves, slowly but surely.",
    "Got it, remembered. You're basically unforgettable now.",
]
LEARN_CONFIRM_DECLINED_PHRASES = [
    "Fair enough, I'll leave it. No harm done.",
    "No worries, skipping that one.",
    "Understood — won't remember that particular look.",
]
LEARN_CONFIRM_FAIL_PHRASES = [
    "Hmm, that didn't quite work — couldn't save it. We'll get it next time.",
]

_YES_WORDS = {"yes", "yeah", "yep", "yup", "sure", "please", "okay", "ok", "go", "ahead", "do", "definitely"}
_NO_WORDS = {"no", "nope", "nah", "dont", "don't", "skip", "not", "never"}


def _parse_yes_no(transcript: str) -> bool | None:
    """None (not True/False) on anything ambiguous — the caller must treat
    that as "decline", never as "confirm" (fail-safe: never learn on an
    unclear answer, same guardrail as the arbiter's malformed-output case).
    """
    words = set(re.findall(r"[a-z']+", transcript.lower()))
    if words & _NO_WORDS:
        return False
    if words & _YES_WORDS:
        return True
    return None


def _read_pending_learn_confirm() -> dict | None:
    """Read + delete the marker. Returns None if missing/stale/corrupt, in
    which case any referenced frame file is best-effort cleaned up too —
    it's single-use and would otherwise sit there orphaned.
    """
    info = None
    try:
        with open(PENDING_LEARN_CONFIRM_MARKER, encoding="utf-8") as f:
            raw = json.load(f)
        if time.time() - float(raw.get("ts", 0)) <= PENDING_LEARN_CONFIRM_STALE_S:
            info = raw
        else:
            frame_path = raw.get("frame_path")
            if frame_path:
                try:
                    os.remove(frame_path)
                except OSError:
                    pass
    except Exception:
        pass
    try:
        os.remove(PENDING_LEARN_CONFIRM_MARKER)
    except OSError:
        pass
    return info


def _confirm_learn_sample(name: str, frame_path: str) -> bool:
    """Shell out to stackchan-vision-loop.py --confirm-learn — same reuse
    reasoning as _complete_enrollment above."""
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stackchan-vision-loop.py")
    try:
        result = subprocess.run(
            [sys.executable, script_path, "--confirm-learn", name, frame_path],
            capture_output=True, text=True, timeout=30,
        )
        logger.info(
            "confirm-learn subprocess name=%r exit=%s stdout=%r",
            name, result.returncode, result.stdout[-500:],
        )
        return result.returncode == 0 and "Learned" in result.stdout
    except Exception:
        logger.exception("confirm-learn subprocess failed")
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


# Map the emotion tag Wheatley prefixes onto each reply (see SYSTEM_PROMPT)
# to an actual avatar face. Synonyms are tolerated in case he improvises a tag
# outside the requested set; the six canonical faces are idle/happy/thinking/
# sad/surprised/embarrassed (set_avatar's valid values — "neutral" maps to idle).
_EMOTION_TO_FACE = {
    "happy": "happy", "excited": "happy", "pleased": "happy", "amused": "happy",
    "sad": "sad", "disappointed": "sad", "sorry": "sad",
    "surprised": "surprised", "alarmed": "surprised", "shocked": "surprised", "angry": "surprised",
    "thinking": "thinking", "confused": "thinking", "unsure": "thinking", "curious": "thinking",
    "embarrassed": "embarrassed", "awkward": "embarrassed", "worried": "embarrassed", "nervous": "embarrassed",
    "idle": "idle", "neutral": "idle", "calm": "idle",
}
_LEADING_TAG_RE = re.compile(r"^\s*[\[(]\s*([a-zA-Z]+)\s*[\])]\s*[:\-—]?\s*")


def _split_emotion(reply: str):
    """(face, spoken_text): pull a leading [emotion] tag off an LLM reply and
    map it to an avatar face. A leading bracketed token is ALWAYS stripped so
    TTS never reads it aloud, even if the word isn't a known emotion; an
    unknown or absent tag yields face None. Never returns empty speech."""
    m = _LEADING_TAG_RE.match(reply or "")
    if not m:
        return None, reply
    face = _EMOTION_TO_FACE.get(m.group(1).lower())
    spoken = reply[m.end():].lstrip()
    return face, (spoken or reply)


# The [CANT] failure token (see the SYSTEM_PROMPT addition + the
# DREAM_CAPTURE_ENABLED block above). Matched case-insensitively anywhere
# in the text — the prompt asks for end-of-reply, but the model
# occasionally misplaces markers — so TTS can never read it aloud.
_CANT_TOKEN_RE = re.compile(r"\s*\[CANT\]", re.IGNORECASE)


def _strip_cant(text: str) -> tuple[str, bool]:
    """(clean_text, found): remove every [CANT] token from text.

    Collapses the leftover whitespace so a stripped mid-text token can't
    leave a double space for TTS to trip on. clean_text may come back
    empty if the text was ONLY the token — callers must not speak that.
    """
    if not text or "[" not in text:
        return text, False
    clean, n = _CANT_TOKEN_RE.subn(" ", text)
    if not n:
        return text, False
    return " ".join(clean.split()), True


def _capture_cant(transcript: str, reply_tagless: str, session_id: str = "") -> None:
    """Append a [CANT] turn to the Dream Loop wishlist. Never raises.

    Line format matches dream/wishlist_add.py and DREAM_LOOP.md's contract:
    one JSON object per line, {ts, source, transcript, reply, note, photo},
    ts = epoch float. The append is a single write() of one complete line
    in append mode, so concurrent turns can't interleave mid-line. Light
    dedupe: skip when the LAST line already has this transcript
    (case-folded) — re-asking the same thing shouldn't stack duplicates.
    """
    if not DREAM_CAPTURE_ENABLED:
        return
    try:
        try:
            last_line = None
            with open(WISHLIST_PATH, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        last_line = line
            if last_line:
                prev = json.loads(last_line)
                prev_transcript = str(prev.get("transcript") or "")
                if prev_transcript.strip().casefold() == transcript.strip().casefold():
                    logger.info(
                        "session=%s dream capture deduped (same transcript as last): %r",
                        session_id, transcript,
                    )
                    return
        except Exception:
            pass  # missing file / unreadable last line never blocks the append
        os.makedirs(os.path.dirname(WISHLIST_PATH), exist_ok=True)
        entry = {
            "ts": time.time(),
            "source": "voice-cant",
            "transcript": transcript,
            "reply": reply_tagless,
            "note": None,
            "photo": None,
        }
        with open(WISHLIST_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        logger.info("session=%s dream capture: wishlisted %r", session_id, transcript)
    except Exception:
        logger.exception("dream capture failed (turn unaffected)")


def _speak(text: str, face_before: str | None = None) -> None:
    """Speak text through the gateway, reverting to a relaxed look-at-user pose."""
    try:
        sess = MCPSession(GATEWAY_URL)
        sess.initialize()
        if face_before:
            sess.call_tool("set_avatar", {"face": face_before})
        sess.call_tool("say", {"text": text}, timeout=30)
        sess.call_tool("set_avatar", {"face": "idle"})
        sess.call_tool("move_head", {"yaw": _user_yaw(), "pitch": LOOK_AT_USER_PITCH + 6})
    except Exception:
        logger.exception("speak failed")


# Thinking-sound filler (2026-07-14): a short spoken "hmm" the moment the
# user finishes asking, before the LLM answer lands. The gap between their
# question and his first word was reading as "is he even listening?" — the
# LED thinking-chase is invisible on the bright bench, so an audible beat
# closes that gap and makes the exchange feel like a real back-and-forth.
# Only the general chat path uses it; the deterministic intents
# (colour/tray/dance/inventory) already speak their own ack. `say` blocks
# until the device finishes playing, so the filler and the answer never
# overlap. Kill switch STACKCHAN_VOICE_THINKING_SOUND=0.
THINKING_SOUND_ENABLED = os.environ.get(
    "STACKCHAN_VOICE_THINKING_SOUND", "1"
).strip().lower() not in ("0", "false", "no", "off")

THINKING_SOUND_PHRASES = [
    "Hmm.",
    "Hmmm, right.",
    "Ooh, let me think.",
    "Right, thinking...",
    "Hmm, good question.",
    "Let me have a think.",
    "Right, hang on...",
    "Hmm, one sec.",
]


def _speak_thinking_sound() -> None:
    """Speak a short thinking filler while the answer generates.

    Keeps the thinking face/pose already set by _set_thinking_pose (a bare
    say() doesn't touch the avatar) and leaves the LED thinking-chase running
    (the marker is still up). Never raises — a filler is never worth breaking
    a turn over.
    """
    if not THINKING_SOUND_ENABLED:
        return
    try:
        sess = MCPSession(GATEWAY_URL)
        sess.initialize()
        sess.call_tool(
            "say",
            {"text": _pick("thinking-sound", THINKING_SOUND_PHRASES)},
            timeout=15,
        )
    except Exception:
        logger.exception("thinking sound failed (turn unaffected)")


def _set_thinking_pose() -> None:
    """Hold eye contact with a thinking face while formulating the answer.

    Changed 2026-07-14: was a look-DOWN concentrating pose (pitch 60, face
    "sad"); the user asked him to keep looking at them with a thinking face
    instead. A small yaw cock keeps it characterful — head tilted, visibly
    *considering* you — rather than a flat dead-ahead stare. Pitch matches
    his at-the-user talking pose (see LOOK_AT_USER_PITCH).
    """
    try:
        sess = MCPSession(GATEWAY_URL)
        sess.initialize()
        sess.call_tool("set_avatar", {"face": THINKING_FACE})
        sess.call_tool("move_head", {"yaw": -6, "pitch": LOOK_AT_USER_PITCH + 6})
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


# Bias transcription toward Wheatley's actual command vocabulary + his name.
# For a small fixed command set, an initial_prompt + beam search + VAD trimming
# hugely improve short-utterance accuracy on base.en WITHOUT a bigger model
# (which the xet CDN currently blocks downloading) — "do a dance" was landing as
# "look down"/"tweet me". Env-overridable.
WHISPER_PROMPT = os.environ.get(
    "STACKCHAN_VOICE_WHISPER_PROMPT",
    "A person is talking to Wheatley, a helpful robot assistant on a desk rail. "
    "Likely phrases: Hey Wheatley; do a dance; come here; go home; go to your "
    "dock; what time is it; how is your battery; what colour is this; where are "
    "my tools; where is the multimeter; look at me; turn around; stop; hello.",
)


def _transcribe(ogg_path: str) -> str:
    model = _get_whisper_model()
    segments, _info = model.transcribe(
        ogg_path,
        language="en",
        beam_size=5,                       # beam search >> greedy on short commands
        temperature=0.0,                   # deterministic
        initial_prompt=WHISPER_PROMPT,     # bias toward the real command vocabulary
        condition_on_previous_text=False,  # each turn independent (no carry-over hallucination)
        vad_filter=True,                   # trim non-speech so whisper focuses on the words
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


# ─── live self-status for the system prompt ─────────────────────────────────
# One compact telemetry line (battery + rail) injected into the system
# prompt each turn so "how's your battery?" / "where are you on the rail?"
# get TRUE answers in character. Cached for 30s; every fetch is bounded by
# short timeouts and degrades to omitting itself on any failure.

STATUS_CACHE_TTL_S = 30.0
STATUS_FETCH_TIMEOUT_S = 2.0
# The rail bridge's `linked` flag means "ever heard a status this boot" —
# status_age_ms is the real freshness signal (same gate as stackchan-idle.py).
RAIL_STATUS_MAX_AGE_MS = 10_000
# The home end of the rail IS the charge dock (see the charge-dock build
# notes): homed + parked within this many mm of home = "on the dock".
DOCKED_POS_MM = 3.0

_status_cache_lock = threading.Lock()
_status_cache: dict = {"ts": 0.0, "line": None}


def _tool_result_json(resp) -> dict:
    """Parse a gateway tools/call JSON-RPC response into the tool's own JSON
    payload — same extraction shape as stackchan-idle.py / rail_dance.py."""
    try:
        content = ((resp or {}).get("result") or {}).get("content") or []
        if content:
            return json.loads(content[0].get("text", "") or "{}") or {}
    except Exception:
        pass
    return {}


def _fetch_live_status_line() -> str | None:
    """Battery + rail snapshot as one system-prompt line, or None.

    Battery and rail are fetched independently so one failing doesn't drop
    the other; anything missing is simply omitted from the line.
    """
    parts: list[str] = []
    # Current local time from the gateway host's clock. The device itself has
    # no RTC/NTP time (firmware doesn't sync it), so this is how "what time is
    # it?" gets a TRUE answer. Always available, independent of the device.
    try:
        parts.append("the current time is "
                     + time.strftime("%A %H:%M", time.localtime()))
    except Exception:
        pass
    try:
        sess = MCPSession(GATEWAY_URL, timeout=STATUS_FETCH_TIMEOUT_S)
        sess.initialize()
    except Exception:
        logger.warning("live status: gateway unreachable — time-only status line")
        return ("Live status (your real telemetry right now): "
                + " | ".join(parts) + ".") if parts else None

    try:
        info = _tool_result_json(sess.call_tool("get_device_info", {}))
        battery = info.get("battery") or {}
        level = battery.get("level")
        charging = battery.get("charging")
        if isinstance(level, (int, float)):
            seg = f"battery {int(level)}%"
            if charging is True:
                seg += ", charging"
            elif charging is False:
                seg += ", on battery power"
            parts.append(seg)
    except Exception:
        logger.info("live status: get_device_info failed", exc_info=True)

    try:
        st = _tool_result_json(sess.call_tool("self.rail.status", {}))
        age = st.get("status_age_ms")
        if st.get("linked") and isinstance(age, (int, float)) \
                and age <= RAIL_STATUS_MAX_AGE_MS:
            pos = st.get("pos_mm")
            homed = bool(st.get("homed"))
            seg = "rail "
            if homed and isinstance(pos, (int, float)):
                # State the rail's usable travel too, so he doesn't invent a
                # rail length from the position number alone (2026-07-14: he
                # told the user his rail "is 185mm").
                seg += f"{pos:.0f}mm from home along your 896mm rail, homed"
            else:
                # Un-homed pos_mm is raw encoder garbage (hand-sliding the
                # carriage desyncs it) — never quote a number he'd repeat as
                # fact. He CAN still move: homing is how the reading resets.
                seg += ("not homed — position reading unreliable until you "
                        "re-home; you can still move along your 896mm rail")
            if (
                homed
                and isinstance(pos, (int, float))
                and abs(pos) <= DOCKED_POS_MM
                and st.get("moving") is False
            ):
                seg += ", parked on the charge dock"
            parts.append(seg)
    except Exception:
        logger.info("live status: rail status failed", exc_info=True)

    if not parts:
        return None
    return (
        "Live status (your real telemetry right now): "
        + " | ".join(parts)
        + ". Use this to answer truthfully about the time, your battery, "
        "charging, or rail position."
    )


def _cached_status_line() -> str | None:
    """30s-cached wrapper around _fetch_live_status_line. Also the target of
    the prefetch thread in _handle_capture — warming the cache while
    transcription runs means the Claude call almost never waits on it."""
    now = time.time()
    with _status_cache_lock:
        if now - _status_cache["ts"] <= STATUS_CACHE_TTL_S:
            return _status_cache["line"]
    line = _fetch_live_status_line()
    with _status_cache_lock:
        _status_cache["ts"] = time.time()
        _status_cache["line"] = line
    return line


def _system_prompt_with_status() -> str:
    line = _cached_status_line()
    return SYSTEM_PROMPT if not line else f"{SYSTEM_PROMPT}\n\n{line}"


def _claude_request(
    messages: list,
    api_key: str,
    tools: list | None = None,
    system: str | None = None,
    stream: bool = False,
) -> urllib.request.Request:
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 300,
        "system": system or SYSTEM_PROMPT,
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools
    if stream:
        payload["stream"] = True
    return urllib.request.Request(
        ANTHROPIC_URL,
        data=json.dumps(payload).encode(),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )


def _call_claude_api(
    messages: list,
    api_key: str,
    tools: list | None = None,
    system: str | None = None,
) -> dict:
    req = _claude_request(messages, api_key, tools=tools, system=system)
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


def _take_photo_via_mcp(
    question: str,
    cue_phrases: list[str] | None = None,
    cue_key: str = "photo-cue",
    pose: tuple[int, int] | None = None,
) -> tuple[str, str] | None:
    """Call the gateway's take_photo tool; return (base64_jpeg, media_type) or None.

    cue_phrases/cue_key override the spoken "hold it up" cue — the colour
    intent passes its own ack lines so the promise matches the question.

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
        # pose override (2026-07-14): the tray intent aims the camera DOWN at
        # the desk (he's INVERTED on the rail now — the old "can't reach a
        # table surface" note below predates the rail mount). Default
        # unchanged: straight at the user.
        p_yaw, p_pitch = pose if pose else (0, LOOK_AT_USER_PITCH)
        sess.call_tool("move_head", {"yaw": p_yaw, "pitch": p_pitch})
        sess.call_tool(
            "say",
            {"text": _pick(cue_key, cue_phrases or PHOTO_CUE_PHRASES)},
            timeout=15,
        )
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

    system = _system_prompt_with_status()
    messages = [{"role": "user", "content": transcript}]
    try:
        data = _call_claude_api(messages, api_key, tools=[VISION_TOOL], system=system)
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
                data = _call_claude_api(messages, api_key, system=system)
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


# ─── colour-identification intent ────────────────────────────────────────────
# Assistive feature: the user is colour-blind. "What colour is this Sharpie?"
# must be answered deterministically — ALWAYS photograph, ALWAYS answer from
# the photo — unlike normal chat, where Claude may or may not decide to use
# the take_photo tool. Same hook shape as the dance/inventory intents in
# _handle_capture; kill switch STACKCHAN_COLOUR_CHECK=0 (COLOUR_CHECK_ENABLED
# above, next to the streaming switch).

COLOUR_ACK_PHRASES = [
    "Right — hold it up, let me have a look...",
    "Ooh, colour check. Hold it up to the old eyeball, hang on...",
    "Right, hold it up nice and steady — having a look now...",
    "One sec — hold it where I can see it, doing a colour check...",
]
# Photo failure reuses CAMERA_FAIL_PHRASES; these cover "photo fine, vision
# call fell over".
COLOUR_VISION_FAIL_PHRASES = [
    "Got the photo, then my brain fell over working out the colour. Give it another go?",
    "Had a look, honestly did, but the thinking end's not cooperating. Ask me once more?",
    "Photo's fine, analysis... less fine. Sorry. Try me again in a tick.",
]

COLOUR_VISION_PROMPT = (
    "The user is colour-blind and needs colours identified reliably. Look at "
    "the most prominent foreground/handheld object (likely held toward the "
    "camera). Name its colour(s) plainly and specifically ('dark green', "
    "'red-orange'). For pens/markers: the CAP and barrel-end colour indicates "
    "the ink colour — state that explicitly. Reply as one short spoken "
    "sentence in Wheatley's voice (British, chatty), no markdown."
)

_COLOUR_OBJECT_WORDS = r"(?:sharpie|pen|marker|wire|led|cable|tag)"
_COLOUR_NAME_WORDS = (
    r"(?:red|green|blue|yellow|orange|purple|pink|brown|black|grey|gray|white)"
)
# Generous on colour-question phrasings, conservative otherwise: every
# pattern requires the word colo(u)r itself, except the last, which instead
# requires an explicit colour name in the "is this the red one" form.
_COLOUR_INTENT_RES = [
    # "what colour is this/that/it", "what colour's that"
    re.compile(r"\bwhat\s+colou?r(?:'s|\s+is)\s+(?:this|that|it)\b", re.IGNORECASE),
    # "what's the colour of this ...", "what is the colour ..."
    re.compile(r"\bwhat(?:'s|\s+is)\s+the\s+colou?r\b", re.IGNORECASE),
    # "which colour ..." (any continuation)
    re.compile(r"\bwhich\s+colou?r\b", re.IGNORECASE),
    # "what colour sharpie is this", "what colour is this pen / the wire ..."
    re.compile(rf"\bwhat\s+colou?r\b.*\b{_COLOUR_OBJECT_WORDS}s?\b", re.IGNORECASE),
    # "is this the red one", "is that a green one"
    re.compile(
        rf"\bis\s+(?:this|that)\s+(?:the\s+|a\s+)?{_COLOUR_NAME_WORDS}\s+one\b",
        re.IGNORECASE,
    ),
]


def _is_colour_question(transcript: str) -> bool:
    return any(p.search(transcript) for p in _COLOUR_INTENT_RES)


# ─── tray-contents intent ────────────────────────────────────────────────────
# "What's in the tray?" — he looks DOWN at the scan tray (he hangs INVERTED on
# the rail above the desk; the tray zone sits at the FAR END, bounded by four
# ArUco corner markers) and describes each item by NAME + COLOUR (the user is
# colour-blind — colours in words, always). Deterministic like the colour
# intent: one photo, one vision call, one spoken answer.
# TRAY_PITCH: physical move_head pitch that points the camera at the desk —
# calibrated 2026-07-14 overnight (photo sweep). Override: STACKCHAN_TRAY_PITCH.
TRAY_CHECK_ENABLED = os.environ.get(
    "STACKCHAN_TRAY_CHECK", "1"
).strip().lower() not in ("0", "false", "no", "off")
TRAY_PITCH = int(os.environ.get("STACKCHAN_TRAY_PITCH", "85"))
TRAY_STATION_MM = int(os.environ.get("STACKCHAN_TRAY_STATION_MM", "780"))

TRAY_ACK_PHRASES = [
    "Right, let me have a look in the tray...",
    "Tray inspection! One of my favourites. Peering down now...",
    "Having a look at the tray, one moment...",
]
TRAY_VISION_FAIL_PHRASES = [
    "I looked, but my eye's not cooperating. Try me again in a minute?",
    "Hmm. Photo happened, brain didn't. Ask me again?",
]
TRAY_VISION_PROMPT = (
    "This is a downward photo of a scan tray zone on a desk, bounded by four "
    "small square black-and-white markers (the corners). The user is "
    "COLOUR-BLIND and relies on you for colours. List the physical items "
    "lying inside (or partially inside) the marker zone: for each, its NAME "
    "and its COLOUR(S) in plain words. Ignore the markers themselves, the "
    "desk texture, and anything clearly outside the zone. If the tray zone "
    "is empty, say so. If no markers are visible, describe what IS on the "
    "desk below instead and mention you couldn't see the tray corners. "
    "Reply as ONE short spoken paragraph in Wheatley's voice (British, "
    "chatty), no markdown, no lists."
)

_TRAY_INTENT_RES = [
    re.compile(r"\bwhat('?s| is| do you see)?\s+(is\s+)?(in|on)\s+(the|my|your)\s+tray\b", re.I),
    re.compile(r"\b(check|look\s+(in|at)|inspect|scan)\s+(the|my|your)\s+tray\b", re.I),
    re.compile(r"\btray\s+(contents|check|inventory)\b", re.I),
]


def _is_tray_question(transcript: str) -> bool:
    return any(p.search(transcript) for p in _TRAY_INTENT_RES)


def _answer_tray_question(transcript: str, session_id: str) -> None:
    """Look down into the tray zone and describe items by name + colour.

    Same deterministic contract as the colour intent: once the ack is spoken
    the turn is OWNED — failures apologise and return, never fall to chat.
    Does NOT move the rail (rail moves stay deliberate/user-driven; if he's
    not near the tray the vision prompt's no-markers branch says so honestly).
    """
    api_key = os.environ.get(ANTHROPIC_API_KEY_ENV, "").strip()
    if not api_key:
        _clear_thinking_marker()
        _speak(_pick("no-key", NO_KEY_PHRASES))
        return

    photo = _take_photo_via_mcp(
        f"Tray contents check (user asked: {transcript})",
        cue_phrases=TRAY_ACK_PHRASES,
        cue_key="tray-ack",
        pose=(0, TRAY_PITCH),
    )
    if photo is None:
        _clear_thinking_marker()
        _speak(_pick("camera-fail", CAMERA_FAIL_PHRASES))
        return

    b64_data, media_type = photo
    messages = [{
        "role": "user",
        "content": [
            {"type": "image",
             "source": {"type": "base64", "media_type": media_type,
                        "data": b64_data}},
            {"type": "text",
             "text": f'{TRAY_VISION_PROMPT}\n\nThe user asked: "{transcript}"'},
        ],
    }]
    try:
        data = _call_claude_api(messages, api_key, system=SYSTEM_PROMPT)
        reply = _extract_text(data.get("content", []))
    except Exception:
        logger.exception("session=%s tray vision call failed", session_id)
        reply = ""
    # restore an at-the-user pose either way (he was staring at the desk)
    try:
        sess = MCPSession(GATEWAY_URL)
        sess.initialize()
        sess.call_tool("move_head", {"yaw": _user_yaw(), "pitch": LOOK_AT_USER_PITCH})
    except Exception:
        pass
    if not reply:
        _clear_thinking_marker()
        _speak(_pick("tray-vision-fail", TRAY_VISION_FAIL_PHRASES))
        return
    logger.info("session=%s tray answer: %r", session_id, reply)
    _clear_thinking_marker()
    face, spoken = _split_emotion(reply)
    spoken, cant = _strip_cant(spoken)
    if cant:
        _capture_cant(transcript, spoken, session_id)
        if not spoken:
            spoken = _pick("tray-vision-fail", TRAY_VISION_FAIL_PHRASES)
    _speak(spoken, face_before=(face or "idle"))


def _answer_colour_question(transcript: str, session_id: str) -> None:
    """Photograph and answer a colour question, deterministically.

    Never delegates the look-or-don't-look decision to the LLM: one photo,
    one vision call, one spoken answer. Speaks its own apology on photo or
    vision failure — by then the ack has already promised a look, so the
    caller must NOT fall through to normal chat afterwards (the question
    was handled, just unsuccessfully).
    """
    api_key = os.environ.get(ANTHROPIC_API_KEY_ENV, "").strip()
    if not api_key:
        _clear_thinking_marker()
        _speak(_pick("no-key", NO_KEY_PHRASES))
        return

    # The cue inside _take_photo_via_mcp is the ack — spoken BEFORE the
    # capture, so "hold it up" lands while there's still time to comply.
    photo = _take_photo_via_mcp(
        f"What colour is the held-up object? (user asked: {transcript})",
        cue_phrases=COLOUR_ACK_PHRASES,
        cue_key="colour-ack",
    )
    if photo is None:
        _clear_thinking_marker()
        _speak(_pick("camera-fail", CAMERA_FAIL_PHRASES))
        return

    b64_data, media_type = photo
    messages = [{
        "role": "user",
        "content": [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": b64_data,
                },
            },
            {
                "type": "text",
                "text": f'{COLOUR_VISION_PROMPT}\n\nThe user asked: "{transcript}"',
            },
        ],
    }]
    try:
        # SYSTEM_PROMPT (not the telemetry-augmented variant) keeps Wheatley's
        # voice + emotion tag; the colour brief rides in the user turn.
        data = _call_claude_api(messages, api_key, system=SYSTEM_PROMPT)
        reply = _extract_text(data.get("content", []))
    except Exception:
        logger.exception("session=%s colour vision call failed", session_id)
        reply = ""
    if not reply:
        _clear_thinking_marker()
        _speak(_pick("colour-vision-fail", COLOUR_VISION_FAIL_PHRASES))
        return
    logger.info("session=%s colour answer: %r", session_id, reply)
    _clear_thinking_marker()
    face, spoken = _split_emotion(reply)
    # This path shares SYSTEM_PROMPT, so a can't-tell answer may carry
    # [CANT] too — strip (never speak it) and wishlist it the same way.
    spoken, cant = _strip_cant(spoken)
    if cant:
        _capture_cant(transcript, spoken, session_id)
        if not spoken:
            spoken = _pick("colour-vision-fail", COLOUR_VISION_FAIL_PHRASES)
    _speak(spoken, face_before=(face or "idle"))


# ─── sentence-streaming reply path ───────────────────────────────────────────
# The Claude call streams as SSE and each sentence is spoken the moment it
# completes, while the rest of the reply is still generating.
#
# Serialization: the gateway's `say` tool only returns after the device has
# played the audio (synthesize_and_send pushes Opus frames paced at
# real-time under gateway.esp32.tts_lock), so sequential blocking say calls
# here guarantee ordered, gap-free-but-never-overlapping sentences — and
# the tts_lock additionally serializes us against any other concurrent
# speaker (idle loop, sensor reactor). No client-side queue needed: the
# already-generated SSE bytes just sit in the socket buffer while a chunk
# plays.


def _iter_sse_events(resp):
    """Yield parsed JSON data payloads from an Anthropic SSE response.

    Every Anthropic data payload carries its own "type" field, so the
    "event:" lines are redundant and skipped. `resp` is the file-like
    urllib response; iterating it yields lines (urllib de-chunks).
    """
    data_lines: list[str] = []
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            if data_lines:
                try:
                    yield json.loads("\n".join(data_lines))
                except json.JSONDecodeError:
                    logger.warning("stream: unparseable SSE data: %r", data_lines[:1])
                data_lines = []
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:  # trailing event without a final blank line
        try:
            yield json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            pass


# Sentence end: . ! or ? (optionally run-on like "..."), optionally followed
# by a closing quote/bracket, then whitespace. Whitespace is REQUIRED so a
# mid-stream "3." (of "3.5") or a not-yet-complete sentence at the buffer's
# edge never splits early; end-of-reply text is handled by flush().
_SENTENCE_SPLIT_RE = re.compile(r"[.!?]+[\"')\]]*\s")


class _SentenceChunker:
    """Accumulates streamed text deltas; emits speakable chunks at sentence
    boundaries, or at a word gap once SENTENCE_MAX_CHARS is pending."""

    def __init__(self):
        self._buf = ""

    @staticmethod
    def _split_once(buf: str) -> tuple[str | None, str]:
        m = _SENTENCE_SPLIT_RE.search(buf)
        if m:
            return buf[: m.end()].strip(), buf[m.end():]
        if len(buf) > SENTENCE_MAX_CHARS:
            cut = buf.rfind(" ", 0, SENTENCE_MAX_CHARS)
            if cut <= 0:
                cut = SENTENCE_MAX_CHARS
            return buf[:cut].strip(), buf[cut:].lstrip()
        return None, buf

    def feed(self, text: str) -> list[str]:
        self._buf += text
        out: list[str] = []
        while True:
            chunk, rest = self._split_once(self._buf)
            if chunk is None:
                break
            self._buf = rest
            if chunk:  # drop empty splits (e.g. stray leading punctuation)
                out.append(chunk)
        return out

    def flush(self) -> str | None:
        chunk = self._buf.strip()
        self._buf = ""
        return chunk or None


class _StreamSpeaker:
    """Speaks reply chunks in order through one gateway MCP session.

    Mirrors _speak's shape, split across the stream: face is set once
    before the first chunk, every chunk goes out via `say` (blocking until
    played — see the serialization note above), and finish() does the
    idle-face + look-at-user reset exactly once at the end.
    """

    def __init__(self):
        self._sess: MCPSession | None = None
        self.spoke = False

    def _session(self) -> MCPSession:
        if self._sess is None:
            sess = MCPSession(GATEWAY_URL)
            sess.initialize()
            self._sess = sess
        return self._sess

    def speak_chunk(self, text: str, face: str | None = None) -> None:
        text = (text or "").strip()
        if not text:
            return
        sess = self._session()
        if not self.spoke:
            # Speech is starting: stop the LED thinking chase and put the
            # emotion-tag face up, same as the batch path's _speak.
            _clear_thinking_marker()
            sess.call_tool("set_avatar", {"face": face or "idle"})
        self.spoke = True
        sess.call_tool("say", {"text": text}, timeout=30)

    def finish(self) -> None:
        if not self.spoke:
            return
        try:
            sess = self._session()
            sess.call_tool("set_avatar", {"face": "idle"})
            sess.call_tool("move_head", {"yaw": _user_yaw(), "pitch": LOOK_AT_USER_PITCH + 6})
        except Exception:
            logger.exception("stream finish (pose reset) failed")


def _ask_claude_streaming(transcript: str, session_id: str = "") -> bool:
    """Stream the reply and speak it sentence-by-sentence.

    Returns True when the turn was fully handled (all speech done, pose
    reset). Returns False when the caller should run the batch path
    instead — safe in every False case because either nothing has been
    spoken yet, or (tool-use fallback) only preamble text from the first
    response, which the batch path never speaks (it only speaks the
    post-photo follow-up's text).
    """
    api_key = os.environ.get(ANTHROPIC_API_KEY_ENV, "").strip()
    if not api_key:
        return False  # batch path speaks the no-key phrase

    system = _system_prompt_with_status()
    messages = [{"role": "user", "content": transcript}]
    t0 = time.time()
    try:
        resp = urllib.request.urlopen(
            _claude_request(
                messages, api_key, tools=[VISION_TOOL], system=system, stream=True
            ),
            timeout=30,
        )
    except Exception:
        # Batch retry reproduces the exact current error phrasing
        # (HTTP-error vs network-error) — don't duplicate it here.
        logger.warning("stream open failed; falling back to batch", exc_info=True)
        return False

    speaker = _StreamSpeaker()
    chunker = _SentenceChunker()
    reply_parts: list[str] = []  # full text, for the log
    first_say_at: float | None = None

    def _speak_next(chunk: str) -> None:
        nonlocal first_say_at
        # [CANT] rides at the very END of the reply, and the chunker only
        # splits at sentence-punctuation-plus-whitespace or at a word gap,
        # so the token always arrives whole inside a single chunk — in
        # practice the flush() tail (it has no trailing whitespace, so no
        # sentence split ever fires after it). Stripping each chunk on its
        # accumulated text before speaking is therefore split-proof;
        # detection/capture runs once on the full reply after the stream.
        chunk, _ = _strip_cant(chunk)
        if not chunk:
            return  # chunk was only the token — nothing speakable
        if not speaker.spoke:
            # The emotion tag rides in front of the first sentence; strip
            # it and apply the face with the first chunk (batch parity).
            face, spoken = _split_emotion(chunk)
            if first_say_at is None:
                first_say_at = time.time()
            speaker.speak_chunk(spoken, face or "idle")
        else:
            speaker.speak_chunk(chunk)

    try:
        try:
            for data in _iter_sse_events(resp):
                dtype = data.get("type")
                if dtype == "content_block_start":
                    block = data.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        # Vision turn — the batch path owns the photo
                        # round-trip. Abandon the stream for this turn.
                        logger.info(
                            "session=%s stream: tool_use started, deferring to batch",
                            session_id,
                        )
                        return False
                elif dtype == "content_block_delta":
                    delta = data.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        reply_parts.append(text)
                        for chunk in chunker.feed(text):
                            _speak_next(chunk)
                elif dtype == "error":
                    raise RuntimeError(f"stream error event: {data.get('error')}")
                elif dtype == "message_stop":
                    break
        finally:
            try:
                resp.close()
            except Exception:
                pass
        tail = chunker.flush()
        if tail:
            _speak_next(tail)
    except Exception:
        logger.exception("session=%s streaming reply failed", session_id)
        if not speaker.spoke:
            return False  # clean slate — batch retry
        # Mid-utterance failure: end gracefully rather than re-asking and
        # double-speaking the start of the reply via the batch path.
        speaker.finish()
        return True

    if not speaker.spoke:
        # Empty reply — rare; let batch retry once and speak its own
        # empty-answer phrase if it repeats. (Also covers a reply that was
        # ONLY tags: the batch retry does its own strip + capture.)
        logger.info("session=%s stream produced no speakable text", session_id)
        return False

    # Wishlist capture on the accumulated full reply (chunks were only
    # stripped for speech above; this is the authoritative detection).
    _, spoken_full = _split_emotion("".join(reply_parts))
    reply_clean, cant = _strip_cant(spoken_full)
    if cant:
        _capture_cant(transcript, reply_clean, session_id)

    logger.info(
        "session=%s streamed reply (first say at +%.1fs, total %.1fs): %r",
        session_id,
        (first_say_at - t0) if first_say_at is not None else -1.0,
        time.time() - t0,
        "".join(reply_parts),
    )
    speaker.finish()
    return True


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
    # Warm the live-status cache while transcription runs so the status
    # line adds ~zero latency to the Claude call (30s cache, ~2s timeouts;
    # a concurrent fetch at ask-time is harmless, just redundant).
    threading.Thread(target=_cached_status_line, daemon=True).start()
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
            enrolled, resolved_name = _complete_enrollment(name) if name else (False, None)
            _clear_thinking_marker()
            if name and enrolled:
                if resolved_name and resolved_name != name:
                    _speak(
                        _pick("enroll-renamed", ENROLL_RENAMED_PHRASES)
                        .format(said_name=name, saved_name=resolved_name)
                    )
                else:
                    _speak(_pick("enroll-confirm", ENROLL_CONFIRM_PHRASES).format(name=name))
            else:
                _speak(_pick("enroll-fail", ENROLL_FAIL_PHRASES))
            return

        # The arbiter judged a fresh capture "definite + good" and proposed
        # learning it (see sensor_reactor.py's LEARN_CONFIRM_ASK_PHRASES) —
        # this tap-to-talk answer is a yes/no to that, not a normal question.
        pending_learn = _read_pending_learn_confirm()
        if pending_learn is not None:
            answer = _parse_yes_no(transcript)
            logger.info(
                "session=%s learn-confirm transcript=%r answer=%r name=%r",
                session_id, transcript, answer, pending_learn.get("name"),
            )
            _clear_thinking_marker()
            if answer is True:
                ok = _confirm_learn_sample(
                    pending_learn.get("name", ""), pending_learn.get("frame_path", "")
                )
                _speak(
                    _pick("learn-confirm-yes", LEARN_CONFIRM_YES_PHRASES) if ok
                    else _pick("learn-confirm-fail", LEARN_CONFIRM_FAIL_PHRASES)
                )
            else:
                # False or ambiguous (None) both decline — fail-safe, never
                # learn on an unclear answer.
                frame_path = pending_learn.get("frame_path")
                if frame_path:
                    try:
                        os.remove(frame_path)
                    except OSError:
                        pass
                _speak(_pick("learn-declined", LEARN_CONFIRM_DECLINED_PHRASES))
            return

        # "Do a dance" — intent shortcut to the rail choreography in
        # stackchan_mcp/rail_dance.py (rebuilt 2026-07-13; the original hook
        # was lost to a git revert). Wrapped so it can NEVER break a turn.
        try:
            from stackchan_mcp import rail_dance
            if rail_dance.is_dance_request(transcript):
                outcome = rail_dance.try_start_dance(start_delay_s=2.5)
                logger.info("session=%s dance intent (%s): %r", session_id, outcome, transcript)
                _clear_thinking_marker()
                if outcome == "started":
                    _speak("Oh! Right! Dancing. Yes! Watch this—")
                elif outcome == "busy":
                    _speak("I'm already dancing! One routine at a time, please.")
                else:
                    _speak("I would, honestly, but the rail's not feeling it right now.")
                return
        except Exception:
            logger.exception("dance intent hook failed; falling through to chat")

        # "Where are my earplugs?" — inventory lookup intent, answered
        # straight from the local HomeBox instance via
        # stackchan_mcp/inventory.py (same hook shape as the dance intent
        # above). Wrapped so it can NEVER break a turn. Deliberately does
        # NOT fire on "desk"/"bench" phrasing (extract_query returns None
        # there) — rail-findable "on the desk" questions belong to the
        # find_item.py concept and fall through to normal chat.
        try:
            from stackchan_mcp import inventory
            inv_query = inventory.extract_query(transcript)
            if inv_query:
                try:
                    inv_results = inventory.find_items(inv_query)
                    inv_speech = inventory.format_speech(inv_query, inv_results)
                except inventory.InventoryUnavailable as exc:
                    inv_results = None
                    inv_speech = inventory.format_unavailable()
                    logger.warning(
                        "session=%s inventory unavailable for %r: %s",
                        session_id, inv_query, exc,
                    )
                logger.info(
                    "session=%s inventory intent query=%r hits=%s: %r",
                    session_id, inv_query,
                    "n/a" if inv_results is None else len(inv_results),
                    transcript,
                )
                _clear_thinking_marker()
                _speak(inv_speech)
                return
        except Exception:
            logger.exception("inventory intent hook failed; falling through to chat")

        # "What colour is this Sharpie?" — assistive colour identification
        # (the user is colour-blind; see the colour-intent section above).
        # ALWAYS photographs and answers, unlike normal chat where Claude
        # may or may not use the camera. Only intent DETECTION may fall
        # through to chat on an unexpected bug; once matched, the handler
        # owns the turn outright — photo/vision failures speak an apology
        # and return, never re-ask via non-deterministic chat.
        if COLOUR_CHECK_ENABLED:
            try:
                is_colour = _is_colour_question(transcript)
            except Exception:
                is_colour = False
                logger.exception("colour intent match failed; falling through to chat")
            if is_colour:
                logger.info("session=%s colour intent: %r", session_id, transcript)
                try:
                    _answer_colour_question(transcript, session_id)
                except Exception:
                    logger.exception("session=%s colour handler failed", session_id)
                    _clear_thinking_marker()
                    _speak(
                        "Tried a colour check there and it went properly "
                        "sideways on my end. Sorry. Give it another go?"
                    )
                return

        # Tray-contents intent — same deterministic owns-the-turn contract as
        # colour above ("what's in the tray?" -> look down, photo, name+colour).
        if TRAY_CHECK_ENABLED:
            try:
                is_tray = _is_tray_question(transcript)
            except Exception:
                is_tray = False
                logger.exception("tray intent match failed; falling through to chat")
            if is_tray:
                logger.info("session=%s tray intent: %r", session_id, transcript)
                try:
                    _answer_tray_question(transcript, session_id)
                except Exception:
                    logger.exception("session=%s tray handler failed", session_id)
                    _clear_thinking_marker()
                    _speak(
                        "Tried to check the tray and tripped over my own "
                        "eyeball. Sorry. Ask me again?"
                    )
                return

        # Not one of the deterministic intents — this is an open chat turn, so
        # the answer takes an LLM round-trip. Fill that gap with a spoken "hmm"
        # so he audibly acknowledges he heard the whole question (the intents
        # above already speak their own ack, so this is chat-path only).
        _speak_thinking_sound()

        t1 = time.time()
        # Streaming path first (unless killed via STACKCHAN_VOICE_STREAMING=0):
        # speaks the reply itself, sentence by sentence. False = fall through
        # to the batch path (no key / tool-use turn / early failure — safe,
        # see _ask_claude_streaming's contract).
        if STREAMING_ENABLED and _ask_claude_streaming(transcript, session_id):
            logger.info(
                "session=%s reply handled via streaming in %.1fs",
                session_id, time.time() - t1,
            )
            return
        reply = _ask_claude(transcript)
        logger.info(
            "session=%s claude reply in %.1fs: %r",
            session_id, time.time() - t1, reply,
        )
        _clear_thinking_marker()
        # Match his face to the tone of the reply (emotion tag he prefixes).
        # Fall back to idle so a neutral answer doesn't linger on the
        # thinking/"sad" concentrating face left over from _set_thinking_pose.
        face, spoken = _split_emotion(reply)
        # [CANT] = the reply is a can't-do-that / don't-know — wishlist it
        # for the nightly Dream Loop, and never let TTS read the token.
        spoken, cant = _strip_cant(spoken)
        if cant:
            _capture_cant(transcript, spoken, session_id)
            if not spoken:  # reply was effectively only tags — rare
                spoken = "Yeah — that one's a bit beyond me right now, honestly."
        logger.info("session=%s reply emotion=%r cant=%s", session_id, face, cant)
        _speak(spoken, face_before=(face or "idle"))
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
