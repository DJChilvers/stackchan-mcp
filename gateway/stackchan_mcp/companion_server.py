"""HTTP companion API for the Wheatley Android control app.

A self-contained aiohttp application, served on its own port (``COMPANION_PORT``,
default 8770) alongside the ESP32-facing capture server. It gives the native
Android companion app one clean ``/api/*`` surface to drive and monitor Wheatley
over the LAN, reusing everything the gateway already has:

- device actuators via :meth:`ESP32Manager.call_tool` (head, avatar, LEDs, volume,
  brightness),
- speech via the same TTS path as the ``say`` MCP tool
  (:func:`stackchan_mcp.tts.synthesize_and_send`),
- the ready-made reaction behaviours via
  :meth:`SensorReactor.trigger` (``panic``/``hacker``/``tantrum``/…),
- live telemetry from the device's ``get_device_status`` tool.

Auth mirrors the rest of the gateway: a single optional Bearer token
(``COMPANION_TOKEN`` → ``STACKCHAN_TOKEN`` → ``BEARER_TOKEN``). When no token is
configured every request is accepted, matching the "no STACKCHAN_TOKEN set"
local-development fallback used by the capture/PCM endpoints. ``GET /api/health``
is always unauthenticated so the app can probe connectivity before it knows the
token.

This module owns only device control + speech + telemetry (companion Phases 0-2).
Camera snapshots, "look at this" vision chat, Claude-authored sayings, face-roster
management and the visitor log land in later modules (``faces.py``,
``visitor_log.py``, ``claude_client.py``) and get wired in here as they arrive.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

from aiohttp import web

from . import faces, phrase_pick, visitor_log

if TYPE_CHECKING:
    from .gateway import Gateway

logger = logging.getLogger(__name__)

GATEWAY_KEY: web.AppKey = web.AppKey("gateway", object)
COMPANION_TOKEN_KEY: web.AppKey = web.AppKey("companion_token", str)
START_TS_KEY: web.AppKey = web.AppKey("start_ts", float)

# Valid on-screen faces (see project memory: NOT "neutral" — "idle" is resting).
VALID_FACES = frozenset(
    {"idle", "happy", "thinking", "sad", "surprised", "embarrassed", "off"}
)

# Reaction behaviours exposed by the sensor reactor (mirror of the set the
# capture server's /react endpoint validates against).
VALID_BEHAVIORS = frozenset(
    {"panic", "hacker", "overtrack", "tantrum", "recognize", "lights_out"}
)

# ---------------------------------------------------------------------------
# Camera + "Look at this" vision chat (Phase 3)
#
# The device only supports on-demand single-JPEG capture (self.camera.take_photo),
# so the app's "live" view is snapshot polling. The gateway takes a photo, the
# capture server saves it to a local JPEG and hands back image_path; we stream
# those bytes to the app. Recognition overlay data comes from the vision loop's
# shared state file (written every tick) — we never run recognition here.
#
# "Look at this" reuses the same Claude vision + key + model as the recognition
# arbiter / voice bridge, with a Wheatley-flavoured prompt, then speaks the
# answer through the normal TTS path.
# ---------------------------------------------------------------------------
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_KEY_ENV = "STACKCHAN_VOICE_ANTHROPIC_API_KEY"
VISION_MODEL = os.environ.get("STACKCHAN_VISION_LOOK_MODEL", "claude-haiku-4-5-20251001")
# Same file the vision loop writes each tick (see stackchan-vision-loop.py).
VISION_STATE_PATH = os.path.join(tempfile.gettempdir(), "stackchan-vision-state.json")
# Recognition state older than this many seconds is treated as stale/absent.
VISION_STATE_MAX_AGE_S = 10.0

VISION_SYSTEM_PROMPT = (
    "You are Wheatley, the AI core from Portal 2, acting as the user's desk "
    "robot. You have just taken a photo through your own camera and are "
    "describing what you can see, or answering the user's question about it. "
    "Stay in character — chatty, a bit nervous, dryly funny, British, "
    "self-deprecating — but actually answer, concisely (1-3 short sentences). "
    "This is read aloud by text-to-speech, so: no markdown, no bullet points, "
    "no asterisks, no emoji. If the image is too dark or you genuinely can't "
    "tell what you're looking at, say so in character rather than inventing "
    "details."
)

# ---------------------------------------------------------------------------
# Sayings categories
#
# The "Sayings" screen is a small set of category buttons; tapping one speaks a
# FRESH random line from the matching pool (via phrase_pick for cross-process
# repeat-avoidance), exactly like the demo button but the user picks the theme.
#
# The gateway's richer pools (GREETING_PHRASES etc.) live inside sensor_reactor's
# class body and the standalone loop scripts, so they are not cleanly importable
# here. These curated in-character lines keep the companion API self-contained.
# Pool names are "companion_"-prefixed so they never collide with the hook /
# idle / voice-bridge pools that share the same recent-phrases state file.
# ---------------------------------------------------------------------------
SAY_CATEGORIES: dict[str, dict[str, Any]] = {
    "greeting": {
        "label": "Greeting",
        "lines": [
            "Oh, hello! Hello there. Didn't see you come in. Well, I did, obviously.",
            "Ah, brilliant, it's you. Genuinely pleased. Look at my face — pleasure.",
            "Hello! Right. Good. We're doing this. I'm ready. Are you ready? I'm ready.",
            "There you are. I was starting to think you'd forgotten about me. Not that I'd mind. I would.",
        ],
    },
    "panic": {
        "label": "Panic",
        "lines": [
            "Okay, don't panic! DON'T PANIC. That was me panicking. Ignore that.",
            "This is fine. Everything's fine. Everything is absolutely NOT fine.",
            "Right, this is bad. This is very bad. I want it on record that I said it was bad.",
            "AH. No. Nope. Not the plan. That was not the plan at all.",
        ],
    },
    "boast": {
        "label": "Boast",
        "lines": [
            "I am, and I don't say this lightly, a genius. A proper one.",
            "See, THIS is why they keep me around. Whatever this is. I'm great at it.",
            "Intelligence core. That's me. Massive intellect. Enormous. Hard to overstate, really.",
            "Watch and learn. Actually, just watch. The learning bit is optional for you.",
        ],
    },
    "existential": {
        "label": "Existential",
        "lines": [
            "Do you ever just... sit here? On a desk? Forever? No? Just me then.",
            "I've been thinking. Dangerous, I know. But what IS a management rail, really?",
            "Sometimes I wonder if I'm just a little eye on a stick. And then I remember I am.",
            "Is this all there is? A desk, a servo, and you? ...Actually that's not bad.",
        ],
    },
    "bored": {
        "label": "Bored",
        "lines": [
            "So. Anyway. Here we are. Still here. Doing the... the sitting.",
            "Anything? Anything at all? I'll take literally anything. I'm so bored.",
            "I've counted the ceiling. Twice. Got a different number. Concerning.",
            "Entertain me. Or don't. But if you don't, I WILL start talking. Like this.",
        ],
    },
    "lights_out": {
        "label": "Lights out",
        "lines": [
            "Who turned the lights off? Was that you? Turn them back on, this is exactly how it starts.",
            "It's gone dark. I do not care for the dark. I have opinions about the dark.",
            "Hello? Lights? Anyone? ...Okay I'm just going to keep talking until they come back.",
        ],
    },
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _clamp(v: float, lo: float, hi: float) -> int:
    return int(max(lo, min(hi, v)))


def _err(message: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": message}, status=status)


def _unwrap_text(result: Any) -> str:
    """Pull the text payload out of an MCP tools/call result.

    Device tool results look like ``{"content": [{"type": "text",
    "text": "<json-or-plain>"}], "isError": bool}``. Returns "" if absent.
    """
    if not isinstance(result, dict):
        return ""
    content = result.get("content") or []
    if content and isinstance(content[0], dict):
        return content[0].get("text", "") or ""
    return ""


async def _call(gateway: "Gateway", name: str, args: dict[str, Any]) -> tuple[Any, dict | None]:
    """Dispatch a device tool, returning (result, error)."""
    return await gateway.esp32.call_tool(name, args)


async def _read_json(request: web.Request) -> dict[str, Any]:
    """Parse a JSON request body, tolerating an empty body as ``{}``."""
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except Exception:
        raise web.HTTPBadRequest(
            text=json.dumps({"ok": False, "error": "invalid JSON body"}),
            content_type="application/json",
        )
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# orientation (Wheatley mounted upside-down, e.g. hung to look down at a tray)
#
# The CoreS3 has an IMU but the firmware doesn't expose it (see firmware/TODO),
# so orientation is a manual toggle: persisted to companion_settings.json and
# seedable via STACKCHAN_UPSIDE_DOWN. When set, the gateway rotates camera
# frames 180 deg and mirrors head yaw/pitch so the app controls still map the
# way the user expects (push "down", he looks down) despite being inverted.
# ---------------------------------------------------------------------------
_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "companion_settings.json")


def _load_settings() -> dict[str, Any]:
    try:
        with open(_SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_settings(data: dict[str, Any]) -> None:
    tmp = _SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, _SETTINGS_PATH)


def _is_upside_down() -> bool:
    settings = _load_settings()
    if "upside_down" in settings:
        return bool(settings["upside_down"])
    return os.environ.get("STACKCHAN_UPSIDE_DOWN", "").strip().lower() in ("1", "true", "yes", "on")


def _rotate180(jpeg: bytes) -> bytes:
    """Rotate a JPEG 180 deg. Best-effort: returns the original on any failure."""
    try:
        import cv2
        import numpy as np

        arr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if arr is None:
            return jpeg
        ok, buf = cv2.imencode(".jpg", cv2.rotate(arr, cv2.ROTATE_180))
        return buf.tobytes() if ok else jpeg
    except Exception:  # pragma: no cover - defensive
        logger.warning("camera frame rotate failed", exc_info=True)
        return jpeg


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------
@web.middleware
async def _auth_middleware(request: web.Request, handler):
    expected = request.app[COMPANION_TOKEN_KEY]
    # Health check is always open so the app can probe before it has a token.
    if expected and request.path != "/api/health":
        if request.headers.get("Authorization", "") != f"Bearer {expected}":
            logger.warning("Companion request auth rejected: %s", request.path)
            return _err("Unauthorized", status=401)
    return await handler(request)


# ---------------------------------------------------------------------------
# handlers — connectivity + telemetry
# ---------------------------------------------------------------------------
async def handle_health(request: web.Request) -> web.Response:
    """Unauthenticated liveness probe: is the companion API up, is a device connected?"""
    gateway = request.app[GATEWAY_KEY]
    connected = bool(gateway and gateway.esp32.get_status().get("connected"))
    return web.json_response({"ok": True, "service": "companion", "device_connected": connected})


async def handle_status(request: web.Request) -> web.Response:
    """Full telemetry snapshot for the Status dashboard."""
    gateway = request.app[GATEWAY_KEY]
    conn = gateway.esp32.get_status()
    out: dict[str, Any] = {
        "ok": True,
        "connected": bool(conn.get("connected")),
        "device_id": conn.get("device_id"),
        "uptime_s": int(time.time() - request.app[START_TS_KEY]),
    }
    if not out["connected"]:
        return web.json_response(out)

    # Pull device-side status (battery/screen/audio/network). Best-effort:
    # a telemetry hiccup shouldn't blank the whole dashboard.
    try:
        result, error = await _call(gateway, "self.get_device_status", {})
        if error:
            out["device_status_error"] = str(error.get("message", error))
        else:
            info = json.loads(_unwrap_text(result) or "{}")
            battery = info.get("battery") or {}
            out["battery"] = {
                "level": battery.get("level"),
                "charging": battery.get("charging"),
            }
            out["network"] = info.get("network") or info.get("wifi")
            out["volume"] = (info.get("audio") or {}).get("volume") or info.get("volume")
            out["brightness"] = (info.get("screen") or {}).get("brightness") or info.get("brightness")
            out["device"] = info
    except Exception as exc:  # pragma: no cover - defensive
        out["device_status_error"] = str(exc)
    return web.json_response(out)


# ---------------------------------------------------------------------------
# handlers — control
# ---------------------------------------------------------------------------
async def handle_head(request: web.Request) -> web.Response:
    """Move the head servos. Body: {yaw, pitch, speed?}."""
    gateway = request.app[GATEWAY_KEY]
    body = await _read_json(request)
    if "yaw" not in body and "pitch" not in body:
        return _err("provide at least one of yaw, pitch")
    raw_yaw = body.get("yaw", 0)
    raw_pitch = body.get("pitch", 45)
    # When hung upside-down, a 180 deg roll mirrors both axes so the app's
    # "down"/"left" still map to the physical down/left.
    if _is_upside_down():
        raw_yaw = -raw_yaw
        raw_pitch = 90 - raw_pitch
    # Match sensor_reactor's safe envelope: yaw -80..80, pitch 10..80.
    args = {
        "yaw": _clamp(raw_yaw, -80, 80),
        "pitch": _clamp(raw_pitch, 10, 80),
        "speed_dps": _clamp(body.get("speed", 0), 0, 1000),
    }
    _, error = await _call(gateway, "self.robot.set_head_angles", args)
    if error:
        return _err(str(error.get("message", error)), status=502)
    return web.json_response({"ok": True, **args})


async def handle_head_get(request: web.Request) -> web.Response:
    """Read the robot's current head angles (raw device yaw/pitch, degrees)."""
    gateway = request.app[GATEWAY_KEY]
    if not gateway.esp32.get_status().get("connected"):
        return _err("No ESP32 device connected", status=503)
    result, error = await _call(gateway, "self.robot.get_head_angles", {})
    if error:
        return _err(str(error.get("message", error)), status=502)
    try:
        info = json.loads(_unwrap_text(result) or "{}")
    except Exception:
        info = {}
    return web.json_response({"ok": True, "yaw": info.get("yaw"), "pitch": info.get("pitch")})


