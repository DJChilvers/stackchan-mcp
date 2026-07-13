"""Wheatley behaviour catalogue — context-tagged units the Arbiter schedules.

Each behaviour moves ONLY through the MovementController (never raw MCP) and
reads the ContextEngine snapshot for its inputs. Behaviours are tagged with the
SocialContext(s) they belong to; a ``transition`` behaviour fires once on
context ENTER instead of being scheduled in the idle rotation.

Design notes:
- Phase 3 (this file) implements the behaviours that WORK TODAY. The ones that
  need the LD2450 radar + live rail/yaw position calibration (pegboard_check,
  tray_check, full approach) are present but STUBBED (log + return) so the
  catalogue is complete and Phase 4 just fills the run() bodies.
- Verbal owner-greeting stays LIGHT (perk + happy face) on purpose: the existing
  vision-loop -> sensor_reactor recognise path already speaks the named greeting,
  so the engine only adds a non-verbal acknowledgement to avoid double-greeting.
"""
from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from .context_engine import SocialContext as C

logger = logging.getLogger(__name__)

# Rail-assisted face-follow (user steer: rail first-class). When his head is
# turned past this many degrees to hold the user in view, roll the carriage
# that way so the head can recentre — he follows you along the desk. Flip the
# sign live if he rolls the WRONG way.
RAIL_ASSIST_YAW = float(os.environ.get("STACKCHAN_RAIL_ASSIST_YAW", "18"))
RAIL_FOLLOW_MM = int(os.environ.get("STACKCHAN_RAIL_FOLLOW_MM", "55"))
RAIL_FOLLOW_SIGN = int(os.environ.get("STACKCHAN_RAIL_FOLLOW_SIGN", "1"))


# ── small helpers (say / face via the controller's MCP client) ──────────────
def _say(mv, text: str) -> None:
    try:
        mv.call("say", {"text": text})
    except Exception:
        logger.debug("say failed", exc_info=True)


def _face(mv, name: str) -> None:
    try:
        mv.call("set_avatar", {"face": name})
    except Exception:
        logger.debug("set_avatar failed", exc_info=True)


def _face_offset(snap: dict) -> tuple[float, float]:
    f = snap.get("face") or {}
    return (f.get("dx") or 0.0), (f.get("dy") or 0.0)


# ── behaviour unit ──────────────────────────────────────────────────────────
@dataclass
class Behaviour:
    name: str
    contexts: set
    run: Callable          # run(mv, snap) -> None
    weight: float = 1.0
    cooldown_s: float = 30.0
    precondition: Optional[Callable] = None   # (snap) -> bool
    transition: bool = False                  # fire on context ENTER, not scheduled
    _last: float = field(default=0.0, repr=False)  # last-run monotonic (set by arbiter)

    def eligible(self, ctx, snap, now) -> bool:
        if self.transition:
            return False
        if ctx not in self.contexts:
            return False
        if now - self._last < self.cooldown_s:
            return False
        if self.precondition is not None:
            try:
                if not self.precondition(snap):
                    return False
            except Exception:
                return False
        return True


# ── OWNER behaviours ────────────────────────────────────────────────────────
def _rail_recenter(mv) -> None:
    """Roll the carriage when his head is cocked far to hold the user in view,
    so the head can recentre and he FOLLOWS the user along the desk. Uses the
    measured head yaw (feedback). Non-blocking; skips while the rail is mid-move
    so nudges don't stack. Sign is env-configurable — flip if he rolls away."""
    try:
        rs = mv.rail_state() or {}
        if rs.get("moving") or rs.get("crashed") or rs.get("homed") is False:
            return
        pose = mv.pose() or {}
        hy = pose.get("yaw")
        if hy is None or abs(hy) < RAIL_ASSIST_YAW:
            return
        direction = 1 if hy > 0 else -1
        mv.nudge(RAIL_FOLLOW_SIGN * direction * RAIL_FOLLOW_MM, wait=False)
        logger.info("rail-follow: head yaw %s -> nudge %smm",
                    hy, RAIL_FOLLOW_SIGN * direction * RAIL_FOLLOW_MM)
    except Exception:
        logger.debug("rail recenter failed", exc_info=True)


