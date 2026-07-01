"""LED handlers: RGB base LEDs (stub implementation with in-memory state).

Mirrors the real firmware's self.led.* tools (firmware/main/boards/stackchan/
stackchan.cc) — there is no single "set_led_color" tool on real hardware.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..tools import LedSetAllParams, LedSetColorParams, LedSetManyParams

logger = logging.getLogger(__name__)

LED_COUNT = 12

# ---------------------------------------------------------------------------
# In-memory device state
# ---------------------------------------------------------------------------

_leds: list[dict[str, int]] = [{"r": 0, "g": 0, "b": 0} for _ in range(LED_COUNT)]


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def set_color(args: dict[str, Any]) -> bool:
    """Set a single indexed LED (in-memory stub)."""
    params = LedSetColorParams(**args)
    _leds[params.index] = {"r": params.r, "g": params.g, "b": params.b}
    logger.info(
        "led.set_color index=%d rgb=(%d,%d,%d)", params.index, params.r, params.g, params.b
    )
    return True


def set_all(args: dict[str, Any]) -> bool:
    """Set all LEDs to the same color (in-memory stub)."""
    params = LedSetAllParams(**args)
    for i in range(LED_COUNT):
        _leds[i] = {"r": params.r, "g": params.g, "b": params.b}
    logger.info("led.set_all rgb=(%d,%d,%d)", params.r, params.g, params.b)
    return True


def set_many(args: dict[str, Any]) -> bool:
    """Set multiple LEDs from a JSON-encoded array of [r,g,b] triples (in-memory stub)."""
    params = LedSetManyParams(**args)
    triples = json.loads(params.colors)
    for i, (r, g, b) in enumerate(triples[:LED_COUNT]):
        _leds[i] = {"r": r, "g": g, "b": b}
    logger.info("led.set_many written=%d", min(len(triples), LED_COUNT))
    return True


def clear(_args: dict[str, Any] | None = None) -> bool:
    """Turn off all LEDs (in-memory stub)."""
    for i in range(LED_COUNT):
        _leds[i] = {"r": 0, "g": 0, "b": 0}
    logger.info("led.clear")
    return True
