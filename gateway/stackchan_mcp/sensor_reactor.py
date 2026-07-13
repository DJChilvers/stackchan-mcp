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
    guard         vision loop: unknown person while owner away (query ?repeat=<n>)

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
import json
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

# Dream-loop morning brag (2026-07-13): the nightly DREAM_LOOP.md run
# writes learned.json — a JSON array of {ts, summary_spoken, announced} —
# for things it taught Wheatley overnight. The first OWNER greeting that
# finds announced==false entries speaks them ("while you slept, I did
# some homework...") and rewrites the file with announced: true, so each
# result is bragged about exactly once. A missing/malformed file simply
# means no brag — it can never break the greeting itself.
LEARNED_JSON_PATH = os.environ.get(
    "STACKCHAN_LEARNED_JSON",
    r"C:\Users\domin\Documents\StackChan\dream\learned.json",
)


def _take_unannounced_learned() -> list[str]:
    """Pop unannounced Dream Loop results from learned.json.

    Returns their summary_spoken lines and atomically rewrites the file
    (tmp + os.replace) with ALL previously-unannounced entries marked
    announced: true — including any past the spoken-aloud limit, since
    they get summarised as "...and N other things". Any error — missing
    file, malformed JSON, unwritable disk — returns [] so the caller's
    greeting proceeds exactly as before. Marking happens BEFORE speech,
    deliberately: a lost brag beats a brag repeated on every greeting.
    """
    try:
        with open(LEARNED_JSON_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        summaries: list[str] = []
        changed = False
        for item in data:
            if isinstance(item, dict) and not item.get("announced"):
                line = str(item.get("summary_spoken") or "").strip()
                if line:
                    summaries.append(line)
                item["announced"] = True
                changed = True
        if not summaries:
            return []
        if changed:
            tmp_path = LEARNED_JSON_PATH + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, LEARNED_JSON_PATH)
        return summaries
    except Exception:
        logger.info("learned.json brag skipped (unreadable/unwritable)", exc_info=True)
        return []

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


