"""Scan-zone vision: an ArUco-marked tray Wheatley looks down at.

Four DICT_4X4_50 corner markers (ID0=TL, ID1=TR, ID2=BR, ID3=BL) define a
rectangular zone. Given a captured JPEG this module can:

- detect the four markers,
- rectify the zone to a flat, upright top-down view (the marker IDs fix the
  orientation, so the output is correct even though Wheatley is mounted
  upside-down and views at an angle),
- decide empty-vs-occupied by comparing against a stored "empty zone" reference
  (colour-robust: any change from empty reads as occupied), and
- persist the reference image + the acquired head pose.

Pure vision + file persistence — no device I/O. The companion server drives the
head/camera and feeds captured bytes to these functions.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Final

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_GATEWAY_DIR: Final[Path] = Path(__file__).resolve().parent.parent
REFERENCE_PATH: Final[Path] = _GATEWAY_DIR / "scan_zone_reference.jpg"
STATE_PATH: Final[Path] = _GATEWAY_DIR / "scan_zone.json"

ZONE_SIZE: Final[int] = 300          # rectified output is ZONE_SIZE x ZONE_SIZE px
OCC_INSET: Final[float] = 0.12       # ignore this fraction at each edge (the markers)
OCC_THRESHOLD: Final[float] = 15.0   # mean abs-diff (0-255) above which zone reads occupied

MARKER_IDS: Final[tuple[int, ...]] = (0, 1, 2, 3)  # TL, TR, BR, BL

_DICT = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
_DETECTOR = cv2.aruco.ArucoDetector(_DICT, cv2.aruco.DetectorParameters())


def _decode(jpeg: bytes):
    return cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)


def detect_markers(jpeg: bytes) -> dict[int, tuple[float, float]]:
    """Return {marker_id: (cx, cy)} for every DICT_4X4_50 marker found."""
    img = _decode(jpeg)
    if img is None:
        return {}
    corners, ids, _ = _DETECTOR.detectMarkers(img)
    if ids is None:
        return {}
    out: dict[int, tuple[float, float]] = {}
    for i, c in zip(ids.flatten(), corners):
        cx, cy = c.reshape(-1, 2).mean(axis=0)
        out[int(i)] = (float(cx), float(cy))
    return out


def has_all(centers: dict) -> bool:
    return all(k in centers for k in MARKER_IDS)


def rectify(jpeg: bytes, centers: dict, size: int = ZONE_SIZE) -> bytes | None:
    """Warp the zone to a flat top-down JPEG. Requires all four markers."""
    if not has_all(centers):
        return None
    img = _decode(jpeg)
    if img is None:
        return None
    src = np.float32([centers[0], centers[1], centers[2], centers[3]])
    dst = np.float32([[0, 0], [size, 0], [size, size], [0, size]])
    flat = cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (size, size))
    ok, buf = cv2.imencode(".jpg", flat)
    return buf.tobytes() if ok else None


def _inner_gray(jpeg: bytes):
    img = _decode(jpeg)
    if img is None:
        return None
    s = img.shape[0]
    m = int(s * OCC_INSET)
    inner = img[m:s - m, m:s - m]
    gray = cv2.cvtColor(inner, cv2.COLOR_BGR2GRAY)
    return cv2.GaussianBlur(gray, (5, 5), 0)


def occupancy(flat_jpeg: bytes, reference_jpeg: bytes | None,
              threshold: float = OCC_THRESHOLD) -> tuple[bool, float]:
    """(occupied, score). Reference-diff when a reference exists (colour-robust);
    otherwise a crude non-white fallback."""
    cur = _inner_gray(flat_jpeg)
    if cur is None:
        return False, 0.0
    if reference_jpeg:
        ref = _inner_gray(reference_jpeg)
        if ref is not None and ref.shape == cur.shape:
            score = float(cv2.absdiff(cur, ref).mean())
            return score > threshold, round(score, 2)
    # fallback (no reference yet): fraction of darker-than-paper pixels, x100
    score = round(float((cur < 170).mean()) * 100.0, 2)
    return score > 8.0, score


# ---------------------------------------------------------------------------
# persistence
# ---------------------------------------------------------------------------
def save_reference(flat_jpeg: bytes) -> None:
    REFERENCE_PATH.write_bytes(flat_jpeg)


def load_reference() -> bytes | None:
    try:
        return REFERENCE_PATH.read_bytes()
    except OSError:
        return None


def has_reference() -> bool:
    return REFERENCE_PATH.exists()


def save_pose(yaw: float, pitch: float) -> None:
    STATE_PATH.write_text(json.dumps({"yaw": yaw, "pitch": pitch}))


def load_pose() -> dict | None:
    try:
        d = json.loads(STATE_PATH.read_text())
        return {"yaw": d["yaw"], "pitch": d["pitch"]}
    except Exception:
        return None
