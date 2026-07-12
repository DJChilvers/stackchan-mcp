"""Tests for scan_zone vision (detect / rectify / occupancy) using synthetic
ArUco frames — no device needed."""

from __future__ import annotations

import cv2
import numpy as np

from stackchan_mcp import scan_zone


def _frame_with_markers(item: bool = False, size: int = 400, mk: int = 60) -> bytes:
    """White frame with the 4 corner markers; optionally a dark item in the middle."""
    img = np.full((size, size, 3), 255, np.uint8)
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    gen = getattr(cv2.aruco, "generateImageMarker", None) or cv2.aruco.drawMarker
    # ID0=TL, ID1=TR, ID2=BR, ID3=BL
    spots = {0: (10, 10), 1: (size - mk - 10, 10),
             2: (size - mk - 10, size - mk - 10), 3: (10, size - mk - 10)}
    for mid, (x, y) in spots.items():
        img[y:y + mk, x:x + mk] = cv2.cvtColor(gen(d, mid, mk), cv2.COLOR_GRAY2BGR)
    if item:
        cv2.rectangle(img, (size // 2 - 40, size // 2 - 40), (size // 2 + 40, size // 2 + 40), (30, 30, 30), -1)
    ok, buf = cv2.imencode(".jpg", img)
    return buf.tobytes()


def test_detect_finds_all_four():
    centers = scan_zone.detect_markers(_frame_with_markers())
    assert scan_zone.has_all(centers)
    assert set(centers) == {0, 1, 2, 3}


def test_rectify_produces_square():
    frame = _frame_with_markers()
    centers = scan_zone.detect_markers(frame)
    flat = scan_zone.rectify(frame, centers)
    assert flat is not None
    img = cv2.imdecode(np.frombuffer(flat, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[0] == img.shape[1] == scan_zone.ZONE_SIZE


def test_rectify_needs_all_markers():
    centers = scan_zone.detect_markers(_frame_with_markers())
    del centers[2]
    assert scan_zone.rectify(_frame_with_markers(), centers) is None


def test_occupancy_reference_diff_flips_with_item():
    empty = scan_zone.rectify(_frame_with_markers(item=False),
                              scan_zone.detect_markers(_frame_with_markers(item=False)))
    occ_frame = _frame_with_markers(item=True)
    filled = scan_zone.rectify(occ_frame, scan_zone.detect_markers(occ_frame))
    assert empty is not None and filled is not None

    # empty vs its own reference -> not occupied
    is_occ, score = scan_zone.occupancy(empty, empty)
    assert is_occ is False and score < scan_zone.OCC_THRESHOLD

    # item vs empty reference -> occupied
    is_occ, score = scan_zone.occupancy(filled, empty)
    assert is_occ is True and score > scan_zone.OCC_THRESHOLD


def test_persistence_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(scan_zone, "REFERENCE_PATH", tmp_path / "ref.jpg")
    monkeypatch.setattr(scan_zone, "STATE_PATH", tmp_path / "state.json")
    assert scan_zone.has_reference() is False
    assert scan_zone.load_pose() is None

    scan_zone.save_reference(b"\xff\xd8jpeg")
    assert scan_zone.has_reference() is True
    assert scan_zone.load_reference() == b"\xff\xd8jpeg"

    scan_zone.save_pose(4, 72)
    assert scan_zone.load_pose() == {"yaw": 4, "pitch": 72}
