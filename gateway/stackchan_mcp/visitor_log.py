"""Visitor log: a rolling timeline of face-recognition events.

Every time the vision loop reacts to a detected face it appends one entry here
(see ``stackchan-vision-loop.py::_log_visitor_safe``), and the companion API
serves the timeline to the Android app's Faces screen.

Storage is a JSONL file (one entry per line) plus a sibling directory of small
JPEG thumbnails — one per entry. An entry looks like::

    {"id": "1720008000000", "ts": 1720008000.0, "name": "Dominic",
     "known": true, "score": 0.62, "thumb": "1720008000000.jpg"}

Paths default next to the gateway package and are overridable via
``STACKCHAN_VISITOR_LOG`` / ``STACKCHAN_VISITOR_THUMBS``. The log self-trims to
the most recent ``MAX_ENTRIES`` on every append, pruning orphaned thumbnails, so
a long-running gateway can't grow it without bound.

This module deliberately has NO OpenCV/numpy dependency: callers pass an
already-encoded JPEG ``bytes`` for the thumbnail. All persistence failures are
caught and logged — logging a visitor must never break recognition.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

# gateway/ (one level above this package directory)
_GATEWAY_DIR: Final[Path] = Path(__file__).resolve().parent.parent

LOG_PATH_ENV: Final[str] = "STACKCHAN_VISITOR_LOG"
THUMBS_DIR_ENV: Final[str] = "STACKCHAN_VISITOR_THUMBS"
DEFAULT_LOG_PATH: Final[Path] = _GATEWAY_DIR / "visitor_log.jsonl"
DEFAULT_THUMBS_DIR: Final[Path] = _GATEWAY_DIR / "visitor_thumbs"

# Keep only the most recent N events (and their thumbnails).
MAX_ENTRIES: Final[int] = 200


def resolve_log_path() -> Path:
    override = os.environ.get(LOG_PATH_ENV)
    return Path(override).expanduser() if override else DEFAULT_LOG_PATH


def resolve_thumbs_dir() -> Path:
    override = os.environ.get(THUMBS_DIR_ENV)
    return Path(override).expanduser() if override else DEFAULT_THUMBS_DIR


def _read_raw(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []
    entries: list[dict[str, Any]] = []
    try:
        with log_path.open("r", encoding="utf-8") as f:
            for raw in f:
                stripped = raw.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    entries.append(obj)
    except OSError as exc:
        logger.warning("Failed to read visitor log %s: %s", log_path, exc)
    return entries


def append(
    name: str | None,
    known: bool,
    score: float,
    thumb_jpeg: bytes | None = None,
    *,
    ts: float | None = None,
    log_path: Path | None = None,
    thumbs_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Append one recognition event; returns the written entry (or None on error).

    ``thumb_jpeg`` is an already-encoded JPEG for the timeline thumbnail. Fire
    and forget — any disk error is logged and swallowed.
    """
    if ts is None:
        ts = time.time()
    if log_path is None:
        log_path = resolve_log_path()
    if thumbs_dir is None:
        thumbs_dir = resolve_thumbs_dir()

    entry_id = str(int(ts * 1000))
    thumb_name: str | None = None
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if thumb_jpeg:
            thumbs_dir.mkdir(parents=True, exist_ok=True)
            thumb_name = f"{entry_id}.jpg"
            (thumbs_dir / thumb_name).write_bytes(thumb_jpeg)
    except OSError as exc:
        logger.warning("Failed to write visitor thumbnail: %s", exc)
        thumb_name = None

    entry = {
        "id": entry_id,
        "ts": ts,
        "name": name,
        "known": bool(known),
        "score": round(float(score), 4),
        "thumb": thumb_name,
    }
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.warning("Failed to append visitor log entry: %s", exc)
        return None

    _trim(log_path, thumbs_dir)
    return entry


def _trim(log_path: Path, thumbs_dir: Path) -> None:
    """Keep only the newest MAX_ENTRIES lines; delete orphaned thumbnails."""
    entries = _read_raw(log_path)
    if len(entries) <= MAX_ENTRIES:
        return
    keep = entries[-MAX_ENTRIES:]
    dropped = entries[:-MAX_ENTRIES]
    try:
        tmp = log_path.with_suffix(log_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for e in keep:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        os.replace(tmp, log_path)
    except OSError as exc:
        logger.warning("Failed to trim visitor log: %s", exc)
        return
    for e in dropped:
        thumb = e.get("thumb")
        if thumb:
            try:
                (thumbs_dir / thumb).unlink(missing_ok=True)
            except OSError:
                pass


def read(limit: int = 100, *, log_path: Path | None = None) -> list[dict[str, Any]]:
    """Return up to ``limit`` most-recent entries, newest first."""
    if log_path is None:
        log_path = resolve_log_path()
    entries = _read_raw(log_path)
    entries.reverse()  # newest first
    if limit > 0:
        entries = entries[:limit]
    return entries


def thumb_path(thumb_name: str, *, thumbs_dir: Path | None = None) -> Path | None:
    """Resolve a thumbnail filename to a path, guarding against traversal."""
    if not thumb_name or os.path.basename(thumb_name) != thumb_name:
        return None
    if thumbs_dir is None:
        thumbs_dir = resolve_thumbs_dir()
    path = thumbs_dir / thumb_name
    return path if path.exists() else None
