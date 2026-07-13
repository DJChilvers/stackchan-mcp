"""Wheatley Behaviour Engine — Phase 2: the ContextEngine.

Derives ONE current :class:`SocialContext` — *who is around* — by fusing the
live signals the rest of the system already produces, exposes it, and reports
enter/exit transitions. Phase 3's scheduler/arbiter consumes ``current()`` to
pick context-appropriate behaviours; this module deliberately does NOT move the
robot or run any loop itself (that's Phase 3).

The six states (see ``design/behaviour-engine.md``):

- ``CHARGING`` — on the dock, charging, nobody actively engaging.
- ``ALONE``    — no person signal for a while.
- ``STRANGER`` — an unknown person present AND the owner is absent.
- ``COMPANY``  — a person present, not resolved as the owner.
- ``OWNER``    — Dominic recognised, or clearly at the desk (recent input).
- ``ENGAGED``  — actively listening/talking (a live voice turn / very recent
  owner interaction).

Signals fused (all REUSED — no signal is re-implemented here):

- **Face presence + identity** — the vision loop
  (``stackchan-vision-loop.py``) writes ``%TEMP%\\stackchan-vision-state.json``
  every tick: ``{ts, face_visible, person, name, is_owner, present, dx, dy,
  ...}``. We added the tiny ``is_owner`` field to that existing write (derived
  from ``STACKCHAN_OWNER_NAME``); the ContextEngine reads the file
  staleness-gated (~5 s) — a stale/missing file simply means "no face signal",
  never an error. *We reuse the vision loop's existing state file rather than
  adding a second one — the brief's suggested stackchan-face-state.json would
  duplicate data the loop already publishes.*
- **Presence radar (LD2450)** — ``self.presence.read`` via the MovementController's
  MCP client (Port C). ``{ok, age_ms, targets:[{x_mm, y_mm, distance_mm,
  angle_deg, speed_cms}]}``. Not wired until the module arrives tomorrow;
  ``{ok:false}`` (or any error) degrades to "no radar signal" cleanly. When it
  works, a target in the front zone = a person here + where/how far — the raw
  bearing/distance is surfaced in ``snapshot()`` for Phase 3's assess/approach.
- **Owner-here heuristics** — keyboard/mouse idle seconds via
  ``GetLastInputInfo`` (the SAME mechanism as the vision loop's
  ``_seconds_since_user_input``), plus a live voice turn from
  ``MovementController.is_quiesced()`` (the devicechat marker).
- **Battery / dock** — ``MovementController.battery()`` (level/charging) and the
  rail ``pos_mm`` (near the home/dock end => on-dock candidate).

Design notes:

- **Time is monotonic.** All hysteresis / staleness logic uses a passed-in
  ``clock`` (default ``time.monotonic``) so correctness never depends on the
  wall clock. Signal *timestamps that come from other processes* (the vision
  file's ``ts``) are wall-clock and compared only against ``time.time()`` for
  staleness — never mixed into the monotonic hysteresis maths.
- **Hysteresis, no flapping.** Each ``update()`` computes an *instantaneous*
  candidate from the raw signals by clear priority; the public ``current()``
  only switches once a *different* candidate has persisted for
  ``switch_hold_s`` (a few seconds). Same-as-current candidates confirm
  instantly. This stops a one-frame blip (a face lost in low light, a single
  radar ghost) from yanking the context back and forth.
- **Guarded.** Every signal read is wrapped so a bad/absent signal degrades to
  "unknown/absent" and can never raise; ``update()`` cannot throw.

stdlib + existing deps only (json / os / time / ctypes via the vision-loop
mechanism). No new third-party imports.
"""

from __future__ import annotations

import json
import logging
import os
import time
from enum import Enum

logger = logging.getLogger(__name__)


# ─── the states ───────────────────────────────────────────────────────────────


class SocialContext(str, Enum):
    """Who is around. ``str`` mixin so it JSON-serialises as its name."""

    CHARGING = "CHARGING"
    ALONE = "ALONE"
    STRANGER = "STRANGER"
    COMPANY = "COMPANY"
    OWNER = "OWNER"
    ENGAGED = "ENGAGED"