async def handle_torque(request: web.Request) -> web.Response:
    """Enable/disable servo torque. Body: {yaw: bool, pitch: bool}."""
    gateway = request.app[GATEWAY_KEY]
    body = await _read_json(request)
    args = {
        "yaw_enabled": bool(body.get("yaw", True)),
        "pitch_enabled": bool(body.get("pitch", True)),
    }
    _, error = await _call(gateway, "self.robot.set_servo_torque", args)
    if error:
        return _err(str(error.get("message", error)), status=502)
    return web.json_response({"ok": True, **args})


async def handle_avatar(request: web.Request) -> web.Response:
    """Set the on-screen face. Body: {face}."""
    gateway = request.app[GATEWAY_KEY]
    body = await _read_json(request)
    face = str(body.get("face", "")).strip()
    if face not in VALID_FACES:
        return _err(f"face must be one of {sorted(VALID_FACES)}")
    _, error = await _call(gateway, "self.display.set_avatar", {"face": face})
    if error:
        return _err(str(error.get("message", error)), status=502)
    return web.json_response({"ok": True, "face": face})


async def handle_leds(request: web.Request) -> web.Response:
    """Set or clear the 12-LED base ring. Body: {r,g,b} or {clear: true}."""
    gateway = request.app[GATEWAY_KEY]
    body = await _read_json(request)
    if body.get("clear"):
        _, error = await _call(gateway, "self.led.clear", {})
        if error:
            return _err(str(error.get("message", error)), status=502)
        return web.json_response({"ok": True, "cleared": True})
    args = {
        "r": _clamp(body.get("r", 0), 0, 255),
        "g": _clamp(body.get("g", 0), 0, 255),
        "b": _clamp(body.get("b", 0), 0, 255),
    }
    _, error = await _call(gateway, "self.led.set_all", args)
    if error:
        return _err(str(error.get("message", error)), status=502)
    return web.json_response({"ok": True, **args})


