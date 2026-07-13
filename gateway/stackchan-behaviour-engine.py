#!/usr/bin/env python3
"""stackchan-behaviour-engine.py — Wheatley's context-driven behaviour brain.

The Behaviour Engine (design: Documents/StackChan/design/behaviour-engine.md):
MovementController (how he moves) + ContextEngine (who's around) + Arbiter
(which behaviour to run). This is the eventual REPLACEMENT for the scattered
stackchan-idle.py vignette loop — but it runs as its OWN process with its OWN
lock, so it can be A/B tested by launching it INSTEAD of the idle loop. It is
deliberately NOT added to any scheduled task yet.

Run it (for testing — stop the idle loop first so they don't both move him):
    set STACKCHAN_BEHAVIOUR_ENGINE=1
    .venv\\Scripts\\python.exe stackchan-behaviour-engine.py

Dry self-test (no device, mock movement + context):
    .venv\\Scripts\\python.exe stackchan-behaviour-engine.py --dry
"""
from __future__ import annotations

import logging
import os
import random
import socket
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

LOG_PATH = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", ".")),
    "stackchan-behaviour-engine.log",
)
logging.basicConfig(
    filename=LOG_PATH, level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("behaviour-engine")

TICK_MIN_S = float(os.environ.get("STACKCHAN_ENGINE_TICK_MIN_S", "1.5"))
TICK_MAX_S = float(os.environ.get("STACKCHAN_ENGINE_TICK_MAX_S", "3.0"))
LOCK_PORT = int(os.environ.get("STACKCHAN_ENGINE_LOCK_PORT", "8779"))
STATUS_URL = "http://127.0.0.1:8767/status"


def _single_instance_lock():
    """Bind a localhost port as a single-instance mutex. Returns the socket
    (keep it alive) or None if another instance already holds it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(1)
        return s
    except OSError:
        return None


def _device_connected() -> bool:
    try:
        with urllib.request.urlopen(STATUS_URL, timeout=4) as r:
            return b'"connected":true' in r.read()
    except Exception:
        return False


def run_live():
    if os.environ.get("STACKCHAN_BEHAVIOUR_ENGINE", "0") != "1":
        print("Refusing to run: set STACKCHAN_BEHAVIOUR_ENGINE=1 to enable the "
              "live engine (and stop the idle loop first so they don't both "
              "move him). Use --dry for the offline self-test.")
        return 2
    lock = _single_instance_lock()
    if lock is None:
        print("Another behaviour-engine instance is already running. Exiting.")
        return 1

    from stackchan_mcp.movement import MovementController
    from stackchan_mcp.context_engine import ContextEngine
    from stackchan_mcp.arbiter import Arbiter
    from stackchan_mcp.behaviours import CATALOG

    mv = MovementController()
    try:
        mv.initialize()
    except Exception:
        logger.debug("movement initialize failed (will retry via calls)", exc_info=True)
    ctx = ContextEngine(mv)
    arb = Arbiter(mv, ctx, CATALOG)
    logger.info("behaviour engine started; limits=%s", mv.limits())
    print(f"behaviour engine running (log: {LOG_PATH})")

    while True:
        try:
            if not _device_connected():
                time.sleep(8)
                continue
            mv.refresh_orientation()
            chosen = arb.tick()
            if chosen:
                logger.debug("tick -> %s", chosen)
        except Exception:
            logger.warning("engine tick failed", exc_info=True)
        time.sleep(random.uniform(TICK_MIN_S, TICK_MAX_S))


# ── dry self-test (mock movement + context, no device) ──────────────────────
class _MockMovement:
    def __init__(self):
        self.calls = []
    def __getattr__(self, name):
        def _rec(*a, **k):
            self.calls.append((name, a, k))
            return True
        return _rec
    # explicit signals the arbiter reads
    def is_crashed(self): return False
    def is_quiesced(self): return False
    def can_move(self, *, big=True): return True
    def gaze_pose(self, **k): return {"visual_yaw": 0, "visual_pitch": 0}
    def limits(self): return {"yaw": (-130, 160)}


class _MockContext:
    def __init__(self, script):
        self._script = script     # list of (SocialContext, snapshot)
        self._i = -1
        self._cbs = []
    def on_transition(self, cb): self._cbs.append(cb)
    def update(self):
        prev = self._script[self._i][0] if self._i >= 0 else None
        self._i = min(self._i + 1, len(self._script) - 1)
        cur = self._script[self._i][0]
        if prev is not None and cur != prev:
            for cb in self._cbs:
                cb(prev, cur, self._script[self._i][1])
    def current(self): return self._script[self._i][0]
    def snapshot(self): return self._script[self._i][1]


def run_dry():
    from stackchan_mcp.context_engine import SocialContext as C
    from stackchan_mcp.arbiter import Arbiter
    from stackchan_mcp.behaviours import CATALOG

    def snap(**over):
        base = {"face": {"seen": False}, "radar": {"ok": False},
                "input_idle_s": 5, "voice_turn": False,
                "battery": {"level": 80, "charging": False},
                "rail": {"pos_mm": 400, "on_dock": False},
                "owner_signal": False, "person_present": False}
        base.update(over)
        return base

    script = [
        (C.ALONE, snap()),
        (C.OWNER, snap(face={"seen": True, "is_owner": True, "dx": 0.3, "dy": 0.1},
                       owner_signal=True)),               # -> greet transition then face_follow
        (C.OWNER, snap(face={"seen": True, "is_owner": True, "dx": 0.3, "dy": 0.1})),
        (C.ENGAGED, snap(voice_turn=True)),               # -> quiesced
        (C.STRANGER, snap(face={"seen": True, "is_owner": False, "dx": -0.4},
                          person_present=True)),          # -> assess/watch
        (C.CHARGING, snap(rail={"pos_mm": 10, "on_dock": True},
                          battery={"level": 100, "charging": True})),
    ]
    mv = _MockMovement()
    ctx = _MockContext(script)
    # reset cooldowns
    for b in CATALOG:
        b._last = 0.0
    t = [0.0]
    arb = Arbiter(mv, ctx, CATALOG, clock=lambda: t[0])
    print("=== behaviour-engine dry self-test ===")
    for step in range(len(script)):
        t[0] += 100.0
        chosen = arb.tick()
        print(f"  ctx={ctx.current().value:9s} -> behaviour={chosen}")
    print(f"  movement calls recorded: {len(mv.calls)} "
          f"(e.g. {[c[0] for c in mv.calls[:6]]})")
    print("OK: arbiter picked context-appropriate behaviours, safety+quiesce honoured.")
    return 0


if __name__ == "__main__":
    if "--dry" in sys.argv:
        raise SystemExit(run_dry())
    raise SystemExit(run_live())
