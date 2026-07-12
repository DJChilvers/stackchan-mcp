"""Known-face roster operations for the companion API.

Reads and mutates the same on-disk store the vision loop
(``stackchan-vision-loop.py``) owns, so the two stay in lock-step:

- ``known_faces.json`` — ``{name: [embedding_vector, ...]}``; the number of
  vectors is that person's sample count.
- ``known_faces_photos/{name}.jpg`` — one representative reference crop per name.
- ``face_greetings.json`` — ``{name: "custom line"}``; NEW here (the vision loop
  doesn't write it yet), used to personalise the recognition greeting.

Paths mirror the vision loop's env overrides so both processes agree:
``STACKCHAN_VISION_KNOWN_FACES`` / ``STACKCHAN_VISION_REFERENCE_PHOTOS_DIR``,
plus ``STACKCHAN_FACE_GREETINGS`` for the greetings file. All default next to
the gateway package.

Writes to ``known_faces.json`` use the same atomic tmp+replace the vision loop
uses; a concurrent write is at worst a lost update, never a corrupt file.
Roster mutations raise :class:`FaceError` (with a human message + ``status``
hint) which the HTTP layer maps to 400/404/409.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

_GATEWAY_DIR: Final[Path] = Path(__file__).resolve().parent.parent

KNOWN_FACES_ENV: Final[str] = "STACKCHAN_VISION_KNOWN_FACES"
PHOTOS_DIR_ENV: Final[str] = "STACKCHAN_VISION_REFERENCE_PHOTOS_DIR"
GREETINGS_ENV: Final[str] = "STACKCHAN_FACE_GREETINGS"

DEFAULT_KNOWN_FACES: Final[Path] = _GATEWAY_DIR / "known_faces.json"
DEFAULT_PHOTOS_DIR: Final[Path] = _GATEWAY_DIR / "known_faces_photos"
DEFAULT_GREETINGS: Final[Path] = _GATEWAY_DIR / "face_greetings.json"


class FaceError(Exception):
    """A roster operation failed for a client-correctable reason."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# path resolution
# ---------------------------------------------------------------------------
def known_faces_path() -> Path:
    override = os.environ.get(KNOWN_FACES_ENV)
    return Path(override).expanduser() if override else DEFAULT_KNOWN_FACES


def photos_dir() -> Path:
    override = os.environ.get(PHOTOS_DIR_ENV)
    return Path(override).expanduser() if override else DEFAULT_PHOTOS_DIR


def greetings_path() -> Path:
    override = os.environ.get(GREETINGS_ENV)
    return Path(override).expanduser() if override else DEFAULT_GREETINGS


# ---------------------------------------------------------------------------
# low-level JSON io
# ---------------------------------------------------------------------------
def _load_known() -> dict[str, Any]:
    path = known_faces_path()
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        logger.exception("failed to load known_faces.json")
        return {}


def _save_known(data: dict[str, Any]) -> None:
    path = known_faces_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _load_greetings() -> dict[str, str]:
    path = greetings_path()
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_greetings(data: dict[str, str]) -> None:
    path = greetings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def _validate_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise FaceError("name must not be empty")
    # Names double as photo filenames — reject anything that isn't a plain base.
    if os.path.basename(name) != name or any(c in name for c in '\\/:*?"<>|'):
        raise FaceError("name contains invalid characters")
    return name


def _photo_file(name: str) -> Path:
    return photos_dir() / f"{name}.jpg"


# ---------------------------------------------------------------------------
# public roster API
# ---------------------------------------------------------------------------
def list_faces() -> list[dict[str, Any]]:
    """Return the roster: one dict per enrolled name (sorted by name)."""
    known = _load_known()
    greetings = _load_greetings()
    out: list[dict[str, Any]] = []
    for name in sorted(known, key=str.lower):
        vectors = known.get(name) or []
        out.append({
            "name": name,
            "samples": len(vectors) if isinstance(vectors, list) else 0,
            "has_photo": _photo_file(name).exists(),
            "greeting": greetings.get(name),
        })
    return out


def photo_path(name: str) -> Path | None:
    """Path to a name's reference JPEG, or None if there isn't one."""
    name = _validate_name(name)
    p = _photo_file(name)
    return p if p.exists() else None


def rename(old: str, new: str) -> None:
    """Rename an enrolled person, moving their embeddings, photo and greeting."""
    old = _validate_name(old)
    new = _validate_name(new)
    known = _load_known()
    if old not in known:
        raise FaceError(f"no such face '{old}'", status=404)
    if new == old:
        return
    if new in known:
        raise FaceError(f"'{new}' already exists", status=409)

    known[new] = known.pop(old)
    _save_known(known)

    old_photo, new_photo = _photo_file(old), _photo_file(new)
    if old_photo.exists():
        try:
            os.replace(old_photo, new_photo)
        except OSError as exc:
            logger.warning("rename photo %s -> %s failed: %s", old_photo, new_photo, exc)

    greetings = _load_greetings()
    if old in greetings:
        greetings[new] = greetings.pop(old)
        _save_greetings(greetings)


def delete(name: str) -> None:
    """Forget a person entirely: embeddings, reference photo and greeting."""
    name = _validate_name(name)
    known = _load_known()
    if name not in known:
        raise FaceError(f"no such face '{name}'", status=404)
    del known[name]
    _save_known(known)

    photo = _photo_file(name)
    if photo.exists():
        try:
            photo.unlink()
        except OSError as exc:
            logger.warning("delete photo %s failed: %s", photo, exc)

    greetings = _load_greetings()
    if name in greetings:
        del greetings[name]
        _save_greetings(greetings)


def get_greeting(name: str) -> str | None:
    name = _validate_name(name)
    return _load_greetings().get(name)


def set_greeting(name: str, line: str) -> None:
    """Set (or clear, if ``line`` is blank) a person's custom greeting."""
    name = _validate_name(name)
    if name not in _load_known():
        raise FaceError(f"no such face '{name}'", status=404)
    greetings = _load_greetings()
    line = (line or "").strip()
    if line:
        greetings[name] = line
    else:
        greetings.pop(name, None)
    _save_greetings(greetings)