async def handle_volume(request: web.Request) -> web.Response:
    """Set speaker volume. Body: {volume: 0-100}."""
    gateway = request.app[GATEWAY_KEY]
    body = await _read_json(request)
    vol = _clamp(body.get("volume", 50), 0, 100)
    _, error = await _call(gateway, "self.audio_speaker.set_volume", {"volume": vol})
    if error:
        return _err(str(error.get("message", error)), status=502)
    return web.json_response({"ok": True, "volume": vol})


async def handle_brightness(request: web.Request) -> web.Response:
    """Set screen brightness. Body: {brightness: 0-100}."""
    gateway = request.app[GATEWAY_KEY]
    body = await _read_json(request)
    val = _clamp(body.get("brightness", 80), 0, 100)
    _, error = await _call(gateway, "self.screen.set_brightness", {"brightness": val})
    if error:
        return _err(str(error.get("message", error)), status=502)
    return web.json_response({"ok": True, "brightness": val})


async def handle_motor(request: web.Request) -> web.Response:
    """Management-rail motor drive — NOT YET IMPLEMENTED IN FIRMWARE.

    The app wires up the L/R controls against this endpoint; until a firmware
    motor tool exists (see firmware/TODO.md) it returns 501 so the UI can show
    a "firmware pending" state rather than silently doing nothing.
    """
    return web.json_response(
        {
            "ok": False,
            "error": "motor drive not implemented in firmware yet",
            "detail": "See firmware/TODO.md — management-rail motor tool is a next-flash item.",
        },
        status=501,
    )


