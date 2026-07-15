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


# ── charge-lock ──────────────────────────────────────────────────────────
# "Dock and charge until over 80% before leaving" (user spec 2026-07-15).
# A persistent P1 claim owner="charging" that pins him to the dock: set when
# he docks (voice command or the low-battery rescue), refreshed by the idle
# loop's battery check while level is below target, released once charged.
# Lower-priority rail movers (tracking P3, ambient drift P4) yield to it, so
# he stays put and tops up — while his HEAD is still free to watch people.
CHARGE_LOCK_PATH = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", ".")),
    "stackchan-charge-until.json",
)


def set_charge_lock(release_over: int = 80) -> None:
    """Pin him to the dock until battery is OVER ``release_over`` percent."""
    try:
        with open(CHARGE_LOCK_PATH, "w", encoding="utf-8") as f:
            json.dump({"release_over": int(release_over), "ts": time.time()}, f)
    except Exception:
        pass
    claim("charging", 1)


def charge_lock_release_over() -> int | None:
    """The % to charge past before leaving, or None if not dock-locked."""
    try:
        with open(CHARGE_LOCK_PATH, encoding="utf-8") as f:
            return int(json.load(f).get("release_over", 80))
    except Exception:
        return None


def clear_charge_lock() -> None:
    for path in (CHARGE_LOCK_PATH,):
        try:
            os.remove(path)
        except Exception:
            pass
    release("charging")
