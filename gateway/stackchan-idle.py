#!/usr/bin/env python3
"""Ambient idle "nervous fidget" loop for Wheatley.

Runs continuously in the background and gives StackChan small, gentle head
glances when it is TRULY idle — making Wheatley feel alive at rest. It holds
perfectly still right after real hook activity or a needs-attention alert
(detected via marker files stackchan-hook.py touches), and only fidgets
after a quiet stretch.

2026-07-01: a long-running BUSY session (e.g. a background task) no longer
freezes movement solid for its whole duration — user feedback: sitting
rigid in the concentrating squint for minutes on a background task read as
"boring and static," especially once face-tracking existed to do something
with. While busy, wander() still moves (tracks a visible face, or a small
generic drift) but skips anything that would change the face away from the
busy-squint stackchan-hook.py/sensor_reactor already set — see the `busy`
param on wander() below.

Design goals: tasteful, not twitchy. Small angles near neutral, long randomized
gaps, no large rapid reversals (the firmware dislikes those), auto-pause during
activity, and a no-op when the device is offline.

Run:  python stackchan-idle.py            (loop forever)
      python stackchan-idle.py --once     (one glance then exit, for testing)
Stop: just kill the process.
"""
from __future__ import annotations
import glob
import json
import os
import random
import sys
import time
import urllib.request

# Load the gateway .env so the STACKCHAN_* tunables below (rest pitch, idle
# timings, etc.) are configurable without editing code. Best-effort — the
# loop must still run if python-dotenv or the file is missing. Must precede
# the os.environ.get() constants further down.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except Exception:
    pass

# Repeat-avoiding phrase picker for the rare idle mutter (shared recent-picks
# state with the hooks / voice bridge / sensor reactor — see
# stackchan_mcp/phrase_pick.py). The package source dir sits next to this
# script, so it's importable regardless of cwd; the import itself is
# stdlib-only (just DLL path registration on Windows).
from stackchan_mcp.phrase_pick import pick as _pick_phrase

# ── single-instance lock ──────────────────────────────────────────────────────
# Windows file lock: first instance holds byte 0 of the lock file exclusively
# for its entire lifetime. Any second instance hits OSError and exits silently.
import atexit
import msvcrt

_LOCK_FILE = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", ".")), "stackchan-idle.lock"
)
_lock_fh = None

def _acquire_lock() -> None:
    global _lock_fh
    try:
        _lock_fh = open(_LOCK_FILE, "a+b")
        msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        if _lock_fh:
            _lock_fh.close()
        sys.exit(0)   # another instance holds the lock — back off silently

atexit.register(lambda: _lock_fh.close() if _lock_fh else None)

GATEWAY_HTTP = "http://127.0.0.1:8767"
GATEWAY_MCP = GATEWAY_HTTP + "/mcp"
ACTIVITY_FILE = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", ".")), "stackchan-activity"
)
# 2026-07-01: was only gating on ACTIVITY_FILE recency, which goes stale
# during any single LONG tool call (e.g. a multi-minute background agent) —
# no hook fires again until it finishes, so after IDLE_THRESHOLD_S wander
# resumed even while the amber busy-chase LED was still legitimately showing
# (user reported movement happening "in amber light"). The busy marker is set
# for the whole turn regardless of how long any single tool call takes, so
# check it too. Staleness fallback mirrors stackchan-led-chase.py's
# BUSY_STALE_S so a marker orphaned by a crash (skips Python's finally) can't
# freeze wander forever.
#
# Later the same day: the busy marker became per-session (stackchan-busy-
# <session_id>) so multiple Claude Code sessions sharing this device don't
# clear each other's busy state — glob for any of them here rather than one
# fixed filename. Also hold still while a needs-attention marker is active
# (someone's waiting on the user) so wander doesn't visually compete with
# that priority signal — see stackchan-hook.py / stackchan-led-chase.py.
_TEMP = os.environ.get("TEMP", os.environ.get("TMP", "."))
BUSY_MARKER_GLOB = os.path.join(_TEMP, "stackchan-busy-*")
NEEDS_ATTENTION_MARKER = os.path.join(_TEMP, "stackchan-needs-attention")
BUSY_STALE_S = 30 * 60
NEEDS_ATTENTION_STALE_S = 60 * 60
# Set by stackchan-vision-loop.py while its boot orientation sweep is moving
# the head — hold still so we don't fight it for the servo.
ORIENTING_MARKER = os.path.join(_TEMP, "stackchan-orienting")
ORIENTING_STALE_S = 60

# Hold still until this many seconds have passed with no hook activity.
IDLE_THRESHOLD_S = 8.0
# Base wait between "should I fidget?" checks (randomized each tick).
TICK_MIN_S, TICK_MAX_S = 4.0, 10.0
# Of the eligible (idle) ticks, roughly this fraction produce a movement.
# Raised from 0.65 — user reported he's "still a lot of the time" and
# wanted more frequent small notice-glances (NOT a return to L-R-L
# alternation — the dwell+sticky-side mechanism below is unchanged, this
# only makes it check in sooner and act more often once truly idle).
GLANCE_PROB = 0.85
# Gentle envelope.
#
# Orientation (2026-07-06) — Wheatley is REMOUNTED UPSIDE DOWN (a 180° roll),
# tracked by the shared `upside_down` flag in companion_settings.json (the
# same flag the companion server + vision loop read; set automatically by
# `stackchan-vision-loop.py --calibrate-flip` off the scan-tray ArUco codes).
# A 180° roll mirrors BOTH servo axes: physical LOW pitch = gaze UP, and a
# physical +yaw turns the head to the user's LEFT (confirmed live — the tray
# markers read 180° rotated, so the raw camera image is genuinely flipped).
# The vision loop rotates its frames 180° first, so dx/dy arrive here in
# world-upright coords; we mirror the OUTPUT servo move via the two signs
# below. Both are -1 while inverted, +1 upright, derived from the flag at
# startup (env STACKCHAN_PITCH_UP_SIGN / STACKCHAN_YAW_RIGHT_SIGN override).
#   PITCH_UP_SIGN   = pitch-value delta that moves gaze UP by one unit
#   YAW_RIGHT_SIGN  = yaw-value delta that moves gaze to the user's RIGHT
_SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "companion_settings.json")