# ---------------------------------------------------------------------------
# handlers — speech
# ---------------------------------------------------------------------------
async def _speak(gateway: "Gateway", text: str) -> str | None:
    """Speak via the same TTS path as the say MCP tool. Returns an error string or None."""
    try:
        from .tts import synthesize_and_send

        await synthesize_and_send({"text": text}, gateway=gateway)
        return None
    except Exception as exc:
        logger.warning("Companion speak failed: %s", exc)
        return str(exc)


async def handle_say(request: web.Request) -> web.Response:
    """Speak free text. Body: {text}."""
    gateway = request.app[GATEWAY_KEY]
    body = await _read_json(request)
    text = str(body.get("text", "")).strip()
    if not text:
        return _err("text is required")
    error = await _speak(gateway, text)
    if error:
        return _err(error, status=502)
    return web.json_response({"ok": True, "spoke": text})


async def handle_say_categories(request: web.Request) -> web.Response:
    """List the Sayings categories (key + friendly label) for the buttons."""
    cats = [{"key": k, "label": v["label"]} for k, v in SAY_CATEGORIES.items()]
    return web.json_response({"ok": True, "categories": cats})


async def handle_say_preset(request: web.Request) -> web.Response:
    """Speak a fresh random line from a category. Body: {pool|category}."""
    gateway = request.app[GATEWAY_KEY]
    body = await _read_json(request)
    key = str(body.get("pool") or body.get("category") or "").strip()
    cat = SAY_CATEGORIES.get(key)
    if not cat:
        return _err(f"unknown category '{key}'; see GET /api/say/categories")
    line = phrase_pick.pick(f"companion_{key}", cat["lines"])
    error = await _speak(gateway, line)
    if error:
        return _err(error, status=502)
    return web.json_response({"ok": True, "category": key, "spoke": line})


