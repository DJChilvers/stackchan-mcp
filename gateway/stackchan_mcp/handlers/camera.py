"""Camera handler: take_photo via ESP32 relay."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def take_photo(esp32_call, question: str) -> dict[str, Any]:
    """Take a photo via ESP32 camera.

    Args:
        esp32_call: async callable (name, arguments) -> (result, error)
        question: question to ask about the captured photo (required by
            the real firmware's self.camera.take_photo tool)

    Returns:
        MCP result content.
    """
    result, error = await esp32_call(
        "self.camera.take_photo", {"question": question}
    )
    if error:
        raise RuntimeError(f"take_photo failed: {error.get('message', str(error))}")
    return result
