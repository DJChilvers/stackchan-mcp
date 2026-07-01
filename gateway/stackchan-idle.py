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

# Hold still until this many seconds have passed with no hook activity.
IDLE_THRESHOLD_S = 12.0
# Base wait between "should I fidget?" checks (randomized each tick).
TICK_MIN_S, TICK_MAX_S = 6.0, 14.0
# Of the eligible (idle) ticks, roughly this fraction produce a movement.
GLANCE_PROB = 0.65
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


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))



# Semantic face frames (see wheatley_avatar.py _spec). Only the wander sets the
# directional/examine ones, so firmware touch reactions + work hooks (which use
# the CENTERED frames) can never dart the optic.
LOOK_LEFT  = "thinking"      # optic darts to the left edge
LOOK_RIGHT = "happy"         # optic darts to the right edge
EXAMINE    = "sad"           # zoom in + squint (lids lowered)
REST       = "idle"          # centered resting gaze
WIDE       = "surprised"     # wide-eyed reaction (centered)
MILD       = "embarrassed"   # mild, unimpressed reaction (centered)
EYE_LEAD   = 0.07            # eye moves this long before the head follows


def _face(session, name):
    try:
        session.set_face(name)
    except Exception:
        pass


def wander(session, pose):
    """Notice → orient → examine → react. Sticky-side + dwell so he lingers on
    a region and never metronomes L-R-L.

    On a fresh point of interest he runs the full attention beat:
      1. eye flicks toward it (eye leads the head),
      2. head turns to follow,
      3. eye recentres — now he's looking straight AT it,
      4. (often) zoom in + squint to examine it, hold a moment,
      5. (sometimes) react — a wide double-take or an unimpressed mild look,
      6. relax back to a calm resting gaze.
    Between points of interest he just dwells, making tiny settling moves.
    """
    dwell = pose.get("dwell", 0)

    # ── still looking at the thing: tiny settle moves, eye stays centered ──
    if dwell > 0:
        pose["dwell"] = dwell - 1
        if random.random() < 0.40:
            ny  = _clamp(pose["y"] + random.randint(-4, 4), YAW_MIN, YAW_MAX)
            np_ = _clamp(pose["p"] + random.randint(-3, 3), PITCH_MIN, PITCH_MAX)
            session.move(ny, np_)
            pose.update(y=ny, p=np_)
        return pose

    # ── commit to a NEW point of interest (sticky side, occasional flip) ───
    side = pose.get("side") or random.choice([-1, 1])
    if random.random() < 0.30:               # something on the far side calls
        side = -side

    ty  = side * random.randint(9, abs(YAW_MAX))
    tp_ = random.randint(PITCH_MIN, PITCH_MAX)

    # 1) eye flicks toward it, 2) head follows, 3) eye recentres onto it
    _face(session, LOOK_RIGHT if side > 0 else LOOK_LEFT)
    time.sleep(EYE_LEAD)
    session.move(ty, tp_)
    time.sleep(random.uniform(0.30, 0.50))
    _face(session, REST)
    time.sleep(random.uniform(0.20, 0.40))

    # 4) examine it, 5) react in varied ways, 6) relax
    r = random.random()
    if r < 0.55:
        _face(session, EXAMINE)                       # zoom in + squint
        time.sleep(random.uniform(0.6, 1.1))          # hold the focused stare
        rr = random.random()
        if rr < 0.30:
            _face(session, WIDE)                      # "!" — taken aback by it
            time.sleep(random.uniform(0.25, 0.5))
        elif rr < 0.50:
            _face(session, MILD)                      # unimpressed, hm
            time.sleep(random.uniform(0.25, 0.5))
        _face(session, REST)                          # relax
    elif r < 0.70:
        _face(session, WIDE)                          # quick double-take
        time.sleep(random.uniform(0.2, 0.4))
        _face(session, REST)
    # else: just keep calmly looking (already at REST)

    pose.update(y=ty, p=tp_, side=side, dwell=random.randint(3, 6))
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

        # Stay still while Claude is actively working / speaking.
        if not once and seconds_since_activity() < IDLE_THRESHOLD_S:
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