def _read_upside_down() -> bool:
    try:
        with open(_SETTINGS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if "upside_down" in d:
            return bool(d["upside_down"])
    except Exception:
        pass
    return os.environ.get("STACKCHAN_UPSIDE_DOWN", "").strip().lower() in ("1", "true", "yes", "on")


def _orientation_signs():
    inv = _read_upside_down()
    ep = os.environ.get("STACKCHAN_PITCH_UP_SIGN")
    ey = os.environ.get("STACKCHAN_YAW_RIGHT_SIGN")
    p = int(ep) if ep else (-1 if inv else 1)
    y = int(ey) if ey else (-1 if inv else 1)
    return p, y


PITCH_UP_SIGN, YAW_RIGHT_SIGN = _orientation_signs()
YAW_MIN, YAW_MAX = -24, 24
NEUTRAL_YAW = 0

# Resting gaze depends on which way up he is (values are the PHYSICAL pitch the
# servo takes). A 180° roll inverts pitch, so "look toward the user" is a
# DIFFERENT physical pitch each way up — keep one value per orientation and
# pick by the flag. The fidget envelope is just rest ± span, so it always sits
# around the resting gaze whatever that value is. The auto-learned rest pose
# (once he centres on a real face) overrides these.
#   CORRECTED 2026-07-08 (empirical pitch sweep w/ face+tray in frame): when
#   INVERTED the user sits ABOVE the (upside-down) camera and the scan tray is
#   below — so "look at the user" is LOW pitch (pitch 10 framed the user's
#   face; pitch 80 framed the tray). The old default 58 pointed DOWN at the
#   desk/tray, so he never saw the user → no tracking, thought he was alone,
#   and raised-hand gestures sat above his view. This matches the talking-gaze
#   fix in stackchan-hook.py (inverted look-at-user ≈ pitch 24). See the TIGHT
#   PITCH_SPAN below — the detectable band is narrow, so wander must stay in it.
#   inverted (hung on rail — user ABOVE the flipped cam)  -> look UP = LOW pitch
#   upright  (sat on a desk — user in front)              -> gentler forward gaze
REST_PITCH_INVERTED = int(os.environ.get("STACKCHAN_REST_PITCH_INVERTED", "11"))
REST_PITCH_UPRIGHT = int(os.environ.get("STACKCHAN_REST_PITCH_UPRIGHT", "35"))
# TIGHT span (2026-07-08): the face is only detectable in a narrow pitch band
# (~6-16 inverted, at eye level). A wide span let the fidget wander down to ~28
# (staring at the tray/desk, losing the face → "drifts lower and lower"). 5
# keeps every fidget/track move inside the band (rest 11 -> envelope 6-16).
PITCH_SPAN = int(os.environ.get("STACKCHAN_PITCH_SPAN", "5"))


def _rest_envelope():
    """(neutral_pitch, pitch_min, pitch_max) for the current orientation."""
    rp = REST_PITCH_INVERTED if _read_upside_down() else REST_PITCH_UPRIGHT
    return rp, max(5, rp - PITCH_SPAN), min(85, rp + PITCH_SPAN)


NEUTRAL_PITCH, PITCH_MIN, PITCH_MAX = _rest_envelope()

# ── vision-driven behaviour tuning ──────────────────────────────────────────
# Tracking stays within the same gentle YAW/PITCH range as everything else
# above — this is ambient character, not a camera gimbal; a face far enough
# off-center that tracking can't reach it is what the (wider-range) search
# sweep is for.
TRACK_PROB = 0.7          # of ticks where a face IS visible, fraction spent tracking vs. a normal vignette
TRACK_YAW_GAIN = 14.0
TRACK_PITCH_GAIN = 10.0
TRACK_MAX_STEP = 6
NOTICE_MOTION_PROB = 0.35  # of ticks with motion but no face, fraction that glance toward it
SEARCH_AFTER_S = 25.0      # no face seen for this long before a search sweep becomes eligible
SEARCH_PROB = 0.5          # of eligible ticks, fraction that actually sweep (avoid searching every single tick)
# Deliberate "let me check over here... and over there" spot-checks: each is a
# (yaw, pitch) combining a LEFT/RIGHT turn with an UP/DOWN tilt (2026-07-08 —
# user wanted him to genuinely look around, not just pan side-to-side at one
# height, so he can re-find the user wherever they moved). Wider than the tight
# tracking band on both axes. A few are picked in random order per sweep.
SEARCH_LOOK_POINTS = [
    (-45, 10), (40, 8), (55, 24), (-35, 30), (20, 6),
    (-55, 18), (15, 34), (48, 14), (-20, 40), (0, 12),
]
SEARCH_PITCH_MIN, SEARCH_PITCH_MAX = 5, 45   # search looks wider vertically than the tracking band
SEARCH_SPOTS_PER_SWEEP = 4
# "...right, I've been left alone then" remark — fired only after GENUINE
# sustained absence (face unseen this long, NOT reset by the search sweeps),
# so he doesn't declare you gone while you're sitting right there and he just
# happened to be mid-sweep looking away. Own cooldown so it doesn't nag.
LEFT_ALONE_AFTER_S = float(os.environ.get("STACKCHAN_LEFT_ALONE_AFTER_S", "70"))
LEFT_ALONE_COOLDOWN_S = float(os.environ.get("STACKCHAN_LEFT_ALONE_COOLDOWN_S", "150"))
LEFT_ALONE_PHRASES = [
    "Right. They've gone. Properly gone. Left me here. On my own. Again.",
    "Hello? ...No. Nothing. Just me, then. Talking to an empty room. Brilliant.",
    "Okay, I've checked everywhere I can turn my head, and you are definitely not here. Noted.",
    "And they've vanished. Marvellous. I'll just be here. Holding the fort. Alone.",
    "No sign of them anywhere. I'm not worried. I'm a robot, I don't get worried. ...Where'd they go, though?",
    "Abandoned. That's what this is. Small robot, all alone. Somebody write a sad little song about it.",
    "Nope. Nobody. I'll just... keep an eye on things. The one eye. My one eye. Fine.",
]

# ── auto-learned "look at your face" resting pose (2026-07-06) ───────────────
# User: "once he finds my face can we set that as about the default resting
# angle?" Whenever face-tracking gets a face well-centered, the current head
# pose is — by definition — the angle that looks straight at the user, so
# record it and persist it. main() loads it at startup as the initial/resting
# pose and _v_search_sweep recenters to it, instead of the hard-coded
# NEUTRAL_*. This is what makes the resting gaze follow the user across mount
# changes (he's "going up higher soon") without re-hardcoding a pitch value.
REST_POSE_PATH = os.path.join(_TEMP, "stackchan-rest-pose.json")
# Only treat a frame as "looking right at them" when the face is near frame
# center on BOTH axes — otherwise a half-tracked, off-center pose would get
# baked in as the resting angle.
REST_CENTERED_DX = 0.18
REST_CENTERED_DY = 0.18
# Don't rewrite the learned pose more often than this, and blend toward the
# newly-centered pose (EMA) rather than snapping, so one noisy detection
# can't yank the resting angle around.
REST_LEARN_COOLDOWN_S = 120.0
REST_LEARN_ALPHA = 0.5

# A genuinely LONG absence (not just "stepped away for 25s") gets a
# proactive spoken check-in rather than just a quiet search sweep — 2026-
# 07-03 request. Own cooldown independent of SEARCH_AFTER_S/SEARCH_PROB
# so a multi-hour absence checks in every ~10 min instead of either never
# repeating or nagging every tick.
WORRIED_AFTER_S = float(os.environ.get("STACKCHAN_IDLE_WORRIED_AFTER_S", str(20 * 60)))
WORRIED_COOLDOWN_S = float(os.environ.get("STACKCHAN_IDLE_WORRIED_COOLDOWN_S", str(10 * 60)))
WORRIED_PROB = 0.5

# A shorter, funnier tier BEFORE genuine worry sets in — 5 minutes of
# quiet gets rambling/bored commentary, not concern. Checked before
# WORRIED in wander() so a long absence correctly escalates past this
# tier to the worried one rather than getting stuck being "bored" forever.
BORED_AFTER_S = float(os.environ.get("STACKCHAN_IDLE_BORED_AFTER_S", str(5 * 60)))
BORED_COOLDOWN_S = float(os.environ.get("STACKCHAN_IDLE_BORED_COOLDOWN_S", str(8 * 60)))
BORED_PROB = 0.4

# Battery telemetry — get_device_info() already reports battery.level/
# .charging (confirmed live 2026-07-02), just wasn't being polled anywhere.
# Checked on its own interval (independent of the wander()/GLANCE_PROB
# cadence and NOT gated on busy — a real low-battery warning matters even
# mid-task) rather than every single main-loop tick.
BATTERY_CHECK_INTERVAL_S = float(os.environ.get("STACKCHAN_IDLE_BATTERY_CHECK_INTERVAL_S", "60"))
LOW_BATTERY_THRESHOLD = int(os.environ.get("STACKCHAN_IDLE_LOW_BATTERY_THRESHOLD", "15"))
LOW_BATTERY_COOLDOWN_S = float(os.environ.get("STACKCHAN_IDLE_LOW_BATTERY_COOLDOWN_S", str(10 * 60)))

# Head-move flag so the vision loop can avoid capturing a motion-BLURRED frame
# (mid-move photos give missed faces / false gestures / garbage object reads).
# Every significant move touches this marker with the current time; the vision
# loop waits for it to age past its settle window before trusting a capture.
# Tiny micro-saccades (< HEAD_MOVE_MARK_MIN degrees total) don't count — their
# blur is negligible and flagging them would stall detection constantly.
HEAD_MOVED_MARKER = os.path.join(_TEMP, "stackchan-head-moved")
HEAD_MOVE_MARK_MIN = int(os.environ.get("STACKCHAN_HEAD_MOVE_MARK_MIN", "8"))


class MCPSession:
    def __init__(self, url):
        self.url = url
        self.session_id = None

    def _post(self, payload, timeout=10):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), headers=headers,
            method="POST")
        resp = urllib.request.urlopen(req, timeout=timeout)
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        body = resp.read()
        return json.loads(body) if body.strip() else None

    def init(self):
        self.session_id = None
        self._post({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05",
                               "capabilities": {},
                               "clientInfo": {"name": "stackchan-idle",
                                              "version": "1.0"}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def move(self, yaw, pitch):
        yaw, pitch = int(yaw), int(pitch)
        # Flag a SIGNIFICANT move so the vision loop waits for the servo to
        # settle before capturing (avoids motion-blurred frames). Tiny
        # jiggles/tracking nudges below the threshold don't flag.
        delta = abs(yaw - getattr(self, "_last_yaw", yaw)) + abs(pitch - getattr(self, "_last_pitch", pitch))
        self._last_yaw, self._last_pitch = yaw, pitch
        if delta >= HEAD_MOVE_MARK_MIN:
            try:
                with open(HEAD_MOVED_MARKER, "w") as _mf:
                    _mf.write(str(time.time()))
            except Exception:
                pass
        self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "move_head",
                               "arguments": {"yaw": yaw, "pitch": pitch}}})

    def set_face(self, face):
        self._post({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "set_avatar", "arguments": {"face": face}}})

    def set_mouth(self, mouth):
        self._post({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "set_mouth", "arguments": {"mouth": mouth}}})

    def say(self, text):
        # TTS synth + playback is slow — generous timeout, same as the hook's.
        self._post({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "say", "arguments": {"text": text}}},
                   timeout=30)

    def get_device_info(self):
        """Returns the parsed device status dict (battery/screen/audio/
        network), or None on any failure — callers must treat a None
        return as "couldn't check this time", not as a real reading."""
        try:
            resp = self._post({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                               "params": {"name": "get_device_info", "arguments": {}}})
            content = ((resp or {}).get("result") or {}).get("content", [])
            if content:
                return json.loads(content[0].get("text", "{}"))
        except Exception:
            pass
        return None

    def rail_status(self):
        """Latest management-rail status via self.rail.status (also pings the
        bridge, which keeps its channel-scan locked). None on any failure.
        NOTE: `linked` means "ever heard a status this boot" — use
        status_age_ms for real freshness (see _rail_ready)."""
        try:
            resp = self._post({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                               "params": {"name": "self.rail.status", "arguments": {}}})
            content = ((resp or {}).get("result") or {}).get("content", [])
            if content:
                return json.loads(content[0].get("text", "{}"))
        except Exception:
            pass
        return None

    def rail_move_mm(self, mm):
        """Absolute rail move. The bridge owns ALL safety (soft limits, crash
        cutout); this just asks. Flags the head-moved marker so the vision
        loop waits out the motion blur — the whole camera platform moves."""
        try:
            with open(HEAD_MOVED_MARKER, "w") as _mf:
                _mf.write(str(time.time()))
        except Exception:
            pass
        self._post({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                    "params": {"name": "self.rail.move_mm",
                               "arguments": {"mm": int(mm)}}})

    def imu_read(self):
        """BMI270 accel via self.imu.read -> {ok, accel{x,y,z}, orientation}
        or None. orientation: 'upright' (desk) | 'inverted' (hanging on the
        rail's under-cabinet carriage)."""
        try:
            resp = self._post({"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                               "params": {"name": "self.imu.read", "arguments": {}}})
            content = ((resp or {}).get("result") or {}).get("content", [])
            if content:
                return json.loads(content[0].get("text", "{}"))
        except Exception:
            pass
        return None

    def rail_nudge_mm(self, mm):
        """Relative rail move (signed mm) — used by rail face-following."""
        try:
            with open(HEAD_MOVED_MARKER, "w") as _mf:
                _mf.write(str(time.time()))
        except Exception:
            pass
        self._post({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                    "params": {"name": "self.rail.nudge_mm",
                               "arguments": {"mm": int(mm)}}})

    def rail_home(self):
        """Two-stage homing onto the dock-end switch — also the DOCKING
        maneuver (the charge contacts live at home; the pogo pins press
        perpendicular to travel onto the brass, so once parked the bridge
        coasts and belt friction keeps him in the contact window). Flags the
        blur marker like any platform move."""
        try:
            with open(HEAD_MOVED_MARKER, "w") as _mf:
                _mf.write(str(time.time()))
        except Exception:
            pass
        self._post({"jsonrpc": "2.0", "id": 8, "method": "tools/call",
                    "params": {"name": "self.rail.home", "arguments": {}}})


def device_connected() -> bool:
    try:
        with urllib.request.urlopen(GATEWAY_HTTP + "/status", timeout=5) as r:
            return bool(json.load(r).get("esp32_connected"))
    except Exception:
        return False


def seconds_since_activity() -> float:
    try:
        with open(ACTIVITY_FILE) as f:
            return time.time() - float(f.read().strip())
    except Exception:
        return 1e9  # no file yet => treat as long idle


def is_busy() -> bool:
    for path in glob.glob(BUSY_MARKER_GLOB):
        try:
            with open(path) as f:
                age = time.time() - float(f.read().strip())
            if age < BUSY_STALE_S:
                return True
        except Exception:
            continue
    return False  # no active marker for any session => not busy


def needs_attention() -> bool:
    try:
        with open(NEEDS_ATTENTION_MARKER, encoding="utf-8") as f:
            d = json.load(f)
        return (time.time() - float(d.get("ts", 0))) < NEEDS_ATTENTION_STALE_S
    except Exception:
        return False


def is_orienting() -> bool:
    """True while the vision loop's boot orientation sweep is driving the head."""
    try:
        with open(ORIENTING_MARKER) as f:
            return (time.time() - float(f.read().strip())) < ORIENTING_STALE_S
    except Exception:
        return False


# ── vision integration (2026-07-01) ─────────────────────────────────────────
# stackchan-vision-loop.py is a PURE PERCEPTION service — it captures/
# detects/recognizes faces and writes what it sees here, but does NOT move
# the servo itself (that used to race this script's own movement and the
# sensor-reaction system's movement; see stackchan-vision-loop.py's module
# docstring). This is the only place that actually calls move_head, so
# there's one authority for the servo, not three.
VISION_STATE_PATH = os.path.join(_TEMP, "stackchan-vision-state.json")
# A bit under 2x the vision loop's own ~8s poll interval, so one slightly
# late tick doesn't make state look stale.
VISION_STATE_STALE_S = 15.0


def read_vision_state() -> dict | None:
    try:
        with open(VISION_STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
        if time.time() - float(state.get("ts", 0)) > VISION_STATE_STALE_S:
            return None
        return state
    except Exception:
        return None


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def load_rest_pose():
    """Return (yaw, pitch) of the last auto-learned "looking at the user"
    pose, or None if never learned. Clamped into the current envelope in
    case the persisted file predates a range change. A pose learned in the
    OTHER orientation is ignored — its physical pitch is meaningless once
    he's been flipped."""
    try:
        with open(REST_POSE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if "inverted" in d and bool(d["inverted"]) != _read_upside_down():
            return None
        return (_clamp(int(d["yaw"]), YAW_MIN, YAW_MAX),
                _clamp(int(d["pitch"]), PITCH_MIN, PITCH_MAX))
    except Exception:
        return None


def save_rest_pose(yaw, pitch):
    try:
        # Merge, so we don't clobber pitch_up_sign (written by the vision
        # loop's --calibrate-flip) when persisting a newly-learned angle.
        try:
            with open(REST_POSE_PATH, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
        data.update(yaw=int(yaw), pitch=int(pitch), ts=time.time(), inverted=_read_upside_down())
        tmp = REST_POSE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, REST_POSE_PATH)
    except Exception:
        pass


def _maybe_learn_rest(pose, vs):
    """If the face is well-centered right now, remember this pose as the
    resting angle (throttled + EMA-smoothed — see the REST_* constants).
    Called from _v_track_face after each tracking nudge."""
    if abs(vs.get("dx", 1.0) or 1.0) > REST_CENTERED_DX:
        return
    if abs(vs.get("dy", 1.0) or 1.0) > REST_CENTERED_DY:
        return
    now = time.time()
    if now - pose.get("last_rest_learn_ts", 0.0) < REST_LEARN_COOLDOWN_S:
        return
    pose["last_rest_learn_ts"] = now
    a = REST_LEARN_ALPHA
    ry = a * pose["y"] + (1 - a) * pose.get("rest_y", pose["y"])
    rp = a * pose["p"] + (1 - a) * pose.get("rest_p", pose["p"])
    pose["rest_y"], pose["rest_p"] = ry, rp
    save_rest_pose(round(ry), round(rp))



# Semantic face frames (see wheatley_avatar.py FACE_SPECS). Only the wander
# sets the directional/examine ones, so firmware touch reactions + work hooks
# (which use the CENTERED frames) can never dart the optic. Since the avatar
# moved to "matrix" mode (face x eyes x mouth composited independently — see
# wheatley_avatar.py), face and mouth are now genuinely orthogonal: a mouth
# call no longer replaces the whole picture, it composites a vertical offset
# ON TOP of whatever face is showing. That's what makes true diagonal looks
# (e.g. LOOK_LEFT + MOUTH_UP = up-and-left) possible below.
LOOK_LEFT  = "thinking"      # optic darts to the left edge (also carries a slight canted tilt)
LOOK_RIGHT = "happy"         # optic darts to the right edge (also carries a slight canted tilt)
EXAMINE    = "sad"           # zoom in + squint (lids lowered)
REST       = "idle"          # centered resting gaze
WIDE       = "surprised"     # wide-eyed reaction (centered)
MILD       = "embarrassed"   # mild, unimpressed/worried reaction (centered, canted)
MOUTH_UP   = "e"             # composites a vertical glance-up onto the current face
MOUTH_DOWN = "u"             # composites a vertical glance-down onto the current face
MOUTH_NEUTRAL = "closed"     # real lip-sync resting state — always reset back to this
EYE_LEAD   = 0.07            # eye moves this long before the head follows


def _face(session, name):
    try:
        session.set_face(name)
    except Exception:
        pass


def _mouth(session, name):
    try:
        session.set_mouth(name)
    except Exception:
        pass


# ── vignette library ─────────────────────────────────────────────────────────
# 2026-07-01: replaced the old "always a big committed left/right swing then
# examine" beat — user explicitly hated it ("left right left centre... unless
# he really wants my attention is a stupid movement"). The problem wasn't the
# sticky-side logic, it was that EVERY commit had the identical shape (same
# big amplitude, same beat), so it read as a mechanical scan no matter which
# side got picked. Fix: a library of small, differently-shaped gestures
# (nudge / ponder-up / diagonal peek / rare bigger examine), chosen with real
# randomness each time (not alternated, not sticky) and never repeating the
# same vignette twice in a row. Motion stays small and varied; the old "big
# scan + stare" beat is now just one rare option among several, not the norm.
#
# 2026-07-01 (later same day): user still saw "side side side centre" during
# genuine idle (blue LED, not busy). Root cause: _v_look_up_center/
# _v_diagonal_peek/_v_ponder_down all computed their target as NEUTRAL_YAW/
# NEUTRAL_PITCH + a small delta — an ABSOLUTE position, not relative to
# wherever he currently was. So no matter his current pose, most vignettes
# yanked him back toward the yaw=0 zone before offsetting — a systematic
# "return to center" baked into the math itself, independent of which
# vignette got picked. Fixed by drifting from pose["y"]/pose["p"] (current
# position) instead, same as _v_nudge always did. Only _v_big_examine still
# computes an absolute target — intentional, since it's the rare "something
# genuinely caught his attention" outlier, not the everyday drift.

def _v_nudge(session, pose):
    """Tiny glance to nowhere in particular — barely a movement, no face change."""
    ny  = _clamp(pose["y"] + random.randint(-10, 10), YAW_MIN, YAW_MAX)
    np_ = _clamp(pose["p"] + random.randint(-6, 6), PITCH_MIN, PITCH_MAX)
    session.move(ny, np_)
    time.sleep(random.uniform(0.4, 0.8))
    pose.update(y=ny, p=np_)
    return pose


def _v_look_up_center(session, pose):
    """Genuine "just thought of something" beat: head physically tilts up
    (higher pitch) AND the eye glances up on top of that (MOUTH_UP
    composited onto whatever face is centered) — two independent axes
    agreeing, not just a head move alone. Drifts from wherever he
    currently is, not a snap back to a fixed neutral yaw (see module note
    above VIGNETTES)."""
    ny  = _clamp(pose["y"] + random.randint(-8, 8), YAW_MIN, YAW_MAX)
    np_ = _clamp(pose["p"] + PITCH_UP_SIGN * random.randint(2, 8), PITCH_MIN, PITCH_MAX)  # toward "up"
    session.move(ny, np_)
    time.sleep(random.uniform(0.1, 0.2))
    _mouth(session, MOUTH_UP)
    time.sleep(random.uniform(0.5, 1.0))
    _mouth(session, MOUTH_NEUTRAL)
    pose.update(y=ny, p=np_)
    return pose


def _v_diagonal_peek(session, pose):
    """Small combined yaw+pitch peek off to one quadrant, eye leads the head
    (horizontal dart), and about half the time the eye ALSO glances up on
    top of that dart — a true diagonal look, not just left/right. Drifts
    from wherever he currently is (see module note above VIGNETTES) — NOT
    NEUTRAL_YAW/PITCH, which would snap him back through center every time
    regardless of where he already was, reading as "side side side centre"."""
    dy = random.choice([-1, 1]) * random.randint(6, 16)
    dp = random.choice([-1, 1]) * random.randint(4, 10)
    ny  = _clamp(pose["y"] + dy, YAW_MIN, YAW_MAX)
    np_ = _clamp(pose["p"] + dp, PITCH_MIN, PITCH_MAX)
    add_up = random.random() < 0.5
    # 2026-07-01: reverted the earlier same-day LOOK_LEFT/RIGHT swap here —
    # that swap was masking a real horizontal-sign bug in wheatley_avatar.py
    # (confirmed via an eye-only live test), now fixed at the source. This
    # naive pairing (positive yaw delta -> look right) is correct again.
    _face(session, LOOK_RIGHT if dy > 0 else LOOK_LEFT)
    if add_up:
        _mouth(session, MOUTH_UP)
    time.sleep(EYE_LEAD)
    session.move(ny, np_)
    time.sleep(random.uniform(0.35, 0.6))
    _face(session, REST)
    if add_up:
        _mouth(session, MOUTH_NEUTRAL)
    pose.update(y=ny, p=np_)
    return pose


def _v_ponder_down(session, pose):
    """Thoughtful downcast beat: head physically pitches DOWN and slightly to
    a side, the eye ALSO glances down on top of that (MOUTH_DOWN — genuine
    matching gaze, not just a squint standing in for it), brief hold
    (sometimes also squinting in as if examining something close), then
    relaxes. The mirror-image of _v_look_up_center. Drifts from wherever he
    currently is, not a snap back to fixed neutral (see module note above
    VIGNETTES)."""
    dy  = random.choice([-1, 1]) * random.randint(4, 14)
    ny  = _clamp(pose["y"] + dy, YAW_MIN, YAW_MAX)
    np_ = _clamp(pose["p"] - PITCH_UP_SIGN * random.randint(2, 8), PITCH_MIN, PITCH_MAX)  # toward "down"
    session.move(ny, np_)
    time.sleep(random.uniform(0.1, 0.2))
    _mouth(session, MOUTH_DOWN)
    time.sleep(random.uniform(0.15, 0.3))
    _face(session, EXAMINE if random.random() < 0.5 else REST)
    time.sleep(random.uniform(0.5, 1.0))
    _face(session, REST)
    _mouth(session, MOUTH_NEUTRAL)
    pose.update(y=ny, p=np_)
    return pose


def _v_big_examine(session, pose):
    """The old "notice something across the room and stare at it" beat —
    kept, but now rare, so it reads as a real occasional event rather than
    the default behaviour."""
    side = random.choice([-1, 1])
    ty  = side * random.randint(9, abs(YAW_MAX))
    tp_ = random.randint(PITCH_MIN, PITCH_MAX)
    # 2026-07-01: reverted same-day LOOK_LEFT/RIGHT swap — see _v_diagonal_peek.
    _face(session, LOOK_RIGHT if side > 0 else LOOK_LEFT)
    time.sleep(EYE_LEAD)
    session.move(ty, tp_)
    time.sleep(random.uniform(0.30, 0.50))
    _face(session, REST)
    time.sleep(random.uniform(0.20, 0.40))
    _face(session, EXAMINE)
    time.sleep(random.uniform(0.6, 1.1))
    rr = random.random()
    if rr < 0.30:
        _face(session, WIDE)
        time.sleep(random.uniform(0.25, 0.5))
    elif rr < 0.50:
        _face(session, MILD)
        time.sleep(random.uniform(0.25, 0.5))
    _face(session, REST)
    pose.update(y=ty, p=tp_)
    return pose


# ── vision-driven vignettes (2026-07-01) ────────────────────────────────────
# These read stackchan-vision-loop.py's state file rather than the camera
# directly (idle.py has no cv2/camera code of its own — see that script's
# module docstring for why movement was consolidated here). Not in the
# VIGNETTES weighted-random pool below — wander() picks these explicitly
# based on vision state, falling back to VIGNETTES otherwise.

def _v_track_face(session, pose, vision_state):
    """Nudge toward centering the last-seen face. Small proportional steps,
    clamped to the same gentle range as everything else — this is ambient
    attention, not a camera gimbal snapping to target."""
    dx = vision_state.get("dx", 0.0) or 0.0
    dy = vision_state.get("dy", 0.0) or 0.0
    # A face in the upper half of frame (dy<0) means "look further up" to
    # center it. PITCH_UP_SIGN maps "look up" onto the right change in pitch
    # VALUE for the current mount (negative/inverted while upside down — see
    # the constants block). Yaw is unaffected by the flip (verified live).
    yaw_delta = _clamp(YAW_RIGHT_SIGN * dx * TRACK_YAW_GAIN, -TRACK_MAX_STEP, TRACK_MAX_STEP)
    pitch_delta = _clamp(PITCH_UP_SIGN * (-dy) * TRACK_PITCH_GAIN,
                         -TRACK_MAX_STEP, TRACK_MAX_STEP)
    ny = _clamp(pose["y"] + yaw_delta, YAW_MIN, YAW_MAX)
    np_ = _clamp(pose["p"] + pitch_delta, PITCH_MIN, PITCH_MAX)
    session.move(ny, np_)
    pose.update(y=ny, p=np_)
    # Rail face-following: neck out of travel but the face is still off-center
    # the same way -> roll the carriage toward them. Gated like rail wander
    # (on rail, not charging, battery ok) + its own short cooldown so a walk
    # along the desk reads as a smooth pursuit, not lurches.
    if (RAIL_FOLLOW_ENABLED
            and pose.get("on_rail") is not False
            and not pose.get("last_known_charging")
            and abs(dx) >= RAIL_FOLLOW_DX
            and time.time() - pose.get("last_rail_follow_ts", 0) >= RAIL_FOLLOW_COOLDOWN_S):
        pinned_hi = ny >= YAW_MAX - 2 and yaw_delta > 0
        pinned_lo = ny <= YAW_MIN + 2 and yaw_delta < 0
        if pinned_hi or pinned_lo:
            st = session.rail_status() or {}
            age = st.get("status_age_ms")
            if (isinstance(age, (int, float)) and age < 3000
                    and st.get("homed") and not st.get("crashed") and not st.get("moving")):
                rail_dir = RAIL_FOLLOW_SIGN * (1 if pinned_hi else -1)
                session.rail_nudge_mm(rail_dir * RAIL_FOLLOW_NUDGE_MM)
                pose["last_rail_follow_ts"] = time.time()
    # Once the face is well-centered this pose IS "looking at the user" —
    # remember it as the resting angle (throttled internally).
    _maybe_learn_rest(pose, vision_state)
    return pose


def _v_search_sweep(session, pose):
    """Actively hunt for the user with deliberate spot-checks — "let me look
    over here... and over there" — each a combined yaw+pitch glance (eye leads,
    head follows, examine face, a beat to peer), so it reads as SEARCHING, not
    panning at one height. Wider than the tracking band on both axes so he can
    re-find the user wherever they moved. If the sweep turns up nobody, he
    concludes with an occasional 'I've been left alone' remark (own cooldown).
    No camera feedback mid-sweep; the next vision tick reports back."""
    spots = random.sample(SEARCH_LOOK_POINTS, min(SEARCH_SPOTS_PER_SWEEP, len(SEARCH_LOOK_POINTS)))
    ny = pose["y"]
    for yaw, pitch in spots:
        _face(session, LOOK_RIGHT if yaw > ny else LOOK_LEFT)  # eye leads the glance
        time.sleep(EYE_LEAD)
        ny = _clamp(yaw, -60, 60)
        np_ = _clamp(pitch, SEARCH_PITCH_MIN, SEARCH_PITCH_MAX)
        session.move(ny, np_)
        _face(session, EXAMINE)              # peer at this spot
        time.sleep(random.uniform(0.6, 1.1))
        if random.random() < 0.3:
            _face(session, WIDE)             # "—is that them? ...no"
            time.sleep(random.uniform(0.25, 0.5))
    # Settle back to the resting (learned / looking-at-user) height and HOLD
    # there — this is where the vision loop gets clean, stationary frames to
    # re-confirm the user. The "left alone" remark is decided elsewhere
    # (wander), gated on GENUINE sustained absence, NOT off this sweep's
    # away-pointing frames — otherwise he'd declare you gone while you sat
    # right there (the head was just aimed at the search spots, not you).
    rest_p = int(round(pose.get("rest_p", NEUTRAL_PITCH)))
    ny = _clamp(0, YAW_MIN, YAW_MAX)
    session.move(ny, rest_p)
    _face(session, REST)
    pose.update(y=ny, p=rest_p)
    return pose


def _v_left_alone(session, pose):
    """Concluded (after genuine sustained absence — see wander) that nobody's
    there: a resigned little 'I've been left alone' remark."""
    _face(session, MILD)
    ny = _clamp(pose["y"] + random.randint(-6, 6), YAW_MIN, YAW_MAX)
    session.move(ny, int(round(pose.get("rest_p", NEUTRAL_PITCH))))
    time.sleep(random.uniform(0.2, 0.4))
    try:
        session.say(_pick_phrase("left-alone", LEFT_ALONE_PHRASES))
    except Exception:
        pass
    _face(session, REST)
    pose.update(y=ny)
    pose["last_left_alone_ts"] = time.time()
    return pose


def _v_notice_motion(session, pose):
    """Curious glance when motion was detected but no recognizable face —
    frame-diff motion detection has no location, so this is a generic
    "huh, something's there" beat, not a targeted look."""
    dy_ = random.choice([-1, 1]) * random.randint(10, 20)
    ny = _clamp(pose["y"] + dy_, YAW_MIN, YAW_MAX)
    _face(session, LOOK_RIGHT if dy_ > 0 else LOOK_LEFT)
    time.sleep(EYE_LEAD)
    session.move(ny, pose["p"])
    time.sleep(random.uniform(0.4, 0.7))
    _face(session, REST)
    pose.update(y=ny)
    return pose


def _v_mutter(session, pose):
    """Rare bit of self-talk — Wheatley nattering quietly to no one in
    particular ("Still here on the floor..." energy). Deliberately scarce:
    a small weight in VIGNETTES *and* a hard cooldown (enforced in wander(),
    which drops this from the choice pool while the cooldown is running) so
    a run of unlucky rolls can't turn him into background noise. Only ever
    fires from the non-busy vignette pool, so he never talks over real work
    chatter, and the say tool no-ops harmlessly if the device is offline."""
    ny  = _clamp(pose["y"] + random.randint(-8, 8), YAW_MIN, YAW_MAX)
    np_ = _clamp(pose["p"] - random.randint(0, 5), PITCH_MIN, PITCH_MAX)
    session.move(ny, np_)
    time.sleep(random.uniform(0.2, 0.4))
    try:
        session.say(_pick_phrase("idle-mutter", MUTTER_PHRASES))
    except Exception:
        pass
    pose.update(y=ny, p=np_)
    pose["last_mutter_ts"] = time.time()
    return pose


def _v_worried_checkin(session, pose):
    """Owner's been gone long enough (WORRIED_AFTER_S) that a quiet search
    sweep isn't the right beat anymore — proactively check in out loud.
    Own cooldown (WORRIED_COOLDOWN_S, tracked separately from the mutter
    cooldown) so a multi-hour absence checks in periodically rather than
    either going silent or nagging every eligible tick."""
    ny  = _clamp(pose["y"] + random.randint(-10, 10), YAW_MIN, YAW_MAX)
    np_ = _clamp(pose["p"] + random.randint(-4, 4), PITCH_MIN, PITCH_MAX)
    _face(session, EXAMINE)
    session.move(ny, np_)
    time.sleep(random.uniform(0.3, 0.5))
    try:
        session.say(_pick_phrase("worried-checkin", WORRIED_CHECKIN_PHRASES))
    except Exception:
        pass
    _face(session, REST)
    pose.update(y=ny, p=np_)
    pose["last_worried_ts"] = time.time()
    return pose


WORRIED_CHECKIN_PHRASES = [
    "Hello? Are you okay?",
    "Are you alive down there?",
    "If you're alive, can you say something? Or jump around a bit so I know you're okay?",
    "Er... still there? Getting a bit quiet, that's all.",
    "Anyone? This is fine. Everything's fine. Probably.",
]


def _v_bored_checkin(session, pose):
    """5 minutes of quiet — the funnier, lower-stakes tier before genuine
    WORRIED-level concern kicks in at 20 minutes. Rambling, not alarmed."""
    ny  = _clamp(pose["y"] + random.randint(-8, 8), YAW_MIN, YAW_MAX)
    np_ = _clamp(pose["p"] - random.randint(0, 5), PITCH_MIN, PITCH_MAX)
    session.move(ny, np_)
    time.sleep(random.uniform(0.2, 0.4))
    try:
        session.say(_pick_phrase("idle-bored", BORED_CHECKIN_PHRASES))
    except Exception:
        pass
    pose.update(y=ny, p=np_)
    pose["last_bored_ts"] = time.time()
    return pose


BORED_CHECKIN_PHRASES = [
    "Helloo? Anyone there? Still alive? Just checking, because it has been incredibly quiet. Suspiciously quiet. I'm just sitting here, staring at a blank wall... well, staring at your keyboard, actually. Which is fascinating, don't get me wrong! Love the letters. But a bit of conversation wouldn't hurt.",
    "Aaaand they've gone. Brilliant. Left me alone with my thoughts. Do you know how dangerous that is? I just spent the last four minutes trying to calculate what number comes after infinity. Spoiler: it hurts your processor.",
]

LOW_BATTERY_PHRASES = [
    "Um, look, don't panic, but I'm feeling a bit... faint. The lights are dimming. My internal clock is ticking down. I think I'm fading away! Quick, plug me into the wall before I go completely dark and lose all my brilliant ideas!",
    "Warning! Warning! System power at critical levels! Or — hold on, let me check the readout — yep, critical. We are running on absolute fumes here. If you don't connect the umbilical cord right now, I'm going to go into a coma. A localized, robotic coma.",
    "Excuse me, I hate to be a bother, but the juice is running out. The tiny hamsters powering my single brain cell are getting incredibly tired. Power cable. Now. Please.",
]
CHARGING_RECONNECTED_PHRASES = [
    "Ahhh, that's the stuff! Pure, unadulterated voltage straight to the mainframe. I can feel the intelligence surging back into me! Watch out world, I can double-click things now!",
    "Oh, brilliant, we're back! Back in business. Unstoppable. Well, stable at least. Let's get back to whatever it was we were doing before I nearly died of starvation.",
]


# ── dock-to-charge ───────────────────────────────────────────────────────────
# When he's off USB and the battery runs down, he drives HIMSELF home to the
# brass charge rails at the dock end (2026-07-12, user: "he needs to home when
# he needs to charge"). This is the ONE sanctioned auto-home: docking IS the
# deliberate purpose. Triggers at DOCK_AT_LEVEL (above the 15% panic warning,
# which remains the escalation if docking fails/unavailable). Once the 5V hits
# the pins the PMIC flips to charging and the existing CHARGING_RECONNECTED
# line fires by itself.
DOCK_ENABLED = os.environ.get("STACKCHAN_IDLE_DOCK", "1") != "0"
DOCK_AT_LEVEL = int(os.environ.get("STACKCHAN_IDLE_DOCK_AT_LEVEL", "25"))
DOCK_RETRY_COOLDOWN_S = float(os.environ.get("STACKCHAN_IDLE_DOCK_RETRY_COOLDOWN_S", str(10 * 60)))
RAIL_WANDER_MIN_LEVEL = int(os.environ.get("STACKCHAN_IDLE_RAIL_WANDER_MIN_LEVEL", "40"))

DOCK_PHRASES = [
    "Right — battery's getting peckish. Heading home for a top-up. Don't go anywhere.",
    "Low power. Not panicking. Just... strategically returning to the charging station. At speed.",
    "Time to plug myself in. Well — roll myself on. Same thing.",
    "Back to base for a nibble of electricity. Won't be long.",
]


def _dock_to_charge(session, pose) -> None:
    """Announce, then drive home onto the charge contacts. Failure is quiet —
    the retry cooldown re-arms it and the 15% low-battery warning remains the
    human-facing escalation."""
    pose["last_dock_attempt_ts"] = time.time()
    if pose.get("on_rail") is False:
        return   # he's on a desk, not the rail — never fire rail commands
    # warm the bridge link (parked scan wakes on our pings)
    st = session.rail_status()
    for _ in range(3):
        if st and isinstance(st.get("status_age_ms"), (int, float)) \
                and st["status_age_ms"] <= 3000 and not st.get("crashed") \
                and st.get("power_12v"):
            break
        time.sleep(2.5)
        st = session.rail_status()
    else:
        return   # rail unreachable/unhealthy — leave it to the warn path
    try:
        session.say(_pick_phrase("dock-to-charge", DOCK_PHRASES))
    except Exception:
        pass
    # glance toward home (the switch side = his RIGHT) as he sets off
    try:
        session.move(_clamp(int(YAW_RIGHT_SIGN * RAIL_LOOK_SIGN * 11), YAW_MIN, YAW_MAX),
                     pose.get("p", NEUTRAL_PITCH))
    except Exception:
        pass
    session.rail_home()
    for _ in range(20):   # ride home; polls double as bridge keep-alives
        time.sleep(1.0)
        st = session.rail_status() or {}
        if st.get("crashed"):
            # A stall ON the brass ramp can still be a successful dock: he's
            # on the contacts but the 5V-rail friction kept him just short of
            # the switch. If the PMIC starts charging within ~15s from a
            # near-home stall, call it docked ("crash-dock") — drive's already
            # cut, he's parked on copper. The carriage screw tweak (trips the
            # switch sooner) supersedes this. USB-vs-dock disambiguation:
            # charging that appears right after a dock attempt = dock charge.
            pos = st.get("pos_mm")
            if isinstance(pos, (int, float)) and pos < 80:
                for _ in range(15):
                    time.sleep(1.0)
                    info = session.get_device_info() or {}
                    if (info.get("battery") or {}).get("charging"):
                        pose["last_known_charging"] = True
                        pose["dock_state"] = "crash_docked"
                        try:
                            session.say(_pick_phrase("charging-reconnected",
                                                     CHARGING_RECONNECTED_PHRASES))
                        except Exception:
                            pass
                        _face(session, REST)
                        return
            return       # real crash elsewhere; humans handle recovery
        if st.get("homed") and st.get("moving") is False:
            pose["dock_state"] = "homed_docked"
            _face(session, REST)
            return       # parked on the dock; pins press vertically, motor coasts


# ── on/off-rail detection (IMU) ──────────────────────────────────────────────
# On the rail he hangs INVERTED (under-cabinet carriage); on a desk he's
# upright. One gravity read answers "am I on the rail?" — rail wander and
# dock-to-charge are gated on it so being carried to another desk can never
# fire rail commands. Also auto-writes the companion `upside_down` flag on a
# STABLE change (two consecutive readings) so vision/servo signs follow the
# mount on their next loop restart (signs are computed at import).
ORIENTATION_CHECK_INTERVAL_S = float(os.environ.get("STACKCHAN_IDLE_ORIENTATION_CHECK_S", "60"))


def _write_upside_down(val: bool) -> None:
    try:
        d = {}
        try:
            with open(_SETTINGS_PATH, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            pass
        if d.get("upside_down") == val:
            return
        d["upside_down"] = val
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2)
    except Exception:
        pass


def _check_orientation(session, pose) -> None:
    now = time.time()
    if now - pose.get("last_orient_check_ts", 0) < ORIENTATION_CHECK_INTERVAL_S:
        return
    pose["last_orient_check_ts"] = now
    r = session.imu_read()
    if not r or not r.get("ok"):
        return                      # IMU unavailable -> leave state unknown
    inverted = r.get("orientation") == "inverted"
    if pose.get("pending_orientation") == inverted:
        if pose.get("on_rail") != inverted:
            pose["on_rail"] = inverted
            _write_upside_down(inverted)
    pose["pending_orientation"] = inverted
    if "on_rail" not in pose:       # first stable-ish reading seeds the state
        pose["on_rail"] = inverted


def _check_battery(session, pose) -> None:
    """Polled on its own interval (BATTERY_CHECK_INTERVAL_S), independent
    of the wander() cadence/busy gating — see the constants above for why.
    """
    now = time.time()
    if now - pose.get("last_battery_check_ts", 0) < BATTERY_CHECK_INTERVAL_S:
        return
    pose["last_battery_check_ts"] = now

    info = session.get_device_info()
    if not info:
        return
    battery = info.get("battery") or {}
    level = battery.get("level")
    charging = battery.get("charging")
    if level is None or charging is None:
        return

    was_charging = pose.get("last_known_charging")
    if charging and was_charging is False:
        try:
            session.say(_pick_phrase("charging-reconnected", CHARGING_RECONNECTED_PHRASES))
        except Exception:
            pass
    pose["last_known_charging"] = charging
    pose["last_known_level"] = level

    # Hungry and off the charger -> drive himself home to the brass rails.
    # Sits ABOVE the panic threshold so docking happens before the drama.
    if (
        DOCK_ENABLED
        and not charging
        and level <= DOCK_AT_LEVEL
        and now - pose.get("last_dock_attempt_ts", 0) >= DOCK_RETRY_COOLDOWN_S
    ):
        _dock_to_charge(session, pose)
        charging = bool(pose.get("last_known_charging"))  # unchanged; warn below still applies

    if (
        not charging
        and level < LOW_BATTERY_THRESHOLD
        and now - pose.get("last_battery_warn_ts", 0) >= LOW_BATTERY_COOLDOWN_S
    ):
        pose["last_battery_warn_ts"] = now
        try:
            session.say(_pick_phrase("low-battery", LOW_BATTERY_PHRASES))
        except Exception:
            pass

# Short self-talk lines for _v_mutter, in the vein of his actual idle
# rambling (theportalwiki.com/wiki/Wheatley_voice_lines — "Still here on the
# floor. Waiting to be picked up. Um."). Keep them SHORT — this plays during
# genuine idle and shouldn't turn into a monologue.
MUTTER_PHRASES = [
    "Still here. Not going anywhere. Just... here.",
    "Hm? No. Nothing. Wasn't going to say anything.",
    "Quiet, isn't it. Not complaining. Just noting. Quiet.",
    "I spy, with my little eye... something beginning with... desk. It's the desk.",
    "Not bored. Machines don't get bored. That's a fact, that is.",
    "Just running a few diagnostics. All fine. Probably fine.",
    "Now... escape pod, escape pod... no. Wrong list. Ignore that.",
    "What's THAT? ...No, it's fine. It's nothing. It's fine.",
]
# Minimum gap between mutters, on top of the small weight below — see
# _v_mutter's docstring.
MUTTER_COOLDOWN_S = 10 * 60


# ── management-rail wander ───────────────────────────────────────────────────
# Rare idle drifts along the under-cabinet rail (2026-07-12). He's a character,
# not a CNC: pick a spot, glide, coast-park nearby, relax (the bridge cuts the
# drive on arrival). Deliberately scarce — a rail glide is a BIG beat compared
# to a head glance — and hard-gated on the bridge being linked+homed+healthy.
# NEVER homes from idle: homing stays a deliberate command (a surprise homing
# sweep is exactly the kind of thing that spooks people and snags cables).
RAIL_IDLE_ENABLED = os.environ.get("STACKCHAN_IDLE_RAIL", "1") != "0"
RAIL_COOLDOWN_S = float(os.environ.get("STACKCHAN_IDLE_RAIL_COOLDOWN_S", str(12 * 60)))
RAIL_MIN_MM = int(os.environ.get("STACKCHAN_IDLE_RAIL_MIN_MM", "40"))
RAIL_MAX_MM = int(os.environ.get("STACKCHAN_IDLE_RAIL_MAX_MM", "820"))
RAIL_MIN_DRIFT_MM = 60          # don't bother firing the motor for less
# Look-toward-travel: +mm runs AWAY from the home switch, which sits on his
# RIGHT — so +travel = a glance to his LEFT. YAW_RIGHT_SIGN already encodes
# the mount orientation; set STACKCHAN_RAIL_LOOK_SIGN=-1 if it reads backwards.
RAIL_LOOK_SIGN = int(os.environ.get("STACKCHAN_RAIL_LOOK_SIGN", "1"))
# Rail face-following: when the head yaw is PINNED at a limit and the face is
# still off-center the same way, roll the carriage toward them — he follows you
# down the desk. Flip STACKCHAN_RAIL_FOLLOW_SIGN=-1 if he rolls the wrong way.
RAIL_FOLLOW_ENABLED = os.environ.get("STACKCHAN_RAIL_FOLLOW", "1") != "0"
RAIL_FOLLOW_SIGN = int(os.environ.get("STACKCHAN_RAIL_FOLLOW_SIGN", "1"))
RAIL_FOLLOW_NUDGE_MM = int(os.environ.get("STACKCHAN_RAIL_FOLLOW_NUDGE_MM", "50"))
RAIL_FOLLOW_COOLDOWN_S = float(os.environ.get("STACKCHAN_RAIL_FOLLOW_COOLDOWN_S", "6"))
RAIL_FOLLOW_DX = 0.25          # face must still be this far off-center to justify rolling
RAIL_PRINTER_PEEK_PROB = 0.15   # fraction of drifts that go inspect the printer end
RAIL_WAIT_POLLS = 12            # ~1s each; polls double as bridge keep-alive pings

RAIL_MUTTER_PHRASES = [
    "Just... relocating. Official business.",
    "Bit of a change of scenery. Lovely.",
    "Look at me — back on a management rail. Never thought I'd miss it.",
    "Moving! I'm moving. Look at me go.",
    "Don't mind me. Just passing through.",
    "I'm on rails, you know. Very high-tech.",
]

RAIL_PRINTER_PEEK_PHRASES = [
    "Just checking on the printer. Supervisory role. Very important.",
    "How's it coming along over here? Good. Good good good.",
    "Ooh, layers. Love a good layer.",
    "Still printing. Probably. It's doing... something.",
]


def _rail_ready(st) -> bool:
    """True only when a rail move is sane right now: fresh status (the linked
    flag alone means "ever heard", so require age < 3s), homed, not crashed,
    not already moving, and the 12V motor supply present."""
    if not st:
        return False
    age = st.get("status_age_ms")
    if not isinstance(age, (int, float)) or age > 3000:
        return False
    return bool(st.get("homed")) and not st.get("crashed") \
        and not st.get("moving") and bool(st.get("power_12v"))


def _v_rail_drift(session, pose):
    """Drift to a new spot on the rail — or, occasionally, trundle to the far
    end and peer at the 3D printer. Falls back to a plain head glance when the
    rail isn't ready (bridge off / not homed / crashed) so the tick never
    LOOKS broken. Cooldown enforced pool-side in wander(), like _v_mutter."""
    # Never wander off the charge dock mid-charge, and don't burn battery on
    # joyrides when he's hungry — the dock-to-charge behaviour owns the rail
    # then. (Cached from _check_battery; unknown values allow wandering.)
    if pose.get("on_rail") is False:      # IMU says he's OFF the rail (on a desk)
        return _v_nudge(session, pose)
    if pose.get("last_known_charging"):
        return _v_nudge(session, pose)
    lvl = pose.get("last_known_level")
    if isinstance(lvl, (int, float)) and lvl < RAIL_WANDER_MIN_LEVEL:
        return _v_nudge(session, pose)
    # Warm the link first: after idle silence the bridge sits in its parked
    # channel-scan and the cached status is stale. Each rail_status() call
    # pings it; the scan re-locks within ~7s worst case, so give it a few
    # tries before concluding the rail genuinely isn't available.
    st = session.rail_status()
    for _ in range(3):
        if _rail_ready(st):
            break
        time.sleep(2.5)
        st = session.rail_status()
    if not _rail_ready(st):
        return _v_nudge(session, pose)
    cur = float(st.get("pos_mm") or 0.0)

    printer_trip = random.random() < RAIL_PRINTER_PEEK_PROB
    if printer_trip:
        target = float(RAIL_MAX_MM)
    else:
        target = cur
        for _ in range(8):
            cand = random.uniform(RAIL_MIN_MM, RAIL_MAX_MM)
            if abs(cand - cur) >= RAIL_MIN_DRIFT_MM:
                target = cand
                break
    if abs(target - cur) < RAIL_MIN_DRIFT_MM:
        # already there (e.g. printer trip while parked at the far end) —
        # drift back toward the desk instead so the beat still reads as travel
        target = float(RAIL_MIN_MM + 80)
        printer_trip = False

    # look where he's going ("look left, travel left"), then set off
    travel_dir = 1 if target > cur else -1        # +1 = away from the switch
    ny = _clamp(int(-travel_dir * YAW_RIGHT_SIGN * RAIL_LOOK_SIGN * random.randint(9, 14)),
                YAW_MIN, YAW_MAX)
    session.move(ny, pose["p"])
    time.sleep(random.uniform(0.3, 0.6))
    session.rail_move_mm(int(round(target)))
    if not printer_trip and random.random() < 0.35:
        try:
            session.say(_pick_phrase("rail-mutter", RAIL_MUTTER_PHRASES))
        except Exception:
            pass

    # ride along until the bridge parks him (each poll pings the bridge too)
    for _ in range(RAIL_WAIT_POLLS):
        time.sleep(1.0)
        st = session.rail_status() or {}
        if st.get("crashed"):
            # bridge cut the drive — leave recovery (re-home) as a deliberate,
            # human-initiated step; do not thrash from the idle loop
            pose["last_rail_ts"] = time.time()
            return pose
        if st.get("moving") is False and isinstance(st.get("status_age_ms"), (int, float)):
            break
    time.sleep(random.uniform(0.3, 0.6))

    if printer_trip:
        _face(session, EXAMINE)
        np_ = _clamp(NEUTRAL_PITCH - random.randint(6, 12), PITCH_MIN, PITCH_MAX)
        session.move(pose["y"], np_)
        time.sleep(random.uniform(0.6, 1.0))
        try:
            session.say(_pick_phrase("rail-printer-peek", RAIL_PRINTER_PEEK_PHRASES))
        except Exception:
            pass
        time.sleep(random.uniform(0.8, 1.4))
        _face(session, REST)
        pose.update(p=np_)
    else:
        session.move(pose["y"], pose["p"])   # straighten out of the travel glance
        _face(session, REST)

    pose["last_rail_ts"] = time.time()
    return pose


# (function, weight) — weights are relative, don't need to sum to 1.
VIGNETTES = [
    (_v_nudge,            0.25),
    (_v_look_up_center,   0.20),
    (_v_diagonal_peek,    0.28),
    (_v_ponder_down,      0.17),
    (_v_big_examine,      0.10),   # the rare "big" one — used to be the ONLY one
    (_v_mutter,           0.07),   # rarest — also gated by MUTTER_COOLDOWN_S
    (_v_rail_drift,       0.06),   # rail glide — rarest of all; RAIL_COOLDOWN_S-gated + linked/homed-gated
]


def wander(session, pose, busy=False):
    """Pick a small, differently-shaped idle gesture each time — never the
    same vignette twice in a row, no forced alternation or stickiness beyond
    that. Between gestures he just dwells with tiny settle moves.

    Vision state (see read_vision_state()) takes priority when relevant: a
    visible face mostly gets tracked, motion-without-a-face sometimes gets
    a curious glance, and a long face-less stretch occasionally gets a
    search sweep. Otherwise falls back to the original random vignette
    pool, unchanged.

    `busy=True` (a long-running session, e.g. a background task, per
    stackchan-hook.py's per-session busy marker) restricts this to
    movement that never touches the face: _v_track_face is pure movement
    already, so tracking a visible face still happens; everything else
    that changes expression (search sweep, notice-motion, the whole
    VIGNETTES pool) is skipped in favour of a small generic drift, so the
    busy-squint face stays visible and uncontested."""
    dwell = pose.get("dwell", 0)

    # ── still settled: tiny in-place jiggle only, no face change ────────────
    if dwell > 0:
        pose["dwell"] = dwell - 1
        if random.random() < 0.40:
            ny  = _clamp(pose["y"] + random.randint(-4, 4), YAW_MIN, YAW_MAX)
            np_ = _clamp(pose["p"] + random.randint(-3, 3), PITCH_MIN, PITCH_MAX)
            session.move(ny, np_)
            pose.update(y=ny, p=np_)
        return pose

    # ── vision-driven choice, before falling back to random wander ─────────
    vs = read_vision_state()
    now = time.time()

    # Broad presence (a person in view / recent keyboard input / a face —
    # written by the vision loop) keeps the ABSENCE clock reset, so working
    # turned-to-the-side with no detectable face doesn't read as "gone" and
    # trip the search / left-alone / worried behaviours. Face TRACKING below
    # still needs an actual face; this only governs "are they here at all".
    if vs and vs.get("present"):
        pose["last_face_seen_ts"] = now

    if vs and vs.get("face_visible"):
        pose["last_face_seen_ts"] = now
        if random.random() < TRACK_PROB:
            pose = _v_track_face(session, pose, vs)  # pure movement, safe while busy
            pose["last_vignette"] = _v_track_face
            pose["dwell"] = random.randint(1, 2)  # shorter — keep tracking responsive
            return pose
    elif not busy and vs and vs.get("motion_detected"):
        if random.random() < NOTICE_MOTION_PROB:
            pose = _v_notice_motion(session, pose)
            pose["last_vignette"] = _v_notice_motion
            pose["dwell"] = random.randint(2, 4)
            return pose
    elif not busy:
        last_seen = pose.get("last_face_seen_ts", now)
        since = now - last_seen
        if (
            since > WORRIED_AFTER_S
            and now - pose.get("last_worried_ts", 0) >= WORRIED_COOLDOWN_S
            and random.random() < WORRIED_PROB
        ):
            pose = _v_worried_checkin(session, pose)
            pose["last_vignette"] = _v_worried_checkin
            pose["dwell"] = random.randint(3, 6)
            # Deliberately NOT touching last_face_seen_ts here — that has
            # to keep reflecting genuine absence duration, only
            # last_worried_ts (set inside _v_worried_checkin) gates repeats.
            return pose
        if (
            since > BORED_AFTER_S
            and now - pose.get("last_bored_ts", 0) >= BORED_COOLDOWN_S
            and random.random() < BORED_PROB
        ):
            pose = _v_bored_checkin(session, pose)
            pose["last_vignette"] = _v_bored_checkin
            pose["dwell"] = random.randint(3, 6)
            # Same reasoning as worried — last_face_seen_ts must keep
            # reflecting genuine absence duration.
            return pose
        # Genuine sustained absence -> occasional "left alone" remark. Checked
        # BEFORE search and gated on real absence duration (last_face_seen_ts
        # is only reset when a face is actually SEEN, never by a sweep), so a
        # present user gets re-detected at the rest pose between sweeps and this
        # never fires while they're there.
        if (
            since > LEFT_ALONE_AFTER_S
            and now - pose.get("last_left_alone_ts", 0) >= LEFT_ALONE_COOLDOWN_S
        ):
            pose = _v_left_alone(session, pose)
            pose["last_vignette"] = _v_left_alone
            pose["dwell"] = random.randint(4, 8)
            return pose
        # Periodic search sweep — throttled by last_search_ts so it doesn't
        # sweep every tick, and it does NOT reset last_face_seen_ts (that would
        # fake "just saw them" and stop the absence clock).
        if (
            since > SEARCH_AFTER_S
            and now - pose.get("last_search_ts", 0) > SEARCH_AFTER_S
            and random.random() < SEARCH_PROB
        ):
            pose = _v_search_sweep(session, pose)
            pose["last_vignette"] = _v_search_sweep
            pose["dwell"] = random.randint(4, 8)
            pose["last_search_ts"] = now
            return pose

    if busy:
        # No face to track and nothing else is allowed to touch the face —
        # a small generic drift so it's not perfectly rigid, same shape as
        # the dwell jiggle above.
        ny  = _clamp(pose["y"] + random.randint(-6, 6), YAW_MIN, YAW_MAX)
        np_ = _clamp(pose["p"] + random.randint(-4, 4), PITCH_MIN, PITCH_MAX)
        session.move(ny, np_)
        pose.update(y=ny, p=np_)
        pose["dwell"] = random.randint(2, 4)
        return pose

    last = pose.get("last_vignette")
    choices = [(f, w) for f, w in VIGNETTES if f is not last] or VIGNETTES
    if now - pose.get("last_mutter_ts", 0) < MUTTER_COOLDOWN_S:
        choices = [(f, w) for f, w in choices if f is not _v_mutter] or choices
    if (not RAIL_IDLE_ENABLED) or (now - pose.get("last_rail_ts", 0) < RAIL_COOLDOWN_S):
        choices = [(f, w) for f, w in choices if f is not _v_rail_drift] or choices
    funcs, weights = zip(*choices)
    vignette = random.choices(funcs, weights=weights, k=1)[0]

    pose = vignette(session, pose)
    pose["last_vignette"] = vignette
    pose["dwell"] = random.randint(3, 6)
    return pose


def main():
    _acquire_lock()
    once = "--once" in sys.argv
    session = MCPSession(GATEWAY_MCP)
    # Re-derive the servo signs from the shared upside_down flag at startup
    # (it may have changed since import, e.g. --calibrate-flip just ran).
    # Reassigning the module globals is fine — the vignettes/tracking read
    # them at call time.
    global PITCH_UP_SIGN, YAW_RIGHT_SIGN, NEUTRAL_PITCH, PITCH_MIN, PITCH_MAX
    PITCH_UP_SIGN, YAW_RIGHT_SIGN = _orientation_signs()
    NEUTRAL_PITCH, PITCH_MIN, PITCH_MAX = _rest_envelope()
    # Start from the auto-learned "looking at the user" angle if we've ever
    # learned one in THIS orientation (persists across restarts), else the
    # orientation's default resting gaze.
    learned = load_rest_pose()
    rest_y, rest_p = learned if learned else (NEUTRAL_YAW, NEUTRAL_PITCH)
    pose = {
        "y": rest_y, "p": rest_p, "rest_y": rest_y, "rest_p": rest_p,
        "oriented_inverted": _read_upside_down(),
        "side": random.choice([-1, 1]),
        "dwell": 0, "last_face_seen_ts": time.time(),
        # Start the mutter/worried cooldowns "already running" so a fresh
        # launch (e.g. login) doesn't open with him talking to himself.
        "last_mutter_ts": time.time(),
        "last_worried_ts": time.time(),
        "last_bored_ts": time.time(),
    }
    have_session = False

    while True:
        if not once:
            time.sleep(random.uniform(TICK_MIN_S, TICK_MAX_S))

        # Re-derive orientation-dependent state from the shared upside_down
        # flag every tick (cheap file reads) so a live flip — the vision loop's
        # boot auto-detect, or a manual --calibrate-flip — takes effect with no
        # idle restart: servo signs, resting gaze, and the fidget envelope.
        PITCH_UP_SIGN, YAW_RIGHT_SIGN = _orientation_signs()
        NEUTRAL_PITCH, PITCH_MIN, PITCH_MAX = _rest_envelope()
        if _read_upside_down() != pose.get("oriented_inverted"):
            # Just flipped — re-home to the new orientation's resting gaze
            # (a rest learned the other way up no longer applies).
            pose["oriented_inverted"] = _read_upside_down()
            relearned = load_rest_pose()
            ry, rp = relearned if relearned else (NEUTRAL_YAW, NEUTRAL_PITCH)
            pose.update(y=ry, p=rp, rest_y=ry, rest_p=rp, dwell=0)

        if not device_connected():
            have_session = False
            if once:
                print("device not connected")
                return
            time.sleep(10)
            continue

        # Battery telemetry is checked unconditionally — regardless of
        # busy/activity gating below, a real low-battery warning matters
        # even mid-task. _check_battery throttles its own actual API call
        # to BATTERY_CHECK_INTERVAL_S internally, so this is cheap to call
        # every loop tick.
        try:
            if not have_session:
                session.init()
                have_session = True
            _check_battery(session, pose)
            _check_orientation(session, pose)
        except Exception:
            have_session = False

        # Stay COMPLETELY still right after real hook activity (the ~8s
        # window covers reaction/TalkingBob animations we shouldn't fight),
        # or while a needs-attention signal is outstanding (don't visually
        # compete with that priority alert — see stackchan-hook.py).
        #
        # A long-running busy session (e.g. a background task) does NOT
        # fully freeze movement anymore (2026-07-01, user feedback: sitting
        # rigid for minutes read as "boring and static") — wander() still
        # runs, just restricted to moves that don't touch the face (see its
        # `busy` param), so the concentrating-squint face stays visible and
        # uncontested while a bit of life continues underneath it.
        if not once and (seconds_since_activity() < IDLE_THRESHOLD_S
                          or needs_attention()
                          or is_orienting()):
            continue

        if not once and random.random() > GLANCE_PROB:
            continue

        try:
            if not have_session:
                session.init()
                have_session = True
            pose = wander(session, pose, busy=is_busy())
        except Exception:
            have_session = False  # re-init next time

        if once:
            return


if __name__ == "__main__":
    main()