def _b_face_follow(mv, snap) -> None:
    """Keep looking at the owner: head tracks the face; the rail rolls to follow
    once the head is turned a fair way (so he follows you along the desk)."""
    f = snap.get("face") or {}
    if not f.get("seen"):
        return
    dx, dy = _face_offset(snap)
    if abs(dx) >= 0.08 or abs(dy) >= 0.08:
        dyaw = int(max(-14, min(14, dx * 20)))
        dpitch = int(max(-10, min(10, -dy * 16)))
        mv.look_rel(dyaw, dpitch)       # head tracks the face
    _rail_recenter(mv)                  # rail follows when the head is turned far


def _b_playful_idle(mv, snap) -> None:
    random.choice([
        lambda: mv.tilt(random.choice(["left", "right"])),
        lambda: mv.lean(random.choice(["in", "left", "right"])),
        lambda: mv.look_rel(random.choice([-18, 18]), random.choice([-6, 6])),
        lambda: mv.nod(1),
    ])()


def _b_greet_owner(mv, snap) -> None:
    """Non-verbal acknowledgement on OWNER-enter (verbal greet handled by the
    existing recognise reaction — don't double-speak)."""
    _face(mv, "happy")
    mv.perk()


def _low_battery(snap) -> bool:
    b = snap.get("battery") or {}
    lvl = b.get("level")
    return lvl is not None and lvl <= 35 and not b.get("charging")


def _b_proactive_status(mv, snap) -> None:
    """Rare, useful, owner-only nudge. v1: low-battery heads-up (the Dream-Loop
    learned-brag is spoken by the recognise path, not here)."""
    if _low_battery(snap):
        lvl = (snap.get("battery") or {}).get("level")
        _say(mv, f"Just so you know, I'm getting a bit low — {lvl} percent. "
                 "I'll take myself to the dock before long.")


# ── ALONE behaviours ────────────────────────────────────────────────────────
def _b_rail_patrol(mv, snap) -> None:
    mv.patrol(passes=1, dwell_s=0.6)


def _b_look_around(mv, snap) -> None:
    mv.scan()


def _b_idle_drift(mv, snap) -> None:
    mv.nudge(random.choice([-40, 40]))
    mv.look_rel(random.choice([-16, 16]), random.choice([-6, 6]))


def _b_ponder(mv, snap) -> None:
    mv.look_rel(0, 16)          # look up
    time.sleep(1.2)
    mv.home_head()


_MUTTERS = [
    "Right. Just me then. Keeping an eye on things.",
    "Everything's under control. Probably. Almost certainly.",
    "Space. On a rail. Living the dream, honestly.",
    "I could organise something. I won't. But I could.",
]


def _b_mutter(mv, snap) -> None:
    _say(mv, random.choice(_MUTTERS))


def _idle_long(snap) -> bool:
    idle = snap.get("input_idle_s")
    return idle is not None and idle > 15 * 60


def _b_settle_to_dock(mv, snap) -> None:
    if not (snap.get("rail") or {}).get("on_dock"):
        mv.rail_home(wait=False)


# ── ENGAGED / STRANGER / COMPANY / CHARGING ─────────────────────────────────
def _look_at_person(mv, snap) -> None:
    """Track the person's face lightly (look AT them)."""
    dx, dy = _face_offset(snap)
    if abs(dx) >= 0.06 or abs(dy) >= 0.06:
        mv.look_rel(int(max(-14, min(14, dx * 20))),
                    int(max(-8, min(8, -dy * 14))))


def listen_attend(mv, snap) -> None:
    """Attentive LISTENING pose: turn toward the speaker and perk UP, instead of
    sitting at the rest pose (which stares at the floor when inverted). Called by
    the arbiter once per voice turn."""
    dx, _ = _face_offset(snap)
    if abs(dx) > 0.05:
        mv.look_rel(int(max(-18, min(18, dx * 22))), 0)   # turn toward them
    mv.perk()                                             # look up, attentive
    _face(mv, "surprised")