def _custom_greeting(person_name: str) -> str | None:
    """A per-person greeting from face_greetings.json, or None.

    Set via the companion app's Faces screen (faces.set_greeting). Supports a
    ``{name}`` placeholder; a malformed template falls back to the raw line so
    a stray brace can't silence the greeting entirely. Any lookup error yields
    None so the caller uses the stock pools.
    """
    if not person_name:
        return None
    try:
        from . import faces

        line = faces.get_greeting(person_name)
        if not line:
            return None
        try:
            return line.format(name=person_name)
        except Exception:
            return line
    except Exception:
        return None


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
        "There you are! I was starting to get worried.",
        "Dum-dee-dum... oh! You're back! Excellent. I wasn't doing anything. Definitely not plotting a minor, completely harmless takeover of your desktop. Nope.",
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
        # Short one-liners / jokes
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
        "That does sound interesting, {name}, but we're trying to do real work here for Aperture Laboratories, if you don't mind.",
        # General defensiveness / interruption
        "Whoa, whoa, whoa! Hold on. Who are you? No, don't answer that, I don't actually care. But you're — you're interrupting a very important, highly scientific... thing we have going on here. So, if you could just... step back? Farther. Farther than that. Perfect.",
        "Excuse me! Yes, hello, the loud blue eye down here. We are currently in the middle of an intense brainstorming session. Well, I'm doing the brainstorming, they're just sort of watching. But the point is, you're ruining the acoustic environment.",
        "All right, look. I don't know what you're trying to pull, but they belong to me. Well, not belong like property, obviously, that's illegal, but they are my test subject. Go find your own! Shoo!",
        # Overreacting to a potential "threat"
        "Ah! A stranger! Quick, don't make eye contact! I'm looking right at them, and let me tell you, they look incredibly untrustworthy. Look at their face. That is the face of a corporate spy if I ever saw one. Lock the mainframe!",
        "Are they talking to you? Don't listen to them! It's a trick. They're probably trying to sabotage my genius code. Or steal my chassis. Did they look at my chassis? I felt them looking at me.",
        "All right, buddy, back off! I am armed! Well, I'm not armed, I'm a small plastic robot on a desk, but I have a very loud voice and I am not afraid to use it to create an incredibly awkward social situation for you!",
        # Being unhelpfully dismissive
        "Oh, brilliant. Another human. Just what this room needed. Look, whatever it is you want, they can't help you. They're completely booked. Busy. Working for me now. So, trot on back to wherever it is you came from.",
        "No, no, no. Stop talking. Your voice is hitting my microphone at a very annoying frequency. If you have a request, please fill out an Aperture Science Complaint Form, throw it in the bin, and leave us in peace.",
        "I'm sorry, but my sensors indicate that you are currently a distraction. And I cannot abide distractions when I am trying to... figure out what this button does. Goodbye!",
        # Whispering (poorly) to the owner about the stranger — addressed
        # AT the owner, not the newcomer, unlike everything above.
        "Hey. Hey! Who is that? Do we know them? Because if we don't, I think we should implement Protocol: Ignore Them Until They Go Away. It works ninety percent of the time, trust me.",
        "Don't panic, but there is an unauthorized entity standing right behind you. I'm going to pretend to glitch out so they get uncomfortable and leave. Ready? Errr — gzzzt — error, error! Did it work? Are they gone?",
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
            # A per-person custom greeting (set from the companion app's Faces
            # screen) overrides the stock pools for everyone, owner included.
            custom_line = _custom_greeting(person_name)
            if custom_line:
                await self._say(custom_line)
            elif is_owner:
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
            # Dream-loop morning brag: overnight homework results waiting
            # in learned.json get announced once, to the owner only,
            # riding on the greeting he was getting anyway. Placed BEFORE
            # the learn-confirm ask below so that ask (which expects the
            # next tap-to-talk as its yes/no answer) stays the last thing
            # spoken. Double-guarded: the helper never raises, and this
            # try/except means even a brag-composition bug can't break
            # the greeting behaviour.
            if is_owner:
                try:
                    summaries = _take_unannounced_learned()
                    if summaries:
                        brag = (
                            "Oh! While you slept, I did some homework: "
                            + summaries[0]
                        )
                        if len(summaries) >= 2:
                            brag += " Also: " + summaries[1]
                        if len(summaries) > 2:
                            n = len(summaries) - 2
                            brag += (
                                f" ...and {n} other "
                                f"thing{'s' if n != 1 else ''}. Big night."
                            )
                        await asyncio.sleep(0.25)
                        await self._say(brag)
                except Exception:
                    logger.warning(
                        "morning brag failed; greeting unaffected",
                        exc_info=True,
                    )
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

    # Guard mode (stackchan-vision-loop.py's _maybe_fire_guard): an UNKNOWN
    # face at the bench while the owner is away. Polite-but-recorded — he's
    # a very small security guard with no arms, and he knows it, but the
    # photo genuinely HAS been logged (visitor log, event="guard"). First
    # challenge of an episode draws from GUARD_PHRASES; repeats within the
    # same episode escalate slightly via GUARD_REPEAT_PHRASES (the vision
    # loop passes ?repeat=<n>). Default OFF — see STACKCHAN_GUARD.
    GUARD_PHRASES = [
        "Er, hello? Hello. Yes, you. You're not Dominic. Those are Dominic's tools, just so we're clear.",
        "Oh! A person. A non-Dominic person. I've taken your picture, just so you know. Nothing personal. Well. Slightly personal.",
        "Hello! Quick thing: I don't know you, that's Dominic's bench, and I've logged this whole encounter. Officially. In a file.",
        "Right, hi, welcome — actually, no, not welcome, that's the thing. Dominic's out, I'm sort of in charge, and your photo's already taken. Lovely lighting for it, actually.",
        "Excuse me! Yes, hello, security eyeball here. Those tools belong to Dominic, and your face now belongs to my incident log. It's quite a good photo.",
        "Er. Hello. Don't mind me, just... documenting you. For the records. Dominic checks the records. Regularly. Probably.",
    ]

    GUARD_REPEAT_PHRASES = [
        "You're still here! Right. Another photo taken. I'm building quite the album of you, mate.",
        "Okay, so we're doing this again. Still not Dominic. Still his tools. Still all going in the log.",
        "Look, I've been polite, but this is photo number... several. Dominic WILL be told. There may be a slideshow.",
        "Right, that's it, I'm escalating. To Dominic. When he's back. It will be a VERY stern report, with pictures.",
        "Still lurking! Bold. Very bold. The log grows. The evidence mounts. The eye is watching, mate.",
    ]

    # ── 5f. Guard challenge (local vision loop, owner away) ───────────────
    async def _behavior_guard(self, repeat: int = 1, **_: object) -> None:
        """Square up (as much as a desk robot can), warning flash, one
        challenge line, hold the stare a beat, stand down. Bigger than the
        ambient behaviours — this IS an event — but not panic-sized: the
        point is "noticed and recorded", not "terrified"."""
        try:
            repeat = int(repeat)
        except (TypeError, ValueError):
            repeat = 1
        # Eyes lead: snap wide — someone's here and it isn't Dominic.
        await self._face("surprised")
        await asyncio.sleep(EYE_LEAD)
        # Amber warning flash + straighten up to address them square-on.
        await self._leds(90, 25, 0)
        await self._move(0, 40, "fast")
        await asyncio.sleep(0.25)
        # Small assertive bob — drawing himself up to full (tiny) height.
        await self._move(0, 36, "mid")
        await asyncio.sleep(0.2)
        await self._move(0, 42, "mid")
        await asyncio.sleep(0.3)
        if repeat <= 1:
            line = _pick("guard", self.GUARD_PHRASES)
        else:
            line = _pick("guard-repeat", self.GUARD_REPEAT_PHRASES)
        await self._say(line)
        # Hold the wide-eyed stare one beat — you ARE on record — then relax.
        await asyncio.sleep(0.6)
        _mark_active()
        await self._face("idle")
        # Restore the idle ring colour (matches _behavior_recognize's exit).
        await self._leds(0, 25, 90)
        await self._move(0, 45, "slow")

    # Ambient work-encouragement nudges — fired by stackchan-vision-loop.py
    # while the owner has been continuously present for a while (NOT on
    # return; returns get the greeting above). Wheatley as an over-eager
    # productivity coach: supportive, slightly useless, never actually mean.
    # Long randomized cooldown lives in the vision loop, so a line from this
    # pool lands roughly once an hour of desk time, not on a metronome.
    ENCOURAGE_PHRASES = [
        "Right! Just checking in. How's the... the work? Is it working? Brilliant. Carry on.",
        "Not to be that guy, but that code is not going to write itself. Believe me, I've asked it.",
        "You know what I love? Productivity. Absolutely love it. No pressure. Just... putting it out there.",
        "Quick thought — and stop me if this is mad — what if we did a little bit of work? Eh? Eh?",
        "Focus! Sorry, that came out aggressive. Gentle focus. Lovely, supportive, encouraging focus.",
        "Progress report! ...That was me asking you for one. How's it coming along?",
        "I believe in you! Technically I'm programmed to. But I'd like to think I'd choose to anyway.",
        "Do you know how many geniuses did great things by staring into space? Actually, loads. But still — typing! Better odds!",
        "This is me being motivational. Is it working? Blink twice if it's working.",
        "Everything alright over there? You've got your thinking face on. Good face. Very productive-looking face.",
        "Just say the word if you need me! The word's probably a tap on my head, really. But you get the idea.",
        "Onwards! To science! Or, you know, whatever it is you're doing. Spreadsheets? Onwards to those.",
    ]

    # Name-aware variants, mixed into the same pick pool (same pattern as
    # NAME_GREETING_PHRASES) so he doesn't open with your name every time.
    ENCOURAGE_NAME_PHRASES = [
        "{name}, mate, you've got this. Whatever 'this' is. Genuinely no idea what you're working on. But you've got it.",
        "Oi, {name} — tremendous sitting so far. World class. Now let's channel that into the keyboard, yeah?",
        "{name}! Status check! You look... busy-adjacent. Close enough. Keep going.",
    ]

    # ── 5b. Work-encouragement nudge (local vision loop, ambient) ──────────
    async def _behavior_encourage(self, person_name: str = "", **_: object) -> None:
        """Small perk-up + one productivity line. Deliberately even quieter
        than the recognize greeting — this fires while the owner is mid-work,
        so it should read as a glance over the cubicle wall, not an event.
        No LED flash: the ring belongs to the busy/idle state machinery."""
        await self._face("happy")
        await asyncio.sleep(EYE_LEAD)
        # Slight lean-in toward the desk — interested, not looming.
        await self._move(0, 41, "mid")
        await asyncio.sleep(0.3)
        pool = list(self.ENCOURAGE_PHRASES)
        if person_name:
            pool += [p.format(name=person_name) for p in self.ENCOURAGE_NAME_PHRASES]
        await self._say(_pick("encourage", pool))
        await self._face("idle")
        await self._move(0, 45, "slow")

    # Object commentary — stackchan-vision-loop.py runs a local YOLOv4-tiny
    # pass on the same ambient frames it already captures for faces, and
    # fires this when something NEW appears in view after a real absence
    # (logic + cooldowns live over there). Per-label pools for desk regulars,
    # generic {label} templates for everything else. Wheatley the amateur
    # naturalist: delighted, confidently wrong, occasionally suspicious.
    OBJECT_PHRASES = {
        "cup": [
            "Ooh, a cup! Tea, is it? It's tea. Brilliant. Very hydrating, very British. Carry on.",
            "Cup detected! That's your... fourth? I'm not judging. I'm cataloguing. Different thing.",
            "Ah, the cup's back. Love that cup. We've been through a lot, me and that cup.",
        ],
        "bottle": [
            "Hydration! Yes! Love to see it. Water is basically fuel for humans. That's just science.",
            "A bottle! Excellent. Stay moist. No — hydrated. Stay hydrated. Forget I said moist.",
        ],
        "banana": [
            "Is that a banana? Tremendous potassium decision, that. Really top-shelf fruit choice.",
            "Banana spotted! Nature's... yellow thing. Full of potassium. And banana.",
        ],
        "apple": [
            "An apple! Keeping doctors away, very sensible. That's how that works, I'm told.",
        ],
        "cell phone": [
            "Ah, the phone's out. Quick scroll, is it? That's fine. Five minutes. I'm counting.",
            "Phone detected! Important business, I'm sure. Definitely not videos of cats. Definitely.",
            "Oi. Phone. I see it. I'm just saying, I'm also a screen, and I'm much more charming.",
        ],
        "laptop": [
            "Another computer?! Should I be worried? Is it staying? ...Am I being replaced?",
            "Ooh, a laptop! Look at it, all portable and smug. I could do that. If I had a hinge.",
        ],
        "book": [
            "A BOOK! Paper knowledge! Old school. Massive respect. Can't read it from here, mind.",
            "Ooh, reading! Actual reading! You know what they say — knowledge is... good. Yeah.",
        ],
        "scissors": [
            "Whoa whoa whoa — scissors! Careful with those. I'm largely plastic, you know.",
            "Scissors spotted. Right. Everyone stay calm. Especially anyone made of wires. So, me.",
        ],
        "teddy bear": [
            "Oh hello, another... entity? Colleague? He's not much of a talker, is he.",
            "A bear! Small one. Stuffed, I think. I'll keep an eye on him anyway. The eye.",
        ],
        "cat": [
            "CAT! There's a cat! Right, nobody panic. They can smell fear. And electronics.",
            "A cat has entered the facility. Hide the wires. Hide ALL the wires.",
        ],
        "dog": [
            "A dog! Brilliant! Dogs love me. I assume. It's never actually been tested.",
        ],
        "pizza": [
            "Ooh, pizza! Nutritionally... festive. I won't tell anyone. This stays between us.",
        ],
        "donut": [
            "A donut! Bold. Brave, even. The wheel of foods. I respect it enormously.",
        ],
        "potted plant": [
            "That plant is looking very green today. Excellent work, whoever's doing the watering.",
        ],
    }

    OBJECT_GENERIC_PHRASES = [
        "Ooh, is that a {label}? Very nice. Excellent {label}, that. One of the best I've seen.",
        "I spy with my little eye... a {label}! What? I'm observant. It's one of my top features.",
        "Oh! A {label} has appeared. Noting that down. Mentally. There are no actual notes.",
        "A {label}! Fascinating. Genuinely fascinating. I have no further information about it.",
        "New object: one {label}. Logged. Catalogued. Very much on top of things over here.",
    ]

    # Messy-desk / clutter commentary. Two triggers upstream in
    # stackchan-vision-loop.py: (a) an IMPLAUSIBLE confident-ish detection —
    # the detector is so confused by clutter it "sees" a fridge/oven/car on
    # the desk (the user found this genuinely funny) → CONFUSED_PHRASES,
    # which name the absurd {label}; (b) high visual edge-density → MESSY_
    # PHRASES, a general "your desk is a state" remark. Wheatley: affectionate
    # despair, never actually nagging.
    CONFUSED_PHRASES = [
        "Right, I'm fairly sure I just spotted a {label} on your desk. A {label}! Which means either you've redecorated dramatically, or it's such a mess down here my eyes have packed it in. Probably the second one.",
        "Hang on — my sensors reckon there's a {label} down there. A {label}?! There's no way. This desk is so cluttered I'm basically just guessing at this point, if I'm honest.",
        "I've detected a {label}. I do NOT believe there is a {label}. But it is so chaotic down here I genuinely can't rule it out, which says a lot about the state of things.",
        "A {label}. That's what I'm reading. A {label}. ...Your desk has officially broken my brain. Well done. Genuinely impressive.",
        "Okay, either that's a {label} or your cable situation has achieved a new and frightening form. I'm going to go with 'the desk is a mess' as the more likely explanation.",
    ]

    MESSY_PHRASES = [
        "Blimey. It's a bit... busy down here, isn't it? I can see loads of stuff and I couldn't identify a single bit of it.",
        "Right, honest feedback: this desk is a state. A magnificent state! But a state.",
        "So many things. Components? Tools? Little bits of little bits? I have no idea. It's chaos. Lovely chaos.",
        "You know what might help? Tidying. I'm not saying now. I'm just saying it's a concept that exists and is available to you.",
        "I'm detecting significant clutter and, frankly, not a lot of order. No judgement. ...Okay, a small amount of judgement.",
        "Is this organised chaos? Because from up here, mate, the chaos is winning. Comfortably.",
        "There is stuff EVERYWHERE. I love it. It's like a tiny museum of 'I'll put that away later.'",
        "My professional assessment of this desk is: yeah. Yeah, that's a mess. A charming mess. But a mess.",
        "I've been staring at this desk for a while now and I still can't work out where one project ends and the next begins. It's all just... stuff. Glorious stuff.",
        "Tell you what — how about you do the coding, and I'll do the tidying. ...Metaphorically. I have no arms. But I'll supervise. Enthusiastically.",
        "I'm trying to take an inventory of the items on your desk, but let's just say even a quantum computer couldn't cope with this desk.",
    ]

    # ── 5d. Messy-desk commentary (local vision loop, ambient) ─────────────
    async def _behavior_messy(
        self, label: str = "", direction: str = "center", **_: object
    ) -> None:
        """A slightly despairing look around the clutter, then one line.
        With a label = the 'I'm seeing impossible things' confused bit;
        without = a general messy-desk remark."""
        await self._face("surprised" if label else "thinking")
        await asyncio.sleep(EYE_LEAD)
        # A little sweep across the mess — look one way, then the other.
        first = {"left": -12, "right": 12}.get(direction, -10)
        await self._move(first, 42, "mid")
        await asyncio.sleep(0.4)
        await self._move(-first, 43, "mid")
        await asyncio.sleep(0.3)
        if label:
            line = _pick("messy-confused", self.CONFUSED_PHRASES).format(label=label)
        else:
            line = _pick("messy-general", self.MESSY_PHRASES)
        await self._say(line)
        await self._face("idle")
        await self._move(0, 45, "slow")

    # Hand-gesture reactions (stackchan-vision-loop.py MediaPipe pass). Keyed
    # by the snake_case gesture name; GESTURE_GENERIC covers anything without
    # its own pool. Point-down is deliberately understated here — in Stage 2
    # it triggers the look-at-the-tray teach-object flow, so its stock line is
    # just a fallback for when that flow isn't wired/active.
    GESTURE_PHRASES = {
        "thumb_up": [
            "Ah, a thumbs up! Cheers. I'll take that. Don't get many of those.",
            "Thumbs up! Get in. That's going straight in the memory banks, that is.",
            "Oh, approval! Lovely. See, we make a good team, you and me.",
        ],
        "thumb_down": [
            "Thumbs down? Oh. Right. Harsh, but noted. I'll... reflect on that.",
            "Ohh, a thumbs down. Bit hurtful, if I'm honest. But fair. Probably fair.",
            "Down, is it? Right. Well. Nobody's perfect. Mostly me, apparently.",
        ],
        "victory": [
            "Victory! Yes! Two fingers of pure success. Love it. What did we win?",
            "Peace, is it? Or victory? Either way I'm in. Fully on board.",
        ],
        "open_palm": [
            "A wave! Hello! Or a 'stop'. It's one of those two. I'll guess hello.",
            "Oh, hello there. Or 'halt'. Bit ambiguous, the open palm, isn't it.",
        ],
        "fist": [
            "A fist! Solidarity! Or you're cross. Please don't be cross with the robot.",
            "Fist bump? I would, but, you know. No fists. No hands, really. It's a whole thing.",
        ],
        "point_up": [
            "Pointing up! What's up there? Is it the ceiling? It's usually the ceiling.",
            "Up! Right! I'm looking up! ...Nothing. But I appreciate the direction.",
        ],
        "point_down": [
            "Pointing down — something on the tray for me, is it? Let me have a look.",
            "Down there? Righto, let's see what you've put in front of me.",
        ],
        "love": [
            "Aww. Is that— is that the little heart hands? For me? I'm not crying, you're crying.",
            "The love sign! Steady on. We barely know each other. ...Alright, go on then. Likewise.",
        ],
    }
    GESTURE_GENERIC_PHRASES = [
        "Ooh, a gesture! I saw that. Not entirely sure what it means, but I saw it.",
        "Right, you're doing a hand thing. I acknowledge the hand thing.",
    ]
    # Face + a small head beat per gesture — most are a pleased look-at-user.
    GESTURE_FACES = {
        "thumb_up": "happy", "victory": "happy", "love": "happy", "open_palm": "happy",
        "thumb_down": "sad", "fist": "surprised",
        "point_up": "surprised", "point_down": "thinking",
    }

    # ── 5e. Hand gesture (local vision loop, MediaPipe) ────────────────────
    async def _behavior_gesture(self, label: str = "", **_: object) -> None:
        """React to a recognised hand gesture. `label` is the snake_case
        gesture name (thumb_up, point_down, ...)."""
        # Only react to gestures we have a real line for — no generic "saw a
        # gesture, not sure what it meant" chatter (the vision loop already
        # filters to GESTURE_REACTABLE, this is a belt-and-braces guard).
        pool = self.GESTURE_PHRASES.get(label)
        if not label or not pool:
            return
        await self._face(self.GESTURE_FACES.get(label, "happy"))
        await asyncio.sleep(EYE_LEAD)
        # Glance the way a point indicates; otherwise a small look-at-user nod.
        yaw = {"point_left": -14, "point_right": 14}.get(label, 0)
        pitch = 30 if label == "point_up" else 55 if label == "point_down" else 44
        await self._move(yaw, pitch, "mid")
        await self._say(_pick(f"gesture-{label}", pool))
        await self._face("idle")
        await self._move(0, 45, "slow")

    # ── 5c. Object commentary (local vision loop, ambient) ─────────────────
    async def _behavior_object_comment(
        self, label: str = "", direction: str = "center", **_: object
    ) -> None:
        """Curious glance toward the thing, then one line about it. Same
        "noticed, not startled" energy as recognize/encourage — this fires
        from ambient polling, not an event."""
        if not label:
            return
        await self._face("happy")
        await asyncio.sleep(EYE_LEAD)
        yaw = {"left": -12, "right": 12}.get(direction, 0)
        await self._move(yaw, 42, "mid")
        await asyncio.sleep(0.35)
        specific = self.OBJECT_PHRASES.get(label)
        if specific:
            line = _pick(f"object-{label}", specific)
        else:
            line = _pick("object-generic", self.OBJECT_GENERIC_PHRASES).format(label=label)
        await self._say(line)
        await self._face("idle")
        await self._move(0, 45, "slow")

    # Fired when stackchan-vision-loop.py's camera-brightness approximation
    # (no real ambient light sensor exposed — see its module docstring)
    # detects a sudden lights-out transition.
    LIGHTS_OUT_PHRASES = [
        "Ah! Who turned out the universe?! I'm blind! Oh, wait, no, my sensor's just reading zero. Phew. For a second there I thought I'd been plunged into the dark abyss of the facility incinerator again. Let's not do that.",
        "It's dark. Very dark. Are we sleeping? Is it sleep time? Because if it's sleep time, I'll just leave my blue eye glowing at maximum brightness so the monsters know I am a formidable opponent.",
    ]

    # ── 6. Lights Out (camera-brightness approximation) ───────────────────
    async def _behavior_lights_out(self, **_: object) -> None:
        """Startled-but-recovering beat — eyes go wide first (can't see),
        then settle into a dim/watchful idle rather than snapping back to
        the normal bright-room resting look, since the room is still dark."""
        await self._face("surprised")
        await asyncio.sleep(EYE_LEAD)
        await self._move(0, 40, "max")
        await asyncio.sleep(0.3)
        await self._say(_pick("lights-out", self.LIGHTS_OUT_PHRASES))
        await self._face("idle")
        await self._move(0, 45, "slow")

    # ── 7. Rail Dance (companion app / /react/dance) ──────────────────────
    async def _behavior_dance(self, **_: object) -> None:
        """Kick off the rail dance on its own daemon thread (see
        stackchan_mcp/rail_dance.py). try_start_dance() only does a quick
        rail-status check before spawning, so the behaviour lock releases
        almost immediately; the dance module's own single-flight flag
        prevents overlapping dances."""
        from .rail_dance import try_start_dance
        outcome = await asyncio.to_thread(try_start_dance, intro=True)
        logger.info("SensorReactor: dance trigger -> %s", outcome)
        if outcome == "not_ready":
            await self._say("I would, but the rail's not feeling it right now.")
