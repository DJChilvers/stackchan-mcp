"""Rail motion arbiter — ONE claim, priority-ordered, no more tug-of-war.

Every rail mover calls ``claim(owner, priority)`` before EVERY rail command and
``release(owner)`` when done. Lower priority number = stronger. See
Documents/StackChan/RAIL_ARBITER.md for the ladder (1 battery, 2 absence-park,
3 social/tracking/dance, 4 ambient drift).

The claim lives in ``%TEMP%\\stackchan-rail-owner.json`` so separate processes
(idle loop, look_at script, gateway behaviours) share it without any server.
Stale claims (>90 s) are ignored — a crashed owner cannot squat.
"""
from __future__ import annotations

import json
import os
import time

CLAIM_PATH = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", ".")),
    "stackchan-rail-owner.json",
)
STALE_S = 90.0


def _read() -> dict | None:
    try:
        with open(CLAIM_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def claim(owner: str, priority: int) -> bool:
    """Try to take (or refresh) the rail claim. True = you may move the rail."""
    now = time.time()
    cur = _read()
    if (
        cur
        and cur.get("owner") != owner
        and now - float(cur.get("ts", 0)) < STALE_S
        and int(cur.get("priority", 99)) < priority
    ):
        return False  # someone stronger holds it
    try:
        with open(CLAIM_PATH, "w", encoding="utf-8") as f:
            json.dump({"owner": owner, "priority": priority, "ts": now}, f)
    except Exception:
        pass  # filesystem hiccup: fail open rather than freeze behaviours
    return True


def release(owner: str) -> None:
    cur = _read()
    if cur and cur.get("owner") == owner:
        try:
            os.remove(CLAIM_PATH)
        except Exception:
            pass


def holder() -> str:
    cur = _read()
    if not cur or time.time() - float(cur.get("ts", 0)) >= STALE_S:
        return ""
    return "%s(p%s)" % (cur.get("owner", "?"), cur.get("priority", "?"))