def _b_attend(mv, snap) -> None:
    # ENGAGED (owner around/talking): look AT them, don't just twitch a perk.
    _look_at_person(mv, snap)


def _orient_to_person(mv, snap) -> None:
    """Turn toward the person using radar bearing if available, else face dx."""
    r = snap.get("radar") or {}
    if r.get("ok") and r.get("nearest_deg") is not None:
        mv.look_at(int(max(-130, min(160, r["nearest_deg"]))), 0)
        return
    dx, _ = _face_offset(snap)
    if abs(dx) > 0.05:
        mv.look_rel(int(max(-16, min(16, dx * 20))), 0)


def _b_assess(mv, snap) -> None:
    """Assess-then-approach step 1: back off a touch to widen the view / frame
    the face for recognition, then take a good look toward them."""
    mv.retreat(60, look=False)
    _orient_to_person(mv, snap)
    _face(mv, "surprised")


def _b_watch(mv, snap) -> None:
    _orient_to_person(mv, snap)


def _b_polite_watch(mv, snap) -> None:
    _orient_to_person(mv, snap)


def _b_rest(mv, snap) -> None:
    mv.look_rel(random.choice([-8, 8]), 0)


# ── STUBS — need LD2450 radar + live position calibration (Phase 4) ──────────
def _stub(label: str):
    def _run(mv, snap):
        logger.info("behaviour STUB %s: TODO Phase 4 (needs live position "
                    "calibration / radar)", label)
    return _run


# ── the catalogue ───────────────────────────────────────────────────────────
CATALOG = [
    # OWNER
    Behaviour("face_follow", {C.OWNER}, _b_face_follow, weight=6.0, cooldown_s=0.0),
    Behaviour("playful_idle", {C.OWNER}, _b_playful_idle, weight=1.5, cooldown_s=18.0),
    Behaviour("greet_owner", {C.OWNER}, _b_greet_owner, transition=True),
    Behaviour("proactive_status", {C.OWNER}, _b_proactive_status,
              weight=1.0, cooldown_s=600.0, precondition=_low_battery),
    # ALONE
    Behaviour("rail_patrol", {C.ALONE}, _b_rail_patrol, weight=2.0, cooldown_s=150.0),
    Behaviour("look_around", {C.ALONE}, _b_look_around, weight=2.0, cooldown_s=80.0),
    Behaviour("idle_drift", {C.ALONE}, _b_idle_drift, weight=1.5, cooldown_s=45.0),
    Behaviour("ponder", {C.ALONE}, _b_ponder, weight=1.0, cooldown_s=70.0),
    Behaviour("mutter", {C.ALONE}, _b_mutter, weight=0.5, cooldown_s=600.0),
    Behaviour("settle_to_dock", {C.ALONE}, _b_settle_to_dock,
              weight=3.0, cooldown_s=300.0, precondition=_idle_long),
    Behaviour("pegboard_check", {C.ALONE}, _stub("pegboard_check"),
              weight=1.0, cooldown_s=900.0),
    Behaviour("tray_check", {C.ALONE}, _stub("tray_check"),
              weight=1.0, cooldown_s=900.0),
    # ENGAGED
    Behaviour("attend", {C.ENGAGED}, _b_attend, weight=1.0, cooldown_s=8.0),
    # STRANGER
    Behaviour("assess", {C.STRANGER}, _b_assess, weight=3.0, cooldown_s=30.0),
    Behaviour("watch_stranger", {C.STRANGER}, _b_watch, weight=2.0, cooldown_s=10.0),
    # COMPANY
    Behaviour("polite_watch", {C.COMPANY}, _b_polite_watch, weight=2.0, cooldown_s=10.0),
    # CHARGING
    Behaviour("rest", {C.CHARGING}, _b_rest, weight=1.0, cooldown_s=45.0),
]
