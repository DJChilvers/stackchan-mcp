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
# Gentle envelope. Pitch convention: HIGHER pitch = look UP, LOWER = look
# DOWN (2026-07-01: confirmed via an isolated servo-only test — pitch=5,
# the minimum, physically looked down; pitch=85, the maximum, looked up.
# Previously documented backwards as "lower = up", which flipped the
# direction of the two vignettes below that reference a specific up/down —
# see _v_look_up_center / _v_ponder_down.)
YAW_MIN, YAW_MAX = -24, 24
PITCH_MIN, PITCH_MAX = 28, 46
NEUTRAL_YAW, NEUTRAL_PITCH = 0, 36

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
SEARCH_YAW_POINTS = [-50, -25, 25, 50, 0]  # wider than the idle range — genuinely "looking around"


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
        self._post({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "move_head",
                               "arguments": {"yaw": int(yaw),
                                             "pitch": int(pitch)}}})

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
    np_ = _clamp(pose["p"] + random.randint(2, 8), PITCH_MIN, PITCH_MAX)  # toward "up"
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
    np_ = _clamp(pose["p"] - random.randint(2, 8), PITCH_MIN, PITCH_MAX)  # toward "down"
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
    # Pitch convention (2026-07-01, confirmed live): HIGHER pitch = look UP,
    # LOWER = look DOWN. A face in the upper half of frame (dy negative)
    # needs MORE pitch to center it, hence the negation below.
    yaw_delta = _clamp(dx * TRACK_YAW_GAIN, -TRACK_MAX_STEP, TRACK_MAX_STEP)
    pitch_delta = _clamp(-dy * TRACK_PITCH_GAIN, -TRACK_MAX_STEP, TRACK_MAX_STEP)
    ny = _clamp(pose["y"] + yaw_delta, YAW_MIN, YAW_MAX)
    np_ = _clamp(pose["p"] + pitch_delta, PITCH_MIN, PITCH_MAX)
    session.move(ny, np_)
    pose.update(y=ny, p=np_)
    return pose


def _v_search_sweep(session, pose):
    """Actively look for a face — wider excursions than normal wander,
    framed as "searching" rather than ambient character motion. No camera
    feedback mid-sweep (idle.py doesn't see frames); it just sweeps and
    lets the next vision-loop tick (~8s cadence, independent of this) report
    back whether it found anyone."""
    _face(session, EXAMINE)
    ny, np_ = pose["y"], pose["p"]
    for yaw in SEARCH_YAW_POINTS:
        ny = _clamp(yaw, -60, 60)
        session.move(ny, NEUTRAL_PITCH)
        time.sleep(random.uniform(0.5, 0.8))
    np_ = NEUTRAL_PITCH
    _face(session, REST)
    pose.update(y=ny, p=np_)
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


# (function, weight) — weights are relative, don't need to sum to 1.
VIGNETTES = [
    (_v_nudge,            0.25),
    (_v_look_up_center,   0.20),
    (_v_diagonal_peek,    0.28),
    (_v_ponder_down,      0.17),
    (_v_big_examine,      0.10),   # the rare "big" one — used to be the ONLY one
    (_v_mutter,           0.07),   # rarest — also gated by MUTTER_COOLDOWN_S
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
        if now - last_seen > SEARCH_AFTER_S and random.random() < SEARCH_PROB:
            pose = _v_search_sweep(session, pose)
            pose["last_vignette"] = _v_search_sweep
            pose["dwell"] = random.randint(4, 8)
            pose["last_face_seen_ts"] = now  # don't sweep again immediately
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
    pose = {
        "y": NEUTRAL_YAW, "p": NEUTRAL_PITCH, "side": random.choice([-1, 1]),
        "dwell": 0, "last_face_seen_ts": time.time(),
        # Start the mutter cooldown "already running" so a fresh launch
        # (e.g. login) doesn't open with him talking to himself.
        "last_mutter_ts": time.time(),
    }
    have_session = False

    while True:
        if not once:
            time.sleep(random.uniform(TICK_MIN_S, TICK_MAX_S))

        if not device_connected():
            have_session = False
            if once:
                print("device not connected")
                return
            time.sleep(10)
            continue

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
                          or needs_attention()):
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