# Priority when several instantaneous candidates could apply. Higher wins.
# ENGAGED (active turn) tops everything social; CHARGING is a resting default
# that only holds when nothing/no-one is actively going on; ALONE is the floor.
_PRIORITY = {
    SocialContext.ENGAGED: 6,
    SocialContext.OWNER: 5,
    SocialContext.STRANGER: 4,
    SocialContext.COMPANY: 3,
    SocialContext.CHARGING: 2,
    SocialContext.ALONE: 1,
}


# ─── tunables (env-overridable; all in SECONDS unless noted) ──────────────────

# A candidate different from the current context must persist this long before
# current() actually switches (hysteresis). Same-as-current confirms instantly.
SWITCH_HOLD_S = float(os.environ.get("STACKCHAN_CTX_SWITCH_HOLD_S", "3.0"))

# The vision-state file is "fresh" only if its wall-clock ts is younger than
# this. ~5 s per the brief: a couple of the loop's fast (2.5 s) ticks, but well
# under its 8 s ambient cadence would be too tight, so we sit at 5 s and treat a
# gap as "no current face signal" (the loop skips ticks while busy anyway).
FACE_STALE_S = float(os.environ.get("STACKCHAN_CTX_FACE_STALE_S", "5.0"))

# Radar frame age (from self.presence.read's own age_ms) beyond which we ignore
# it — an old frame is not "someone here now".
RADAR_STALE_MS = float(os.environ.get("STACKCHAN_CTX_RADAR_STALE_MS", "4000"))
# A radar target within this distance counts as "a person is here". Beyond it,
# a blip is background and ignored for presence.
RADAR_PRESENCE_MM = float(os.environ.get("STACKCHAN_CTX_RADAR_PRESENCE_MM", "2500"))
# Front-zone half-angle (deg, 0 = straight ahead): a target inside this cone is
# "in front, a person to interact with" (feeds Phase 3 assess/approach).
RADAR_FRONT_DEG = float(os.environ.get("STACKCHAN_CTX_RADAR_FRONT_DEG", "35"))

# Keyboard/mouse idle under this => the owner is actively at the desk (an OWNER
# signal that works in the dark, where the camera can't see a face).
INPUT_ACTIVE_S = float(os.environ.get("STACKCHAN_CTX_INPUT_ACTIVE_S", "180"))
# Even fresher input (or a just-ended voice turn) => ENGAGED, not just present.
ENGAGED_INPUT_S = float(os.environ.get("STACKCHAN_CTX_ENGAGED_INPUT_S", "20"))

# The owner is considered ABSENT (so an unknown face reads as STRANGER, not
# COMPANY) only after NO owner signal — recognised-owner face OR recent input —
# for this long. Deliberately generous so a brief look-away doesn't downgrade a
# known owner to "stranger present".
OWNER_ABSENT_S = float(os.environ.get("STACKCHAN_CTX_OWNER_ABSENT_S", "90"))

# Rail pos at/under this (mm from the home switch, where the dock lives) counts
# as "on the dock" for the CHARGING candidate. The charge dock sits at the home
# end (see project memory); keep it a small window near 0.
DOCK_POS_MM = float(os.environ.get("STACKCHAN_CTX_DOCK_POS_MM", "60"))

# Path to the vision loop's shared state file (it owns the write; we only read).
_TEMP = os.environ.get("TEMP", os.environ.get("TMP", "."))
VISION_STATE_PATH = os.path.join(_TEMP, "stackchan-vision-state.json")


# ─── keyboard/mouse idle (same mechanism as vision-loop._seconds_since_user_input) ──


def _seconds_since_user_input() -> float | None:
    """Seconds since the last keyboard/mouse input, or None if unavailable.

    Mirrors ``stackchan-vision-loop._seconds_since_user_input`` — a Windows
    ``GetLastInputInfo`` read; a strong "a human is physically at the computer"
    signal that works in the dark, unlike face detection. Returns None off
    Windows or on any API error (treated as "no input evidence").
    """
    try:
        import ctypes

        class _LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        now_ticks = ctypes.windll.kernel32.GetTickCount()
        idle_ms = (now_ticks - info.dwTime) & 0xFFFFFFFF
        return idle_ms / 1000.0
    except Exception:
        return None


