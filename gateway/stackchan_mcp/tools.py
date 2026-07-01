"""MCP tool definitions for StackChan.

These definitions describe the ESP32 device's tool interface.
Used by the local stub router (mcp_router.py) for testing.
The stdio MCP server (stdio_server.py) defines its own tool list for MCP client.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Tool parameter schemas
# ---------------------------------------------------------------------------


class SetHeadAnglesParams(BaseModel):
    yaw: int = Field(default=0, ge=-90, le=90, description="Yaw angle in degrees (-90 to 90)")
    pitch: int = Field(default=0, description="Pitch angle in degrees (clamped in firmware)")
    speed_dps: int = Field(
        default=0, description="Angular speed in degrees per second (0 = firmware default duration)"
    )


class SetVolumeParams(BaseModel):
    volume: int = Field(ge=0, le=100, description="Volume level (0-100)")


class TakePhotoParams(BaseModel):
    question: str = Field(description="Question to ask about the captured photo")


class LedSetColorParams(BaseModel):
    index: int = Field(ge=0, le=11, description="LED index (0-11)")
    r: int = Field(ge=0, le=255, description="Red (0-255)")
    g: int = Field(ge=0, le=255, description="Green (0-255)")
    b: int = Field(ge=0, le=255, description="Blue (0-255)")


class LedSetAllParams(BaseModel):
    r: int = Field(ge=0, le=255, description="Red (0-255)")
    g: int = Field(ge=0, le=255, description="Green (0-255)")
    b: int = Field(ge=0, le=255, description="Blue (0-255)")


class LedSetManyParams(BaseModel):
    colors: str = Field(
        description="JSON-encoded array of up to 12 [r,g,b] triples starting at index 0"
    )


# ---------------------------------------------------------------------------
# Tool registry (ESP32 device tools — used by local stub router)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "self.robot.get_head_angles",
        "description": "Get current head servo angles (yaw, pitch).",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "self.robot.set_head_angles",
        "description": "Set head servo angles.",
        "inputSchema": SetHeadAnglesParams.model_json_schema(),
    },
    {
        "name": "self.led.set_color",
        "description": "Set a single RGB LED on the StackChan base (index 0-11).",
        "inputSchema": LedSetColorParams.model_json_schema(),
    },
    {
        "name": "self.led.set_all",
        "description": "Set all 12 RGB LEDs on the StackChan base to the same color.",
        "inputSchema": LedSetAllParams.model_json_schema(),
    },
    {
        "name": "self.led.set_many",
        "description": "Set multiple RGB LEDs in one shot from a JSON-encoded array of [r,g,b] triples.",
        "inputSchema": LedSetManyParams.model_json_schema(),
    },
    {
        "name": "self.led.clear",
        "description": "Turn off all 12 RGB LEDs on the StackChan base.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "self.audio_speaker.set_volume",
        "description": "Set speaker volume (0-100).",
        "inputSchema": SetVolumeParams.model_json_schema(),
    },
    {
        "name": "self.camera.take_photo",
        "description": "Take a photo with the device camera and ask a question about it.",
        "inputSchema": TakePhotoParams.model_json_schema(),
    },
    {
        "name": "self.get_device_status",
        "description": "Get device status (battery, connection, angles).",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]
