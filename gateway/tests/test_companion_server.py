"""Tests for the companion API (Android control app) server.

Exercised through a real aiohttp TestServer/TestClient so the auth middleware
runs, with a fake gateway that records device-tool dispatches. No real device,
TTS stack or opuslib required for the Phase 0-2 surface covered here.
"""

from __future__ import annotations

import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from stackchan_mcp.companion_server import (
    SAY_CATEGORIES,
    create_companion_app,
)


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------
class FakeEsp32:
    def __init__(self, *, connected: bool = True, device_status: dict | None = None):
        self._connected = connected
        self._device_status = device_status or {
            "battery": {"level": 84, "charging": False},
            "network": {"ssid": "vodafoneB01EF7", "rssi": -52},
            "audio": {"volume": 60},
            "screen": {"brightness": 75},
        }
        self.calls: list[tuple[str, dict]] = []
        self.error_for: dict[str, dict] = {}  # tool name -> error dict

    def get_status(self) -> dict:
        return {"connected": self._connected, "device_id": "wheatley-1"}

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        if name in self.error_for:
            return None, self.error_for[name]
        if name == "self.get_device_status":
            payload = json.dumps(self._device_status)
            return {"content": [{"type": "text", "text": payload}]}, None
        return {"content": [{"type": "text", "text": "{}"}]}, None


class FakeReactor:
    def __init__(self, *, accept: bool = True):
        self.accept = accept
        self.triggered: list[tuple[str, dict]] = []

    async def trigger(self, behavior: str, **kwargs) -> bool:
        self.triggered.append((behavior, kwargs))
        return self.accept


class FakeGateway:
    def __init__(self, **kw):
        self.esp32 = FakeEsp32(**kw)
        self.sensor_reactor = FakeReactor()


async def _client(gateway=None, token: str = "") -> TestClient:
    gateway = gateway if gateway is not None else FakeGateway()
    app = create_companion_app(gateway=gateway, token=token)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


# ---------------------------------------------------------------------------
# health + auth
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_health_is_unauthenticated_and_reports_connection():
    client = await _client(token="secret")
    try:
        resp = await client.get("/api/health")  # no Authorization header
        assert resp.status == 200
        body = await resp.json()
        assert body["ok"] is True
        assert body["device_connected"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_status_rejects_missing_and_wrong_bearer():
    client = await _client(token="secret")
    try:
        assert (await client.get("/api/status")).status == 401
        resp = await client.get(
            "/api/status", headers={"Authorization": "Bearer nope"}
        )
        assert resp.status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_status_accepts_correct_bearer_and_parses_battery():
    gw = FakeGateway()
    client = await _client(gateway=gw, token="secret")
    try:
        resp = await client.get(
            "/api/status", headers={"Authorization": "Bearer secret"}
        )
        assert resp.status == 200
        body = await resp.json()
        assert body["connected"] is True
        assert body["battery"] == {"level": 84, "charging": False}
        assert body["uptime_s"] >= 0
        assert ("self.get_device_status", {}) in gw.esp32.calls
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_blank_token_disables_auth():
    client = await _client(token="")
    try:
        # No Authorization header, but token unset → allowed.
        assert (await client.get("/api/status")).status == 200
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# control dispatch
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_head_clamps_and_dispatches():
    gw = FakeGateway()
    client = await _client(gateway=gw)
    try:
        resp = await client.post("/api/head", json={"yaw": 200, "pitch": 3, "speed": 120})
        assert resp.status == 200
        name, args = gw.esp32.calls[-1]
        assert name == "self.robot.set_head_angles"
        assert args["yaw"] == 80  # clamped from 200
        assert args["pitch"] == 10  # clamped up from 3
        assert args["speed_dps"] == 120
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_head_requires_an_axis():
    client = await _client()
    try:
        assert (await client.post("/api/head", json={})).status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_avatar_rejects_unknown_face_and_accepts_valid():
    gw = FakeGateway()
    client = await _client(gateway=gw)
    try:
        assert (await client.post("/api/avatar", json={"face": "neutral"})).status == 400
        resp = await client.post("/api/avatar", json={"face": "happy"})
        assert resp.status == 200
        assert gw.esp32.calls[-1] == ("self.display.set_avatar", {"face": "happy"})
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_leds_set_and_clear():
    gw = FakeGateway()
    client = await _client(gateway=gw)
    try:
        await client.post("/api/leds", json={"r": 10, "g": 999, "b": -5})
        assert gw.esp32.calls[-1] == ("self.led.set_all", {"r": 10, "g": 255, "b": 0})
        await client.post("/api/leds", json={"clear": True})
        assert gw.esp32.calls[-1] == ("self.led.clear", {})
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_torque_dispatch():
    gw = FakeGateway()
    client = await _client(gateway=gw)
    try:
        await client.post("/api/torque", json={"yaw": False, "pitch": True})
        assert gw.esp32.calls[-1] == (
            "self.robot.set_servo_torque",
            {"yaw_enabled": False, "pitch_enabled": True},
        )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_device_tool_error_surfaces_as_502():
    gw = FakeGateway()
    gw.esp32.error_for["self.display.set_avatar"] = {"message": "device offline"}
    client = await _client(gateway=gw)
    try:
        resp = await client.post("/api/avatar", json={"face": "idle"})
        assert resp.status == 502
        assert "device offline" in (await resp.json())["error"]
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# motor stub
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_motor_returns_501_until_firmware_exists():
    client = await _client()
    try:
        resp = await client.post("/api/motor", json={"direction": "left", "speed": 50})
        assert resp.status == 501
        assert (await resp.json())["ok"] is False
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# sayings + reactions
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_say_categories_lists_all_pools():
    client = await _client()
    try:
        body = await (await client.get("/api/say/categories")).json()
        keys = {c["key"] for c in body["categories"]}
        assert keys == set(SAY_CATEGORIES)
        assert all(c["label"] for c in body["categories"])
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_say_preset_rejects_unknown_category():
    client = await _client()
    try:
        resp = await client.post("/api/say/preset", json={"pool": "does-not-exist"})
        assert resp.status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_say_preset_speaks_a_line_from_the_pool(monkeypatch):
    spoken: list[str] = []

    async def fake_synth(payload, gateway=None):
        spoken.append(payload["text"])

    monkeypatch.setattr("stackchan_mcp.tts.synthesize_and_send", fake_synth)
    client = await _client()
    try:
        resp = await client.post("/api/say/preset", json={"category": "panic"})
        assert resp.status == 200
        body = await resp.json()
        assert body["category"] == "panic"
        assert body["spoke"] in SAY_CATEGORIES["panic"]["lines"]
        assert spoken == [body["spoke"]]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_react_validates_and_reports_busy():
    gw = FakeGateway()
    client = await _client(gateway=gw)
    try:
        assert (await client.post("/api/react/not-a-behavior")).status == 400
        resp = await client.post("/api/react/hacker")
        assert resp.status == 200
        assert gw.sensor_reactor.triggered[-1][0] == "hacker"

        gw.sensor_reactor.accept = False
        assert (await client.post("/api/react/panic")).status == 409
    finally:
        await client.close()
