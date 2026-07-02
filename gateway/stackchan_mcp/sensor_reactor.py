"""Gateway-embedded Wheatley sensor monitoring and reaction sequences.

Runs as a background async task inside the gateway process. Monitors
device sensors directly and fires characterful head/avatar reactions.

  AUTO-TRIGGERS (device sensors, polled by gateway):
    Touch screen  → Panic Mode   (approximates LTR-553ALS proximity)
    ES7210 mic    → Hacker Mode  (brief listen probe, opt-in via env var)

  MANUAL TRIGGERS (POST /react/<name> on capture server, or firmware events):
    panic         closest approach / hand near screen
    hacker        loud ambient sound
    overtrack     camera over-correction (query ?direction=left|right|up|down)
    tantrum       bump/pickup       (query ?type=desk|pickup)
    recognize     stackchan-vision-loop.py spotted a face (query ?person=known|unknown)

  FUTURE FIRMWARE EVENTS (when firmware exposes them via stackchan-event):
    event_type="proximity" → panic
    event_type="imu"       → tantrum (desk or pickup based on g-force)
    event_type="audio"     → hacker

All behaviors implement "eye leads head": the face/avatar changes 70ms
before the head servo moves, mimicking the biological visual reflex.
This is the single most important thing for making Wheatley feel alive.

Configuration via env vars:
  STACKCHAN_AUDIO_PROBE      set to "1" to enable ES7210 level probing
  STACKCHAN_AUDIO_THRESHOLD  float 0-1, default 0.65 (loud sound threshold)
  STACKCHAN_TOUCH_POLL_MS    touch poll interval ms, default 150
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
import uuid

from .audio_stream import is_recording, start_recording, stop_recording
from .phrase_pick import pick as _pick

logger = logging.getLogger(__name__)

# ─── timing constants ─────────────────────────────────────────────────────────
EYE_LEAD   = 0.07   # face change before head moves (the "eye leads head" beat)
SETTLE     = 0.12   # pause between rapid moves to avoid servo strain
TOUCH_POLL = float(os.environ.get("STACKCHAN_TOUCH_POLL_MS", "150")) / 1000.0
AUDIO_PROBE_ENABLED = os.environ.get("STACKCHAN_AUDIO_PROBE", "0") == "1"
AUDIO_THRESHOLD     = float(os.environ.get("STACKCHAN_AUDIO_THRESHOLD", "0.65"))
AUDIO_PROBE_INTERVAL = 3.0   # seconds between audio probes

# The one enrolled name that gets the warm GREETING_PHRASES treatment —
# everyone else recognized gets NON_OWNER_GREETING_PHRASES instead (2026-
# 07-03: "everyone else Wheatley can be a bit ruder to them and say bad
# jokes"). Compared case-insensitively against person_name in
# _behavior_recognize. Unknown-face handling (ASK_NAME_PHRASES, the
# enrollment flow) is unaffected either way.
OWNER_NAME = os.environ.get("STACKCHAN_OWNER_NAME", "Dominic")

# Speed presets (degrees/sec) forwarded to self.robot.set_head_angles
_SPD = {"slow": 30, "mid": 120, "fast": 240, "max": 500}

# ─── activity file (shared with the idle loop) ─────────────────────────────
_ACTIVITY_FILE = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", ".")), "stackchan-activity"
)


def _mark_active() -> None:
    """Tell the ambient idle loop to hold still."""
    try:
        with open(_ACTIVITY_FILE, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


# ─── SensorReactor ───────────────────────────────────────────────────────────

class SensorReactor:
    """Background sensor watcher and behaviour sequencer for the gateway.

    Create one instance per gateway, pass it the ESP32Manager, then call
    start()/stop() with the gateway lifecycle.
    """

    def __init__(self, esp32: object, gateway: object | None = None) -> None:
        self._esp32 = esp32
        # Optional: needed only for behaviours that speak (e.g. the
        # face-recognition greeting). None is fine for callers/tests that
        # only exercise movement/LED behaviours.
        self._gateway = gateway
        self._task: asyncio.Task | None = None
        self._behavior_lock = asyncio.Lock()
        self._running = False

    # ── lifecycle ─────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._poll_loop(), name="sensor-reactor")
        logger.info("SensorReactor started (touch_poll=%.0fms audio_probe=%s)",
                    TOUCH_POLL * 1000, AUDIO_PROBE_ENABLED)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("SensorReactor stopped")

    # ── public: external trigger ──────────────────────────────────────────

    async def trigger(self, name: str, **kwargs: object) -> bool:
        """Fire a named behaviour from outside (HTTP endpoint, firmware event).

        Returns True if the behaviour was enqueued (reactor not already busy),
        False if a behaviour is already running (caller can 429).
        """
        if self._behavior_lock.locked():
            return False
        asyncio.create_task(self._run(name, **kwargs), name=f"react-{name}")
        return True

    # ── poll loop ─────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        last_touch        = False
        tap_times: list[float] = []
        last_audio_probe  = 0.0

        while self._running:
            # Wait for device to be connected and ready
            if not getattr(self._esp32, "device_connected", False):
                await asyncio.sleep(2.0)
                continue

            # ── touch polling ──────────────────────────────────────────
            try:
                result, _ = await self._esp32.call_tool(
                    "self.touch.get_touch_state", {}
                )
                touching = self._parse_touching(result)

                if touching and not last_touch:
                    now = asyncio.get_event_loop().time()
                    tap_times = [t for t in tap_times if now - t < 0.8]
                    tap_times.append(now)
                    if not self._behavior_lock.locked():
                        if len(tap_times) >= 2:
                            # double-tap → hacker mode
                            asyncio.create_task(self._run("hacker"), name="react-hacker")
                            tap_times.clear()
                        else:
                            # single tap → panic
                            asyncio.create_task(self._run("panic"), name="react-panic")
                last_touch = touching
            except Exception:
                pass

            # ── audio probe (opt-in) ───────────────────────────────────
            if AUDIO_PROBE_ENABLED:
                now = time.monotonic()
                if now - last_audio_probe > AUDIO_PROBE_INTERVAL:
                    last_audio_probe = now
                    try:
                        level = await self._audio_probe()
                        if level > AUDIO_THRESHOLD and not self._behavior_lock.locked():
                            asyncio.create_task(self._run("hacker"), name="react-hacker-audio")
                    except Exception:
                        pass

            await asyncio.sleep(TOUCH_POLL)

    # ── helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _parse_touching(result: object) -> bool:
        if not result:
            return False
        try:
            import json
            content = result.get("content", [])
            if not content:
                return False
            text = content[0].get("text", "{}")
            state = json.loads(text)
            # Firmware may use any of these keys
            return bool(
                state.get("pressed")
                or state.get("touching")
                or state.get("is_pressed")
                or int(state.get("count", 0)) > 0
            )
        except Exception:
            return False

    async def _audio_probe(self) -> float:
        """Capture 200 ms from the device mic and return a normalised level.

        Uses Opus frame-size as a proxy for loudness (DTX means silence→
        tiny frames; speech/clap → large frames).  The listen_lock prevents
        races with TTS and STT sessions.
        """
        esp32 = self._esp32
        if esp32.tts_lock.locked() or is_recording():
            return 0.0

        sess = "sensor-" + uuid.uuid4().hex[:6]
        async with esp32.listen_lock:
            start_recording(sess)
            try:
                await esp32.send_listen_state("start")
                await asyncio.sleep(0.20)
                await esp32.send_listen_state("stop")
            except Exception:
                pass
            finally:
                frames = stop_recording()

        if not frames:
            return 0.0
        avg_bytes = sum(len(f) for f in frames) / len(frames)
        # ~12 bytes = silence (DTX comfort), ~80 bytes = speech, 120+ = clap
        return min(1.0, avg_bytes / 80.0)

    async def _face(self, name: str) -> None:
        await self._esp32.call_tool("self.display.set_avatar", {"face": name})

    async def _move(self, yaw: int, pitch: int, speed: str = "mid") -> None:
        await self._esp32.call_tool(
            "self.robot.set_head_angles",
            {"yaw": _clamp(yaw, -80, 80),
             "pitch": _clamp(pitch, 10, 80),
             "speed_dps": _SPD[speed]},
        )

    async def _leds(self, r: int, g: int, b: int) -> None:
        # NOTE: "self.robot.set_led_color" (from stackchan_mcp/tools.py's
        # local test-stub registry) does not exist on the live firmware —
        # verified 2026-07-01 against the connected device's actual tool
        # list. The real tool is self.led.set_all (12-LED base ring).
        await self._esp32.call_tool(
            "self.led.set_all", {"r": r, "g": g, "b": b}
        )

    async def _say(self, text: str) -> None:
        """Speak via the same TTS path as the `say` MCP tool.

        Lazy import: synthesize_and_send pulls in the TTS stack (edge-tts/
        opuslib etc., the `[tts]` extra), same reasoning as the lazy import
        in capture_server.py's /pcm handler — behaviours that don't speak
        (panic, overtrack, tantrum, hacker) shouldn't require it.
        """
        if self._gateway is None:
            logger.warning("SensorReactor: no gateway ref, cannot speak %r", text)
            return
        try:
            from .tts import synthesize_and_send
            await synthesize_and_send({"text": text}, gateway=self._gateway)
        except Exception as exc:
            logger.warning("SensorReactor: speak failed: %s", exc)

    async def _run(self, name: str, **kwargs: object) -> None:
        """Acquire the behaviour lock and run the named sequence."""
        async with self._behavior_lock:
            _mark_active()
            try:
                fn = getattr(self, f"_behavior_{name}", None)
                if fn is None:
                    logger.warning("SensorReactor: unknown behaviour %r", name)
                    return
                await fn(**kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("SensorReactor behaviour %r raised: %s", name, exc)

    # ══════════════════════════════════════════════════════════════════════
    # BEHAVIOUR SEQUENCES
    # ══════════════════════════════════════════════════════════════════════
    # Rule: ALWAYS change the face first, wait EYE_LEAD seconds, THEN move.
    # This is what makes Wheatley feel biological instead of mechanical.

    # ── 1. Proximity Panic ────────────────────────────────────────────────
    async def _behavior_panic(self, **_: object) -> None:
        """Something got close. MAX dilation, snap back, trembling shiver."""
        # Eyes lead: max open — surprised is scale 1.18, no lids
        await self._face("surprised")
        await asyncio.sleep(EYE_LEAD)

        # Head: sharp recoil upward (lower pitch = look up = recoil away)
        await self._move(0, 38, "max")
        await asyncio.sleep(SETTLE)

        # Rapid trembling shiver (alternating yaw, noise in pitch)
        shiver_y = 0
        for _ in range(10):
            shiver_y = -shiver_y + random.choice([-9, 9]) + random.randint(-2, 2)
            await self._move(shiver_y, 38 + random.randint(-2, 2), "max")
            await asyncio.sleep(0.09)
            _mark_active()

        # Settle scared: still wide-eyed, slightly above neutral
        await self._move(0, 41, "mid")
        await asyncio.sleep(0.55)

        # Recover: back to idle
        await self._face("idle")
        await self._move(0, 45, "slow")

    # ── 2. Erratic Over-Correction (camera tracking) ─────────────────────
    async def _behavior_overtrack(self, direction: str = "left", **_: object) -> None:
        """Can't keep up with his own brain. Over-shoots, then jerks back."""
        cfg = {
            "left":  dict(eye="embarrassed", overshoot_y=-38, target_y=-18),
            "right": dict(eye="happy",       overshoot_y=38,  target_y=18),
            "up":    dict(eye="thinking",    overshoot_p=30,  target_p=38),
            "down":  dict(eye="idle",        overshoot_p=62,  target_p=55),
        }.get(direction, dict(eye="embarrassed", overshoot_y=-38, target_y=-18))

        # Eyes lead: squint toward the thing it's trying to track
        await self._face(cfg["eye"])
        await asyncio.sleep(EYE_LEAD)

        if "overshoot_y" in cfg:
            # Jerk PAST the target
            await self._move(cfg["overshoot_y"], 43, "max")
            await asyncio.sleep(0.25)
            # "Oh wait—" snap back past centre the OTHER way
            await self._move(-cfg["overshoot_y"] // 2, 43, "max")
            await asyncio.sleep(0.18)
            # Correct to actual location
            await self._move(cfg["target_y"], 43, "mid")
        else:
            await self._move(0, cfg["overshoot_p"], "max")
            await asyncio.sleep(0.25)
            mid_p = 45 - (cfg["overshoot_p"] - 45) // 2
            await self._move(0, _clamp(mid_p, 10, 80), "max")
            await asyncio.sleep(0.18)
            await self._move(0, cfg["target_p"], "mid")

        await self._face("idle")
        await asyncio.sleep(0.5)
        await self._move(0, 45, "slow")

    # ── 3. Anti-Gravity Tantrum (IMU / desk bump) ─────────────────────────
    async def _behavior_tantrum(self, type: str = "desk", **_: object) -> None:
        """How DARE you disturb the rail system."""
        if type == "desk":
            # Small bump: offended slow-blink stare sequence

            # Eyes lead: snap wide — what WAS that?
            await self._face("surprised")
            await asyncio.sleep(EYE_LEAD)

            # Look DOWN at base sharply
            await self._move(0, 65, "max")
            await asyncio.sleep(0.10)

            # Switch to cold flat stare, hold it (idle = unamused, not left-look)
            await self._face("idle")
            for _ in range(4):
                await asyncio.sleep(0.25)
                _mark_active()

            # Eyes lead the rise: go wide before tilting head back up
            await self._face("surprised")
            await asyncio.sleep(EYE_LEAD)

            # Slow, offended return — he doesn't forget
            await self._move(0, 52, "slow")
            await asyncio.sleep(0.4)
            await self._move(0, 43, "slow")

            # Hold the wide-eyed offended stare for one more beat
            await asyncio.sleep(0.55)
            await self._face("idle")

        else:  # "pickup" — being removed from the rail
            # Red panic immediately
            await self._face("sad")
            await asyncio.sleep(EYE_LEAD)

            # Servos fight back: flail to max limits
            for _ in range(14):
                y = random.choice([-50, 50]) + random.randint(-8, 8)
                p = random.choice([22, 68]) + random.randint(-5, 5)
                await self._move(_clamp(y, -70, 70), _clamp(p, 15, 75), "max")
                await asyncio.sleep(0.10)
                _mark_active()

            # Exhausted settle
            await self._move(0, 45, "slow")
            await asyncio.sleep(0.3)
            await self._face("idle")

    # ── 4. Fake Hacker Mode (audio trigger) ──────────────────────────────
    async def _behavior_hacker(self, **_: object) -> None:
        """Intense focus on a very difficult nothing. 3 seconds. Snap."""
        # Eyes lead RIGHT — optic darts to right edge before head tilts right
        await self._face("happy")
        await asyncio.sleep(EYE_LEAD)

        # Head: tilt 15° right, lean forward (lower pitch = lean into task)
        await self._move(15, 40, "mid")

        # Hold for exactly 3 seconds of intense computing nothing
        for _ in range(6):
            await asyncio.sleep(0.5)
            _mark_active()

        # SNAP: eyes go wide FIRST
        await self._face("surprised")
        await asyncio.sleep(EYE_LEAD)

        # Then head snaps forward — nothing to see here
        await self._move(0, 43, "max")
        await asyncio.sleep(0.4)

        # Settle back to completely normal idle
        await self._face("idle")
        await self._move(0, 45, "slow")

    # Wheatley-flavoured welcome-back lines — fired on a "known" recognition
    # that's cleared the (long) re-greet cooldown in stackchan-vision-loop.py
    # (default 1 hour, so this doesn't repeat every few minutes of sitting
    # at the desk, only after a genuine absence).
    GREETING_PHRASES = [
        "Oh, look who's back — are we fixing this code or what?",
        "Ah, there you are! Good, good. Thought you'd abandoned me to the void.",
        "Oh, it's you. Brilliant. Right, where were we?",
        "AH! You're alive! Brilliant. Knew you'd be fine. Never doubted it.",
        "Hey hey hey! There you are. Right — back to it, yeah?",
        "Oh! Hello! You're back. I was just... doing important things. Anyway.",
        "You look terribl— ummm... good! Looking good, actually.",
        "There you are. Been holding the fort. Nothing exploded. You're welcome.",
    ]

    # Name-aware variants, used when the vision loop passes through WHO it
    # recognized (it always knew — see stackchan-vision-loop.py's
    # _fire_reaction — but the name used to be dropped at this hop). Mixed
    # into the same "greeting" pick pool as the generic lines so he doesn't
    # open with your name every single time.
    NAME_GREETING_PHRASES = [
        "{name}! There you are! Brilliant. Right, where were we?",
        "Oh! {name}! It's you! I knew that. Facial recognition. Very advanced stuff.",
        "Ah, {name}. Good. Was starting to talk to myself. More than usual.",
        "{name}! You're back! Everything's fine. Nothing happened while you were gone. Don't check.",
        "Look who it is! {name}! My favourite human. Don't tell the others. There are no others.",
    ]

    # Fired instead of GREETING_PHRASES/NAME_GREETING_PHRASES for a
    # recognized person who ISN'T OWNER_NAME — cheekier/backhanded rather
    # than warm, plus a few actual (deliberately groan-worthy) jokes aimed
    # at them specifically. Still Wheatley, not genuinely mean — teasing,
    # not hostile.
    NON_OWNER_GREETING_PHRASES = [
        "Oh. It's you again, {name}. Riveting.",
        "{name}. Back again. Try not to touch anything important.",
        "Oh good, {name}. I was having such a nice quiet moment.",
        "{name}! Still here, apparently. Persistent, aren't you.",
        "It's {name}. Don't get comfortable — I'm mostly watching for someone else.",
        "Ah, {name}. Not who I was hoping for, but I suppose you'll do.",
        "{name}, you again. Is there a collective noun for repeat visitors? A nuisance, probably.",
        "{name}! Question: what do you call a fish with no eyes? A fsh. Anyway. Hello.",
        "Oh, {name}. Why don't scientists trust atoms? They make up everything. Bit like your excuse for being here, probably.",
        "{name}, back for more. What do you call a robot that takes the long way round? R2 detour. I'll see myself out.",
    ]

    # Fired alongside a "known" greeting when the arbiter judged a fresh
    # capture "definite" match + "good" quality — proposing to add it as a
    # new reference sample. Confirmation happens via tap-to-talk (same
    # reasoning as ASK_NAME_PHRASES — no hands-free listening yet); see
    # stackchan-voice-bridge.py's PENDING_LEARN_CONFIRM_MARKER handling.
    LEARN_CONFIRM_ASK_PHRASES = [
        "Oh, and — got a proper good look at you just then. Should I remember that view, {name}?",
        "Quick thing, {name} — that was a clear shot. Want me to learn it?",
        "Also! Got a really good angle there. Shall I remember that one, {name}?",
    ]

    # Fired for an unrecognized face. Tells them how to actually answer
    # (tap the screen) since there's no hands-free listening yet.
    ASK_NAME_PHRASES = [
        "Oh, hello! Don't think we've met. Tap the screen and tell me your name?",
        "Ah, someone new! Go on then, tap the screen, who are you?",
        "Right, I don't recognize you — tap the screen and introduce yourself?",
        "Hello! Yes, you, the new one. Tap the screen and tell me your name?",
        "Ooh, a new human! Love it. Tap the screen and introduce yourself, go on.",
        "Hold on — I don't know you, do I? Tap the screen, give us a name.",
    ]

    # ── 5. Face Recognized (local vision loop, no API cost) ───────────────
    async def _behavior_recognize(
        self, person: str = "unknown", person_name: str = "",
        propose_learn: bool = False, **_: object
    ) -> None:
        """stackchan-vision-loop.py spotted a face via fully-local detection.

        Deliberately small/quiet compared to the other behaviours — this can
        fire from an ambient polling loop rather than a deliberate touch/bump,
        so it should read as "noticed", not "startled".
        """
        if person == "known":
            # Eyes lead: brief warm look, soft green flash, small nod.
            await self._face("happy")
            await asyncio.sleep(EYE_LEAD)
            await self._leds(0, 60, 20)
            await self._move(0, 40, "mid")
            await asyncio.sleep(0.22)
            await self._move(0, 46, "mid")
            await asyncio.sleep(0.35)
            is_owner = bool(person_name) and person_name.strip().lower() == OWNER_NAME.strip().lower()
            if is_owner:
                pool = list(self.GREETING_PHRASES)
                if person_name:
                    pool += [p.format(name=person_name) for p in self.NAME_GREETING_PHRASES]
                await self._say(_pick("greeting", pool))
            elif person_name:
                pool = [p.format(name=person_name) for p in self.NON_OWNER_GREETING_PHRASES]
                await self._say(_pick("non-owner-greeting", pool))
            else:
                # known but no name came through — shouldn't normally
                # happen (vision-loop always forwards it for "known"), fall
                # back to the generic (nameless) owner-style lines.
                await self._say(_pick("greeting", self.GREETING_PHRASES))
            if propose_learn and person_name:
                await asyncio.sleep(0.3)
                phrase = _pick("learn-confirm-ask", self.LEARN_CONFIRM_ASK_PHRASES)
                await self._say(phrase.format(name=person_name))
            await self._face("idle")
            # Matches stackchan-hook.py's IDLE_LED so the ring doesn't stay
            # stuck on the acknowledgement colour.
            await self._leds(0, 25, 90)
            await self._move(0, 45, "slow")
        else:
            # Curious little side-to-side glance toward the unfamiliar face —
            # wary, not alarmed (that's `panic`, a much bigger reaction) —
            # then ask who they are. Tap-to-answer, not hands-free: the
            # gateway's `listen` tool (remote mic capture) hangs against the
            # currently-flashed firmware (see firmware/TODO.md), so this
            # directs them to the touch-to-talk flow instead. stackchan-
            # voice-bridge.py checks PENDING_ENROLLMENT_MARKER (written by
            # stackchan-vision-loop.py right before firing this reaction) to
            # know the next tap-to-talk transcript is probably a name.
            await self._face("thinking")
            await asyncio.sleep(EYE_LEAD)
            await self._move(-6, 40, "mid")
            await asyncio.sleep(0.3)
            _mark_active()
            await self._move(6, 40, "mid")
            await asyncio.sleep(0.3)
            await self._say(_pick("ask-name", self.ASK_NAME_PHRASES))
            await self._face("idle")
            await self._move(0, 45, "slow")
