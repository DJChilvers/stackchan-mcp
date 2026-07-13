"""Voice-searchable HomeBox inventory lookups for Wheatley.

"Hey Wheatley, where are my earplugs?" → search the local HomeBox
instance (native Windows build, http://127.0.0.1:7745) → speak the
location. Consumed by:

- the voice bridge (``stackchan-voice-bridge.py``) via
  :func:`extract_query` (intent sniff, same shape as
  ``rail_dance.is_dance_request``) + :func:`find_items` +
  :func:`format_speech`, and
- the stdio MCP server (``stdio_server.py``) via the ``inventory_find``
  tool, so chat Claude can ask the gateway where things are too.

HomeBox facts this module relies on (proven live 2026-07-12, v0.26.2):

- unified entities search: ``GET /api/v1/entities?q=<term>&pageSize=N``
  returns ``{items, total}``; every hit carries an entity type
  discriminator (Item vs Location).
- location path: ``GET /api/v1/entities/{id}/path`` returns an array
  root→leaf (the last element is the item itself).
- auth: ``Authorization: <token>`` where token = contents of
  ``%TEMP%\\hbox_tok.txt`` (see Documents/HomeBox/update_psu.py). The
  token can go stale — a 401/403, a missing token file, or HomeBox
  simply not running must degrade to a spoken "can't reach the
  inventory right now", never a crash.

Stdlib only (urllib), same rule as rail_dance.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

BASE_URL = os.environ.get("STACKCHAN_HOMEBOX_URL", "http://127.0.0.1:7745")
TOKEN_FILENAME = "hbox_tok.txt"

MAX_RESULTS = 3          # cap spoken/returned hits
TOTAL_BUDGET_S = 4.0     # whole find_items() call, search + paths
SEARCH_PAGE_SIZE = 10    # fetch extra so Location hits can be filtered out


class InventoryUnavailable(Exception):
    """HomeBox can't be reached / won't let us in (stale token, not
    running, ...). Callers speak :func:`format_unavailable` instead of
    erroring."""


# ─── intent sniffing (voice bridge) ──────────────────────────────────────────

# Rail-findable phrasing ("is it on the desk?") conceptually belongs to
# find_item.py / the vision loop, not the HomeBox inventory — leave those
# utterances to normal chat entirely.
_DESK_RE = re.compile(r"\b(?:desk|bench)\b", re.IGNORECASE)

# One pattern per supported question shape. Each captures an optional
# article/possessive (art) and the candidate item phrase (item). Curly
# apostrophes appear in Whisper transcripts, hence [''].
_PATTERNS = (
    # "find (my) multimeter in the inventory"
    re.compile(
        r"\bfind\s+(?:(?P<art>my|the|our|a|an|any|some)\s+)?"
        r"(?P<item>.+?)\s+in\s+(?:the\s+)?inventory\b",
        re.IGNORECASE,
    ),
    # "where is/are/'s/'re (my) earplugs"
    re.compile(
        r"\bwhere(?:['']s|['']re|\s+is|\s+are)\s+"
        r"(?:(?P<art>my|the|our)\s+)?(?P<item>[^.,!?;]+)",
        re.IGNORECASE,
    ),
    # "do we/I have (a/any) earplugs"
    re.compile(
        r"\bdo\s+(?:we|i)\s+have\s+"
        r"(?:(?P<art>a|an|any|the|some)\s+)?(?P<item>[^.,!?;]+)",
        re.IGNORECASE,
    ),
    # "have we got (any) earplugs"
    re.compile(
        r"\bhave\s+we\s+got\s+"
        r"(?:(?P<art>a|an|any|the|some)\s+)?(?P<item>[^.,!?;]+)",
        re.IGNORECASE,
    ),
)

# Filler tails to shave off the item phrase, repeatedly ("...gone then?").
_TRAIL_RE = re.compile(
    r"\s+(?:in\s+(?:the\s+)?inventory|anywhere|somewhere|around\s+here|"
    r"around|lying\s+about|at\s+the\s+moment|right\s+now|these\s+days|"
    r"gone\s+to|gone|at|then|now|please|mate|though|exactly|again|today)"
    r"\s*$",
    re.IGNORECASE,
)
# Filler heads ("spare", "any of those", "a pair of", stray ums).
_LEAD_RE = re.compile(
    r"^(?:um+\s+|uh+\s+|like\s+|spare\s+|extra\s+|more\s+|another\s+|"
    r"any\s+more\s+|any\s+of\s+(?:those|these|the|my)\s+|"
    r"one\s+of\s+(?:those|these|the|my)\s+|a\s+pair\s+of\s+|"
    r"a\s+couple\s+of\s+|some\s+of\s+(?:those|these|the|my)\s+)+",
    re.IGNORECASE,
)

# "where are we", "where is he going", "do we have to leave" — a leading
# pronoun/function word means it's not an item phrase. Be conservative:
# false negatives just fall through to normal chat.
_NOT_ITEM_FIRST_WORDS = frozenset(
    {
        "i", "we", "you", "he", "she", "it", "they", "them", "us", "me",
        "him", "her", "this", "that", "these", "those", "everyone",
        "everybody", "anyone", "anybody", "someone", "somebody",
        "anything", "something", "everything", "to", "been", "got", "had",
        "going", "gone", "left", "any", "some", "all", "each", "either",
        "time", "enough", "lunch", "dinner", "breakfast", "plans",
    }
)
# A pronoun anywhere in the phrase ("time for this", "one of them") means
# it's conversation, not an item name.
_NOT_ITEM_ANY_WORD = frozenset(
    {
        "i", "we", "you", "he", "she", "it", "they", "them", "us", "me",
        "him", "her", "this", "that", "these", "those",
    }
)


def extract_query(text: str | None) -> str | None:
    """Sniff a transcript for an inventory question; return the item
    phrase to search for, or None when this should fall through to
    normal chat. Never raises (callers wrap in try/except anyway).

    Guards (false negatives are fine, false positives are not):
    - "desk"/"bench" anywhere → None (rail find_item territory).
    - phrase must start with an article/possessive OR be 2+ words.
    - leading pronouns/function words ("where are we") → None.
    """
    if not text:
        return None
    if _DESK_RE.search(text):
        return None

    for pattern in _PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        item = m.group("item") or ""
        has_article = bool(m.group("art"))

        # Cut at sentence punctuation, normalise whitespace/quotes.
        item = re.split(r"[.,!?;:]", item)[0]
        item = item.strip().strip("\"'  ").strip()
        # Shave filler heads/tails until stable ("earplugs gone then").
        while True:
            shorter = _TRAIL_RE.sub("", _LEAD_RE.sub("", item)).strip()
            if shorter == item:
                break
            item = shorter
        item = item.lower()

        words = item.split()
        if not words or len(words) > 6:
            return None
        if words[0] in _NOT_ITEM_FIRST_WORDS:
            return None
        if _NOT_ITEM_ANY_WORD.intersection(words):
            return None
        if not has_article and len(words) < 2:
            return None
        if not re.search(r"[a-z0-9]", item):
            return None
        return item

    return None


# ─── HomeBox client ──────────────────────────────────────────────────────────


def _read_token() -> str:
    """Read the HomeBox session token; InventoryUnavailable if absent."""
    path = os.path.join(tempfile.gettempdir(), TOKEN_FILENAME)
    try:
        with open(path, encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError as exc:
        raise InventoryUnavailable(f"token file unreadable: {exc}") from exc
    if not token:
        raise InventoryUnavailable(f"token file empty: {path}")
    return token


def _get_json(url: str, token: str, timeout: float):
    """GET url → parsed JSON. 401/403 and transport errors both raise
    InventoryUnavailable (stale token / HomeBox down look the same to
    the person asking)."""
    req = urllib.request.Request(
        url,
        headers={"Authorization": token, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise InventoryUnavailable(
                f"auth rejected ({exc.code}) — token stale?"
            ) from exc
        raise InventoryUnavailable(f"HomeBox HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise InventoryUnavailable(f"HomeBox unreachable: {exc}") from exc


def _entity_type_name(hit: dict) -> str:
    """Entity type discriminator, tolerant of shape (string field or
    nested {name} object, any of the spellings HomeBox uses)."""
    for key in ("entityType", "entity_type", "type"):
        val = hit.get(key)
        if isinstance(val, dict):
            val = val.get("name")
        if isinstance(val, str) and val:
            return val.lower()
    return ""


def find_items(query: str, *, budget_s: float = TOTAL_BUDGET_S) -> list[dict]:
    """Search HomeBox for ``query``; return up to MAX_RESULTS Item hits as
    ``{name, quantity, location_path: [names root→leaf]}``.

    Raises InventoryUnavailable when HomeBox can't be reached at all
    (down / stale token / missing token file). A hit whose path lookup
    fails (or falls outside the ~4s budget) still comes back, with
    location_path=[] — callers phrase that as "location not filed".
    """
    deadline = time.monotonic() + budget_s
    token = _read_token()

    q = urllib.parse.quote((query or "").strip())
    data = _get_json(
        f"{BASE_URL}/api/v1/entities?q={q}&pageSize={SEARCH_PAGE_SIZE}",
        token,
        timeout=max(0.5, min(2.5, deadline - time.monotonic())),
    )
    hits = data.get("items") or [] if isinstance(data, dict) else []
    items = [
        h for h in hits
        if isinstance(h, dict) and h.get("id") and _entity_type_name(h) == "item"
    ][:MAX_RESULTS]

    results: list[dict] = []
    for hit in items:
        location_path: list[str] = []
        remaining = deadline - time.monotonic()
        if remaining > 0.2:
            try:
                path = _get_json(
                    f"{BASE_URL}/api/v1/entities/{hit['id']}/path",
                    token,
                    timeout=min(1.5, remaining),
                )
                if isinstance(path, list):
                    # Last element is the item itself — drop it (match by
                    # id, fall back to positional drop).
                    if path and isinstance(path[-1], dict) and (
                        path[-1].get("id") == hit["id"]
                        or path[-1].get("name") == hit.get("name")
                    ):
                        path = path[:-1]
                    location_path = [
                        str(p.get("name", "")).strip()
                        for p in path
                        if isinstance(p, dict) and p.get("name")
                    ]
            except Exception as exc:  # path is best-effort — degrade per hit
                logger.warning(
                    "inventory: path lookup failed for %r: %s",
                    hit.get("name"), exc,
                )
        quantity = hit.get("quantity")
        if not isinstance(quantity, int) or isinstance(quantity, bool):
            quantity = 1
        results.append(
            {
                "name": str(hit.get("name") or "unnamed item"),
                "quantity": quantity,
                "location_path": location_path,
            }
        )
    return results


# ─── speech formatting (Wheatley voice) ──────────────────────────────────────


def _location_phrase(location_path: list[str]) -> str | None:
    """'Main Loft, Tub 12' — last 3 levels max, keeps the speech short."""
    names = [n for n in (location_path or []) if n]
    if not names:
        return None
    return ", ".join(names[-3:])


def _is_plural(word: str) -> bool:
    w = word.strip().lower().split()[-1] if word.strip() else ""
    return w.endswith("s") and not w.endswith("ss")


def format_unavailable() -> str:
    """Spoken line for InventoryUnavailable — calm, in character."""
    return (
        "Can't reach the inventory right now. The filing system's having "
        "a moment. Try again in a bit."
    )


def format_speech(query: str, results: list[dict]) -> str:
    """Short Wheatley-style spoken answer for find_items() results."""
    q = (query or "that").strip() or "that"

    if not results:
        return (
            f"Couldn't find {q} in the inventory. Either we don't have "
            "one, or nobody's filed it. Probably the second one."
        )

    if len(results) == 1:
        r = results[0]
        verb = "are" if _is_plural(q) else "is"
        loc = _location_phrase(r.get("location_path") or [])
        if loc is None:
            return (
                f"Your {q} {verb} in the inventory, but nobody wrote down "
                "where. Helpful."
            )
        qty = r.get("quantity", 1)
        extra = (
            f" {qty} of them, apparently."
            if isinstance(qty, int) and qty > 1
            else ""
        )
        return f"Your {q} {verb} in the {loc}.{extra}"

    parts = []
    for r in results[:MAX_RESULTS]:
        name = r.get("name") or "something unnamed"
        loc = _location_phrase(r.get("location_path") or [])
        parts.append(f"{name} in the {loc}" if loc else f"{name}, location not filed")
    listing = "; ".join(parts[:-1]) + f"; and {parts[-1]}"
    return f"Found {len(parts)} matches for {q}: {listing}."
