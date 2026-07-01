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

import json
import logging
import os
import random
import tempfile
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

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
]
TRANSCRIBE_FAIL_PHRASES = [
    "Sorry, didn't quite catch that one.",
    "Hmm, couldn't make that out, try again?",
]

# Reuse the same "concentrating" face/pose as the Claude Code busy hook —
# squint + look down — so listening/thinking reads consistently across both
# voice and coding-work contexts.
THINKING_FACE = "sad"
LOOK_UP_PITCH = 34

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
    """Speak text through the gateway, reverting to a relaxed look-up pose."""
    try:
        sess = MCPSession(GATEWAY_URL)
        sess.initialize()
        if face_before:
            sess.call_tool("set_avatar", {"face": face_before})
        sess.call_tool("say", {"text": text}, timeout=30)
        sess.call_tool("set_avatar", {"face": "idle"})
        sess.call_tool("move_head", {"yaw": 0, "pitch": LOOK_UP_PITCH + 6})
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


def _ask_claude(transcript: str) -> str:
    api_key = os.environ.get(ANTHROPIC_API_KEY_ENV, "").strip()
    if not api_key:
        return random.choice(NO_KEY_PHRASES)

    body = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 300,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": transcript}],
    }).encode()
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        parts = data.get("content", [])
        text = " ".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
        return text or "Er, got an empty answer back. Not sure what happened there."
    except urllib.error.HTTPError as exc:
        body_snippet = exc.read().decode(errors="replace")[:300]
        logger.warning("Claude API HTTP error %s: %s", exc.code, body_snippet)
        return "Ah, the Claude API didn't like that — something's misconfigured, sorry."
    except Exception:
        logger.exception("Claude API call failed")
        return "Couldn't reach Claude just now. Network's playing up, probably."


def _handle_capture(ogg_bytes: bytes, session_id: str) -> None:
    """Runs in a background thread so the HTTP response isn't held up."""
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
            _speak(random.choice(TRANSCRIBE_FAIL_PHRASES))
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
