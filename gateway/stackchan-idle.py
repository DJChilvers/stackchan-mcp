#!/usr/bin/env python3
"""Ambient idle "nervous fidget" loop for Wheatley.

Runs continuously in the background and gives StackChan small, gentle head
glances when it is TRULY idle — making Wheatley feel alive at rest. It holds
perfectly still while Claude is actively working or speaking (detected via the
activity-timestamp file that stackchan-hook.py touches on every event), and
only fidgets after a quiet stretch.

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
# Gentle envelope, biased to look UP (lower pitch = up; the user sits above
# the desk bot, so idle/at-rest should read as "looking at you").
YAW_MIN, YAW_MAX = -24, 24
PITCH_MIN, PITCH_MAX = 28, 46
NEUTRAL_YAW, NEUTRAL_PITCH = 0, 36


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
    (lower pitch) AND the eye glances up on top of that (MOUTH_UP composited
    onto whatever face is centered) — two independent axes agreeing, not
    just a head move alone. Drifts from wherever he currently is, not a
    snap back to a fixed neutral yaw (see module note above VIGNETTES)."""
    ny  = _clamp(pose["y"] + random.randint(-8, 8), YAW_MIN, YAW_MAX)
    np_ = _clamp(pose["p"] - random.randint(2, 8), PITCH_MIN, PITCH_MAX)  # toward "up"
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
    a side, brief hold (sometimes squinting in as if examining something
    close), then relaxes. The mirror-image of _v_look_up_center. Drifts from
    wherever he currently is, not a snap back to fixed neutral (see module
    note above VIGNETTES)."""
    dy  = random.choice([-1, 1]) * random.randint(4, 14)
    ny  = _clamp(pose["y"] + dy, YAW_MIN, YAW_MAX)
    np_ = _clamp(pose["p"] + random.randint(2, 8), PITCH_MIN, PITCH_MAX)  # toward "down"
    session.move(ny, np_)
    time.sleep(random.uniform(0.15, 0.3))
    _face(session, EXAMINE if random.random() < 0.5 else REST)
    time.sleep(random.uniform(0.5, 1.0))
    _face(session, REST)
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


# (function, weight) — weights are relative, don't need to sum to 1.
VIGNETTES = [
    (_v_nudge,            0.25),
    (_v_look_up_center,   0.20),
    (_v_diagonal_peek,    0.28),
    (_v_ponder_down,      0.17),
    (_v_big_examine,      0.10),   # the rare "big" one — used to be the ONLY one
]


def wander(session, pose):
    """Pick a small, differently-shaped idle gesture each time — never the
    same vignette twice in a row, no forced alternation or stickiness beyond
    that. Between gestures he just dwells with tiny settle moves."""
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

    last = pose.get("last_vignette")
    choices = [(f, w) for f, w in VIGNETTES if f is not last] or VIGNETTES
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
    pose = {"y": NEUTRAL_YAW, "p": NEUTRAL_PITCH, "side": random.choice([-1, 1]), "dwell": 0}
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

        # Stay still while Claude is actively working/speaking (recent hook
        # activity, or a still-active busy marker for any session — covers
        # long single tool calls where no hook fires again until it
        # finishes), or while a needs-attention signal is outstanding (don't
        # visually compete with that priority alert — see stackchan-hook.py).
        if not once and (seconds_since_activity() < IDLE_THRESHOLD_S
                          or is_busy() or needs_attention()):
            continue

        if not once and random.random() > GLANCE_PROB:
            continue

        try:
            if not have_session:
                session.init()
                have_session = True
            pose = wander(session, pose)
        except Exception:
            have_session = False  # re-init next time

        if once:
            return


if __name__ == "__main__":
    main()
