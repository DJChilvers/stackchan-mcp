"""Robot handlers: servo and LED (stub implementation with in-memory state)."""

from __future__ import annotations

import logging
from typing import Any

from ..tools import SetHeadAnglesParams

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory device state
# ---------------------------------------------------------------------------

_head_angles: dict[str, int] = {"yaw": 0, "pitch": 0}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def get_head_angles(_args: dict[str, Any] | None = None) -> dict[str, int]:
    """Return current head angles."""
    logger.info("get_head_angles -> %s", _head_angles)
    return dict(_head_angles)


def set_head_angles(args: dict[str, Any]) -> bool:
    """Set head angles (in-memory stub)."""
    params = SetHeadAnglesParams(**args)
    _head_angles["yaw"] = params.yaw
    _head_angles["pitch"] = params.pitch
    logger.info(
        "set_head_angles yaw=%d pitch=%d speed_dps=%d",
        params.yaw,
        params.pitch,
        params.speed_dps,
    )
    return True