# ─── the engine ───────────────────────────────────────────────────────────────


class ContextEngine:
    """Derives ONE :class:`SocialContext` from live signals, with hysteresis.

    Construction never touches the device. Call :meth:`update` on a cadence
    (Phase 3 will, ~1 Hz); it reads the raw signals, computes an instantaneous
    candidate, applies hysteresis, and fires transition callbacks when the held
    context actually changes. :meth:`current` returns the held context;
    :meth:`snapshot` returns every raw signal + the derived context for
    debugging/logging.

    Args:
        movement: a Phase-1 ``MovementController`` (or any object exposing
            ``battery()``, ``is_quiesced()``, ``rail_state()`` and ``.mcp.call``).
            Reused, never re-implemented. May be ``None`` for a pure signal-
            injection test (see the self-test / ``_read_signals`` override).
        clock: monotonic time source for hysteresis (default ``time.monotonic``).
            Injectable so tests can drive time deterministically.
        switch_hold_s: how long a new candidate must persist before switching.
        initial: the context to start in before the first ``update`` (default
            ``ALONE``; no transition is fired for the initial value).
    """

    def __init__(
        self,
        movement=None,
        *,
        clock=time.monotonic,
        switch_hold_s: float = SWITCH_HOLD_S,
        initial: SocialContext = SocialContext.ALONE,
    ) -> None:
        self.movement = movement
        self._clock = clock
        self.switch_hold_s = switch_hold_s
        self._context: SocialContext = initial
        # Pending-candidate hysteresis bookkeeping.
        self._pending: SocialContext | None = None
        self._pending_since: float = 0.0
        # Timestamps (monotonic) of the last time we saw each owner-ish signal,
        # so OWNER can persist across brief look-aways (radar/face gaps).
        self._last_owner_signal: float | None = None
        self._last_person_signal: float | None = None
        self._callbacks: list = []
        self._last_snapshot: dict = {}
        self._context_since: float = self._safe_now()

    # ── public API ───────────────────────────────────────────────────────

    def current(self) -> SocialContext:
        """The current (hysteresis-held) SocialContext."""
        return self._context

    def on_transition(self, cb) -> None:
        """Register ``cb(old, new, snapshot)`` fired when current() changes.

        Callbacks are invoked (guarded) in registration order on every held
        transition. A raising callback is logged and skipped — one bad hook
        can't stop the others or the engine.
        """
        self._callbacks.append(cb)

    def snapshot(self) -> dict:
        """All raw signals + derived context + hysteresis state (for debugging).

        Returns the signal dict from the last :meth:`update` augmented with the
        held context, any pending candidate, and how long each has been in
        effect. Safe to call any time; before the first update it reflects the
        initial state with empty signals.
        """
        held_for = round(self._safe_now() - self._context_since, 2)
        snap = dict(self._last_snapshot)
        snap.update(
            {
                "context": self._context.value,
                "context_held_for_s": held_for,
                "pending": self._pending.value if self._pending else None,
                "pending_for_s": (
                    round(self._safe_now() - self._pending_since, 2)
                    if self._pending
                    else 0.0
                ),
                "instantaneous": snap.get("instantaneous"),
            }
        )
        return snap

    def update(self) -> SocialContext:
        """Read signals, recompute the candidate, apply hysteresis, fire events.

        Returns the (possibly unchanged) current context. Never raises: any
        signal-read failure degrades to an absent/unknown signal.
        """
        now = self._safe_now()
        try:
            signals = self._read_signals()
        except Exception:  # pragma: no cover — belt-and-braces; reads are guarded
            logger.exception("context: signal read failed; treating as no signal")
            signals = self._empty_signals()

        # Track owner/person presence memory so OWNER/COMPANY can ride out short
        # gaps in a single noisy signal.
        if signals.get("owner_signal"):
            self._last_owner_signal = now
        if signals.get("person_present"):
            self._last_person_signal = now

        candidate = self._derive(signals, now)
        signals["instantaneous"] = candidate.value
        self._last_snapshot = signals
        self._apply_hysteresis(candidate, now)
        return self._context

    # ── signal acquisition (each piece independently guarded) ────────────

    def _empty_signals(self) -> dict:
        return {
            "face": {"seen": False, "name": None, "is_owner": False,
                     "present": False, "dx": None, "dy": None, "age_s": None,
                     "fresh": False},
            "radar": {"ok": False, "person_present": False, "front": False,
                      "nearest_mm": None, "nearest_deg": None, "targets": 0},
            "input_idle_s": None,
            "voice_turn": False,
            "battery": {"level": None, "charging": None},
            "rail": {"pos_mm": None, "on_dock": False},
            "owner_signal": False,
            "person_present": False,
        }

    def _read_signals(self) -> dict:
        """Fuse the raw signals into one dict. Each source is independently
        guarded so one dead source never blanks the others."""
        sig = self._empty_signals()

        # 1) Face + identity (vision-loop shared state file, staleness-gated).
        try:
            sig["face"] = self._read_face()
        except Exception:
            logger.debug("context: face read failed", exc_info=True)

        # 2) Presence radar (LD2450 via MCP). Absent module => ok:false.
        try:
            sig["radar"] = self._read_radar()
        except Exception:
            logger.debug("context: radar read failed", exc_info=True)

        # 3) Keyboard/mouse idle seconds.
        sig["input_idle_s"] = _seconds_since_user_input()

        # 4) Live voice turn (devicechat marker) via the MovementController.
        try:
            sig["voice_turn"] = bool(
                self.movement is not None and self.movement.is_quiesced()
            )
        except Exception:
            logger.debug("context: voice-turn read failed", exc_info=True)

        # 5) Battery + rail position (dock proximity).
        try:
            sig["battery"] = self._read_battery()
        except Exception:
            logger.debug("context: battery read failed", exc_info=True)
        try:
            sig["rail"] = self._read_rail()
        except Exception:
            logger.debug("context: rail read failed", exc_info=True)

        # Derived convenience booleans used by _derive and the presence memory.
        idle = sig["input_idle_s"]
        input_active = idle is not None and idle <= INPUT_ACTIVE_S
        face = sig["face"]
        radar = sig["radar"]
        # owner_signal: recognised-owner face (fresh) OR recent keyboard/mouse.
        sig["owner_signal"] = bool(
            (face.get("fresh") and face.get("is_owner")) or input_active
        )
        # person_present: any fresh face OR a person-flag OR a radar target
        # in range. (The vision file's "present" already folds in YOLO-person +
        # recent input, so we OR it in too.)
        sig["person_present"] = bool(
            (face.get("fresh") and (face.get("seen") or face.get("present")))
            or radar.get("person_present")
        )
        return sig

    def _read_face(self) -> dict:
        """Read the vision loop's shared state file, staleness-gated (~5 s).

        Returns a normalised dict; ``fresh`` is False when the file is missing,
        unparseable, or older than ``FACE_STALE_S`` (=> treat as no face signal).
        """
        out = {"seen": False, "name": None, "is_owner": False, "present": False,
               "dx": None, "dy": None, "age_s": None, "fresh": False}
        try:
            with open(VISION_STATE_PATH, encoding="utf-8") as f:
                st = json.load(f)
        except Exception:
            return out
        # ts is wall-clock (written by another process) — compare to time.time()
        # ONLY for staleness; it never enters the monotonic hysteresis maths.
        ts = st.get("ts")
        age = None
        fresh = False
        if isinstance(ts, (int, float)):
            age = max(0.0, time.time() - float(ts))
            fresh = age <= FACE_STALE_S
        out.update(
            {
                "seen": bool(st.get("face_visible")),
                "name": st.get("name"),
                "is_owner": bool(st.get("is_owner")),
                "present": bool(st.get("present")),
                "dx": st.get("dx"),
                "dy": st.get("dy"),
                "age_s": round(age, 2) if age is not None else None,
                "fresh": fresh,
            }
        )
        return out

    def _read_radar(self) -> dict:
        """Read self.presence.read via the MovementController's MCP client.

        Degrades to ``ok:False`` when there's no movement client, no reply, the
        module isn't wired (``ok:false``), or the frame is stale.
        """
        out = {"ok": False, "person_present": False, "front": False,
               "nearest_mm": None, "nearest_deg": None, "targets": 0}
        if self.movement is None or getattr(self.movement, "mcp", None) is None:
            return out
        resp = self.movement.mcp.call("self.presence.read")
        if not isinstance(resp, dict) or not resp.get("ok"):
            return out
        age_ms = resp.get("age_ms")
        if isinstance(age_ms, (int, float)) and age_ms > RADAR_STALE_MS:
            return out  # a stale frame is not "someone here now"
        targets = resp.get("targets") or []
        out["ok"] = True
        out["targets"] = len(targets)
        nearest_mm = None
        nearest_deg = None
        front = False
        person = False
        for t in targets:
            if not isinstance(t, dict):
                continue
            dist = t.get("distance_mm")
            ang = t.get("angle_deg")
            if isinstance(dist, (int, float)) and dist <= RADAR_PRESENCE_MM:
                person = True
                if nearest_mm is None or dist < nearest_mm:
                    nearest_mm = dist
                    nearest_deg = ang
                if isinstance(ang, (int, float)) and abs(ang) <= RADAR_FRONT_DEG:
                    front = True
        out.update({"person_present": person, "front": front,
                    "nearest_mm": nearest_mm, "nearest_deg": nearest_deg})
        return out

    def _read_battery(self) -> dict:
        out = {"level": None, "charging": None}
        if self.movement is None:
            return out
        batt = self.movement.battery() or {}
        out["level"] = batt.get("level")
        out["charging"] = batt.get("charging")
        return out

    def _read_rail(self) -> dict:
        out = {"pos_mm": None, "on_dock": False}
        if self.movement is None:
            return out
        st = self.movement.rail_state() or {}
        pos = st.get("pos_mm")
        out["pos_mm"] = pos
        if isinstance(pos, (int, float)):
            out["on_dock"] = pos <= DOCK_POS_MM
        return out

    # ── the state-machine rules (instantaneous candidate) ────────────────

    def _derive(self, sig: dict, now: float) -> SocialContext:
        """Map raw signals -> the instantaneous candidate context.

        Rules, evaluated by PRIORITY (first match wins). Each rule is commented
        with the design-doc intent. "recently" uses the monotonic presence
        memory (``_last_owner_signal`` / ``_last_person_signal``) so a rule can
        ride out a one-tick gap in a single signal.
        """
        face = sig["face"]
        radar = sig["radar"]
        batt = sig["battery"]
        rail = sig["rail"]
        idle = sig["input_idle_s"]

        input_idle = idle if isinstance(idle, (int, float)) else None
        engaged_input = input_idle is not None and input_idle <= ENGAGED_INPUT_S
        owner_now = sig["owner_signal"]
        owner_recent = (
            self._last_owner_signal is not None
            and (now - self._last_owner_signal) <= OWNER_ABSENT_S
        )
        person_now = sig["person_present"]

        # 1) ENGAGED — a live voice turn (device listening/talking) OR very
        #    recent owner interaction. Highest social priority: he's mid-
        #    exchange, everything else yields.
        if sig["voice_turn"] or engaged_input:
            return SocialContext.ENGAGED

        # 2) OWNER — Dominic recognised (fresh owner face) OR clearly at the
        #    desk (recent keyboard/mouse). owner_now covers both; owner_recent
        #    lets him stay OWNER through a brief look-away rather than flipping
        #    to COMPANY/ALONE the instant the camera loses the face.
        if owner_now or owner_recent:
            return SocialContext.OWNER

        # 3) A person is here but NOT the owner. Split by whether the owner is
        #    around at all:
        if person_now:
            unknown_face = face.get("fresh") and face.get("seen") and not face.get("is_owner")
            # 3a) STRANGER — an UNKNOWN face is present and the owner has been
            #     absent for a while (no owner signal). Guard/watch territory.
            if unknown_face and not owner_recent:
                return SocialContext.STRANGER
            # 3b) COMPANY — someone present (a face we haven't resolved as the
            #     owner, or a radar target) with the owner possibly still about.
            return SocialContext.COMPANY

        # 4) CHARGING — on the dock + charging, and (having reached here) no
        #    active person signal. A restful default, not an interruption of
        #    anyone: it sits BELOW the person states on purpose so he wakes to
        #    greet the moment someone shows up.
        if rail.get("on_dock") and batt.get("charging"):
            return SocialContext.CHARGING

        # 5) ALONE — the floor: no person signal (and not a charging rest).
        return SocialContext.ALONE

    # ── hysteresis + transition dispatch ─────────────────────────────────

    def _apply_hysteresis(self, candidate: SocialContext, now: float) -> None:
        """Switch to ``candidate`` only once it has persisted ``switch_hold_s``.

        - candidate == current: clear any pending, nothing changes.
        - candidate != current, first time seen: start a pending timer.
        - candidate != current, still the same pending: switch once the hold
          elapses. A *different* candidate restarts the timer, so a signal must
          actually settle before it can move the context.
        """
        if candidate == self._context:
            self._pending = None
            return
        if self._pending != candidate:
            # New (or changed) candidate — (re)start its hold timer.
            self._pending = candidate
            self._pending_since = now
            return
        # Same pending candidate as last tick — has it held long enough?
        if (now - self._pending_since) >= self.switch_hold_s:
            self._transition_to(candidate, now)
            self._pending = None

    def _transition_to(self, new: SocialContext, now: float) -> None:
        old = self._context
        if new == old:
            return
        self._context = new
        self._context_since = now
        logger.info("context: %s -> %s", old.value, new.value)
        snap = self.snapshot()
        for cb in list(self._callbacks):
            try:
                cb(old, new, snap)
            except Exception:
                logger.exception("context: transition callback raised (ignored)")

    # ── small helpers ────────────────────────────────────────────────────

    def _safe_now(self) -> float:
        try:
            return float(self._clock())
        except Exception:
            return time.monotonic()


