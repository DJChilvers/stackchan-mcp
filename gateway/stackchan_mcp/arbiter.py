"""Arbiter — the behaviour scheduler.

One tick = read context, then pick at most ONE thing to do, by priority tier:

    SAFETY  >  TRANSITION (context-enter)  >  quiesce  >  SOCIAL/IDLE

- SAFETY: crash -> stop; low battery & not charging & off-dock -> go home.
- TRANSITION: behaviours tagged ``transition=True`` fire once when their context
  is entered (queued by the ContextEngine transition hook).
- quiesce: during a live voice turn, do nothing (the device owns attention).
- SOCIAL/IDLE: weighted-random among behaviours eligible for the current
  context (cooldown + precondition + can_move all respected).

Never raises out of ``tick()`` — a broken behaviour is logged and skipped.
"""
from __future__ import annotations

import logging
import random
import time

logger = logging.getLogger(__name__)

LOW_BATTERY_PCT = 30


def _weighted_choice(behaviours):
    total = sum(max(0.0, b.weight) for b in behaviours)
    if total <= 0:
        return random.choice(behaviours)
    r = random.uniform(0, total)
    upto = 0.0
    for b in behaviours:
        upto += max(0.0, b.weight)
        if upto >= r:
            return b
    return behaviours[-1]


class Arbiter:
    def __init__(self, movement, context, catalog, *, clock=time.monotonic):
        self.mv = movement
        self.ctx = context
        self.catalog = catalog
        self.clock = clock
        self._pending = []          # queued transition behaviours
        self._listening = False     # attentive pose commanded for this voice turn
        self.ctx.on_transition(self._on_transition)

    def _on_transition(self, old, new, snap):
        for b in self.catalog:
            if b.transition and new in b.contexts:
                self._pending.append(b)
                logger.info("arbiter: queued transition behaviour %s (%s->%s)",
                            b.name, getattr(old, "value", old), getattr(new, "value", new))

    def tick(self) -> str | None:
        now = self.clock()

        # 1) refresh context (fires transitions -> queues)
        try:
            self.ctx.update()
        except Exception:
            logger.debug("arbiter: ctx.update failed", exc_info=True)
        try:
            snap = self.ctx.snapshot()
            cur = self.ctx.current()
        except Exception:
            logger.debug("arbiter: snapshot/current failed", exc_info=True)
            return None

        # 2) SAFETY
        try:
            if self.mv.is_crashed():
                self.mv.stop()
                return "safety:crash-stop"
            bat = snap.get("battery") or {}
            rail = snap.get("rail") or {}
            lvl = bat.get("level")
            if (lvl is not None and lvl <= LOW_BATTERY_PCT
                    and not bat.get("charging") and not rail.get("on_dock")):
                self.mv.rail_home(wait=False)
                return "safety:dock-low-battery"
        except Exception:
            logger.debug("arbiter: safety check failed", exc_info=True)

        # 3) live voice turn: hold an ATTENTIVE pose (look UP at the speaker),
        # once per turn — do NOT sit at the rest pose (which stares at the floor
        # when inverted). Then leave further attention to the device.
        if snap.get("voice_turn"):
            if not self._listening:
                self._listening = True
                try:
                    from .behaviours import listen_attend
                    listen_attend(self.mv, snap)
                except Exception:
                    logger.debug("listen_attend failed", exc_info=True)
            return "listening"
        self._listening = False

        # 4) transition behaviours (greet etc.) — highest non-safety priority
        if self._pending:
            b = self._pending.pop(0)
            return self._run(b, snap, now, tag="transition")

        # 5) social / idle — weighted pick among eligible for the current context
        try:
            eligible = [b for b in self.catalog if b.eligible(cur, snap, now)]
        except Exception:
            logger.debug("arbiter: eligibility failed", exc_info=True)
            eligible = []
        if not eligible:
            return None
        # gate big moves on can_move (crash/rail safety); face_follow etc. are small
        try:
            if not self.mv.can_move(big=False):
                return None
        except Exception:
            pass
        b = _weighted_choice(eligible)
        return self._run(b, snap, now, tag=getattr(cur, "value", str(cur)))

    def _run(self, b, snap, now, tag) -> str:
        b._last = now
        try:
            b.run(self.mv, snap)
            logger.info("behaviour: %s [%s]", b.name, tag)
        except Exception:
            logger.warning("behaviour %s raised (skipped)", b.name, exc_info=True)
        return b.name
