"""Trailing-silence endpointing for device-driven (wake-word) listens.

Problem: after a wake word the firmware enters listening with a fixed
30 s timeout (``StackChanBoard: Listening entered ... timeout in
30000 ms``) and streams Opus frames to the gateway the whole time. A
user who speaks a 2-4 s command then waits ~28 s for the turn to
continue. This module watches the inbound frames during a
device-driven capture and, once the user has clearly finished
speaking, asks the device to stop early by sending the same
``{"type":"listen","state":"stop"}`` wire message the MCP ``listen()``
tool already uses (``ESP32Connection.send_listen_state``). The
firmware's ``Application::OnIncomingJson`` dispatches that to
``StopListening()`` -> ``HandleStopListeningEvent()``, which calls
``protocol_->SendStopListening()`` — i.e. the device sends its own
``listen.stop`` back up, and the gateway's existing device-driven stop
branch (``esp32_client.ESP32Manager._handler``) forwards the buffered
audio to the voice bridge exactly as the 30 s-timeout path does.

Safety stance (this is the load-bearing design rule): the VAD NEVER
touches the recording slot and NEVER blocks the capture path. Its only
side effect is the best-effort ``listen.stop`` send. If it is disabled,
misconfigured, crashes, or the send fails, the device's own 30 s
timeout ends the capture and everything behaves exactly as before.

State machine (per capture):

1. Decode each inbound Opus frame (16 kHz mono, 60 ms — the device
   encoder parameters from :mod:`stackchan_mcp.stt.audio_utils`) to PCM
   and measure RMS energy.
2. Speech starts once :data:`SPEECH_START_FRAMES` consecutive frames
   exceed the energy threshold.
3. After speech has started, a continuous run of sub-threshold frames
   lasting ``SILENCE_S`` (and no earlier than ``MIN_S`` after the
   capture began) triggers the stop.
4. A wall-clock watchdog triggers the stop at ``MAX_S`` regardless —
   including when the user said nothing at all (still much snappier
   than the firmware's 30 s).

Environment variables:

* ``STACKCHAN_WAKE_VAD``            — kill switch; ``0``/``false``/``no``/``off``
  disables endpointing entirely (pure device timeout). Default: enabled.
* ``STACKCHAN_WAKE_VAD_ENERGY``     — RMS threshold on int16 PCM
  (default 500). The max-cap log line reports ``peak_rms`` to help tune.
* ``STACKCHAN_WAKE_VAD_SILENCE_S``  — trailing silence to end the turn
  (default 1.5).
* ``STACKCHAN_WAKE_VAD_MIN_S``      — minimum capture time before any
  VAD-driven stop (default 2.5).
* ``STACKCHAN_WAKE_VAD_MAX_S``      — hard cap on the listen window
  (default 10.0).
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .esp32_client import ESP32Connection

logger = logging.getLogger(__name__)


#: Device Opus parameters — mirrors :mod:`stackchan_mcp.stt.audio_utils`
#: (``firmware/main/protocols/websocket_protocol.cc::GetHelloMessage``:
#: 16 kHz mono, 60 ms frames).
SAMPLE_RATE = 16000
CHANNELS = 1
FRAME_MS = 60
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_MS // 1000

#: Consecutive voiced frames (~180 ms) required before we consider
#: speech to have started. Filters out single-frame pops/clicks and the
#: listening-entry popup sound tail.
SPEECH_START_FRAMES = 3

DEFAULT_ENERGY = 500.0
DEFAULT_SILENCE_S = 1.5
DEFAULT_MIN_S = 2.5
DEFAULT_MAX_S = 10.0

_FALSEY = {"0", "false", "no", "off"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "")
    if not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "VAD: ignoring invalid %s=%r (using default %s)", name, raw, default
        )
        return default


def vad_enabled() -> bool:
    """``True`` unless the ``STACKCHAN_WAKE_VAD`` kill switch is off."""
    return os.getenv("STACKCHAN_WAKE_VAD", "1").strip().lower() not in _FALSEY


def arm_wake_vad(
    connection: "ESP32Connection", session_id: str
) -> "WakeVad | None":
    """Create a :class:`WakeVad` for a device-driven capture, or ``None``.

    Never raises: any failure (kill switch, bad env, opuslib missing in
    a way that even the watchdog cannot survive) logs and returns
    ``None`` so the caller's capture path is untouched and the
    firmware's own 30 s timeout remains the stop boundary.
    """
    try:
        if not vad_enabled():
            logger.debug(
                "VAD: disabled via STACKCHAN_WAKE_VAD (session=%s)", session_id
            )
            return None
        return WakeVad(connection, session_id)
    except Exception:
        logger.exception(
            "VAD: arming failed (session=%s); capture continues on the "
            "device's own listen timeout",
            session_id,
        )
        return None


class WakeVad:
    """Per-capture trailing-silence endpointer.

    Constructed when the gateway opens a device-driven recording slot
    (wake word / button / LCD touch) and closed when the capture ends
    (device stop message or disconnect). All methods are invoked from
    the single asyncio event-loop thread, so plain attributes are safe.
    """

    def __init__(self, connection: "ESP32Connection", session_id: str) -> None:
        self._connection = connection
        self.session_id = session_id

        self._energy = _env_float("STACKCHAN_WAKE_VAD_ENERGY", DEFAULT_ENERGY)
        self._silence_s = _env_float(
            "STACKCHAN_WAKE_VAD_SILENCE_S", DEFAULT_SILENCE_S
        )
        self._min_s = _env_float("STACKCHAN_WAKE_VAD_MIN_S", DEFAULT_MIN_S)
        self._max_s = _env_float("STACKCHAN_WAKE_VAD_MAX_S", DEFAULT_MAX_S)

        self._t0 = time.monotonic()
        self._speech_started = False
        self._speech_start_t = 0.0
        self._voiced_run = 0
        self._silence_run = 0
        self._frames = 0
        self._peak_rms = 0.0
        self._stop_scheduled = False
        self._closed = False
        self._stop_task: asyncio.Task[None] | None = None

        # Energy endpointing needs the Opus decoder; the wall-clock max
        # cap below does not. If opuslib is unavailable we keep the cap
        # (still a big win over 30 s) and skip the energy tracking.
        try:
            import opuslib  # type: ignore[import-not-found]

            self._decoder = opuslib.Decoder(SAMPLE_RATE, CHANNELS)
        except Exception as exc:
            self._decoder = None
            logger.warning(
                "VAD: opuslib decoder unavailable (%s); trailing-silence "
                "endpointing disabled, max-cap %.1fs still active",
                exc,
                self._max_s,
            )

        self._watchdog_task: asyncio.Task[None] | None = asyncio.get_running_loop().create_task(
            self._watchdog()
        )
        logger.info(
            "VAD: armed session=%s (energy>=%.0f silence=%.1fs min=%.1fs "
            "max=%.1fs decoder=%s)",
            session_id,
            self._energy,
            self._silence_s,
            self._min_s,
            self._max_s,
            "on" if self._decoder is not None else "off",
        )

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        """Stop watching. Idempotent; never raises.

        Called when the capture ends for any reason (device stop
        message, disconnect, replacement by a newer capture). An
        already-scheduled stop send is left to finish — a redundant
        ``listen.stop`` to an idle device is a firmware no-op
        (``HandleStopListeningEvent`` only acts in the listening state).
        """
        self._closed = True
        task = self._watchdog_task
        self._watchdog_task = None
        if task is not None:
            try:
                task.cancel()
            except Exception:  # pragma: no cover - cancel() is non-raising
                pass

    # -- frame path -------------------------------------------------------

    def feed(self, frame: bytes, session_id: str) -> None:
        """Consume one inbound binary Opus frame.

        Called from the WebSocket read loop for every binary frame while
        this VAD is armed. Frames from other sessions (an old connection
        draining during a swap) are ignored, mirroring the discard logic
        in :func:`stackchan_mcp.audio_stream.handle_audio_frame`.
        """
        if (
            self._closed
            or self._stop_scheduled
            or session_id != self.session_id
            or self._decoder is None
            or not frame
        ):
            return

        try:
            pcm = self._decoder.decode(bytes(frame), SAMPLES_PER_FRAME)
            samples = memoryview(pcm).cast("h")
            n = len(samples)
            if n == 0:
                return
            acc = 0
            for s in samples:
                acc += s * s
            rms = math.sqrt(acc / n)
        except Exception as exc:
            # A mangled frame must not kill endpointing (nor the
            # capture — we never touch the buffer). Debug-level like the
            # STT decoder's skip path.
            logger.debug("VAD: frame decode failed (%s); skipping", exc)
            return

        self._frames += 1
        if rms > self._peak_rms:
            self._peak_rms = rms

        now = time.monotonic()
        if rms >= self._energy:
            self._voiced_run += 1
            self._silence_run = 0
            if not self._speech_started and self._voiced_run >= SPEECH_START_FRAMES:
                self._speech_started = True
                # Backdate to the first voiced frame of the run.
                self._speech_start_t = now - (self._voiced_run * FRAME_MS / 1000.0)
                logger.debug(
                    "VAD: speech started at %.1fs (rms=%.0f session=%s)",
                    self._speech_start_t - self._t0,
                    rms,
                    self.session_id,
                )
        else:
            self._voiced_run = 0
            if self._speech_started:
                self._silence_run += 1

        if self._frames % 16 == 0:  # ~once a second
            logger.debug(
                "VAD: t=%.1fs rms=%.0f speech=%s silence_run=%.1fs",
                now - self._t0,
                rms,
                self._speech_started,
                self._silence_run * FRAME_MS / 1000.0,
            )

        silence_s = self._silence_run * FRAME_MS / 1000.0
        elapsed = now - self._t0
        if (
            self._speech_started
            and silence_s >= self._silence_s
            and elapsed >= self._min_s
        ):
            speech_s = max(0.0, (now - self._speech_start_t) - silence_s)
            self._endpoint(
                f"speech {speech_s:.1f}s + silence {silence_s:.1f}s"
            )

    # -- stop path --------------------------------------------------------

    def _endpoint(self, reason: str) -> None:
        """Schedule the one-and-only stop-listen send for this capture."""
        if self._closed or self._stop_scheduled:
            return
        self._stop_scheduled = True
        logger.info(
            "VAD: %s -> stop-listen (session=%s)", reason, self.session_id
        )
        self._stop_task = asyncio.get_running_loop().create_task(self._send_stop())

    async def _send_stop(self) -> None:
        """Best-effort ``listen.stop`` to the device.

        On success the firmware stops listening and sends its own
        ``listen.stop`` back up, which drives the gateway's normal
        capture-close + voice-bridge push. On failure we only log: the
        device's own listen timeout still ends the capture.
        """
        try:
            await self._connection.send_listen_state("stop")
        except Exception as exc:
            logger.warning(
                "VAD: stop-listen send failed (%s); the device's own "
                "listen timeout will end the capture",
                exc,
            )

    async def _watchdog(self) -> None:
        """Hard cap: end the window at MAX_S even if energy never rose."""
        try:
            await asyncio.sleep(max(self._max_s, self._min_s))
            if self._closed or self._stop_scheduled:
                return
            if self._speech_started:
                reason = f"max window {self._max_s:.1f}s reached"
            else:
                reason = (
                    f"no speech within {self._max_s:.1f}s "
                    f"(peak_rms={self._peak_rms:.0f} threshold={self._energy:.0f})"
                )
            self._endpoint(reason)
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "VAD: watchdog failed; capture continues on the device's "
                "own listen timeout"
            )
