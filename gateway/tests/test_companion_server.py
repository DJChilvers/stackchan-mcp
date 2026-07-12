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
        self.photo_path: str | None = None  # image_path take_photo should return

    def get_status(self) -> dict:
        return {"connected": self._connected, "device_id": "wheatley-1"}

    async def call_tool(self, name: str, arguments: dict):
        self.calls.append((name, arguments))
        if name in self.error_for:
            return None, self.error_for[name]
        if name == "self.get_device_status":
            payload = json.dumps(self._device_status)
            return {"content": [{"type": "text", "text": payload}]}, None
        if name == "self.camera.take_photo":
            payload = json.dumps({"image_path": self.photo_path, "size_bytes": 0})
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


# ---------------------------------------------------------------------------
# camera + vision (Phase 3)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_camera_meta_reads_vision_state(monkeypatch, tmp_path):
    import time as _time

    state = tmp_path / "vision-state.json"
    state.write_text(json.dumps(
        {"ts": _time.time(), "face_visible": True, "person": "known", "name": "Dominic"}
    ))
    monkeypatch.setattr("stackchan_mcp.companion_server.VISION_STATE_PATH", str(state))
    client = await _client()
    try:
        body = await (await client.get("/api/camera/meta")).json()
        assert body["face_visible"] is True
        assert body["name"] == "Dominic"
        assert body["stale"] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_camera_meta_marks_stale_state(monkeypatch, tmp_path):
    state = tmp_path / "vision-state.json"
    state.write_text(json.dumps({"ts": 1.0, "face_visible": True, "name": "Old"}))
    monkeypatch.setattr("stackchan_mcp.companion_server.VISION_STATE_PATH", str(state))
    client = await _client()
    try:
        body = await (await client.get("/api/camera/meta")).json()
        assert body["stale"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_camera_snapshot_streams_jpeg_with_recognition_headers(monkeypatch, tmp_path):
    import time as _time

    jpeg = tmp_path / "shot.jpg"
    jpeg.write_bytes(b"\xff\xd8\xff\xe0JPEGDATA")
    state = tmp_path / "vision-state.json"
    state.write_text(json.dumps(
        {"ts": _time.time(), "face_visible": True, "name": "Dominic"}
    ))
    monkeypatch.setattr("stackchan_mcp.companion_server.VISION_STATE_PATH", str(state))

    gw = FakeGateway()
    gw.esp32.photo_path = str(jpeg)
    client = await _client(gateway=gw)
    try:
        resp = await client.get("/api/camera/snapshot")
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "image/jpeg"
        assert resp.headers["X-Face-Visible"] == "true"
        assert resp.headers["X-Face-Name"] == "Dominic"
        assert (await resp.read()) == b"\xff\xd8\xff\xe0JPEGDATA"
        assert ("self.camera.take_photo", {"question": "companion live view"}) in gw.esp32.calls
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_camera_snapshot_503_when_device_offline():
    gw = FakeGateway(connected=False)
    client = await _client(gateway=gw)
    try:
        assert (await client.get("/api/camera/snapshot")).status == 503
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_vision_ask_503_without_api_key(monkeypatch):
    monkeypatch.delenv("STACKCHAN_VOICE_ANTHROPIC_API_KEY", raising=False)
    client = await _client()
    try:
        resp = await client.post("/api/vision/ask", json={"question": "what is this?"})
        assert resp.status == 503
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_vision_ask_captures_reasons_and_speaks(monkeypatch, tmp_path):
    monkeypatch.setenv("STACKCHAN_VOICE_ANTHROPIC_API_KEY", "sk-test")

    jpeg = tmp_path / "shot.jpg"
    jpeg.write_bytes(b"\xff\xd8\xff\xe0JPEGDATA")

    def fake_vision(api_key, image_b64, question):
        assert api_key == "sk-test"
        assert image_b64  # base64 of the jpeg
        return "Ah, looks like a mug. Probably. I'm about seventy percent on mug."

    spoken: list[str] = []

    async def fake_synth(payload, gateway=None):
        spoken.append(payload["text"])

    monkeypatch.setattr("stackchan_mcp.companion_server._claude_vision_sync", fake_vision)
    monkeypatch.setattr("stackchan_mcp.tts.synthesize_and_send", fake_synth)

    gw = FakeGateway()
    gw.esp32.photo_path = str(jpeg)
    client = await _client(gateway=gw)
    try:
        resp = await client.post("/api/vision/ask", json={"question": "what am I holding?"})
        assert resp.status == 200
        body = await resp.json()
        assert body["answer"].startswith("Ah, looks like a mug")
        assert body["spoke"] is True
        assert spoken == [body["answer"]]
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# faces roster (Phase 4)
# ---------------------------------------------------------------------------
def _seed_faces(tmp_path, monkeypatch, known=None, greetings=None):
    kf = tmp_path / "known_faces.json"
    kf.write_text(json.dumps(known if known is not None else {}))
    monkeypatch.setenv("STACKCHAN_VISION_KNOWN_FACES", str(kf))
    monkeypatch.setenv("STACKCHAN_VISION_REFERENCE_PHOTOS_DIR", str(tmp_path / "photos"))
    gp = tmp_path / "face_greetings.json"
    if greetings:
        gp.write_text(json.dumps(greetings))
    monkeypatch.setenv("STACKCHAN_FACE_GREETINGS", str(gp))
    return kf


@pytest.mark.asyncio
async def test_faces_list_reports_samples_and_greeting(tmp_path, monkeypatch):
    _seed_faces(
        tmp_path, monkeypatch,
        known={"Dominic": [[0.1, 0.2], [0.3, 0.4]], "Alex": [[0.5]]},
        greetings={"Dominic": "Hello you"},
    )
    client = await _client()
    try:
        body = await (await client.get("/api/faces")).json()
        by_name = {f["name"]: f for f in body["faces"]}
        assert by_name["Dominic"]["samples"] == 2
        assert by_name["Dominic"]["greeting"] == "Hello you"
        assert by_name["Alex"]["samples"] == 1
        assert by_name["Alex"]["greeting"] is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_face_rename_moves_embeddings_and_greeting(tmp_path, monkeypatch):
    kf = _seed_faces(
        tmp_path, monkeypatch,
        known={"Dominic": [[0.1]]}, greetings={"Dominic": "Hi {name}"},
    )
    client = await _client()
    try:
        resp = await client.post("/api/faces/Dominic/rename", json={"new_name": "Dom"})
        assert resp.status == 200
        data = json.loads(kf.read_text())
        assert "Dom" in data and "Dominic" not in data
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_face_rename_conflict_and_missing(tmp_path, monkeypatch):
    _seed_faces(tmp_path, monkeypatch, known={"Dominic": [[0.1]], "Alex": [[0.2]]})
    client = await _client()
    try:
        # target already exists -> 409
        assert (await client.post("/api/faces/Dominic/rename", json={"new_name": "Alex"})).status == 409
        # source missing -> 404
        assert (await client.post("/api/faces/Ghost/rename", json={"new_name": "X"})).status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_face_delete(tmp_path, monkeypatch):
    kf = _seed_faces(tmp_path, monkeypatch, known={"Dominic": [[0.1]]})
    client = await _client()
    try:
        assert (await client.delete("/api/faces/Dominic")).status == 200
        assert json.loads(kf.read_text()) == {}
        assert (await client.delete("/api/faces/Dominic")).status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_face_greeting_put_and_clear(tmp_path, monkeypatch):
    _seed_faces(tmp_path, monkeypatch, known={"Dominic": [[0.1]]})
    client = await _client()
    try:
        resp = await client.put("/api/faces/Dominic/greeting", json={"line": "Ah, {name}!"})
        assert resp.status == 200
        # greeting on an unknown face -> 404
        assert (await client.put("/api/faces/Ghost/greeting", json={"line": "hi"})).status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_face_photo_404_when_missing(tmp_path, monkeypatch):
    _seed_faces(tmp_path, monkeypatch, known={"Dominic": [[0.1]]})
    client = await _client()
    try:
        assert (await client.get("/api/faces/Dominic/photo")).status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_face_enroll_validates_name_and_connection(tmp_path, monkeypatch):
    _seed_faces(tmp_path, monkeypatch)
    # bad name is rejected before any subprocess/connection work
    client = await _client()
    try:
        assert (await client.post("/api/faces/enroll", json={"name": "bad/name"})).status == 400
    finally:
        await client.close()
    # valid name but device offline -> 503 (still no subprocess)
    client = await _client(gateway=FakeGateway(connected=False))
    try:
        assert (await client.post("/api/faces/enroll", json={"name": "Dominic"})).status == 503
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# visitor log (Phase 4)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_visitors_empty_and_populated(tmp_path, monkeypatch):
    monkeypatch.setenv("STACKCHAN_VISITOR_LOG", str(tmp_path / "visitors.jsonl"))
    monkeypatch.setenv("STACKCHAN_VISITOR_THUMBS", str(tmp_path / "thumbs"))
    from stackchan_mcp import visitor_log

    client = await _client()
    try:
        assert (await (await client.get("/api/visitors")).json())["visitors"] == []
        entry = visitor_log.append("Dominic", True, 0.7, b"\xff\xd8thumb")
        body = await (await client.get("/api/visitors")).json()
        assert body["visitors"][0]["name"] == "Dominic"
        # thumbnail is served
        assert (await client.get(f"/api/visitors/{entry['id']}/thumb")).status == 200
        assert (await client.get("/api/visitors/nope/thumb")).status == 404
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# orientation (upside-down mount)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_orientation_get_set_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr("stackchan_mcp.companion_server._SETTINGS_PATH", str(tmp_path / "s.json"))
    monkeypatch.delenv("STACKCHAN_UPSIDE_DOWN", raising=False)
    client = await _client()
    try:
        assert (await (await client.get("/api/orientation")).json())["upside_down"] is False
        resp = await client.post("/api/orientation", json={"upside_down": True})
        assert (await resp.json())["upside_down"] is True
        assert (await (await client.get("/api/orientation")).json())["upside_down"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_head_mirrors_axes_when_upside_down(monkeypatch, tmp_path):
    monkeypatch.setattr("stackchan_mcp.companion_server._SETTINGS_PATH", str(tmp_path / "s.json"))
    monkeypatch.delenv("STACKCHAN_UPSIDE_DOWN", raising=False)
    gw = FakeGateway()
    client = await _client(gateway=gw)
    try:
        await client.post("/api/orientation", json={"upside_down": True})
        await client.post("/api/head", json={"yaw": 40, "pitch": 20})
        name, args = gw.esp32.calls[-1]
        assert name == "self.robot.set_head_angles"
        assert args["yaw"] == -40        # mirrored
        assert args["pitch"] == 70        # 90 - 20
    finally:
        await client.close()
