"""Wheatley Behaviour Engine — Phase 1: the centralised MovementController.

ONE importable module that is the single path for ALL head + rail motion.
It owns, in ONE place:

- **Calibrated limits** — yaw ``-130..160``, pitch ``5..85`` (the exact bounds
  the gateway's ``move_head`` MCP tool enforces; see
  ``stackchan_mcp/stdio_server.py``). Rail ``0..896`` mm absolute, nudge
  ``+/-100`` mm.
- **Orientation** — Wheatley hangs *inverted* on the rail. A 180 deg roll
  mirrors both servo axes, so a "look up / look right" means a *different*
  physical servo value depending on which way up he is. The idle loop already
  derives ``PITCH_UP_SIGN`` / ``YAW_RIGHT_SIGN`` from a shared ``upside_down``
  flag; we read the *same* flag (same file + env overrides) so a ``look()``
  means the same visual thing regardless of mount.
- **Pose** — ``pose()`` reads real servo feedback (``get_head_angles``) and
  rail status, cached ~500 ms.
- **Safety gating** — ``can_move()`` refuses big moves when the rail is
  crashed; ``respect_quiesce`` optionally no-ops head moves while a fresh
  ``stackchan-busy-devicechat`` marker says a voice turn is live.

This fixes a real class of bug: three separate yaw clamps scattered across the
code, and ``rail_dance.py`` calling a WRONG tool name (``self.robot.set_head_angles``
is a *device* tool, not exposed at the gateway MCP boundary — the head moves
silently failed). Everything routes through ``move_head`` here, clamped once.

Coordinate convention for the semantic primitives (``look`` / ``look_rel`` /
gestures):

- ``yaw``   — visual horizontal. ``+`` = toward Wheatley's RIGHT, ``0`` =
  straight ahead. Mount-independent.
- ``pitch`` — visual vertical *offset from the forward/level gaze*. ``+`` = UP,
  ``0`` = looking level at the user, ``-`` = down. Mount-independent.

Internally these are converted to *physical* servo values with the orientation
signs and an orientation-aware rest pitch, then clamped to the calibrated
physical limits. Callers that genuinely have raw servo numbers (e.g. a ported
choreography table) can use :meth:`look_physical` to skip the semantic mapping
but still get central clamping + safety + feedback.

No third-party dependencies: stdlib only (urllib + json + glob + time). The
controller is not itself threaded; long sequences (e.g. rail_dance) run it on a
caller-owned daemon thread, exactly as the standalone scripts do.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import time
import urllib.request

logger = logging.getLogger(__name__)

# ─── constants: the single source of truth for limits ────────────────────────

GATEWAY_MCP_URL = os.environ.get("STACKCHAN_MCP_URL", "http://127.0.0.1:8767/mcp")

# Calibrated 2026-07-13 from a servo-feedback sweep (real stops -135/+166; we
# clamp just inside). These MUST match the guard in stdio_server.move_head — if
# the firmware calibration changes, change it there and mirror it here.
YAW_MIN, YAW_MAX = -130, 160
PITCH_MIN, PITCH_MAX = 5, 85          # M5Stack-recommended operating band

# Rail soft limits (self.rail.move_mm 0..896; self.rail.nudge_mm +/-100).
RAIL_MIN_MM, RAIL_MAX_MM = 0, 896
NUDGE_MAX_MM = 100

# Rail as a gaze axis. +mm runs AWAY from the home switch, which sits on his
# RIGHT — so a carriage move toward +mm swings his body (and thus his gaze) to
# his LEFT. RAIL_LOOK_SIGN lets a rig flip that if it reads backwards. These
# mirror stackchan-idle's RAIL_LOOK_SIGN / rail-follow constants so the two
# agree about which way "toward the user" is.
RAIL_LOOK_SIGN = int(os.environ.get("STACKCHAN_RAIL_LOOK_SIGN", "1"))
# Rail-assisted face-follow: when head yaw pins at a limit and the target is
# still off-centre the SAME way, roll the carriage toward them.
RAIL_FOLLOW_SIGN = int(os.environ.get("STACKCHAN_RAIL_FOLLOW_SIGN", "1"))
RAIL_FOLLOW_NUDGE_MM = int(os.environ.get("STACKCHAN_RAIL_FOLLOW_NUDGE_MM", "50"))
RAIL_FOLLOW_DX = 0.25          # target must still be this far off-centre to roll
# How much visual yaw one mm of carriage travel is "worth" when deciding
# head-vs-rail split in look_at(). ~896 mm of desk covers roughly the head's
# usable yaw span; this is a soft heuristic for coordination, not a calibration.
RAIL_YAW_PER_MM = float(os.environ.get("STACKCHAN_RAIL_YAW_PER_MM", "0.30"))
# Freshness gate for a rail move to be sane right now (idle loop uses the same
# 3 s window): status younger than this, homed, not crashed, not moving.
RAIL_STATUS_FRESH_MS = 3000
# Don't bother firing the motor for a smaller move than this (mirrors idle).
RAIL_MIN_DRIFT_MM = int(os.environ.get("STACKCHAN_RAIL_MIN_DRIFT_MM", "60"))

# The physical "look level at the user" pitch differs by mount (a 180 roll
# inverts pitch): inverted the user sits ABOVE the flipped camera so level gaze
# is a LOW pitch; upright it's a gentler forward value. Mirrors the idle loop's
# REST_PITCH_* so a semantic pitch=0 lands on the same physical pose everywhere.
REST_PITCH_INVERTED = int(os.environ.get("STACKCHAN_REST_PITCH_INVERTED", "11"))
REST_PITCH_UPRIGHT = int(os.environ.get("STACKCHAN_REST_PITCH_UPRIGHT", "35"))

# Speed presets accepted by the move_head MCP tool (mirrors stdio_server).
SPEED_PRESETS = ("low", "mid", "high")
DEFAULT_SPEED = os.environ.get("STACKCHAN_MOVE_SPEED", "mid")

# Shared orientation flag — the SAME file the companion server, vision loop and
# idle loop read. Lives at the gateway root (one dir up from this package).
_GATEWAY_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.path.join(_GATEWAY_ROOT, "companion_settings.json")

# Safety markers live in %TEMP%. The devicechat marker is refreshed ~every 15 s
# for the whole voice turn; older than 120 s = orphaned (gateway died mid-turn)
# and must NOT gate movement forever.
_TEMP = os.environ.get("TEMP", os.environ.get("TMP", "."))
DEVICECHAT_MARKER = os.path.join(_TEMP, "stackchan-busy-devicechat")
DEVICECHAT_STALE_S = 120.0
BUSY_MARKER_GLOB = os.path.join(_TEMP, "stackchan-busy-*")
BUSY_STALE_S = 30 * 60

POSE_CACHE_S = 0.5           # pose() re-reads no more often than this
FEEDBACK_TOLERANCE_DEG = 8   # servo undershoots ~4 deg; only log beyond this


def _clamp(v: int, lo: int, hi: int) -> int:
    return lo if v < lo else hi if v > hi else v


# ─── orientation (centralised; read the SAME flag as the idle loop) ──────────


def read_upside_down() -> bool:
    """True when Wheatley is mounted inverted (hanging on the rail).

    Reads the shared ``upside_down`` flag from ``companion_settings.json``
    (written by the companion server / vision-loop ``--calibrate-flip``),
    falling back to the ``STACKCHAN_UPSIDE_DOWN`` env var. Never raises.
    """
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if "upside_down" in d:
            return bool(d["upside_down"])
    except Exception:
        pass
    return os.environ.get("STACKCHAN_UPSIDE_DOWN", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def orientation_signs() -> tuple[int, int]:
    """``(PITCH_UP_SIGN, YAW_RIGHT_SIGN)`` for the current mount.

    Both ``-1`` while inverted, ``+1`` upright. Env overrides
    (``STACKCHAN_PITCH_UP_SIGN`` / ``STACKCHAN_YAW_RIGHT_SIGN``) win, exactly
    like ``stackchan-idle._orientation_signs`` — kept sign-for-sign identical so
    the two agree.
    """
    inv = read_upside_down()
    ep = os.environ.get("STACKCHAN_PITCH_UP_SIGN")
    ey = os.environ.get("STACKCHAN_YAW_RIGHT_SIGN")
    p = int(ep) if ep else (-1 if inv else 1)
    y = int(ey) if ey else (-1 if inv else 1)
    return p, y


# ─── minimal MCP-over-HTTP client (SSE-or-plain-JSON tolerant) ───────────────


class McpClient:
    """One MCP-HTTP session; tolerant of both SSE and plain-JSON replies.

    Same shape as ``rail_dance._McpClient`` / ``stackchan-idle.MCPSession`` —
    stdlib urllib, session-id handling, ``data:`` line extraction.
    """

    def __init__(self, url: str = GATEWAY_MCP_URL, timeout: float = 15.0) -> None:
        self.url = url
        self.timeout = timeout
        self.session_id: str | None = None
        self._next_id = 1
        self._initialized = False

    def _post(self, body: dict, timeout: float | None = None) -> dict | None:
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
        resp = urllib.request.urlopen(req, timeout=timeout or self.timeout)
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

    def initialize(self) -> bool:
        """Handshake. Returns True on success, False on any transient error."""
        try:
            self._post({
                "jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "movement-controller", "version": "1"},
                },
            })
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self._initialized = True
            return True
        except Exception as exc:
            logger.warning("movement: MCP initialize failed: %s", exc)
            return False

    def call(self, name: str, args: dict | None = None,
             timeout: float | None = None) -> dict | None:
        """Call a tool, returning the parsed JSON payload.

        Returns ``None`` on any transient failure (network, no session, bad
        payload) — callers treat that as "couldn't do it this time", never a
        crash. Lazily initializes the session on first use.
        """
        if not self._initialized and not self.initialize():
            return None
        call_id = self._next_id
        self._next_id += 1
        try:
            r = self._post({
                "jsonrpc": "2.0", "id": call_id, "method": "tools/call",
                "params": {"name": name, "arguments": args or {}},
            }, timeout=timeout)
        except Exception as exc:
            logger.warning("movement: tools/call %s failed: %s", name, exc)
            return None
        try:
            content = ((r or {}).get("result") or {}).get("content", [])
            if content:
                return json.loads(content[0].get("text", "{}"))
        except Exception as exc:
            logger.debug("movement: could not parse %s reply: %s", name, exc)
        return {}


# ─── the controller ──────────────────────────────────────────────────────────


class MovementController:
    """Single path for all head + rail motion.

    Construction never touches the device. Every method degrades gracefully:
    on a transient MCP error they return ``False``/``None`` and log, never
    raise. Limits and orientation are resolved ONCE here and applied to every
    primitive, so callers can't re-introduce a stray clamp or a flipped sign.

    Args:
        client: an :class:`McpClient` (or compatible ``.call(name, args)``
            object). If ``None``, one is created against ``url``.
        url: MCP endpoint (default the local gateway).
        default_speed: preset str or int dps forwarded to ``move_head``.
        respect_quiesce: when True, semantic + physical head moves are skipped
            while a fresh devicechat (voice-turn) marker is present. Soft: it's
            a default a caller can override per-call via ``force=``.
        verify_feedback: when True, ``look`` reads ``get_head_angles`` after a
            move and logs if the servo lands >tolerance from target.
    """

    def __init__(
        self,
        client: McpClient | None = None,
        *,
        url: str = GATEWAY_MCP_URL,
        default_speed=DEFAULT_SPEED,
        respect_quiesce: bool = False,
        verify_feedback: bool = False,
        yaw_limits: tuple[int, int] = (YAW_MIN, YAW_MAX),
        pitch_limits: tuple[int, int] = (PITCH_MIN, PITCH_MAX),
        rail_limits: tuple[int, int] = (RAIL_MIN_MM, RAIL_MAX_MM),
    ) -> None:
        self.mcp = client if client is not None else McpClient(url)
        self.default_speed = default_speed
        self.respect_quiesce = respect_quiesce
        self.verify_feedback = verify_feedback
        self.yaw_min, self.yaw_max = yaw_limits
        self.pitch_min, self.pitch_max = pitch_limits
        self.rail_min, self.rail_max = rail_limits
        # Orientation resolved once at construction; refresh() re-reads it.
        self.pitch_up_sign, self.yaw_right_sign = orientation_signs()
        self.inverted = read_upside_down()
        self._pose_cache: dict | None = None
        self._pose_ts = 0.0

    # ── orientation / config introspection ──────────────────────────────

    def refresh_orientation(self) -> None:
        """Re-read the shared upside_down flag (mount may have changed)."""
        self.pitch_up_sign, self.yaw_right_sign = orientation_signs()
        self.inverted = read_upside_down()

    @property
    def rest_pitch(self) -> int:
        """Physical pitch that looks LEVEL at the user, for this mount."""
        return REST_PITCH_INVERTED if self.inverted else REST_PITCH_UPRIGHT

    def limits(self) -> dict:
        """The resolved limits + orientation — handy for logging / the arbiter."""
        return {
            "yaw": [self.yaw_min, self.yaw_max],
            "pitch": [self.pitch_min, self.pitch_max],
            "rail_mm": [self.rail_min, self.rail_max],
            "nudge_max_mm": NUDGE_MAX_MM,
            "inverted": self.inverted,
            "pitch_up_sign": self.pitch_up_sign,
            "yaw_right_sign": self.yaw_right_sign,
            "rest_pitch": self.rest_pitch,
            "default_speed": self.default_speed,
        }

    # ── coordinate mapping (the ONE place signs/limits are applied) ──────

    def to_physical(self, yaw: int, pitch: int) -> tuple[int, int]:
        """Map VISUAL (yaw+=his right, pitch+=up-from-level) -> physical servo.

        Applies the orientation signs about neutral (yaw 0) and the
        orientation-aware rest pitch, then clamps to the calibrated physical
        limits. This is the single chokepoint every semantic move flows
        through.
        """
        phys_yaw = self.yaw_right_sign * int(yaw)
        phys_pitch = self.rest_pitch + self.pitch_up_sign * int(pitch)
        return (
            _clamp(phys_yaw, self.yaw_min, self.yaw_max),
            _clamp(phys_pitch, self.pitch_min, self.pitch_max),
        )

    # ── safety gating (soft helpers; callers decide) ─────────────────────

    def _marker_fresh(self, path: str, stale_s: float) -> bool:
        try:
            return (time.time() - os.path.getmtime(path)) < stale_s
        except OSError:
            return False

    def is_quiesced(self) -> bool:
        """True while a fresh voice-turn (devicechat) marker is present."""
        return self._marker_fresh(DEVICECHAT_MARKER, DEVICECHAT_STALE_S)

    def is_busy(self) -> bool:
        """True while ANY fresh stackchan-busy-* marker is present.

        (A Claude Code session / reactor turn holding the device.) Soft signal
        for the arbiter; not auto-enforced by the primitives.
        """
        for p in glob.glob(BUSY_MARKER_GLOB):
            if self._marker_fresh(p, BUSY_STALE_S):
                return True
        return False

    def rail_state(self) -> dict:
        """Latest rail status dict (never None; {} on failure)."""
        return self.mcp.call("self.rail.status") or {}

    def is_crashed(self) -> bool:
        return bool(self.rail_state().get("crashed"))

    def can_move(self, *, big: bool = True) -> bool:
        """Gate for whether it's safe to move now.

        ``big`` moves (rail travel, large head throws) are refused while the
        rail reports crashed. Small head fidgets can still run (``big=False``)
        so an expression isn't blocked by a rail fault. Battery is consulted as
        a soft signal only — a critically low reading vetoes big moves.
        """
        if big and self.is_crashed():
            return False
        if big:
            batt = self.battery()
            lvl = (batt or {}).get("level")
            charging = (batt or {}).get("charging")
            if lvl is not None and lvl <= 3 and not charging:
                logger.warning("movement: battery %s%% — refusing big move", lvl)
                return False
        return True

    def battery(self) -> dict | None:
        """``{level, charging}`` from get_device_info, or None on failure."""
        info = self.mcp.call("get_device_info")
        if not info:
            return None
        return info.get("battery") or {}

    # ── pose ─────────────────────────────────────────────────────────────

    def pose(self, *, max_age_s: float = POSE_CACHE_S) -> dict:
        """Current pose: measured head angles + rail status, cached ~500 ms.

        Returns a dict::

            {"yaw": int|None, "pitch": int|None,      # MEASURED servo feedback
             "rail": {...}|{}, "pos_mm": float|None,
             "crashed": bool, "moving": bool, "homed": bool, "ts": float}

        ``yaw``/``pitch`` are the *physical* measured angles (from
        ``get_head_angles`` — real feedback, not an echo). Values are ``None``
        when the read failed this cycle.
        """
        now = time.time()
        if self._pose_cache is not None and (now - self._pose_ts) < max_age_s:
            return self._pose_cache
        head = self.mcp.call("get_head_angles") or {}
        rail = self.rail_state()
        pose = {
            "yaw": head.get("yaw"),
            "pitch": head.get("pitch"),
            "rail": rail,
            "pos_mm": rail.get("pos_mm"),
            "crashed": bool(rail.get("crashed")),
            "moving": bool(rail.get("moving")),
            "homed": bool(rail.get("homed")),
            "ts": now,
        }
        self._pose_cache = pose
        self._pose_ts = now
        return pose

    def _invalidate_pose(self) -> None:
        self._pose_cache = None

    def gaze_pose(self, *, max_age_s: float = POSE_CACHE_S) -> dict:
        """The COMBINED gaze pose: head + rail treated as one pointing system.

        Wheatley's gaze is not just his neck — where the carriage sits on the
        rail rotates his whole body, so the direction he faces is
        ``(rail_pos, head_yaw, head_pitch)`` together. Returns::

            {"head_yaw": int|None, "head_pitch": int|None,   # physical, measured
             "rail_mm": float|None,
             "visual_yaw": float|None,   # head yaw + rail contribution, VISUAL frame
             "visual_pitch": float|None, # head pitch mapped back to visual
             "crashed": bool, "homed": bool, "moving": bool}

        ``visual_yaw`` folds the carriage position into a single "which way is
        he looking" number in the mount-independent visual frame, so a
        behaviour can reason about total gaze without juggling two axes.
        """
        ps = self.pose(max_age_s=max_age_s)
        hy, hp = ps.get("yaw"), ps.get("pitch")
        mm = ps.get("pos_mm")
        # Head physical -> visual (invert the sign mapping used by to_physical).
        vis_yaw = None
        vis_pitch = None
        if hy is not None:
            vis_yaw = self.yaw_right_sign * hy
            # Carriage contribution: +mm swings gaze to his LEFT (negative
            # visual yaw) via RAIL_LOOK_SIGN. Referenced to mid-rail so the
            # centre of the desk reads as ~0 body-rotation.
            if isinstance(mm, (int, float)):
                mid = (self.rail_min + self.rail_max) / 2.0
                vis_yaw += -RAIL_LOOK_SIGN * (mm - mid) * RAIL_YAW_PER_MM
        if hp is not None:
            vis_pitch = self.pitch_up_sign * (hp - self.rest_pitch)
        return {
            "head_yaw": hy,
            "head_pitch": hp,
            "rail_mm": mm,
            "visual_yaw": vis_yaw,
            "visual_pitch": vis_pitch,
            "crashed": ps.get("crashed", False),
            "homed": ps.get("homed", False),
            "moving": ps.get("moving", False),
        }

    # ── head primitives ──────────────────────────────────────────────────

    def _resolve_speed(self, speed):
        s = self.default_speed if speed is None else speed
        # Pass presets straight through; ints straight through; anything else
        # falls back to the default preset so move_head never rejects on speed.
        if isinstance(s, str) and s in SPEED_PRESETS:
            return s
        if isinstance(s, int) and not isinstance(s, bool) and s >= 1:
            return s
        return DEFAULT_SPEED if DEFAULT_SPEED in SPEED_PRESETS else "mid"

    def look_physical(self, yaw: int, pitch: int, *, speed=None,
                      force: bool = False) -> bool:
        """Move the head to RAW physical servo angles (clamped + gated).

        For callers that already have physical values (e.g. a ported
        choreography table). Still goes through the single clamp + quiesce
        gate + optional feedback check, so it's a safe replacement for a raw
        ``move_head`` call. Returns True if the command was sent.
        """
        if self.respect_quiesce and not force and self.is_quiesced():
            logger.debug("movement: quiesced — skipping head move")
            return False
        y = _clamp(int(yaw), self.yaw_min, self.yaw_max)
        p = _clamp(int(pitch), self.pitch_min, self.pitch_max)
        args = {"yaw": y, "pitch": p, "speed": self._resolve_speed(speed)}
        res = self.mcp.call("move_head", args)
        if res is None or (isinstance(res, dict) and res.get("error")):
            logger.warning("movement: move_head failed: %s", res)
            return False
        self._invalidate_pose()
        if self.verify_feedback:
            self._verify(y, p)
        return True

    def look(self, yaw: int, pitch: int, *, speed=None, force: bool = False) -> bool:
        """Look in a VISUAL direction (yaw+=his right, pitch+=up-from-level).

        Mount-independent: the same call means the same visual thing whether
        Wheatley is upright or hanging inverted. Converts to physical via the
        orientation signs + rest pitch, clamps, gates, and moves.
        """
        y, p = self.to_physical(yaw, pitch)
        return self.look_physical(y, p, speed=speed, force=force)

    def look_rel(self, dyaw: int, dpitch: int, *, speed=None,
                 force: bool = False) -> bool:
        """Nudge the gaze by a VISUAL delta from the current measured pose.

        Reads ``get_head_angles`` to find where the head actually is, then adds
        the delta in *physical* space (signs already baked into the current
        reading). Falls back to rest if feedback is unavailable.
        """
        ps = self.pose()
        cy = ps.get("yaw")
        cp = ps.get("pitch")
        if cy is None or cp is None:
            cy, cp = 0, self.rest_pitch
        # dyaw/dpitch are visual; convert the *delta* through the signs.
        phys_dy = self.yaw_right_sign * int(dyaw)
        phys_dp = self.pitch_up_sign * int(dpitch)
        return self.look_physical(cy + phys_dy, cp + phys_dp,
                                  speed=speed, force=force)

    def home_head(self, *, speed=None, force: bool = False) -> bool:
        """Centre the head: yaw 0, level (rest) pitch."""
        return self.look(0, 0, speed=speed, force=force)

    def _verify(self, target_yaw: int, target_pitch: int) -> None:
        """Read servo feedback and log if it lands far from target."""
        time.sleep(0.25)
        head = self.mcp.call("get_head_angles") or {}
        ay, ap = head.get("yaw"), head.get("pitch")
        if ay is None or ap is None:
            return
        if (abs(ay - target_yaw) > FEEDBACK_TOLERANCE_DEG
                or abs(ap - target_pitch) > FEEDBACK_TOLERANCE_DEG):
            logger.info(
                "movement: feedback deviates — target (%d,%d) actual (%s,%s)",
                target_yaw, target_pitch, ay, ap,
            )

    # ── rail primitives (crash-aware, poll-until-parked) ─────────────────

    def rail_ready(self, st: dict | None = None) -> bool:
        """True only when a rail move is sane RIGHT NOW.

        Fresh status (``linked`` alone just means "ever heard this boot", so we
        require ``status_age_ms`` < ~3 s), homed, not crashed, not already
        moving, and the 12 V motor supply present. Mirrors
        ``stackchan-idle._rail_ready`` so both agree on readiness.
        """
        if st is None:
            st = self.rail_state()
        if not st:
            return False
        age = st.get("status_age_ms")
        if not isinstance(age, (int, float)) or age > RAIL_STATUS_FRESH_MS:
            return False
        return (
            bool(st.get("homed"))
            and not st.get("crashed")
            and not st.get("moving")
            and bool(st.get("power_12v"))
        )

    def _wait_parked(self, timeout_s: float = 10.0) -> bool:
        """Poll rail.status until it stops moving. False on crash.

        Mirrors ``rail_dance._wait_parked``: a crash short-circuits to False so
        the caller aborts the rest of a sequence.
        """
        for _ in range(int(timeout_s * 2)):
            time.sleep(0.5)
            st = self.rail_state()
            if st.get("crashed"):
                logger.warning("movement: rail crashed while moving")
                return False
            if st.get("moving") is False:
                return True
        return True

    def roll_to(self, mm: int, *, wait: bool = True,
                timeout_s: float = 12.0) -> bool:
        """Move the carriage to an ABSOLUTE position (mm from home).

        Clamped to the rail soft limits. Refuses if crashed. When ``wait``,
        polls until parked (crash-aware) and returns whether it parked cleanly.
        """
        if not self.can_move(big=True):
            logger.warning("movement: roll_to refused (crashed/low-batt)")
            return False
        target = _clamp(int(mm), self.rail_min, self.rail_max)
        res = self.mcp.call("self.rail.move_mm", {"mm": target})
        if res is None:
            return False
        self._invalidate_pose()
        return self._wait_parked(timeout_s) if wait else True

    def nudge(self, mm: int, *, wait: bool = True, timeout_s: float = 8.0) -> bool:
        """Move the carriage a RELATIVE distance (signed; +=away from home).

        Clamped to ``+/-NUDGE_MAX_MM``. Refuses if crashed; polls until parked.
        """
        if not self.can_move(big=True):
            return False
        delta = _clamp(int(mm), -NUDGE_MAX_MM, NUDGE_MAX_MM)
        res = self.mcp.call("self.rail.nudge_mm", {"mm": delta})
        if res is None:
            return False
        self._invalidate_pose()
        return self._wait_parked(timeout_s) if wait else True

    def rail_home(self, *, wait: bool = True, timeout_s: float = 25.0) -> bool:
        """Two-stage home onto the limit switch (zeroes position)."""
        res = self.mcp.call("self.rail.home")
        if res is None:
            return False
        self._invalidate_pose()
        return self._wait_parked(timeout_s) if wait else True

    def stop(self) -> bool:
        """Immediately stop the carriage and hold (also aborts homing)."""
        res = self.mcp.call("self.rail.stop")
        self._invalidate_pose()
        return res is not None

    # ── composed gestures (build on the primitives; tasteful amplitudes) ──
    # All amplitudes are VISUAL and sit well inside the calibrated limits so
    # the central clamp never has to bite; orientation is handled for free by
    # look().

    def nod(self, times: int = 2, *, depth: int = 14, speed="high") -> bool:
        """Yes — dip the gaze down and back up, ``times`` times."""
        ok = True
        for _ in range(max(1, times)):
            ok &= self.look_rel(0, -depth, speed=speed)
            time.sleep(0.22)
            ok &= self.look_rel(0, depth, speed=speed)
            time.sleep(0.22)
        return ok

    def shake(self, times: int = 2, *, amp: int = 22, speed="high") -> bool:
        """No — swing the gaze left/right around centre, ``times`` times."""
        ok = self.look(0, 0, speed=speed)
        for _ in range(max(1, times)):
            ok &= self.look(-amp, 0, speed=speed)
            time.sleep(0.2)
            ok &= self.look(amp, 0, speed=speed)
            time.sleep(0.2)
        ok &= self.look(0, 0, speed=speed)
        return ok

    def tilt(self, direction: str = "right", *, amp: int = 18) -> bool:
        """Quizzical tilt: a small yaw lean toward ``'left'``/``'right'``.

        (No roll axis on this head, so a tilt reads as a held off-centre look.)
        """
        sign = -1 if str(direction).lower().startswith("l") else 1
        return self.look(sign * amp, 4, speed="low")

    def perk(self, *, up: int = 8, wide: int = 0) -> bool:
        """Listening acknowledgement: head up + (optionally) turn toward front.

        Matches the idle loop's listen-perk (pitch up by ~8 visual). ``wide``
        adds a small yaw if you want him to face front while perking.
        """
        return self.look(wide, up, speed="mid")

    def double_take(self, *, to: int = 40, speed="high") -> bool:
        """Glance away, snap back, then look again — surprised recognition."""
        ok = self.look(to, 4, speed=speed)
        time.sleep(0.18)
        ok &= self.look(0, 0, speed=speed)
        time.sleep(0.12)
        ok &= self.look(int(to * 0.7), 6, speed=speed)
        time.sleep(0.15)
        ok &= self.look(0, 0, speed="mid")
        return ok

    def scan(self, points=None, *, dwell_s: float = 0.6, speed="mid") -> bool:
        """Deliberately look around a list of VISUAL ``(yaw, pitch)`` spots.

        Defaults to a pleasant left-to-right sweep across the desk. Each point
        is clamped centrally; a crash mid-scan aborts.
        """
        if points is None:
            points = [(-45, 6), (-20, 2), (10, 0), (35, 4), (55, 8)]
        ok = True
        for (y, p) in points:
            if self.is_crashed():
                return False
            ok &= self.look(y, p, speed=speed)
            time.sleep(dwell_s)
        return ok

    def sweep(self, span: int = 60, *, steps: int = 6, speed="mid",
              dwell_s: float = 0.25) -> bool:
        """Smooth pan across ``+/- span`` visual yaw at level pitch."""
        span = min(abs(span), min(abs(self.yaw_min), self.yaw_max))
        ok = True
        for i in range(steps + 1):
            frac = -1.0 + 2.0 * i / steps
            ok &= self.look(int(frac * span), 0, speed=speed)
            time.sleep(dwell_s)
        return ok

    def bow(self, *, depth: int = 22, hold_s: float = 1.0) -> bool:
        """A little theatrical bow: dip well down, hold, return to level."""
        ok = self.look(0, -depth, speed="mid")
        time.sleep(hold_s)
        ok &= self.look(0, 0, speed="low")
        return ok

    def look_behind(self, side: str = "right", *, speed="mid") -> bool:
        """Turn the wide new yaw toward the pegboard / over his shoulder.

        Uses most of the calibrated yaw range (now that the head reaches
        ~+/-130+). ``'right'`` uses the positive extreme, ``'left'`` the
        negative one.
        """
        if str(side).lower().startswith("l"):
            target = int(self.yaw_min * 0.85)   # e.g. ~-110 visual
        else:
            target = int(self.yaw_max * 0.85)   # e.g. ~+136 visual
        return self.look(target, 6, speed=speed)

    # ── coordinated head + rail gaze (rail is first-class) ───────────────
    # These decide how much to use the RAIL versus the head to point at a
    # target. Where the head alone can't reach (or shouldn't have to crane to a
    # limit), the carriage rolls to bring the target into a comfortable head
    # range — one coordinated "gaze pose", not head-only.

    def look_at(self, yaw: int, pitch: int = 0, *, speed=None,
                use_rail: bool = True, comfort_yaw: int = 90,
                wait: bool = True) -> bool:
        """Point the whole gaze at a VISUAL ``(yaw, pitch)`` using head + rail.

        If ``|yaw|`` is within a comfortable head range (``comfort_yaw``), just
        turns the head. Beyond that, and when ``use_rail`` and the rail is
        ready, it rolls the carriage to absorb the excess — the body rotates so
        the head only has to turn a comfortable amount — then aims the head at
        the residual angle. Rail-unavailable degrades gracefully to head-only
        (clamped), so the call always does *something* sane.

        Returns True if the head command was sent.
        """
        yaw = int(yaw)
        residual = yaw
        if (use_rail and abs(yaw) > comfort_yaw
                and self.can_move(big=True)):
            st = self.rail_state()
            if self.rail_ready(st):
                cur = st.get("pos_mm")
                if isinstance(cur, (int, float)):
                    # Excess yaw beyond comfort -> carriage travel. +visual-yaw
                    # is to his LEFT, produced by -mm (RAIL_LOOK_SIGN). Convert
                    # the over-comfort angle into a mm move and roll there.
                    excess = yaw - (comfort_yaw if yaw > 0 else -comfort_yaw)
                    dmm = -RAIL_LOOK_SIGN * excess / max(RAIL_YAW_PER_MM, 0.01)
                    target = _clamp(int(round(cur + dmm)),
                                    self.rail_min, self.rail_max)
                    if abs(target - cur) >= RAIL_MIN_DRIFT_MM:
                        # Turn the head toward travel first, then roll.
                        self.look(comfort_yaw if yaw > 0 else -comfort_yaw,
                                  pitch, speed=speed)
                        self.roll_to(target, wait=wait)
                        # Residual = what the head still needs after the body
                        # rotation the carriage just provided.
                        moved_yaw = -RAIL_LOOK_SIGN * (target - cur) * RAIL_YAW_PER_MM
                        residual = yaw - moved_yaw
        return self.look(int(round(residual)), pitch, speed=speed)

    def face(self, direction: str, *, speed=None, use_rail: bool = True) -> bool:
        """Face a named direction, coordinating head + rail.

        Directions: ``'front'`` / ``'user'`` (level, centred), ``'left'`` /
        ``'right'`` (a comfortable turn that side), ``'pegboard'`` /
        ``'behind'`` (the wide over-the-shoulder look, rail-assisted),
        ``'home'`` / ``'dock'`` (toward the home/charge end),
        ``'printer'`` / ``'far'`` (toward the far end of the desk).
        """
        d = str(direction).lower()
        if d in ("front", "user", "centre", "center", "ahead"):
            return self.home_head(speed=speed)
        if d in ("left",):
            return self.look_at(-70, 4, speed=speed, use_rail=use_rail)
        if d in ("right",):
            return self.look_at(70, 4, speed=speed, use_rail=use_rail)
        if d in ("pegboard", "behind", "shoulder"):
            return self.look_at(int(self.yaw_max * 0.9), 6, speed=speed,
                                use_rail=use_rail, comfort_yaw=100)
        if d in ("home", "dock"):
            # Home switch is on his RIGHT; roll toward 0 mm and glance that way.
            if use_rail and self.rail_ready():
                self.roll_to(self.rail_min + 40, wait=True)
            return self.look(int(RAIL_LOOK_SIGN * 12), 4, speed=speed)
        if d in ("printer", "far", "end"):
            if use_rail and self.rail_ready():
                self.roll_to(self.rail_max - 60, wait=True)
            return self.look(int(-RAIL_LOOK_SIGN * 12), 6, speed=speed)
        logger.debug("movement: face(%r) unknown — facing front", direction)
        return self.home_head(speed=speed)

    # ── expressive rail gestures (movement vocabulary, like head gestures) ─
    # Built on roll_to / nudge, crash-aware, tasteful amplitudes.

    def approach(self, mm: int = 120, *, look: bool = True, wait: bool = True) -> bool:
        """Roll TOWARD the user / a point of interest (a small eager advance).

        Moves the carriage ``mm`` toward the home/desk end (his right) by
        default — "coming over to have a look". Glances that way as he sets off.
        """
        if not self.can_move(big=True):
            return False
        if look:
            self.look(int(RAIL_LOOK_SIGN * 10), 2, speed="mid")
        return self.nudge(-abs(int(mm)), wait=wait)

    def retreat(self, mm: int = 120, *, look: bool = True, wait: bool = True) -> bool:
        """Roll AWAY (a wary or giving-space pull-back)."""
        if not self.can_move(big=True):
            return False
        if look:
            self.look(int(-RAIL_LOOK_SIGN * 8), 4, speed="mid")
        return self.nudge(abs(int(mm)), wait=wait)

    def lean(self, direction: str = "in", *, mm: int = 35) -> bool:
        """A small emphatic nudge — curiosity / lean-in punctuation.

        ``'in'`` / ``'user'`` leans toward the user (his right, -mm);
        ``'out'`` / ``'away'`` the other way. Deliberately small so it reads as
        body language, not travel.
        """
        d = str(direction).lower()
        sign = 1 if d in ("out", "away", "back") else -1
        return self.nudge(sign * RAIL_LOOK_SIGN * abs(int(mm)), wait=True)

    def peek(self, mm: int = 90, *, dwell_s: float = 1.0,
             look_out: bool = True) -> bool:
        """Dart the carriage OUT, hold a beat, dart back — a curious peek.

        Returns to the starting position. No-op (returns False) if the rail
        isn't ready or the start position is unknown.
        """
        if not self.can_move(big=True):
            return False
        st = self.rail_state()
        if not self.rail_ready(st):
            return False
        start = st.get("pos_mm")
        if not isinstance(start, (int, float)):
            return False
        start = int(round(start))
        out = _clamp(start + abs(int(mm)), self.rail_min, self.rail_max)
        if look_out:
            self.look(int(-RAIL_LOOK_SIGN * 12), 8, speed="high")
        self.roll_to(out, wait=True)
        time.sleep(dwell_s)
        ok = self.roll_to(start, wait=True)
        self.look(0, 0, speed="mid")
        return ok

    def patrol(self, span=None, *, passes: int = 1, dwell_s: float = 0.7,
               scan_head: bool = True) -> bool:
        """Slow glide along the desk, looking about — "keeping watch".

        ``span`` is an optional ``(min_mm, max_mm)`` window (defaults to a safe
        inset of the full rail). Glides end to end ``passes`` times; when
        ``scan_head`` it turns the head to look around at each end. Crash-aware
        (aborts the patrol on a crash).
        """
        if not self.can_move(big=True):
            return False
        if not self.rail_ready():
            return False
        lo, hi = span if span else (self.rail_min + 40, self.rail_max - 40)
        lo = _clamp(int(lo), self.rail_min, self.rail_max)
        hi = _clamp(int(hi), self.rail_min, self.rail_max)
        for _ in range(max(1, passes)):
            for target, gaze in ((hi, -RAIL_LOOK_SIGN * 14),
                                 (lo, RAIL_LOOK_SIGN * 14)):
                if self.is_crashed():
                    return False
                if scan_head:
                    self.look(int(gaze), 6, speed="low")
                if not self.roll_to(target, wait=True):
                    return False
                if scan_head:
                    self.look(0, 4, speed="low")
                time.sleep(dwell_s)
        return True

    # ── rail-assisted face-follow (centralised from stackchan-idle) ──────

    def rail_follow_step(self, dx: float, head_yaw_cmd: int,
                         yaw_delta: float) -> bool:
        """One rail-follow nudge: roll the carriage when the neck is pinned.

        Centralises the idle loop's rail-follow rule. Call it right after a
        head-tracking move. Given the tracker's horizontal error ``dx`` (target
        offset from frame centre, ``-1..1``), the PHYSICAL yaw the head was just
        commanded to (``head_yaw_cmd``), and the yaw ``yaw_delta`` that command
        applied, this rolls the carriage toward the target only when the head
        has pinned at a yaw limit and the target is still off-centre the same
        way. Rolls ``RAIL_FOLLOW_SIGN * (+/-nudge)`` — following the user down
        the desk. Returns True if it nudged.
        """
        if abs(dx) < RAIL_FOLLOW_DX:
            return False
        if not self.can_move(big=True):
            return False
        pinned_hi = head_yaw_cmd >= self.yaw_max - 2 and yaw_delta > 0
        pinned_lo = head_yaw_cmd <= self.yaw_min + 2 and yaw_delta < 0
        if not (pinned_hi or pinned_lo):
            return False
        if not self.rail_ready():
            return False
        rail_dir = RAIL_FOLLOW_SIGN * (1 if pinned_hi else -1)
        return self.nudge(rail_dir * RAIL_FOLLOW_NUDGE_MM, wait=False)


# ─── dry self-test (no device required) ──────────────────────────────────────


def _selftest() -> int:
    """Construct the controller and print resolved limits/orientation.

    Degrades cleanly offline: MCP calls are attempted but any failure is
    reported without raising, so this is safe to run while the device is busy
    or the gateway is down.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("=== MovementController dry self-test ===")
    print(f"settings file : {SETTINGS_PATH} (exists={os.path.exists(SETTINGS_PATH)})")
    print(f"MCP url       : {GATEWAY_MCP_URL}")

    mc = MovementController(respect_quiesce=True, verify_feedback=False)
    lim = mc.limits()
    print("\nResolved config (limits + orientation, applied centrally):")
    for k, v in lim.items():
        print(f"  {k:14s}: {v}")

    print("\nOrientation mapping sanity (VISUAL -> physical servo):")
    for (vy, vp, label) in [
        (0, 0, "look level, straight ahead"),
        (0, 20, "look UP"),
        (0, -20, "look DOWN"),
        (40, 0, "look to HIS RIGHT"),
        (-40, 0, "look to HIS LEFT"),
    ]:
        py, pp = mc.to_physical(vy, vp)
        print(f"  visual(yaw={vy:+4d}, pitch={vp:+4d})  ->  physical(yaw={py:+4d}, "
              f"pitch={pp:+3d})   # {label}")

    print("\nCoordinated look_at() head-vs-rail split (offline reasoning):")
    for vy in (30, 90, 130):
        comfort = 90
        if abs(vy) <= comfort:
            print(f"  visual_yaw {vy:+4d}  ->  head-only ({vy:+d}), no rail needed")
        else:
            excess = vy - comfort
            dmm = -RAIL_LOOK_SIGN * excess / max(RAIL_YAW_PER_MM, 0.01)
            print(f"  visual_yaw {vy:+4d}  ->  head to {comfort:+d} + roll carriage "
                  f"~{dmm:+.0f} mm to absorb {excess:+d} deg")

    print("\nRail gesture vocabulary available:")
    print("  approach, retreat, lean, peek, patrol, look_at, face, "
          "rail_follow_step (+ head: nod/shake/tilt/perk/double_take/scan/"
          "sweep/bow/look_behind)")

    print("\nSafety markers:")
    print(f"  quiesced (voice turn live): {mc.is_quiesced()}")
    print(f"  busy (any session/turn)   : {mc.is_busy()}")

    print("\nLive device probe (guarded — OK if offline/busy):")
    if mc.mcp.initialize():
        ps = mc.pose()
        print(f"  measured head yaw/pitch : {ps.get('yaw')}, {ps.get('pitch')}")
        rail = ps.get("rail") or {}
        if rail:
            print(f"  rail linked/homed/crashed: {rail.get('linked')}, "
                  f"{rail.get('homed')}, {rail.get('crashed')}  pos_mm={rail.get('pos_mm')}")
        else:
            print("  rail status             : (no reply)")
        gz = mc.gaze_pose()
        print(f"  combined gaze (visual)  : yaw~{gz.get('visual_yaw')}, "
              f"pitch~{gz.get('visual_pitch')}  (rail_mm={gz.get('rail_mm')})")
        batt = mc.battery()
        print(f"  battery                 : {batt}")
        print(f"  rail_ready              : {mc.rail_ready()}")
        print(f"  can_move(big=True)      : {mc.can_move(big=True)}")
    else:
        print("  gateway unreachable — skipped live probe (this is fine).")

    print("\nOK: constructed + resolved without raising.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