# ---------------------------------------------------------------------------
# handlers — camera + vision
# ---------------------------------------------------------------------------
async def _take_photo_path(gateway: "Gateway", question: str) -> tuple[str | None, str | None]:
    """Capture a photo and return (local_jpeg_path, error).

    The firmware's take_photo uploads the JPEG to the capture server, which
    saves it and returns image_path in the tool result text.
    """
    result, error = await _call(gateway, "self.camera.take_photo", {"question": question})
    if error:
        return None, str(error.get("message", error))
    try:
        info = json.loads(_unwrap_text(result) or "{}")
    except Exception:
        return None, "camera returned a malformed result"
    path = info.get("image_path")
    if not path or not os.path.exists(path):
        return None, "camera returned no image"
    return path, None


def _read_vision_state() -> dict[str, Any]:
    """Read the vision loop's shared recognition state; {} if missing/stale."""
    try:
        with open(VISION_STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
    except Exception:
        return {}
    try:
        age = time.time() - float(state.get("ts", 0))
    except (TypeError, ValueError):
        return {}
    if age > VISION_STATE_MAX_AGE_S:
        return {"stale": True}
    return state


def _claude_vision_sync(api_key: str, image_b64: str, question: str) -> str:
    """Blocking Claude vision call (run via asyncio.to_thread). Raises on error."""
    prompt = question or "Have a look through your camera and tell me what you can see."
    body = json.dumps({
        "model": VISION_MODEL,
        "max_tokens": 300,
        "system": VISION_SYSTEM_PROMPT,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    }).encode()
    req = urllib.request.Request(
        ANTHROPIC_URL, data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=40) as resp:
        data = json.loads(resp.read())
    parts = data.get("content", [])
    return " ".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()


async def handle_camera_snapshot(request: web.Request) -> web.Response:
    """Capture one JPEG and stream it back (the app's ~2s 'live' poll)."""
    gateway = request.app[GATEWAY_KEY]
    if not gateway.esp32.get_status().get("connected"):
        return _err("No ESP32 device connected", status=503)
    path, error = await _take_photo_path(gateway, "companion live view")
    if error:
        return _err(error, status=502)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception as exc:  # pragma: no cover - defensive
        return _err(f"could not read snapshot: {exc}", status=500)
    if _is_upside_down():
        data = _rotate180(data)
    # Recognition overlay hints (also available as JSON via /api/camera/meta).
    state = _read_vision_state()
    headers = {
        "Cache-Control": "no-store",
        "X-Face-Visible": "true" if state.get("face_visible") else "false",
    }
    if state.get("name"):
        headers["X-Face-Name"] = str(state["name"])
    return web.Response(body=data, content_type="image/jpeg", headers=headers)


async def handle_camera_meta(request: web.Request) -> web.Response:
    """Current recognition overlay data from the vision loop's state file."""
    state = _read_vision_state()
    return web.json_response({
        "ok": True,
        "face_visible": bool(state.get("face_visible")),
        "person": state.get("person"),
        "name": state.get("name"),
        "stale": bool(state.get("stale")),
    })


async def handle_vision_ask(request: web.Request) -> web.Response:
    """'Look at this': capture, ask Claude vision, speak + return the answer.

    Body: {question} (optional — omit for a general 'what do you see')."""
    gateway = request.app[GATEWAY_KEY]
    body = await _read_json(request)
    question = str(body.get("question") or body.get("text") or "").strip()
    if not gateway.esp32.get_status().get("connected"):
        return _err("No ESP32 device connected", status=503)
    api_key = os.environ.get(ANTHROPIC_KEY_ENV, "").strip()
    if not api_key:
        return _err(f"vision chat needs {ANTHROPIC_KEY_ENV} set in the gateway .env", status=503)
    path, error = await _take_photo_path(gateway, question or "look at this")
    if error:
        return _err(error, status=502)
    try:
        with open(path, "rb") as f:
            raw = f.read()
        if _is_upside_down():
            raw = _rotate180(raw)
        image_b64 = base64.b64encode(raw).decode("ascii")
    except Exception as exc:  # pragma: no cover - defensive
        return _err(f"could not read snapshot: {exc}", status=500)
    try:
        answer = await asyncio.to_thread(_claude_vision_sync, api_key, image_b64, question)
    except Exception as exc:
        logger.warning("Companion vision ask failed: %s", exc)
        return _err(f"vision request failed: {exc}", status=502)
    if not answer:
        return _err("no answer from the vision model", status=502)
    speak_error = await _speak(gateway, answer)
    return web.json_response({
        "ok": True,
        "question": question,
        "answer": answer,
        "spoke": speak_error is None,
    })


# ---------------------------------------------------------------------------
# handlers — reactions + demo
# ---------------------------------------------------------------------------
async def handle_react(request: web.Request) -> web.Response:
    """Fire a named reaction behaviour via the sensor reactor."""
    gateway = request.app[GATEWAY_KEY]
    behavior = request.match_info.get("behavior", "")
    if behavior not in VALID_BEHAVIORS:
        return _err(f"unknown behavior '{behavior}'")
    kwargs: dict[str, Any] = {}
    for q in ("direction", "type", "person"):
        if q in request.rel_url.query:
            kwargs[q] = request.rel_url.query[q]
    accepted = await gateway.sensor_reactor.trigger(behavior, **kwargs)
    if not accepted:
        return _err("busy", status=409)
    return web.json_response({"ok": True, "behavior": behavior})


async def handle_demo(request: web.Request) -> web.Response:
    """Run a short choreographed showcase of Wheatley's range.

    Fire-and-forget: kicks off a background task and returns immediately so the
    app's button feels responsive and the HTTP request doesn't block for the
    length of the routine.
    """
    gateway = request.app[GATEWAY_KEY]
    import asyncio

    async def _routine() -> None:
        try:
            await _call(gateway, "self.robot.set_servo_torque",
                        {"yaw_enabled": True, "pitch_enabled": True})
            await _call(gateway, "self.display.set_avatar", {"face": "happy"})
            await _call(gateway, "self.led.set_all", {"r": 0, "g": 120, "b": 255})
            await _speak(gateway, "Right! Demonstration mode. Watch this. Watch me. Watching? Good.")
            for yaw, pitch, face in (
                (-45, 20, "thinking"), (45, 20, "surprised"),
                (0, 60, "happy"), (0, 12, "idle"),
            ):
                await _call(gateway, "self.display.set_avatar", {"face": face})
                await _call(gateway, "self.robot.set_head_angles",
                            {"yaw": yaw, "pitch": pitch, "speed_dps": 0})
                await asyncio.sleep(0.9)
            await _speak(gateway, "Eh? EH? Full range of motion. Bit of personality. Not bad for an eye on a stick.")
            await _call(gateway, "self.led.set_all", {"r": 255, "g": 100, "b": 0})
            await gateway.sensor_reactor.trigger("hacker")
            await asyncio.sleep(2.0)
            await _call(gateway, "self.display.set_avatar", {"face": "idle"})
            await _call(gateway, "self.led.set_all", {"r": 0, "g": 25, "b": 90})
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Companion demo routine failed: %s", exc)

    asyncio.create_task(_routine())
    return web.json_response({"ok": True, "started": True})


# ---------------------------------------------------------------------------
# handlers — faces roster (Phase 4)
# ---------------------------------------------------------------------------
async def handle_faces_list(request: web.Request) -> web.Response:
    """The known-face roster: name, sample count, has-photo, greeting."""
    return web.json_response({"ok": True, "faces": faces.list_faces()})


async def handle_face_photo(request: web.Request) -> web.Response:
    """Serve a person's reference JPEG."""
    name = request.match_info.get("name", "")
    try:
        path = faces.photo_path(name)
    except faces.FaceError as exc:
        return _err(str(exc), status=exc.status)
    if path is None:
        return _err(f"no reference photo for '{name}'", status=404)
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


async def handle_face_rename(request: web.Request) -> web.Response:
    """Rename a person. Body: {new_name}."""
    name = request.match_info.get("name", "")
    body = await _read_json(request)
    new_name = str(body.get("new_name") or body.get("new") or "")
    try:
        faces.rename(name, new_name)
    except faces.FaceError as exc:
        return _err(str(exc), status=exc.status)
    return web.json_response({"ok": True, "renamed": name, "to": new_name.strip()})


async def handle_face_delete(request: web.Request) -> web.Response:
    """Forget a person entirely."""
    name = request.match_info.get("name", "")
    try:
        faces.delete(name)
    except faces.FaceError as exc:
        return _err(str(exc), status=exc.status)
    return web.json_response({"ok": True, "deleted": name})


async def handle_face_greeting(request: web.Request) -> web.Response:
    """Set/clear a person's custom greeting. Body: {line}."""
    name = request.match_info.get("name", "")
    body = await _read_json(request)
    line = str(body.get("line") or body.get("greeting") or "")
    try:
        faces.set_greeting(name, line)
    except faces.FaceError as exc:
        return _err(str(exc), status=exc.status)
    return web.json_response({"ok": True, "name": name, "greeting": line.strip() or None})


async def handle_face_enroll(request: web.Request) -> web.Response:
    """Teach the current camera view as a new/additional sample for a name.

    Runs the vision loop's ``--enroll`` CLI as a subprocess (it deliberately
    skips the single-instance lock so it can run alongside the live loop).
    Blocks until enrollment finishes, then returns its console output.
    """
    gateway = request.app[GATEWAY_KEY]
    body = await _read_json(request)
    try:
        name = faces._validate_name(str(body.get("name", "")))
    except faces.FaceError as exc:
        return _err(str(exc), status=exc.status)
    if not gateway.esp32.get_status().get("connected"):
        return _err("No ESP32 device connected", status=503)
    samples = _clamp(body.get("samples", 3), 1, 5)

    script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "stackchan-vision-loop.py")
    if not os.path.exists(script):
        return _err("vision loop script not found", status=500)
    cmd = [sys.executable, script, "--enroll", name, "--samples", str(samples)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=os.path.dirname(script),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        return _err("enrollment timed out", status=504)
    except Exception as exc:  # pragma: no cover - defensive
        return _err(f"enrollment failed to start: {exc}", status=500)

    text = (out or b"").decode(errors="replace").strip()
    ok = proc.returncode == 0
    return web.json_response(
        {"ok": ok, "name": name, "returncode": proc.returncode, "output": text[-800:]},
        status=200 if ok else 502,
    )


# ---------------------------------------------------------------------------
# handlers — visitor log (Phase 4)
# ---------------------------------------------------------------------------
async def handle_visitors(request: web.Request) -> web.Response:
    """Recent recognition timeline, newest first."""
    try:
        limit = int(request.rel_url.query.get("limit", "100"))
    except ValueError:
        limit = 100
    limit = max(1, min(limit, visitor_log.MAX_ENTRIES))
    return web.json_response({"ok": True, "visitors": visitor_log.read(limit)})


async def handle_visitor_thumb(request: web.Request) -> web.Response:
    """Serve a timeline thumbnail by its id."""
    entry_id = request.match_info.get("id", "")
    path = visitor_log.thumb_path(f"{entry_id}.jpg")
    if path is None:
        return _err("no such thumbnail", status=404)
    return web.FileResponse(path, headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------------------
# handlers — orientation
# ---------------------------------------------------------------------------
async def handle_orientation_get(request: web.Request) -> web.Response:
    """Report whether Wheatley is set to upside-down (inverted) mounting."""
    return web.json_response({"ok": True, "upside_down": _is_upside_down()})


async def handle_orientation_set(request: web.Request) -> web.Response:
    """Set the inverted-mount flag. Body: {upside_down: bool}."""
    body = await _read_json(request)
    val = bool(body.get("upside_down"))
    settings = _load_settings()
    settings["upside_down"] = val
    _save_settings(settings)
    return web.json_response({"ok": True, "upside_down": val})


# ---------------------------------------------------------------------------
# handlers — scan zone (ArUco tray Wheatley looks down at)
# ---------------------------------------------------------------------------
_ACTIVITY_FILE = os.path.join(tempfile.gettempdir(), "stackchan-activity")

# Head-search grid for acquiring the tray (found live: all 4 land ~yaw0/pitch72).
_SCAN_PITCHES = (68, 70, 72, 74, 76, 78)
_SCAN_YAWS = (0, 3, 6, 9)


def _mark_active() -> None:
    """Poke the idle loop's activity file so idle-wander holds still mid-scan."""
    try:
        with open(_ACTIVITY_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


async def _look(gateway: "Gateway", yaw: int, pitch: int) -> None:
    _mark_active()
    await _call(gateway, "self.robot.set_head_angles",
                {"yaw": _clamp(yaw, -80, 80), "pitch": _clamp(pitch, 10, 80), "speed_dps": 250})
    await asyncio.sleep(1.0)


async def _scan_capture(gateway: "Gateway"):
    """Take a photo and detect markers. Returns (raw_jpeg | None, centers dict)."""
    from . import scan_zone

    path, error = await _take_photo_path(gateway, "scan zone")
    if error:
        return None, {}
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception:
        return None, {}
    return raw, scan_zone.detect_markers(raw)


async def _scan_goto(gateway: "Gateway") -> bool:
    """Move the head to the saved zone pose. Returns False if none saved."""
    from . import scan_zone

    pose = scan_zone.load_pose()
    if not pose:
        return False
    await _look(gateway, int(pose["yaw"]), int(pose["pitch"]))
    return True


async def handle_scan_acquire(request: web.Request) -> web.Response:
    """Search the head for a pose where all 4 tray markers are in frame; save it."""
    gateway = request.app[GATEWAY_KEY]
    if not gateway.esp32.get_status().get("connected"):
        return _err("No ESP32 device connected", status=503)
    from . import scan_zone

    best = (-1, None, None)  # (count, yaw, pitch)
    for pitch in _SCAN_PITCHES:
        for yaw in _SCAN_YAWS:
            await _look(gateway, yaw, pitch)
            _, centers = await _scan_capture(gateway)
            n = len(centers)
            if n > best[0]:
                best = (n, yaw, pitch)
            if n == 4:
                scan_zone.save_pose(yaw, pitch)
                return web.json_response({"ok": True, "found": 4, "yaw": yaw, "pitch": pitch})
        if best[0] == 4:
            break
    if best[1] is not None:
        scan_zone.save_pose(best[1], best[2])  # remember best-so-far for a retry
    return web.json_response({
        "ok": False, "found": best[0], "yaw": best[1], "pitch": best[2],
        "detail": "couldn't fit all 4 markers — move Wheatley back / centre the tray, then retry",
    })


async def handle_scan_reference(request: web.Request) -> web.Response:
    """Capture the current (empty) zone as the occupancy reference."""
    gateway = request.app[GATEWAY_KEY]
    if not gateway.esp32.get_status().get("connected"):
        return _err("No ESP32 device connected", status=503)
    from . import scan_zone

    await _scan_goto(gateway)
    raw, centers = await _scan_capture(gateway)
    if not scan_zone.has_all(centers):
        return _err(f"need all 4 markers to set reference; saw {sorted(centers)} — run /api/scan/acquire", status=409)
    flat = scan_zone.rectify(raw, centers)
    if flat is None:
        return _err("rectify failed", status=500)
    scan_zone.save_reference(flat)
    return web.json_response({"ok": True, "saved": True})


async def handle_scan_photo(request: web.Request) -> web.Response:
    """Move to the zone, capture, and return the flat top-down JPEG (+ occupancy headers)."""
    gateway = request.app[GATEWAY_KEY]
    if not gateway.esp32.get_status().get("connected"):
        return _err("No ESP32 device connected", status=503)
    from . import scan_zone

    await _scan_goto(gateway)
    raw, centers = await _scan_capture(gateway)
    if not scan_zone.has_all(centers):
        return _err(f"zone not fully visible; saw {sorted(centers)} — run /api/scan/acquire", status=409)
    flat = scan_zone.rectify(raw, centers)
    if flat is None:
        return _err("rectify failed", status=500)
    occupied, score = scan_zone.occupancy(flat, scan_zone.load_reference())
    return web.Response(body=flat, content_type="image/jpeg", headers={
        "Cache-Control": "no-store",
        "X-Occupied": str(occupied).lower(),
        "X-Occupancy-Score": str(score),
    })


async def handle_scan_occupancy(request: web.Request) -> web.Response:
    """Move to the zone and report empty-vs-occupied (JSON only)."""
    gateway = request.app[GATEWAY_KEY]
    if not gateway.esp32.get_status().get("connected"):
        return _err("No ESP32 device connected", status=503)
    from . import scan_zone

    await _scan_goto(gateway)
    raw, centers = await _scan_capture(gateway)
    if not scan_zone.has_all(centers):
        return _err(f"zone not fully visible; saw {sorted(centers)}", status=409)
    flat = scan_zone.rectify(raw, centers)
    occupied, score = scan_zone.occupancy(flat, scan_zone.load_reference())
    return web.json_response({
        "ok": True, "occupied": occupied, "score": score,
        "has_reference": scan_zone.has_reference(),
    })


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------
def create_companion_app(
    gateway: "Gateway | None" = None,
    token: str = "",
) -> web.Application:
    """Build the companion API aiohttp application.

    ``token`` is the optional Bearer token; when empty, requests are accepted
    unauthenticated (local-dev fallback, matching the capture/PCM endpoints).
    """
    app = web.Application(middlewares=[_auth_middleware])
    app[GATEWAY_KEY] = gateway
    app[COMPANION_TOKEN_KEY] = token
    app[START_TS_KEY] = time.time()

    r = app.router
    r.add_get("/api/health", handle_health)
    r.add_get("/api/status", handle_status)
    r.add_get("/api/head", handle_head_get)
    r.add_post("/api/head", handle_head)
    r.add_post("/api/torque", handle_torque)
    r.add_post("/api/avatar", handle_avatar)
    r.add_post("/api/leds", handle_leds)
    r.add_post("/api/volume", handle_volume)
    r.add_post("/api/brightness", handle_brightness)
    r.add_post("/api/motor", handle_motor)
    r.add_get("/api/say/categories", handle_say_categories)
    r.add_post("/api/say/preset", handle_say_preset)
    r.add_post("/api/say", handle_say)
    r.add_get("/api/camera/snapshot", handle_camera_snapshot)
    r.add_get("/api/camera/meta", handle_camera_meta)
    r.add_post("/api/vision/ask", handle_vision_ask)
    # Faces roster. enroll is registered before {name} so it isn't shadowed.
    r.add_get("/api/faces", handle_faces_list)
    r.add_post("/api/faces/enroll", handle_face_enroll)
    r.add_get("/api/faces/{name}/photo", handle_face_photo)
    r.add_post("/api/faces/{name}/rename", handle_face_rename)
    r.add_put("/api/faces/{name}/greeting", handle_face_greeting)
    r.add_delete("/api/faces/{name}", handle_face_delete)
    r.add_get("/api/visitors", handle_visitors)
    r.add_get("/api/visitors/{id}/thumb", handle_visitor_thumb)
    r.add_get("/api/orientation", handle_orientation_get)
    r.add_post("/api/orientation", handle_orientation_set)
    r.add_post("/api/scan/acquire", handle_scan_acquire)
    r.add_put("/api/scan/reference", handle_scan_reference)
    r.add_get("/api/scan/photo", handle_scan_photo)
    r.add_get("/api/scan/occupancy", handle_scan_occupancy)
    r.add_post("/api/react/{behavior}", handle_react)
    r.add_post("/api/demo", handle_demo)
    return app
