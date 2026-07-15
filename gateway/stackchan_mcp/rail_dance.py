"""Wheatley's rail dance — shared behaviour for voice and reactor triggers.

Port of the standalone ``rail_dance.py`` choreography script into a
gateway-side module so it can be triggered by:

- the voice bridge (``stackchan-voice-bridge.py``), when a wake-word
  utterance sniffs as a dance request (see :func:`is_dance_request`), and
- the sensor reactor (``GET /react/dance`` on the capture server :8766),
  so the companion app can fire it too.

Design notes:

- The routine talks to the gateway over MCP-HTTP (127.0.0.1:8767/mcp)
  exactly like the original script and every other helper process
  (stackchan-idle.py, voice bridge...). This is safe even when called
  from inside the gateway process itself, BECAUSE the routine always
  runs on its own daemon thread — never on the asyncio event loop that
  serves the HTTP/WS endpoints. :func:`try_start_dance` does one quick
  synchronous status check and returns; the ~40 s choreography happens
  in the background.
- Single-flight: a module-level flag means only one dance at a time,
  regardless of trigger source ("busy" outcome for the loser).
- Guarded: the rail must be linked + homed + not crashed
  (self.rail.status) or the outcome is "not_ready" and nothing moves.
  Callers turn that into the spoken "I would, but the rail's not
  feeling it" style reply.
- Choreography vs. the original script: MORE head movement (user
  feedback) — ambient yaw swings widened to +/-30, brief +/-60
  flourishes, and a head bob between every rail nudge. Pitch stays in
  15..60 (firmware-safe; reactor clamps at 10..80).

No third-party dependencies: urllib + threading only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.request

from .movement import MovementController

logger = logging.getLogger(__name__)

GATEWAY_MCP_URL = "http://127.0.0.1:8767/mcp"

# ─── intent sniffing ─────────────────────────────────────────────────────────

# Generous-but-safe: fires on "dance (for me)", "do a little dance", "can
# you dance", "dancing", "boogie", "shimmy", "bust a move", "show me your
# moves"... but never crashes (callers wrap in try/except anyway) and
# backs off on obvious negations ("stop dancing", "don't dance").
_DANCE_RE = re.compile(
    r"\b(?:"
    r"dan(?:ce|ces|cing|ced)"
    r"|boog(?:ie|y|ying)"
    r"|shimm(?:y|ies|ying)"
    r"|jig"
    r"|bust\s+(?:a|some)\s+moves?"
    r"|shake\s+(?:your|those|some)\s+(?:stuff|groove|moves?|booty|hips)"
    r"|show\s+(?:us|me)\s+(?:your|some)\s+moves"
    r")\b",
    re.IGNORECASE,
)
_DANCE_NEG_RE = re.compile(
    r"\b(?:stop|quit|no\s+more|don'?t|do\s+not|never|enough)\b"
    r"[\s\w,]{0,24}?"
    r"\b(?:danc\w*|boog\w*|shimm\w*|jig|moves?)\b",
    re.IGNORECASE,
)


def is_dance_request(text: str | None) -> bool:
    """True when a transcript looks like a request for the rail dance."""
    if not text:
        return False
    if _DANCE_NEG_RE.search(text):
        return False
    return bool(_DANCE_RE.search(text))


# ─── minimal MCP-over-HTTP client (same shape as the original script) ────────


class _McpClient:
    """One MCP-HTTP session; tolerant of both SSE and plain-JSON replies."""

    def __init__(self, url: str = GATEWAY_MCP_URL, timeout: float = 30.0) -> None:
        self.url = url
        self.timeout = timeout
        self.session_id: str | None = None
        self._next_id = 1

    def _post(self, body: dict) -> dict | None:
        req = urllib.request.Request(
            self.url,
            data=json.dumps(body).encode(),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        if self.session_id:
            req.add_header("mcp-session-id", self.session_id)
        resp = urllib.request.urlopen(req, timeout=self.timeout)
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        raw = resp.read().decode("utf-8", "replace")
        data_lines = [
            line[5:].strip()
            for line in raw.splitlines()
            if line.strip().startswith("data:")
        ]
        if data_lines:
            return json.loads(data_lines[-1])
        return json.loads(raw) if raw.strip() else None

    def initialize(self) -> None:
        self._post({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "rail-dance", "version": "2"},
            },
        })
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, name: str, args: dict | None = None) -> dict:
        call_id = self._next_id
        self._next_id += 1
        r = self._post({
            "jsonrpc": "2.0", "id": call_id, "method": "tools/call",
            "params": {"name": name, "arguments": args or {}},
        })
        try:
            return json.loads(r["result"]["content"][0]["text"])
        except Exception:
            return {}


# ─── rail helpers ────────────────────────────────────────────────────────────


# One shared MovementController for the whole routine — this is the single path
# for head + rail motion (Phase-1 behaviour engine). It owns the calibrated
# limits, orientation signs and crash-aware rail waits, so the choreography just
# asks for poses and the controller does the clamping/mapping. The old inline
# head helper called ``self.robot.set_head_angles`` — a DEVICE tool NOT exposed
# at the gateway MCP boundary — so every head move here silently failed; routing
# through the controller's ``move_head`` path is the fix.
_mc: MovementController | None = None


def _controller() -> MovementController:
    global _mc
    if _mc is None:
        # verify_feedback off (fast choreography); respect_quiesce off (a dance
        # is an explicit request — it should run even mid-idle-chatter).
        _mc = MovementController(url=GATEWAY_MCP_URL)
    return _mc


def _rail_status(c: _McpClient) -> dict:
    return c.call("self.rail.status")


def _is_ready(st: dict) -> bool:
    return bool(st.get("linked") and st.get("homed") and not st.get("crashed"))


def rail_ready() -> bool:
    """Quick synchronous check: linked + homed + not crashed.

    Never raises — a gateway/HTTP failure just reads as "not ready".
    """
    try:
        c = _McpClient(timeout=8.0)
        c.initialize()
        return _is_ready(_rail_status(c))
    except Exception as exc:
        logger.warning("rail_dance: status check failed: %s", exc)
        return False


def _wait_parked(c: _McpClient, timeout_s: float = 8.0) -> bool:
    """Poll until the rail stops moving. False on crash."""
    for _ in range(int(timeout_s * 2)):
        time.sleep(0.5)
        st = _rail_status(c)
        if st.get("crashed"):
            return False
        if st.get("moving") is False:
            return True
    return True


def _move(c: _McpClient, mm: int) -> bool:
    # Absolute rail move via the controller (crash-aware park wait built in).
    return _controller().roll_to(int(mm))


def _move_with_sweep(
    c: _McpClient, mm: int, sweep: tuple[tuple[int, int], ...],
    timeout_s: float = 10.0,
) -> bool:
    """Glide the rail while sweeping the head through (yaw, pitch) poses.

    Kicks off the move without blocking, sweeps the head through the poses as
    the carriage travels, then waits for it to park (crash-aware) — all through
    the shared controller.
    """
    mc = _controller()
    if not mc.roll_to(int(mm), wait=False):
        return False
    for (yaw, pitch) in sweep:
        _head(c, yaw, pitch)
        time.sleep(0.5)
        if mc.is_crashed():
            return False
    return mc._wait_parked(timeout_s)


def _head(c: _McpClient, yaw: int, pitch: int) -> None:
    # Choreography poses are PHYSICAL servo values -> look_physical (clamped +
    # gated centrally). This is THE fix: the old call used the device-only
    # ``self.robot.set_head_angles`` name, which isn't an MCP tool at the
    # gateway, so head moves silently no-op'd.
    _controller().look_physical(int(yaw), int(pitch), speed="mid")


def _face(c: _McpClient, name: str) -> None:
    c.call("self.display.set_avatar", {"face": name})


def _say(c: _McpClient, text: str) -> None:
    c.call("say", {"text": text})


# ─── the routine ─────────────────────────────────────────────────────────────

_dance_gate = threading.Lock()
_dancing = False


def is_dancing() -> bool:
    return _dancing


def try_start_dance(*, intro: bool = False, start_delay_s: float = 0.0) -> str:
    """Check the rail and kick off the dance on a daemon thread.

    Returns one of:
      "started"   — dance thread launched.
      "busy"      — a dance is already running.
      "not_ready" — rail not linked/homed, crashed, or gateway unreachable.

    ``intro``: speak the "nobody panic" intro line (used by the reactor
    path; the voice path speaks its own acknowledgement instead).
    ``start_delay_s``: worker sleeps this long before its first action,
    so a caller-spoken acknowledgement gets a head start (the gateway's
    tts_lock serialises speech anyway; this just keeps the choreography
    from starting under the ack).
    """
    global _dancing
    with _dance_gate:
        if _dancing:
            return "busy"
        if not rail_ready():
            return "not_ready"
        _dancing = True
    threading.Thread(
        target=_dance_worker,
        kwargs={"intro": intro, "start_delay_s": start_delay_s},
        daemon=True,
        name="rail-dance",
    ).start()
    return "started"


# Max yaw amplitude for the whips/spin-gag. Firmware currently clamps yaw to
# +/-90; the yaw-unlock flash raises that to +/-135 — bump the env then
# (STACKCHAN_DANCE_MAX_YAW=130) and the choreography grows into it.
_DANCE_YAW = int(os.environ.get("STACKCHAN_DANCE_MAX_YAW", "88"))


def _whip(c: "_McpClient", yaw: int, pitch: int) -> None:
    """A fast head throw — physical pose at high speed, via the controller."""
    try:
        _controller().look_physical(int(yaw), int(pitch), speed=400)
    except Exception:
        pass


def _dance_worker(intro: bool, start_delay_s: float) -> None:
    global _dancing
    c: _McpClient | None = None
    try:
        if start_delay_s > 0:
            time.sleep(start_delay_s)
        c = _McpClient()
        c.initialize()

        # A commanded task (RAIL_ARBITER.md P2) outranks passive people-
        # tracking (P3): claim the rail so look_at yields until the dance ends.
        try:
            from stackchan_mcp import rail_arbiter
            rail_arbiter.claim("task", 2)
        except Exception:
            pass

        # Re-check right before moving — the ack window is long enough
        # for a crash/unlink to have happened since try_start_dance().
        if not _is_ready(_rail_status(c)):
            logger.warning("rail_dance: rail went not-ready before start; aborting")
            return

        if intro:
            _say(c, "Right! Nobody panic, but I am about to dance. On rails. "
                    "Prepare yourselves.")
            time.sleep(0.8)

        logger.info("rail_dance: taking the stage")
        _face(c, "happy")
        Y = _DANCE_YAW
        # Warm-up: three fast full whips while still parked (v2: BIG head).
        for wy in (-Y, Y, -Y):
            _whip(c, wy, 50)
            time.sleep(0.35)
        _whip(c, 0, 45)
        if not _move(c, 250):
            raise RuntimeError("rail crashed during opening glide")

        logger.info("rail_dance: shimmy")
        _say(c, "And a one, and a two...")
        # Nudge + FULL yaw throw + a deep bob between every nudge (v2 tempo).
        for delta, yaw in ((60, -Y), (-60, Y), (45, -(Y - 15)), (-45, Y - 15),
                           (30, -(Y - 30)), (-30, Y - 30)):
            c.call("self.rail.nudge_mm", {"mm": delta})
            _whip(c, yaw, 25)          # throw across + deep dip
            time.sleep(0.4)
            _whip(c, yaw, 60)          # bob right back up
            time.sleep(0.4)

        # The spin gag: he TRIES to spin — three violent alternating whips —
        # then admits the neck only goes so far. Peak Wheatley.
        logger.info("rail_dance: spin attempt")
        _say(c, "Right — SPIN! Spinning! Here we go!")
        for wy in (-Y, Y, -Y, Y):
            _whip(c, wy, 45)
            time.sleep(0.32)
        _whip(c, 0, 45)
        _face(c, "sad")
        _say(c, "...Nope. That's the whole neck. That's all of it.")
        time.sleep(0.6)
        _face(c, "happy")

        logger.info("rail_dance: the big glide")
        _say(c, "Daaa, da-da-daaaa! I'm a robot on raaails!")
        _head(c, -45, 45)
        if not _move_with_sweep(c, 520,
                                sweep=((-45, 50), (45, 40), (-30, 55), (30, 45))):
            raise RuntimeError("rail crashed during big glide")
        # Full-tilt flourish at max amplitude.
        _whip(c, -_DANCE_YAW, 50)
        time.sleep(0.4)
        _whip(c, _DANCE_YAW, 50)
        time.sleep(0.4)
        _whip(c, 0, 45)

        logger.info("rail_dance: glide home-ish")
        if not _move_with_sweep(c, 280, sweep=((25, 55), (-25, 35), (15, 50))):
            raise RuntimeError("rail crashed during return glide")
        _head(c, 0, 45)

        logger.info("rail_dance: the bow")
        _face(c, "sad")            # squint ~ stage humility
        _head(c, 0, 15)            # bow down toward the audience
        time.sleep(1.2)
        _head(c, 0, 45)
        _face(c, "happy")
        _say(c, "Thank you! Thank you. I'm here all week. Literally. "
                "I live on this rail.")
        time.sleep(1.2)
        _face(c, "idle")
        _head(c, 0, 20)            # settle looking roughly at the user
        logger.info("rail_dance: done, parked at ~280mm")
    except Exception as exc:
        logger.warning("rail_dance: aborted: %s", exc)
        # Best-effort recovery so he doesn't freeze mid-pose.
        try:
            if c is not None:
                _say(c, "Ow. Right. That's the dance over, apparently.")
                _face(c, "idle")
                _head(c, 0, 20)
        except Exception:
            pass
    finally:
        _dancing = False
        try:
            from stackchan_mcp import rail_arbiter
            rail_arbiter.release("task")
        except Exception:
            pass
