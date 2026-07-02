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

import json
import logging
import time
from typing import TYPE_CHECKING, Any

from aiohttp import web

from . import phrase_pick

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
    # Match sensor_reactor's safe envelope: yaw -80..80, pitch 10..80.
    args = {
        "yaw": _clamp(body.get("yaw", 0), -80, 80),
        "pitch": _clamp(body.get("pitch", 45), 10, 80),
        "speed_dps": _clamp(body.get("speed", 0), 0, 1000),
    }
    _, error = await _call(gateway, "self.robot.set_head_angles", args)
    if error:
        return _err(str(error.get("message", error)), status=502)
    return web.json_response({"ok": True, **args})


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
    r.add_post("/api/react/{behavior}", handle_react)
    r.add_post("/api/demo", handle_demo)
    return app
