"""Phrase picker with cross-process repeat-avoidance.

random.choice alone repeats itself often enough to be noticeable, especially
from small pools and short-lived processes with no memory between runs.
pick() persists the last few choices per pool in a temp json file and
excludes them from the next draw — up to half the pool size, so small pools
always still have options.

The state file is shared with C:\\Users\\domin\\tools\\stackchan-hook.py,
which carries its own copy of this logic (_pick) — that script is
deliberately stdlib-only/standalone so a broken gateway checkout can never
break the Claude Code hooks. Keep the file format and semantics in sync if
either side changes: {"<pool_name>": ["most recent", ...oldest]}.

Pool names must be unique across ALL callers (hook, voice bridge, sensor
reactor, idle loop) since they share one file.
"""
from __future__ import annotations

import json
import os
import random

RECENT_PHRASES_FILE = os.path.join(
    os.environ.get("TEMP", os.environ.get("TMP", ".")),
    "stackchan-recent-phrases.json",
)


def pick(pool_name: str, phrases: list) -> str:
    try:
        with open(RECENT_PHRASES_FILE, encoding="utf-8") as f:
            recent = json.load(f)
        if not isinstance(recent, dict):
            recent = {}
    except Exception:
        recent = {}
    avoid = recent.get(pool_name, [])
    candidates = [p for p in phrases if p not in avoid] or phrases
    choice = random.choice(candidates)
    keep = max(1, len(phrases) // 2)
    recent[pool_name] = ([choice] + [p for p in avoid if p != choice])[:keep]
    try:
        with open(RECENT_PHRASES_FILE, "w", encoding="utf-8") as f:
            json.dump(recent, f)
    except Exception:
        pass
    return choice