# ─── dry self-test (no device required) ───────────────────────────────────────


def _selftest() -> int:
    """Drive the state machine with injected signal sets on a fake clock.

    No device, no gateway, no files needed: a subclass overrides ``_read_signals``
    to return scripted signals and a manual clock advances time, so we can
    assert each state and the hysteresis hold deterministically.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("=== ContextEngine dry self-test ===")
    print(f"vision-state file : {VISION_STATE_PATH} "
          f"(exists={os.path.exists(VISION_STATE_PATH)})")
    print(f"switch_hold_s     : {SWITCH_HOLD_S}   face_stale_s: {FACE_STALE_S}")

    # A manual monotonic clock we advance by hand.
    clock = {"t": 1000.0}

    def now():
        return clock["t"]

    # An engine whose signals we inject directly (bypasses all real I/O).
    class _Scripted(ContextEngine):
        scripted: dict = {}

        def _read_signals(self) -> dict:
            sig = self._empty_signals()
            sig.update(self.scripted)
            # Recompute the two derived booleans the base normally sets, so a
            # scripted test only has to specify the primitive signals it cares
            # about (face/radar/input/voice) and gets owner/person for free.
            idle = sig.get("input_idle_s")
            input_active = isinstance(idle, (int, float)) and idle <= INPUT_ACTIVE_S
            face = sig["face"]
            radar = sig["radar"]
            sig["owner_signal"] = bool(
                (face.get("fresh") and face.get("is_owner")) or input_active
            )
            sig["person_present"] = bool(
                (face.get("fresh") and (face.get("seen") or face.get("present")))
                or radar.get("person_present")
            )
            return sig

    eng = _Scripted(movement=None, clock=now, switch_hold_s=3.0,
                    initial=SocialContext.ALONE)

    seen: list = []
    eng.on_transition(lambda o, n, s: seen.append((o.value, n.value)))

    def face_sig(*, seen_=True, owner=False, name=None, present=True):
        return {"seen": seen_, "name": name, "is_owner": owner,
                "present": present, "dx": 0.0, "dy": 0.0, "age_s": 0.1,
                "fresh": True}

    def radar_sig(present=False, front=False, mm=None, deg=None, n=0):
        return {"ok": True, "person_present": present, "front": front,
                "nearest_mm": mm, "nearest_deg": deg, "targets": n}

    def step(label, scripted, *, advance, expect):
        """Set signals, advance the clock, update, and assert current()."""
        eng.scripted = scripted
        clock["t"] += advance
        got = eng.update()
        ok = got == expect
        inst = eng.snapshot().get("instantaneous")
        print(f"  [{'OK ' if ok else 'ERR'}] {label:38s} "
              f"inst={inst:8s} -> current={got.value:8s} (want {expect.value})")
        assert ok, f"{label}: expected {expect}, got {got}"

    print("\nScenario walk (fake clock; hold=3s):")
    # Start ALONE. An owner face appears -> needs to hold 3s before switching.
    step("owner face, +1s (within hold)",
         {"face": face_sig(owner=True, name="Dominic")},
         advance=1.0, expect=SocialContext.ALONE)     # pending, not yet held
    step("owner face persists, +3s (held)",
         {"face": face_sig(owner=True, name="Dominic")},
         advance=3.0, expect=SocialContext.OWNER)     # now switches
    # Owner starts talking -> ENGAGED (higher priority; still needs the hold).
    step("voice turn live, +1s",
         {"voice_turn": True, "face": face_sig(owner=True, name="Dominic")},
         advance=1.0, expect=SocialContext.OWNER)     # pending ENGAGED
    step("voice turn persists, +3s",
         {"voice_turn": True, "face": face_sig(owner=True, name="Dominic")},
         advance=3.0, expect=SocialContext.ENGAGED)
    # Owner leaves; nobody there. Voice ends -> candidate OWNER (memory warm),
    # which must first hold; then once owner-memory lapses the candidate becomes
    # ALONE, restarting the hold (different candidate) — so ALONE needs its own
    # settle tick. This two-stage settle IS the anti-flap behaviour.
    step("empty frame, +3s (voice ended, owner memory warm)",
         {}, advance=3.0, expect=SocialContext.ENGAGED)  # OWNER pending, not held
    step("empty frame, +100s (owner memory lapsed, ALONE now pending)",
         {}, advance=100.0, expect=SocialContext.ENGAGED)  # candidate flips to ALONE
    step("empty frame, +3s (ALONE held)",
         {}, advance=3.0, expect=SocialContext.ALONE)
    # A stranger arrives while the owner is away -> STRANGER.
    step("unknown face, +1s", {"face": face_sig(owner=False, name=None)},
         advance=1.0, expect=SocialContext.ALONE)      # pending
    step("unknown face persists, +3s",
         {"face": face_sig(owner=False, name=None)},
         advance=3.0, expect=SocialContext.STRANGER)
    # Radar alone (module live) with a near target -> a person present = COMPANY
    # (no face identity, owner not around long enough to matter). Switching from
    # the settled STRANGER needs the candidate to register then hold: two ticks.
    radar_only = {"radar": radar_sig(present=True, front=True, mm=900, deg=5, n=1)}
    step("radar target only, +1s (COMPANY pending)",
         radar_only, advance=1.0, expect=SocialContext.STRANGER)
    step("radar target persists, +3s (COMPANY held)",
         radar_only, advance=3.0, expect=SocialContext.COMPANY)
    # Everyone gone, on the dock + charging -> CHARGING (register then hold).
    dock = {"battery": {"level": 74, "charging": True},
            "rail": {"pos_mm": 12.0, "on_dock": True}}
    step("on dock + charging, +1s (CHARGING pending)",
         dock, advance=1.0, expect=SocialContext.COMPANY)
    step("on dock + charging persists, +3s (CHARGING held)",
         dock, advance=3.0, expect=SocialContext.CHARGING)

    print("\nTransitions fired (enter/exit):")
    for (o, n) in seen:
        print(f"  {o:9s} -> {n}")

    print("\nFinal snapshot():")
    snap = eng.snapshot()
    for k in ("context", "context_held_for_s", "pending", "instantaneous"):
        print(f"  {k:20s}: {snap.get(k)}")
    print("  raw signal keys     : "
          + ", ".join(k for k in snap if k not in
                      ("context", "context_held_for_s", "pending",
                       "pending_for_s", "instantaneous")))

    # Sanity: a bad/exploding signal source must not crash update().
    class _Boom(ContextEngine):
        def _read_signals(self):
            raise RuntimeError("signal source on fire")

    boom = _Boom(movement=None, clock=now)
    boom.update()  # must swallow and stay put
    print(f"\nGuard check: exploding signal source -> stayed "
          f"{boom.current().value} without raising. OK.")

    print("\nOK: all scripted contexts matched; hysteresis + guards verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
