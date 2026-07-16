#!/usr/bin/env python3
"""
stackchan-vision-loop.py — local, offline, face-aware ambient vision loop.

Polls a low-frame-rate photo from StackChan's camera (via the gateway's
existing `take_photo` MCP tool), runs face detection + recognition FULLY
LOCALLY (OpenCV's YuNet detector + SFace recognizer, both small ONNX models
under gateway/models/ — no Claude API call, ever), and fires a small
reaction through the gateway's existing sensor-reaction system
(`SensorReactor._behavior_recognize`, via `POST /react/recognize`) when it
notices a known or unknown face. This is the "don't burn API credits"
counterpart to the on-request camera tool wired into stackchan-voice-bridge.py
— that one costs a Claude API call because Claude decided to look; this one
costs nothing per tick because it never leaves the LAN.

PURE PERCEPTION, NO SERVO CONTROL (changed 2026-07-01): this script does
NOT call move_head. It writes what it sees (face offset/identity, motion)
to a shared JSON state file (VISION_STATE_PATH) every tick; stackchan-
idle.py reads that file and does all the actual head movement (tracking,
searching when no face has been seen in a while, glancing toward motion).
Earlier this script drove the servo directly, which raced the sensor-
reaction system's own head movement (confirmed live: tracking lost the
face mid-loop right as a greeting reaction's nod animation was playing).
Routing all movement through idle.py — which already respects the busy/
activity markers that a reaction sets — removes the race structurally
instead of papering over it with more ordering/locking here.

"Teaching" a face:
    python stackchan-vision-loop.py --enroll "Dominic"
Captures a few samples of whoever is in front of the camera, computes SFace
embeddings, and stores them (averaged over however many samples exist so
far) in known_faces.json next to this script. Re-running --enroll for the
same name adds more samples rather than replacing them. Also invoked as a
subprocess by stackchan-voice-bridge.py to complete the "ask an unknown
face's name" flow — see PENDING_ENROLLMENT_MARKER below.

Run:  python stackchan-vision-loop.py            (loop forever)
      python stackchan-vision-loop.py --once      (one tick then exit)
      python stackchan-vision-loop.py --enroll "Name" [--samples 3]
Stop: kill the process (or use stackchan-vision-loop-start.vbs to run
      hidden, same pattern as stackchan-idle.py — NOT auto-started at
      login; launch it deliberately).
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import logging
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from dotenv import load_dotenv

# Needed for the arbiter's STACKCHAN_VOICE_ANTHROPIC_API_KEY (same .env
# entry the voice bridge uses) — this script never loaded .env before the
# arbiter existed, since everything else here is fully local/no-API-key.
load_dotenv(Path(__file__).resolve().parent / ".env")

# ── single-instance lock (same pattern as stackchan-idle.py) ───────────────
import atexit
import msvcrt

TEMP = os.environ.get("TEMP", os.environ.get("TMP", "."))
_LOCK_FILE = os.path.join(TEMP, "stackchan-vision-loop.lock")
_lock_fh = None


def _acquire_lock() -> None:
    global _lock_fh
    try:
        _lock_fh = open(_LOCK_FILE, "a+b")
        msvcrt.locking(_lock_fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        if _lock_fh:
            _lock_fh.close()
        sys.exit(0)  # another instance holds the lock — back off silently


atexit.register(lambda: _lock_fh.close() if _lock_fh else None)

LOG_PATH = os.path.join(TEMP, "stackchan-vision-loop.log")
logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("vision-loop")

GATEWAY_MCP = "http://127.0.0.1:8767/mcp"
CAPTURE_PORT = int(os.environ.get("CAPTURE_PORT", "8766"))
REACT_URL = f"http://127.0.0.1:{CAPTURE_PORT}/react/recognize"
ENCOURAGE_REACT_URL = f"http://127.0.0.1:{CAPTURE_PORT}/react/encourage"
OBJECT_REACT_URL = f"http://127.0.0.1:{CAPTURE_PORT}/react/object_comment"
MESSY_REACT_URL = f"http://127.0.0.1:{CAPTURE_PORT}/react/messy"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(SCRIPT_DIR, "models")
YUNET_MODEL = os.environ.get(
    "STACKCHAN_VISION_DETECT_MODEL",
    os.path.join(MODELS_DIR, "face_detection_yunet_2023mar.onnx"),
)
SFACE_MODEL = os.environ.get(
    "STACKCHAN_VISION_RECOGNIZE_MODEL",
    os.path.join(MODELS_DIR, "face_recognition_sface_2021dec.onnx"),
)
KNOWN_FACES_PATH = os.environ.get(
    "STACKCHAN_VISION_KNOWN_FACES", os.path.join(SCRIPT_DIR, "known_faces.json")
)
# One representative aligned face-crop per enrolled name — Claude can't
# compare against a raw embedding vector, it needs an actual photo. Written
# during enrollment (see _enroll), read by the arbiter (see
# _call_arbiter/_ask_claude_arbiter below).
REFERENCE_PHOTOS_DIR = os.environ.get(
    "STACKCHAN_VISION_REFERENCE_PHOTOS_DIR", os.path.join(SCRIPT_DIR, "known_faces_photos")
)

# SFace's documented default cosine-similarity threshold for "same person".
MATCH_THRESHOLD = float(os.environ.get("STACKCHAN_VISION_MATCH_THRESHOLD", "0.363"))
POLL_INTERVAL_S = float(os.environ.get("STACKCHAN_VISION_POLL_INTERVAL_S", "8"))
# Adaptive cadence: the device cam is snapshot-based (~1.3s/photo), so the
# default 8s ambient poll is far too slow to catch a hand gesture — it lands
# between snapshots. When a tick is "hot" (motion in frame or a gesture just
# fired) the loop drops to FAST_POLL_INTERVAL_S for the next HOT_TICKS ticks,
# so while you're actually there gesturing it watches ~every 2.5s, then relaxes
# back to 8s when idle (gentle on the device). Set FAST==POLL to disable.
FAST_POLL_INTERVAL_S = float(os.environ.get("STACKCHAN_VISION_FAST_POLL_INTERVAL_S", "2.5"))
FAST_POLL_HOT_TICKS = int(os.environ.get("STACKCHAN_VISION_FAST_POLL_HOT_TICKS", "5"))
# Debounce for UNKNOWN-face prompts only (2026-07-06): don't re-fire the
# "who are you?" ask more often than this. Known-person greetings moved to
# absence-based logic (below) — the old flat timer both under-greeted short
# real absences and re-greeted someone who'd sat at the desk the whole hour.
COOLDOWN_S = float(os.environ.get("STACKCHAN_VISION_COOLDOWN_S", str(60 * 60)))

# Absence-based re-greeting (2026-07-06, "make him a more active assistant"):
# a KNOWN person is greeted when they REAPPEAR after a real absence. "Real
# absence" needs BOTH a wall-clock gap AND enough executed looks that missed
# them (see _note_look) — ticks skipped for busy/pause/offline bump nothing,
# so a long Claude session with the owner sat in frame the whole time doesn't
# read as the owner having left.
ABSENCE_GREET_S = float(os.environ.get("STACKCHAN_VISION_ABSENCE_GREET_S", str(20 * 60)))
ABSENCE_MIN_MISSED_LOOKS = int(os.environ.get("STACKCHAN_VISION_ABSENCE_MIN_MISSED_LOOKS", "8"))
# Presence hint independent of the camera seeing a face (2026-07-06 — user
# reported false "you're not here!" greetings in LOW LIGHT, where YuNet loses
# the face even though they never left). If there's been recent keyboard/mouse
# input (works in the dark, unlike face detection) OR motion in frame, we have
# evidence a human is still present, so a face-less tick is NOT counted as
# absence — it just freezes the missed-look counter rather than climbing it.
# The welcome-back greeting then only fires after a REAL departure (no input,
# no motion, no face for the full absence window). PRESENCE_INPUT_WINDOW_S =
# how recent the last input must be to count as "still here".
PRESENCE_INPUT_WINDOW_S = float(os.environ.get("STACKCHAN_VISION_PRESENCE_INPUT_S", str(3 * 60)))

# Ambient work-encouragement nudge (sensor_reactor ENCOURAGE_PHRASES): fired
# while the owner is continuously present (recently seen, not just greeted),
# at most once per this many seconds — stretched by a random x1.0-1.5 each
# time so it reads as personality, not a cron job. 0 disables entirely.
ENCOURAGE_COOLDOWN_S = float(os.environ.get("STACKCHAN_VISION_ENCOURAGE_COOLDOWN_S", str(45 * 60)))

# ── local object commentary (YOLOv4-tiny on the same ambient frames) ──────
# Runs a cheap CPU object-detection pass (COCO 80-class, ~260ms/frame — see
# _detect_objects) on frames the loop already captured for faces, and fires
# sensor_reactor's object_comment behaviour when something NEW shows up.
# "New" is the whole trick: we do NOT narrate a static desk every 8s — a
# label only speaks once, then goes quiet until it has LEFT view for a while
# and come back (see OBJECT_FORGET_S), so a mug that sits there all day is a
# single remark, not a mantra. 0 for the cooldown disables the whole thing.
OBJECT_MODEL_CFG = os.environ.get(
    "STACKCHAN_VISION_OBJECT_CFG", os.path.join(MODELS_DIR, "yolov4-tiny.cfg")
)
OBJECT_MODEL_WEIGHTS = os.environ.get(
    "STACKCHAN_VISION_OBJECT_WEIGHTS", os.path.join(MODELS_DIR, "yolov4-tiny.weights")
)
OBJECT_NAMES_PATH = os.environ.get(
    "STACKCHAN_VISION_OBJECT_NAMES", os.path.join(MODELS_DIR, "coco.names")
)
OBJECT_ENABLED = os.environ.get("STACKCHAN_VISION_OBJECT_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
# 0.4 is deliberately a bit permissive: the device cam is only 320x240, and
# confident desk objects still land around 0.4-0.5 (a remote read 0.42 in
# testing). Raise toward 0.5+ if he starts confidently announcing things
# that aren't there; the novelty gate + global cooldown already cap how
# often any single false positive can speak (once per OBJECT_FORGET_S, and
# never more than once per OBJECT_COOLDOWN_S overall).
OBJECT_CONF_THRESHOLD = float(os.environ.get("STACKCHAN_VISION_OBJECT_CONF", "0.4"))
# A 'person' box at/above this confidence counts as "the user is here" for the
# idle absence clock, even with no detectable face (turned to the side, head
# down working). Kept modestly permissive — a partial torso still reads as a
# person; false "person" hits just make him a bit less likely to feel alone.
PERSON_PRESENCE_CONF = float(os.environ.get("STACKCHAN_VISION_PERSON_PRESENCE_CONF", "0.4"))
OBJECT_NMS_THRESHOLD = float(os.environ.get("STACKCHAN_VISION_OBJECT_NMS", "0.4"))
# Global rate-limit so he doesn't chain object remarks — at most one per
# this many seconds regardless of how many new things appear. 0 disables.
OBJECT_COOLDOWN_S = float(os.environ.get("STACKCHAN_VISION_OBJECT_COOLDOWN_S", str(12 * 60)))
# A label must be ABSENT this long before a re-appearance counts as new and
# is eligible to be remarked on again. Long enough that fidgeting a mug in
# and out of frame doesn't re-trigger it.
OBJECT_FORGET_S = float(os.environ.get("STACKCHAN_VISION_OBJECT_FORGET_S", str(30 * 60)))
# 'person' is handled by the face pipeline; a few COCO classes are noisy or
# not worth a remark (chairs, dining tables everywhere). Skip them.
OBJECT_IGNORE = set(
    x.strip() for x in os.environ.get(
        "STACKCHAN_VISION_OBJECT_IGNORE", "person,chair,dining table,tv,couch,bench"
    ).split(",") if x.strip()
)

# ── messy-desk / clutter commentary ───────────────────────────────────────
# Two triggers, both routed to sensor_reactor's `messy` behaviour:
#  (a) IMPLAUSIBLE detection — the detector "sees" a fridge/oven/car on the
#      desk (it did: a phantom 'refrigerator' at 0.37 on this very cam). That
#      confident-nonsense IS the clutter signal — YOLO grasping at a cluttered
#      frame — and it's the funny bit the user liked. Detected down to
#      IMPLAUSIBLE_FLOOR (lower than real objects: a semi-sure absurd guess
#      still counts). Routed with the {label} so he names the impossible thing.
#  (b) VISUAL CLUTTER — high Canny edge-density. Measured range on this cam:
#      ~0.01 bare tray, ~0.08 busy room. A cluttered desk closeup should sit
#      high; EDGE_THRESHOLD default 0.09 is above 'busy room', so tune DOWN
#      against the real desk if he never bites (logged on every fire). Needs
#      EDGE_MIN_STREAK consecutive ticks so a one-frame fluke can't trigger it.
# Mess is a persistent state, not a novelty, so the shared cooldown is LONG —
# he remarks a couple of times a day, never nags. 0 disables the feature.
CLUTTER_ENABLED = os.environ.get("STACKCHAN_VISION_CLUTTER_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
CLUTTER_COOLDOWN_S = float(os.environ.get("STACKCHAN_VISION_CLUTTER_COOLDOWN_S", str(4 * 60 * 60)))
IMPLAUSIBLE_FLOOR = float(os.environ.get("STACKCHAN_VISION_IMPLAUSIBLE_FLOOR", "0.25"))
CLUTTER_EDGE_THRESHOLD = float(os.environ.get("STACKCHAN_VISION_CLUTTER_EDGE", "0.09"))
CLUTTER_EDGE_MIN_STREAK = int(os.environ.get("STACKCHAN_VISION_CLUTTER_EDGE_STREAK", "2"))
# COCO classes that cannot plausibly be ON a desk — a confident-ish hit here
# is the detector being confused by clutter, not a real object. (Animals like
# cat/dog/bird stay OUT — they're plausible and have their own fun object
# pools; big wildlife and vehicles/appliances/furniture go in.)
OBJECT_IMPLAUSIBLE = set(
    x.strip() for x in os.environ.get(
        "STACKCHAN_VISION_OBJECT_IMPLAUSIBLE",
        "refrigerator,oven,microwave,toaster,sink,toilet,bed,"
        "car,truck,bus,train,boat,airplane,motorcycle,bicycle,"
        "traffic light,fire hydrant,stop sign,parking meter,"
        "elephant,bear,zebra,giraffe,horse,cow,sheep",
    ).split(",") if x.strip()
)

# ── hand gesture recognition (MediaPipe, on the same ambient frames) ──────────
# Stage 1: recognise the 7 built-in MediaPipe gestures + a custom point-
# up/down/left/right derived from the hand landmarks (MediaPipe only labels
# "Pointing_Up"). The device cam is snapshot-based (~POLL_INTERVAL_S apart),
# not a video stream, so this is turn-taking: hold the gesture a beat and the
# next tick catches it. Debounced so one held gesture = one reaction, not a
# stream (fires again only after GESTURE_COOLDOWN_S, or immediately if the
# gesture CHANGES). Fires sensor_reactor's `gesture` behaviour. 0 cooldown
# disables reactions but leave detection on for the Stage-2 teach-object flow.
GESTURE_MODEL = os.environ.get(
    "STACKCHAN_VISION_GESTURE_MODEL", os.path.join(MODELS_DIR, "gesture_recognizer.task")
)
# Default OFF since 2026-07-12 (user: "never seems to work") — set
# STACKCHAN_VISION_GESTURE_ENABLED=1 to re-enable the MediaPipe gesture path.
GESTURE_ENABLED = os.environ.get("STACKCHAN_VISION_GESTURE_ENABLED", "0").strip().lower() not in ("0", "false", "no", "off")
GESTURE_MIN_SCORE = float(os.environ.get("STACKCHAN_VISION_GESTURE_MIN_SCORE", "0.5"))
GESTURE_MIN_HAND_CONF = float(os.environ.get("STACKCHAN_VISION_GESTURE_MIN_HAND_CONF", "0.5"))
GESTURE_COOLDOWN_S = float(os.environ.get("STACKCHAN_VISION_GESTURE_COOLDOWN_S", "20"))
GESTURE_REACT_URL = f"http://127.0.0.1:{CAPTURE_PORT}/react/gesture"
# MediaPipe category_name -> our snake_case canonical gesture name. "Pointing_
# Up" is resolved to a real direction (point_up/down/left/right) from landmarks.
_MP_GESTURE_MAP = {
    "Thumb_Up": "thumb_up", "Thumb_Down": "thumb_down", "Victory": "victory",
    "Closed_Fist": "fist", "Open_Palm": "open_palm", "ILoveYou": "love",
    "Pointing_Up": "point_up",
}
# Only these deliberate, meaningful gestures get a reaction. Anything else —
# an unmapped pose, or a point_left/point_right (which a HAND WORKING ON THE
# DESK trips constantly: fingers out, moving sideways) — is IGNORED, no
# reaction, no "saw a gesture, not sure what it meant" chatter (user: that
# fired far too often off normal desk-work hand movement).
GESTURE_REACTABLE = set(
    x.strip() for x in os.environ.get(
        "STACKCHAN_VISION_GESTURE_REACTABLE",
        "thumb_up,thumb_down,victory,fist,open_palm,love,point_up,point_down",
    ).split(",") if x.strip()
)

# ── ambient object-location memory (ArUco tool markers) ───────────────────
# Every ARUCO_EVERY_N-th executed tick, a near-free DICT_4X4_50 pass runs on
# the SAME frame this tick already captured/decoded for faces. Any marker
# that's in marker_registry.json (the shared id->name map find_item.py owns)
# upserts object_locations.json — {name: {station_mm, ts, method,
# seen_count}} — with the rail carriage's CURRENT station, so Wheatley
# passively learns where tools live just by looking around, and
# find_item.py's memory-first lookup becomes a one-move affair. A sighting is
# only recorded when the rail status is trustworthy (fresh + homed + not
# mid-move): a location without a reliable station is useless — find_item
# drives to that number. File formats and the atomic-write convention are
# find_item.py's; it writes object_locations.json too (last-write-wins is
# fine for a slow-moving inventory). Kill switch: STACKCHAN_VISION_ARUCO=0.
ARUCO_ENABLED = os.environ.get("STACKCHAN_VISION_ARUCO", "1").strip().lower() not in ("0", "false", "no", "off")
ARUCO_EVERY_N = max(1, int(os.environ.get("STACKCHAN_VISION_ARUCO_EVERY_N", "3") or "3"))
MARKER_REGISTRY_PATH = os.environ.get(
    "STACKCHAN_MARKER_REGISTRY", r"C:\Users\domin\Documents\StackChan\marker_registry.json"
)
OBJECT_LOCATIONS_PATH = os.environ.get(
    "STACKCHAN_OBJECT_LOCATIONS", r"C:\Users\domin\Documents\StackChan\object_locations.json"
)
# Trust the bridge's cached rail status only when it's this fresh — the same
# 3s bar stackchan-idle.py applies everywhere it acts on pos_mm (`linked`
# alone just means "ever heard a status this boot").
ARUCO_RAIL_FRESH_MS = float(os.environ.get("STACKCHAN_VISION_ARUCO_RAIL_FRESH_MS", "3000"))
# find_item.py's STATION_DEDUPE_MM convention: within this radius it's the
# "same place" (re-sighting stays silent); beyond it the item MOVED (INFO).
ARUCO_SAME_STATION_MM = 25.0

# Written every tick — stackchan-idle.py reads this to decide whether to
# track a face, search for one, or glance toward motion. See the module
# docstring for why movement lives there and not here.
VISION_STATE_PATH = os.path.join(TEMP, "stackchan-vision-state.json")

# Shared with stackchan-idle.py — holds the auto-learned resting angle.
REST_POSE_PATH = os.path.join(TEMP, "stackchan-rest-pose.json")

# Mount orientation — the SINGLE source of truth, shared with the companion
# server (stackchan_mcp/companion_server.py `_is_upside_down`) via the same
# companion_settings.json, and read by stackchan-idle.py too. When inverted,
# the RAW camera image comes out rotated 180 deg (confirmed via the scan-tray
# ArUco markers reading ~180 deg), so YuNet can't detect an upright-in-world
# face until we rotate frames back. --calibrate-flip sets this from the tray.
SETTINGS_PATH = os.path.join(SCRIPT_DIR, "companion_settings.json")


def _is_upside_down() -> bool:
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if "upside_down" in d:
            return bool(d["upside_down"])
    except Exception:
        pass
    return os.environ.get("STACKCHAN_UPSIDE_DOWN", "").strip().lower() in ("1", "true", "yes", "on")


# Boot-time auto-orientation runs a short camera sweep and can move the head;
# this marker tells stackchan-idle.py to hold still while it does (so the two
# don't fight for the servo at login). Set STACKCHAN_AUTO_ORIENT=0 to disable
# the boot sweep entirely (falls back to the persisted flag).
ORIENTING_MARKER = os.path.join(TEMP, "stackchan-orienting")
AUTO_ORIENT_ENABLED = os.environ.get("STACKCHAN_AUTO_ORIENT", "1").strip().lower() not in ("0", "false", "no", "off")


def _touch_marker(path: str) -> None:
    try:
        with open(path, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def _clear_marker(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass

# Motion detection: cheap frame-differencing (grayscale, downscaled, mean
# absolute difference) — no extra model needed. Only meaningful as a signal
# when no face is in frame (a face already gives idle.py plenty to work
# with); mainly for "something moved, glance over" when nobody recognizable
# is currently visible.
MOTION_DIFF_THRESHOLD = float(os.environ.get("STACKCHAN_VISION_MOTION_THRESHOLD", "12.0"))

# "Lights out" detection — SENSOR ONLY (2026-07-16). Previously approximated
# from the camera's mean brightness, which false-fired on auto-exposure dips /
# a hand over the lens / a dark corner, AND needed a photo (so once the radar
# gate stopped ambient photos it couldn't work in an empty room). Now it reads
# the LTR-553 ambient-light sensor directly (self.light.read, firmware
# 2026-07-12+) on a slow independent poll — no camera involved. Fires on a real
# lit->dark transition: a lit baseline (ch0 >= LUX_LIT_MIN_CH0) then two
# consecutive genuinely-dark reads (ch0 <= LUX_DARK_MAX_CH0), once per cooldown.
# Sensor missing / old firmware / any error -> no lights-out (silent, no guess).
LIGHTS_OUT_HISTORY_LEN = 5
LIGHTS_OUT_COOLDOWN_S = float(os.environ.get("STACKCHAN_VISION_LIGHTS_OUT_COOLDOWN_S", str(30 * 60)))
LIGHTS_LUX_ENABLED = os.environ.get("STACKCHAN_VISION_LIGHTS_LUX", "1").strip() not in ("0", "false", "no", "")
# How often to poll the lux sensor (a device I2C read). Slow on purpose —
# lights-out needs no fine resolution, and this keeps peripheral load off him.
LIGHTS_LUX_POLL_S = float(os.environ.get("STACKCHAN_VISION_LIGHTS_LUX_POLL_S", "30"))
# ch0 (visible) thresholds. Calibrate once real dark/lit samples exist (an
# evening-lit bench read ch0 ~= 4-6; lights fully off should read ~0-1).
LUX_LIT_MIN_CH0 = float(os.environ.get("STACKCHAN_VISION_LUX_LIT_MIN", "3"))
LUX_DARK_MAX_CH0 = float(os.environ.get("STACKCHAN_VISION_LUX_DARK_MAX", "1"))

# Set when an unrecognized face triggers the "who are you?" prompt (see
# sensor_reactor.py's _behavior_recognize unknown branch); stackchan-voice-
# bridge.py checks this to know a tap-to-answer transcript is probably a
# name introduction rather than a normal question.
PENDING_ENROLLMENT_MARKER = os.path.join(TEMP, "stackchan-pending-enrollment")

# Head-settle coordination: stackchan-idle.py touches HEAD_MOVED_MARKER on every
# significant head move. We wait for it to age past HEAD_SETTLE_S before
# capturing so we don't detect on a motion-BLURRED frame (missed faces / false
# gestures / garbage objects). If it's STILL moving after HEAD_SETTLE_MAX_WAIT_S
# (a sustained sweep), skip the tick rather than trust a blur.
HEAD_MOVED_MARKER = os.path.join(TEMP, "stackchan-head-moved")
HEAD_SETTLE_S = float(os.environ.get("STACKCHAN_VISION_HEAD_SETTLE_S", "0.4"))
HEAD_SETTLE_MAX_WAIT_S = float(os.environ.get("STACKCHAN_VISION_HEAD_SETTLE_MAX_WAIT_S", "1.2"))


def _head_settling() -> bool:
    """True if the head moved within the last HEAD_SETTLE_S (still settling)."""
    try:
        with open(HEAD_MOVED_MARKER) as f:
            moved_at = float(f.read().strip())
    except (OSError, ValueError):
        return False
    return time.time() - moved_at < HEAD_SETTLE_S

# ── arbiter: Claude second opinion for genuinely uncertain local matches ──
# SFace's cosine score has no built-in middle ground — this band around
# MATCH_THRESHOLD is where the local model is genuinely unsure and a second
# opinion is worth the API cost; clearly-known or clearly-unknown scores
# never call the arbiter, so this is NOT a per-tick cost (2026-07-02 design
# discussion: costs only apply to real ambiguity, not continuous polling).
UNCERTAIN_LOW = float(os.environ.get("STACKCHAN_VISION_UNCERTAIN_LOW", "0.2"))
UNCERTAIN_HIGH = float(os.environ.get("STACKCHAN_VISION_UNCERTAIN_HIGH", "0.5"))
ARBITER_MODEL = os.environ.get("STACKCHAN_VISION_ARBITER_MODEL", "claude-haiku-4-5-20251001")
# Reuses the voice bridge's key — same Anthropic account, no reason for a
# second one just because this is a different script.
ARBITER_ANTHROPIC_KEY_ENV = "STACKCHAN_VOICE_ANTHROPIC_API_KEY"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

# "Should I remember that view of you?" learning-confirmation prompt: at
# most once per person per this many seconds, independent of the hourly
# greet cooldown, so a run of good-quality uncertain frames can't turn into
# spam. Default 24h (2026-07-02 decision: "once per day per person").
LEARN_CONFIRM_COOLDOWN_S = float(
    os.environ.get("STACKCHAN_VISION_LEARN_CONFIRM_COOLDOWN_S", str(24 * 60 * 60))
)
# Cap growth per person — "a handful of distinct good angles beats many
# marginal ones" (brief's guardrail). Once a name has this many samples,
# stop proposing new ones even if a genuinely new good view shows up.
LEARN_SAMPLE_CAP = int(os.environ.get("STACKCHAN_VISION_LEARN_SAMPLE_CAP", "8"))
# Written when a definite+good arbiter verdict wants to propose learning;
# stackchan-voice-bridge.py checks this on the next tap-to-talk answer.
PENDING_LEARN_CONFIRM_MARKER = os.path.join(TEMP, "stackchan-pending-learn-confirm")

# ── guard mode: challenge unknown people while the owner is away ───────────
# When an UNKNOWN face turns up AND the owner is absent (no known-face
# sighting AND no keyboard/mouse input for GUARD_OWNER_AWAY_S), Wheatley
# challenges them (sensor_reactor's _behavior_guard via /react/guard), saves
# the full frame, and appends a reviewable entry to the visitor log
# (stackchan_mcp/visitor_log.py — event="guard", note, full-frame photo).
# DEFAULT OFF — guests shouldn't get challenged before the user opts in.
# Enable with STACKCHAN_GUARD=1 (gateway/.env or the loop's environment).
# Busy/voice-chat ticks are skipped wholesale by _should_skip_tick, so a
# challenge can never talk over an active conversation by construction.
GUARD_ENABLED = os.environ.get("STACKCHAN_GUARD", "0").strip().lower() not in ("0", "false", "no", "off")
# The owner counts as AWAY only when BOTH signals agree for this long: the
# camera hasn't seen a known face, and the keyboard/mouse have been idle
# (GetLastInputInfo — the same "human at the computer" signal the absence
# greeting uses, so low light alone can't fake a departure).
GUARD_OWNER_AWAY_S = float(os.environ.get("STACKCHAN_GUARD_OWNER_AWAY_S", "300"))
# At most one challenge per this many seconds (global, so rapid episode
# churn can't machine-gun a visitor). Repeats within one episode escalate
# the phrasing (see sensor_reactor.GUARD_REPEAT_PHRASES).
GUARD_COOLDOWN_S = float(os.environ.get("STACKCHAN_GUARD_COOLDOWN_S", "120"))
# An episode (and its phrase escalation counter) resets once NO person has
# been visible for this long; a known face ends it immediately.
GUARD_EPISODE_RESET_S = float(os.environ.get("STACKCHAN_GUARD_EPISODE_RESET_S", "60"))
GUARD_REACT_URL = f"http://127.0.0.1:{CAPTURE_PORT}/react/guard"

# Shared with the idle loop / led-chase / voice bridge — skip a tick rather
# than fight Claude Code's active work or an in-progress voice exchange.
BUSY_MARKER = os.path.join(TEMP, "stackchan-busy")
BUSY_STALE_S = 30 * 60
# Per-session Claude Code markers (stackchan-busy-<session_id>, written by
# stackchan-hook.py) AND the gateway's stackchan-busy-devicechat (device is
# LISTENING / mid voice-chat turn) — same glob convention as
# stackchan-idle.py's BUSY_MARKER_GLOB. Only markers younger than 120s
# count here: the gateway refreshes its marker every ~15s while a turn is
# live, so anything older is an orphan and must not freeze captures.
BUSY_MARKER_GLOB = os.path.join(TEMP, "stackchan-busy-*")
# Keep detecting while look_at is tracking, so the head-mounted camera can
# camera-verify radar tracks (personhood). Default off — enable together with
# STACKCHAN_LOOKAT_CAMERA_VERIFY and a faster poll, then walk-test.
TRACK_ASSIST = os.environ.get("STACKCHAN_VISION_TRACK_ASSIST", "0").strip() not in ("0", "false", "no", "")
BUSY_GLOB_STALE_S = 120.0
VOICE_THINKING_MARKER = os.path.join(TEMP, "stackchan-voice-thinking")
VOICE_STALE_S = 90
# Convenience pause switch — touch this file to stop captures without
# killing the process (e.g. for privacy), remove it to resume.
PAUSE_MARKER = os.path.join(TEMP, "stackchan-vision-paused")

# ── Radar-gated camera (2026-07-16, TRACKING_PLAN "vision/camera architecture") ──
# The camera fires ONLY when radar sees a person AND we're due to (re)identify — NOT
# every POLL_INTERVAL_S. Empty room → zero camera, which kills the #36 camera-load
# hard-lockup at SOURCE (the old every-8s poll dropped his ping 2567ms→8ms and crash-
# looped him). While someone's present: identify once, re-check every CAMERA_RECHECK_S;
# a tracker/behaviour request (CAMERA_REQUEST_MARKER, fresh) forces an immediate shot.
RADAR_GATE = os.environ.get("STACKCHAN_VISION_RADAR_GATE", "1").strip() not in ("0", "false", "no", "")
CAMERA_RECHECK_S = float(os.environ.get("STACKCHAN_VISION_CAMERA_RECHECK_S", "45"))
IDLE_POLL_S = float(os.environ.get("STACKCHAN_VISION_IDLE_POLL_S", "6"))
CAMERA_REQUEST_MARKER = os.path.join(TEMP, "stackchan-vision-request")

# ── Idle ambient scans (2026-07-16) ───────────────────────────────────────
# While the room is EMPTY (radar healthy + no target), occasionally spend ONE
# photo on a passive, no-face, no-speech pass — ArUco tool/tag locations into
# object_locations.json + keep object-novelty warm — so passive tool-location
# learning survives the radar gate (which otherwise means zero photos, hence
# zero looking-around, whenever nobody is in front of him). Deliberately SLOW
# (default 180s) and head-settle gated, so it can't recreate the #36 photo-flood
# lockup; skipped entirely when radar is UNAVAILABLE (a radar error usually
# means the device is offline, and take_photo would just stall on its timeout).
AMBIENT_SCAN_ENABLED = os.environ.get("STACKCHAN_VISION_AMBIENT_SCAN", "1").strip() not in ("0", "false", "no", "")
AMBIENT_SCAN_S = float(os.environ.get("STACKCHAN_VISION_AMBIENT_SCAN_S", "180"))


def _marker_active(path: str, stale_s: float) -> bool:
    try:
        with open(path) as f:
            written_at = float(f.read().strip())
    except (OSError, ValueError):
        return False
    if time.time() - written_at > stale_s:
        try:
            os.remove(path)
        except OSError:
            pass
        return False
    return True


def _any_recent_busy_marker() -> bool:
    """True if any stackchan-busy-* marker is younger than 120s.

    Unlike :func:`_marker_active` this never deletes the files — they
    belong to other processes (Claude Code sessions, the gateway) which
    manage their own lifecycles. Content is a float unix timestamp; fall
    back to mtime if it isn't (stale-proof either way).
    """
    now = time.time()
    for path in glob.glob(BUSY_MARKER_GLOB):
        # Camera-verify assist (STACKCHAN_VISION_TRACK_ASSIST, default off):
        # keep detecting (and publishing vision-state) while look_at is
        # tracking, so its head-mounted camera can grant personhood to a
        # radar track. Without this the loop pauses on busy-lookat and
        # verification never gets a fresh frame. Default off = unchanged.
        if (TRACK_ASSIST
                and os.path.basename(path) == "stackchan-busy-lookat"):
            continue
        try:
            with open(path) as f:
                written_at = float(f.read().strip())
        except (OSError, ValueError):
            try:
                written_at = os.path.getmtime(path)
            except OSError:
                continue
        if now - written_at < BUSY_GLOB_STALE_S:
            return True
    return False


def _should_skip_tick() -> bool:
    if os.path.exists(PAUSE_MARKER):
        return True
    if _marker_active(BUSY_MARKER, BUSY_STALE_S):
        return True
    if _any_recent_busy_marker():
        return True
    if _marker_active(VOICE_THINKING_MARKER, VOICE_STALE_S):
        return True
    return False


class MCPSession:
    def __init__(self, url, timeout=30):
        self.url = url
        self.timeout = timeout
        self.session_id = None
        self._next_id = 1

    def _post(self, payload, timeout=None):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        resp = urllib.request.urlopen(req, timeout=timeout or self.timeout)
        sid = resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        body = resp.read()
        return json.loads(body) if body.strip() else None

    def initialize(self):
        self._post({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "stackchan-vision-loop", "version": "1.0"},
            },
        })
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, name, arguments, timeout=None):
        call_id = self._next_id
        self._next_id += 1
        return self._post(
            {"jsonrpc": "2.0", "id": call_id, "method": "tools/call",
             "params": {"name": name, "arguments": arguments}},
            timeout=timeout,
        )


_RADAR_WARN = {"ts": 0.0}
RADAR_WARN_COOLDOWN_S = float(os.environ.get("STACKCHAN_VISION_RADAR_WARN_COOLDOWN_S", "300"))


def _warn_radar(msg: str) -> None:
    """Throttled WARNING when the radar read fails / reports not-ok — so a dead
    radar (which silently pauses all presence-gated camera work) is
    distinguishable in the log from a genuinely empty room. A clean empty read
    never warns. At most once per RADAR_WARN_COOLDOWN_S."""
    now = time.time()
    if now - _RADAR_WARN["ts"] >= RADAR_WARN_COOLDOWN_S:
        _RADAR_WARN["ts"] = now
        logger.warning("radar unavailable (%s) — presence-gated camera paused until it recovers", msg)


def _radar_read(sess: MCPSession) -> tuple[bool, bool]:
    """Read the LD2450 radar. Cheap — no camera. Returns (ok, present):
      ok=False      = radar unavailable (error / ok:false / no content),
      present=True  = at least one target in view.
    Fail-safe: ANY problem -> (False, False), so a flaky radar means NO photo
    (fail toward no load), never a spurious burst. An error (as opposed to a
    clean empty read) additionally logs a throttled warning via _warn_radar."""
    try:
        result = sess.call_tool("self.presence.read", {}, timeout=5)
        content = ((result or {}).get("result") or {}).get("content", [])
        if not content:
            _warn_radar("no content")
            return (False, False)
        info = json.loads(content[0].get("text", "") or "{}")
        if not info.get("ok"):
            _warn_radar("presence.read ok=false")
            return (False, False)
        return (True, len(info.get("targets") or []) > 0)
    except Exception as exc:
        _warn_radar(f"presence.read failed: {exc}")
        return (False, False)


def _camera_request_fresh() -> bool:
    """A tracker/behaviour request for a fresh identify shot: CAMERA_REQUEST_MARKER
    touched within the last 30 s. Consumed (deleted) on read so it fires once."""
    try:
        if time.time() - os.path.getmtime(CAMERA_REQUEST_MARKER) < 30.0:
            try:
                os.remove(CAMERA_REQUEST_MARKER)
            except OSError:
                pass
            return True
    except OSError:
        pass
    return False


def _camera_decision(sess: MCPSession, last_photo_ts: float,
                     last_ambient_ts: float, once: bool) -> str | None:
    """Radar-gated camera decision (TRACKING_PLAN §0.5), extended with idle
    ambient scans. Returns one of:
      "identify" — take a full face/greeting photo (person present & due, an
                   explicit request, or gate disabled / single-run).
      "ambient"  — take a passive no-face scan (room empty, radar healthy, and
                   AMBIENT_SCAN_S elapsed): learn tool/tag locations, no speech.
      None       — no photo this cycle (present but identity still fresh, radar
                   down, or not yet due) — caller just does the cheap radar poll.
    """
    if once or not RADAR_GATE:
        return "identify"
    if _camera_request_fresh():
        return "identify"
    ok, present = _radar_read(sess)
    if present:
        return "identify" if (time.time() - last_photo_ts) >= CAMERA_RECHECK_S else None
    # Room empty. Passive ambient scan only when radar is HEALTHY — a radar
    # error usually means the device is offline, and we must not stall on a
    # take_photo timeout every cycle chasing tags nobody can see.
    if ok and AMBIENT_SCAN_ENABLED and (time.time() - last_ambient_ts) >= AMBIENT_SCAN_S:
        return "ambient"
    return None


def _take_photo(sess: MCPSession, question: str) -> str | None:
    """Call the gateway's take_photo tool; return a local JPEG path or None."""
    try:
        result = sess.call_tool("take_photo", {"question": question}, timeout=20)
        content = ((result or {}).get("result") or {}).get("content", [])
        if not content:
            logger.warning("take_photo returned no content: %r", result)
            return None
        info = json.loads(content[0].get("text", "") or "{}")
        image_path = info.get("image_path")
        if not image_path or not os.path.exists(image_path):
            logger.warning("take_photo result missing image_path: %r", info)
            return None
        return image_path
    except Exception:
        logger.exception("take_photo via MCP failed")
        return None


def _fire_reaction(person: str, name: str | None = None, propose_learn: bool = False) -> None:
    # Forward the recognized name so the greeting can actually use it —
    # recognition always knew WHO it matched, but only "known"/"unknown"
    # used to survive this hop, so greetings were stuck generic.
    url = f"{REACT_URL}?person={person}"
    if name:
        url += "&name=" + urllib.parse.quote(name)
    if propose_learn:
        url += "&learn=1"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            logger.info("fired react/recognize?person=%s -> %s", person, resp.status)
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            logger.info("react/recognize?person=%s skipped (reactor busy)", person)
        else:
            logger.warning("react/recognize?person=%s failed: HTTP %s", person, exc.code)
    except Exception:
        logger.exception("react/recognize?person=%s failed", person)


def _fire_encourage(name: str | None = None) -> None:
    url = ENCOURAGE_REACT_URL
    if name:
        url += "?name=" + urllib.parse.quote(name)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            logger.info("fired react/encourage -> %s", resp.status)
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            logger.info("react/encourage skipped (reactor busy)")
        else:
            logger.warning("react/encourage failed: HTTP %s", exc.code)
    except Exception:
        logger.exception("react/encourage failed")


def _fire_object_comment(label: str, direction: str = "center") -> None:
    url = OBJECT_REACT_URL + "?label=" + urllib.parse.quote(label)
    if direction and direction != "center":
        url += "&direction=" + urllib.parse.quote(direction)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            logger.info("fired react/object_comment label=%s -> %s", label, resp.status)
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            logger.info("react/object_comment label=%s skipped (reactor busy)", label)
        else:
            logger.warning("react/object_comment label=%s failed: HTTP %s", label, exc.code)
    except Exception:
        logger.exception("react/object_comment label=%s failed", label)


def _fire_messy(label: str = "", direction: str = "center") -> None:
    url = MESSY_REACT_URL
    params = []
    if label:
        params.append("label=" + urllib.parse.quote(label))
    if direction and direction != "center":
        params.append("direction=" + urllib.parse.quote(direction))
    if params:
        url += "?" + "&".join(params)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            logger.info("fired react/messy label=%s -> %s", label or "-", resp.status)
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            logger.info("react/messy skipped (reactor busy)")
        else:
            logger.warning("react/messy failed: HTTP %s", exc.code)
    except Exception:
        logger.exception("react/messy failed")


def _pick_new_object(detections: list, object_last_seen: dict, now: float) -> tuple | None:
    """Given this tick's detections (label, conf, direction, implausible) and
    the per-label last-seen timestamps, refresh all last-seen stamps and
    return the single best NEW object — one not seen within OBJECT_FORGET_S —
    or None. Highest-confidence new label wins (implausible or not; the caller
    routes on the implausible flag)."""
    chosen = None
    for label, conf, direction, implausible in detections:
        last = object_last_seen.get(label, 0.0)
        is_new = now - last >= OBJECT_FORGET_S
        object_last_seen[label] = now  # seen this tick, regardless
        if is_new and chosen is None:
            chosen = (label, conf, direction, implausible)
    return chosen


def _maybe_fire_object(new_object, last_object_comment: list, now: float) -> bool:
    """Fire a queued object comment if the global anti-chatter cooldown
    allows. `new_object` is a (label, conf, direction) 3-tuple. `last_object_
    comment` is a 1-element mutable timestamp holder. A comment blocked by the
    cooldown is simply dropped (novelty was already consumed by
    _pick_new_object), NOT queued — better to miss one remark than to build a
    backlog that fires long after the thing appeared."""
    if not new_object or OBJECT_COOLDOWN_S <= 0:
        return False
    if now - last_object_comment[0] < OBJECT_COOLDOWN_S:
        return False
    label, conf, direction = new_object
    last_object_comment[0] = now
    logger.info("firing object comment label=%s conf=%.2f dir=%s", label, conf, direction)
    _fire_object_comment(label, direction)
    return True


def _comment_on_scene(new_object, edge_frac, messy_state: dict,
                      last_object_comment: list, now: float) -> bool:
    """Lowest-priority ambient commentary, run once per tick after greeting/
    encourage have had their say. Priority within: an implausible 'confused'
    detection > a real new object > general visual clutter. Returns True if
    he spoke (so the caller doesn't stack another remark)."""
    # (a) implausible detection -> confused-by-mess bit, naming the absurdity
    if new_object is not None:
        label, conf, direction, implausible = new_object
        if implausible:
            if _maybe_fire_messy(messy_state, now, label=label, direction=direction):
                return True
        elif _maybe_fire_object((label, conf, direction), last_object_comment, now):
            return True
    # (b) general visual clutter (stable over CLUTTER_EDGE_MIN_STREAK ticks)
    return _maybe_fire_messy(messy_state, now, edge_frac=edge_frac)


def _maybe_fire_messy(messy_state: dict, now: float, label: str = "",
                      direction: str = "center", edge_frac: float | None = None) -> bool:
    """Fire a messy-desk remark, shared long cooldown across both triggers.
    `label` set = implausible-object 'confused' bit (novelty already applied
    upstream). `edge_frac` set = general-clutter bit, which additionally
    requires the streak (maintained in messy_state by the caller) so a single
    busy frame can't trigger it."""
    if not CLUTTER_ENABLED or CLUTTER_COOLDOWN_S <= 0:
        return False
    if now - messy_state["ts"] < CLUTTER_COOLDOWN_S:
        return False
    if label:
        messy_state["ts"] = now
        logger.info("firing messy(confused) label=%s", label)
        _fire_messy(label=label, direction=direction)
        return True
    if edge_frac is not None and messy_state["streak"] >= CLUTTER_EDGE_MIN_STREAK:
        messy_state["ts"] = now
        logger.info("firing messy(clutter) edge=%.3f streak=%d", edge_frac, messy_state["streak"])
        _fire_messy()
        return True
    return False


def _seconds_since_user_input() -> float | None:
    """Seconds since the last keyboard/mouse input on this Windows session, or
    None if unavailable (non-Windows / API failure). Uses GetLastInputInfo — a
    strong 'a human is physically at the computer' signal that works in the
    dark, unlike face detection. GetTickCount wraps ~every 49.7 days; dwTime is
    the same clock, so the subtraction stays valid over the short windows we
    care about."""
    try:
        import ctypes

        class _LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        now_ticks = ctypes.windll.kernel32.GetTickCount()
        idle_ms = (now_ticks - info.dwTime) & 0xFFFFFFFF
        return idle_ms / 1000.0
    except Exception:
        return None


def _user_present_hint(motion_detected: bool) -> bool:
    """True if there's non-camera evidence a human is present right now:
    recent keyboard/mouse input, or motion in the frame. Used to avoid
    counting a face-less tick as absence (see PRESENCE_INPUT_WINDOW_S)."""
    if motion_detected:
        return True
    idle = _seconds_since_user_input()
    return idle is not None and idle <= PRESENCE_INPUT_WINDOW_S


def _note_look(seen_key: str | None, last_seen_ts: dict, missed_looks: dict,
               suppress_absence: bool = False) -> None:
    """Bookkeeping for absence-based greeting: on every EXECUTED tick, bump
    the missed-look counter for each known-seen-before name that was NOT the
    one recognized, and reset the one that was. Ticks that never run (busy /
    paused / device offline) bump nothing — 'absent' must mean "we actually
    looked and they weren't there", not "we weren't looking".

    `suppress_absence` freezes the counters (no bump) when we DID look and saw
    no face but have other evidence a human is here (recent input / motion) —
    so low light losing the face doesn't fake a departure. A real sighting
    (seen_key set) still resets that person's counter regardless."""
    if not suppress_absence:
        for known_name in list(last_seen_ts):
            if known_name != seen_key:
                missed_looks[known_name] = missed_looks.get(known_name, 0) + 1
    if seen_key is not None:
        missed_looks[seen_key] = 0


def _log_visitor_safe(recognizer, img, face, name, known, score) -> None:
    """Append a visitor-log entry with a small face thumbnail.

    Fire-and-forget: a logging failure must never disrupt recognition. The
    thumbnail is the aligned 112x112 crop (same one used for reference photos),
    JPEG-encoded here so stackchan_mcp.visitor_log needs no OpenCV.
    """
    try:
        import cv2
        from stackchan_mcp import visitor_log

        crop = recognizer.alignCrop(img, face)
        ok, buf = cv2.imencode(".jpg", crop)
        thumb = buf.tobytes() if ok else None
        visitor_log.append(name, known, float(score), thumb)
    except Exception:
        logger.debug("visitor log append failed", exc_info=True)


# ─── guard mode (see GUARD_* config above) ──────────────────────────────────

def _guard_note_tick(guard_state: dict, now: float,
                     person_visible: bool, known_seen: bool) -> None:
    """Per-tick guard bookkeeping, called on every EXECUTED tick (skipped
    busy/paused ticks bump nothing, same reasoning as _note_look).

    `person_visible` = any face OR a YOLO 'person' box this tick — keeps the
    episode alive while someone is demonstrably still at the bench even if
    their face turns away. `known_seen` = an enrolled face was recognized,
    which both refreshes the owner-presence clock and ends any active
    episode immediately (the owner returning stands the guard down)."""
    if person_visible:
        guard_state["last_person_seen_ts"] = now
    if known_seen:
        guard_state["last_known_seen_ts"] = now
        if guard_state["episode_active"]:
            logger.info("guard: known face seen — standing down (episode over)")
        guard_state["episode_active"] = False
        guard_state["challenges"] = 0
    elif (
        guard_state["episode_active"]
        and now - guard_state["last_person_seen_ts"] >= GUARD_EPISODE_RESET_S
    ):
        logger.info(
            "guard: nobody visible for %.0fs — episode reset", GUARD_EPISODE_RESET_S
        )
        guard_state["episode_active"] = False
        guard_state["challenges"] = 0


def _guard_owner_away(guard_state: dict, now: float) -> bool:
    """True when the owner reads as ABSENT: no known-face sighting AND no
    keyboard/mouse input for GUARD_OWNER_AWAY_S. Input reading failing
    (non-Windows/API error) counts as no input evidence — same 'can't prove
    presence' semantics as _user_present_hint, erring here toward the
    challenge only because the known-face clock must ALSO agree."""
    if now - guard_state["last_known_seen_ts"] < GUARD_OWNER_AWAY_S:
        return False
    idle = _seconds_since_user_input()
    if idle is not None and idle < GUARD_OWNER_AWAY_S:
        return False
    return True


def _fire_guard(repeat: int) -> None:
    url = GUARD_REACT_URL + (f"?repeat={repeat}" if repeat > 1 else "")
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            logger.info("fired react/guard repeat=%d -> %s", repeat, resp.status)
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            logger.info("react/guard skipped (reactor busy)")
        else:
            logger.warning("react/guard failed: HTTP %s", exc.code)
    except Exception:
        logger.exception("react/guard failed")


def _maybe_fire_guard(guard_state: dict, recognizer, img, face,
                      score: float, now: float) -> bool:
    """Fire one guard challenge if the per-challenge cooldown allows.

    Caller has already established: guard enabled, unknown face in frame,
    owner away (_guard_owner_away). This function owns the cooldown, the
    episode escalation counter, the evidence (full oriented frame + aligned
    face crop into the visitor log, event="guard" + note) and the
    /react/guard trigger. Evidence failures never block the spoken
    challenge — the deterrent matters more than the paperwork."""
    if now - guard_state["last_challenge_ts"] < GUARD_COOLDOWN_S:
        return False
    guard_state["episode_active"] = True
    guard_state["challenges"] += 1
    guard_state["last_challenge_ts"] = now
    challenge_n = guard_state["challenges"]
    away_for = now - guard_state["last_known_seen_ts"]
    try:
        import cv2
        from stackchan_mcp import visitor_log

        ok_full, full_buf = cv2.imencode(".jpg", img)
        crop = recognizer.alignCrop(img, face)
        ok_thumb, thumb_buf = cv2.imencode(".jpg", crop)
        note = (
            f"guard challenge #{challenge_n}: unknown person at the bench, "
            f"owner unseen for {away_for / 60.0:.0f} min"
        )
        entry = visitor_log.append(
            None, False, float(score),
            thumb_buf.tobytes() if ok_thumb else None,
            event="guard",
            note=note,
            photo_jpeg=full_buf.tobytes() if ok_full else None,
        )
        logger.info(
            "guard: challenge #%d (owner unseen %.0fs) — logged entry id=%s photo=%s",
            challenge_n, away_for,
            (entry or {}).get("id"), (entry or {}).get("photo"),
        )
    except Exception:
        logger.exception("guard: evidence capture/log failed (still challenging)")
    _fire_guard(challenge_n)
    return True


# Owner name for the is_owner flag written into the shared vision state — the
# SAME convention sensor_reactor.py uses (STACKCHAN_OWNER_NAME, default
# "Dominic"), compared case-insensitively, so the ContextEngine's OWNER context
# and the reactor's greeting agree on who "the owner" is.
OWNER_NAME = os.environ.get("STACKCHAN_OWNER_NAME", "Dominic")


def _write_vision_state(
    face_visible: bool, person: str | None, name: str | None,
    dx: float, dy: float, motion_detected: bool, present: bool = False,
) -> None:
    # is_owner is derived here (not passed in) so BOTH call sites populate it for
    # free: true only when a recognised name equals OWNER_NAME. Guarded str ops
    # so a malformed name can never break the vision tick's state write.
    try:
        is_owner = bool(name) and name.strip().lower() == OWNER_NAME.strip().lower()
    except Exception:
        is_owner = False
    state = {
        "ts": time.time(),
        "face_visible": face_visible,
        "person": person,
        "name": name,
        "is_owner": is_owner,
        "dx": dx,
        "dy": dy,
        "motion_detected": motion_detected,
        # Broader "the user is here" than face_visible: true if a face is seen
        # OR a person is in frame (YOLO — covers working turned-to-the-side)
        # OR there's been recent keyboard/mouse input. The idle loop uses THIS
        # for its absence clock so it doesn't decide you're gone just because
        # your face turned away. (Raw motion is NOT used — his own head
        # movement between snapshots dominates the frame diff.)
        "present": present,
    }
    try:
        tmp = VISION_STATE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, VISION_STATE_PATH)
    except Exception:
        logger.exception("failed to write vision state")


def _write_pending_enrollment_marker() -> None:
    try:
        with open(PENDING_ENROLLMENT_MARKER, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def _write_pending_learn_confirm_marker(name: str, frame_path: str) -> None:
    """The frame itself is referenced here rather than recaptured — it's
    already the exact frame the arbiter judged "definite + good", so
    voice-bridge.py's confirmation flow reuses it directly for the
    embedding rather than taking a fresh (possibly worse) shot."""
    try:
        with open(PENDING_LEARN_CONFIRM_MARKER, "w") as f:
            json.dump({"name": name, "frame_path": frame_path, "ts": time.time()}, f)
    except Exception:
        pass


def _small_gray(img):
    import cv2
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return cv2.resize(gray, (80, 60))


def _motion_score(prev_small, cur_small) -> float:
    import numpy as np
    if prev_small is None:
        return 0.0
    diff = np.abs(cur_small.astype(np.int16) - prev_small.astype(np.int16))
    return float(diff.mean())


def _read_lux_ch0(sess: "MCPSession") -> float | None:
    """The LTR-553's ch0 (visible) count, or None if the sensor is missing /
    firmware too old / any error — the caller treats None as 'no reading',
    never as dark, so a missing sensor can never fire a false lights-out."""
    try:
        resp = sess.call_tool("self.light.read", {}, timeout=6)
        text = (((resp or {}).get("result") or {}).get("content") or [{}])[0].get("text", "{}")
        d = json.loads(text)
        if not d.get("ok"):
            return None
        return float(d.get("ch0", 0))
    except Exception:
        return None


def _check_lights_out_lux(sess: "MCPSession", lux_state: dict) -> None:
    """Sensor-only lights-out: on a slow LIGHTS_LUX_POLL_S cadence, read the lux
    ch0 and fire /react/lights_out on a genuine lit->dark transition — a lit
    baseline then two consecutive dark reads, at most once per
    LIGHTS_OUT_COOLDOWN_S. `lux_state` = {"history": [...], "ts": last_fire,
    "poll_ts": last_poll}. Independent of the camera, so it works in an empty
    (dark) room too. Never raises out to the loop."""
    if not LIGHTS_LUX_ENABLED or LIGHTS_OUT_COOLDOWN_S <= 0:
        return
    now = time.time()
    if now - lux_state.get("poll_ts", 0.0) < LIGHTS_LUX_POLL_S:
        return
    lux_state["poll_ts"] = now
    ch0 = _read_lux_ch0(sess)
    if ch0 is None:
        return
    hist = lux_state.setdefault("history", [])
    triggered = False
    if len(hist) >= 3:
        # hist[-1] is the previous read; the baseline excludes it so the "was
        # it lit before?" check isn't dragged down by the first dark read of
        # the transition we're confirming (mirrors the old camera logic).
        prev = hist[-1]
        older = hist[:-1]
        baseline = sum(older) / len(older)
        if (
            ch0 <= LUX_DARK_MAX_CH0
            and prev <= LUX_DARK_MAX_CH0
            and baseline >= LUX_LIT_MIN_CH0
            and now - lux_state.get("ts", 0.0) >= LIGHTS_OUT_COOLDOWN_S
        ):
            lux_state["ts"] = now
            triggered = True
    hist.append(ch0)
    del hist[:-LIGHTS_OUT_HISTORY_LEN]
    if triggered:
        logger.info("lights-out (lux): ch0=%.1f fell from a lit baseline (>= %.1f)", ch0, LUX_LIT_MIN_CH0)
        _fire_lights_out()


def _fire_lights_out() -> None:
    url = f"http://127.0.0.1:{CAPTURE_PORT}/react/lights_out"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            logger.info("fired react/lights_out -> %s", resp.status)
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            logger.info("react/lights_out skipped (reactor busy)")
        else:
            logger.warning("react/lights_out failed: HTTP %s", exc.code)
    except Exception:
        logger.exception("react/lights_out failed")


# ─── face detection / recognition ──────────────────────────────────────────

def _load_detector():
    import cv2
    return cv2.FaceDetectorYN.create(YUNET_MODEL, "", (320, 320))


def _load_recognizer():
    import cv2
    return cv2.FaceRecognizerSF.create(SFACE_MODEL, "")


def _load_object_model():
    """YOLOv4-tiny COCO detector, or None if disabled / files missing.
    Returns (DetectionModel, class_names) so a missing model just quietly
    turns the feature off rather than crashing the whole vision loop."""
    if not OBJECT_ENABLED:
        return None
    if not (os.path.exists(OBJECT_MODEL_CFG) and os.path.exists(OBJECT_MODEL_WEIGHTS)
            and os.path.exists(OBJECT_NAMES_PATH)):
        logger.warning(
            "object detection enabled but model files missing (%s / %s / %s) — disabling",
            OBJECT_MODEL_CFG, OBJECT_MODEL_WEIGHTS, OBJECT_NAMES_PATH,
        )
        return None
    try:
        import cv2
        net = cv2.dnn.readNetFromDarknet(OBJECT_MODEL_CFG, OBJECT_MODEL_WEIGHTS)
        model = cv2.dnn.DetectionModel(net)
        model.setInputParams(size=(416, 416), scale=1 / 255.0, swapRB=True)
        with open(OBJECT_NAMES_PATH, encoding="utf-8") as f:
            names = [ln.strip() for ln in f if ln.strip()]
        logger.info("object detector loaded (%d COCO classes)", len(names))
        return (model, names)
    except Exception:
        logger.exception("failed to load object detector — disabling")
        return None


def _detect_objects(object_model, img):
    """Return (objects, person_present). `objects` is a deduped list of
    (label, confidence, direction, implausible), best-confidence-first, one box
    per label (real objects need OBJECT_CONF_THRESHOLD; IMPLAUSIBLE classes
    count from the lower IMPLAUSIBLE_FLOOR). `person_present` is True if a
    'person' box is in frame at >= PERSON_PRESENCE_CONF — a presence signal for
    the idle absence clock, independent of face detection (a person turned to
    the side has no detectable face but is very much still here)."""
    if object_model is None:
        return [], False
    model, names = object_model
    # Run at the lowest floor either path needs, then apply the per-label
    # floor below (so one detect() pass serves both real + implausible).
    floor = min(OBJECT_CONF_THRESHOLD, IMPLAUSIBLE_FLOOR, PERSON_PRESENCE_CONF)
    try:
        classes, scores, boxes = model.detect(
            img, confThreshold=floor, nmsThreshold=OBJECT_NMS_THRESHOLD
        )
    except Exception:
        logger.debug("object detect failed", exc_info=True)
        return [], False
    frame_w = img.shape[1]
    best: dict = {}
    person_present = False
    for cls, score, box in zip(classes, scores, boxes):
        label = names[int(cls)] if 0 <= int(cls) < len(names) else None
        if not label:
            continue
        conf = float(score)
        if label == "person" and conf >= PERSON_PRESENCE_CONF:
            person_present = True
        if label in OBJECT_IGNORE:
            continue
        implausible = label in OBJECT_IMPLAUSIBLE
        if conf < (IMPLAUSIBLE_FLOOR if implausible else OBJECT_CONF_THRESHOLD):
            continue
        if label in best and conf <= best[label][0]:
            continue
        cx = box[0] + box[2] / 2
        frac = cx / frame_w
        direction = "left" if frac < 0.38 else "right" if frac > 0.62 else "center"
        best[label] = (conf, direction, implausible)
    objects = sorted(
        ((lbl, c, d, im) for lbl, (c, d, im) in best.items()),
        key=lambda t: t[1], reverse=True,
    )
    return objects, person_present


def _edge_fraction(img) -> float:
    """Fraction of Canny edge pixels — a cheap 'visual busyness' proxy.
    ~0.01 for a bare surface, ~0.08 for a busy room, higher for real clutter.
    The messy-desk trigger's (b) signal; see CLUTTER_EDGE_THRESHOLD."""
    import cv2
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 60, 160)
    return float((edges > 0).mean())


# ─── hand gesture recognition (MediaPipe) ───────────────────────────────────

def _load_gesture_model():
    """MediaPipe GestureRecognizer (IMAGE mode), or None if disabled / missing
    / mediapipe unavailable — so a missing model quietly turns gestures off
    rather than crashing the loop (same fail-safe as _load_object_model)."""
    if not GESTURE_ENABLED:
        return None
    if not os.path.exists(GESTURE_MODEL):
        logger.warning("gesture recognition enabled but model missing (%s) — disabling", GESTURE_MODEL)
        return None
    try:
        from mediapipe.tasks.python import vision, BaseOptions
        opts = vision.GestureRecognizerOptions(
            base_options=BaseOptions(model_asset_path=GESTURE_MODEL),
            num_hands=1,
            min_hand_detection_confidence=GESTURE_MIN_HAND_CONF,
            min_hand_presence_confidence=GESTURE_MIN_HAND_CONF,
        )
        model = vision.GestureRecognizer.create_from_options(opts)
        logger.info("gesture recognizer loaded")
        return model
    except Exception:
        logger.exception("failed to load gesture recognizer — disabling")
        return None


def _pointing_direction(landmarks) -> str | None:
    """From the 21 hand landmarks, if this is a single-finger point (index
    extended, the other three fingers curled), return point_up/down/left/
    right from the index-finger vector. Else None. Landmarks are normalized
    [0,1] with origin TOP-LEFT, so +y is downward on screen."""
    try:
        tip, pip, mcp = landmarks[8], landmarks[6], landmarks[5]
        # index extended = tip clearly beyond the pip joint from the mcp
        # (distance tip->mcp bigger than pip->mcp). Other fingers curled =
        # their tips closer to the wrist than their pips.
        def _curled(tip_i, pip_i):
            return _dist(landmarks[tip_i], landmarks[0]) < _dist(landmarks[pip_i], landmarks[0])
        index_extended = _dist(tip, mcp) > _dist(pip, mcp) * 1.15
        others_curled = sum(_curled(t, p) for t, p in ((12, 10), (16, 14), (20, 18))) >= 2
        if not (index_extended and others_curled):
            return None
        dx, dy = tip.x - mcp.x, tip.y - mcp.y
        if abs(dy) >= abs(dx):
            return "point_up" if dy < 0 else "point_down"
        # camera image is un-mirrored world-upright here; +x = user's left in
        # frame, but for a point we just report screen-left/right.
        return "point_right" if dx > 0 else "point_left"
    except Exception:
        return None


def _dist(a, b) -> float:
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5


def _detect_gesture(gesture_model, img):
    """Return (gesture_name, score) for the top hand gesture in `img`, or None.
    MediaPipe's 7 built-ins are snake_cased; a 'Pointing_Up' (or any single-
    finger point) is refined to point_up/down/left/right via the landmarks."""
    if gesture_model is None:
        return None
    try:
        import cv2
        import mediapipe as mp
        rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        res = gesture_model.recognize(mp_img)
    except Exception:
        logger.debug("gesture recognize failed", exc_info=True)
        return None
    if not res.gestures or not res.gestures[0]:
        return None
    top = res.gestures[0][0]
    name = _MP_GESTURE_MAP.get(top.category_name)
    score = float(top.score)
    landmarks = res.hand_landmarks[0] if res.hand_landmarks else None
    # Refine any pointing pose (MediaPipe only ever labels Pointing_Up) into a
    # real direction — this is how we get point_down, which has no built-in.
    if landmarks is not None:
        pointed = _pointing_direction(landmarks)
        if pointed is not None:
            return (pointed, max(score, GESTURE_MIN_SCORE))
    if name is None or top.category_name == "None" or score < GESTURE_MIN_SCORE:
        return None
    return (name, score)


def _fire_gesture(name: str) -> None:
    url = GESTURE_REACT_URL + "?label=" + urllib.parse.quote(name)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            logger.info("fired react/gesture %s -> %s", name, resp.status)
    except urllib.error.HTTPError as exc:
        if exc.code == 409:
            logger.info("react/gesture %s skipped (reactor busy)", name)
        else:
            logger.warning("react/gesture %s failed: HTTP %s", name, exc.code)
    except Exception:
        logger.exception("react/gesture %s failed", name)


def _maybe_fire_gesture(detected, gesture_state: dict, now: float):
    """Debounce hand gestures: fire on a NEW gesture, or the same one again
    only after GESTURE_COOLDOWN_S (so a held pose = one reaction, not a
    stream). A no-hand tick resets the memory so re-showing the same gesture
    later counts as fresh. Returns the fired gesture name (for Stage-2 flows
    to consume) or None. GESTURE_COOLDOWN_S<=0 keeps detection but fires no
    reaction. `detected` is (name, score) or None."""
    if detected is None:
        gesture_state["last"] = None
        return None
    name, score = detected
    # Ignore anything that isn't a deliberate, meaningful gesture (e.g. a
    # point_left/right from a hand just working on the desk) — no reaction,
    # no chatter. Reset last so a real gesture right after still fires.
    if name not in GESTURE_REACTABLE:
        gesture_state["last"] = None
        return None
    if (name == gesture_state.get("last") and GESTURE_COOLDOWN_S > 0
            and now - gesture_state.get("ts", 0.0) < GESTURE_COOLDOWN_S):
        return None
    gesture_state["last"] = name
    gesture_state["ts"] = now
    logger.info("gesture: %s (%.2f)", name, score)
    if GESTURE_COOLDOWN_S > 0:
        _fire_gesture(name)
    return name


def _detect_largest_face(detector, img):
    h, w = img.shape[:2]
    detector.setInputSize((w, h))
    _, faces = detector.detect(img)
    if faces is None or len(faces) == 0:
        return None
    faces = sorted(faces, key=lambda f: f[2] * f[3], reverse=True)
    return faces[0]


def _embed_face(recognizer, img, face_row):
    aligned = recognizer.alignCrop(img, face_row)
    return recognizer.feature(aligned)


def _load_known_faces() -> dict:
    if not os.path.exists(KNOWN_FACES_PATH):
        return {}
    try:
        with open(KNOWN_FACES_PATH) as f:
            return json.load(f)
    except Exception:
        logger.exception("failed to load known_faces.json")
        return {}


def _save_known_faces(data: dict) -> None:
    tmp = KNOWN_FACES_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, KNOWN_FACES_PATH)


def _save_reference_photo(name: str, aligned_crop) -> None:
    import cv2
    os.makedirs(REFERENCE_PHOTOS_DIR, exist_ok=True)
    path = os.path.join(REFERENCE_PHOTOS_DIR, f"{name}.jpg")
    cv2.imwrite(path, aligned_crop)


def _best_match(recognizer, embedding, known: dict) -> tuple[str | None, float]:
    import numpy as np
    import cv2

    best_name, best_score = None, -1.0
    for name, vectors in known.items():
        for vec in vectors:
            ref = np.array(vec, dtype=np.float32).reshape(1, -1)
            score = recognizer.match(embedding, ref, cv2.FaceRecognizerSF_FR_COSINE)
            if score > best_score:
                best_score, best_name = score, name
    return best_name, best_score


# ─── arbiter: Claude second opinion for uncertain local matches ───────────

ARBITER_SYSTEM_PROMPT = (
    "You are a face-verification arbiter for a home robot. You are shown "
    "two images: a REFERENCE photo of a known person, and a NEW photo the "
    "robot just captured. Decide whether the NEW photo shows the SAME "
    "person as the REFERENCE photo.\n\n"
    "Respond with ONLY a JSON object, no other text, no markdown, exactly "
    "this shape:\n"
    '{"match": "no|uncertain|probable|definite", "frame_quality": "poor|good", "notes": "short optional string"}\n\n'
    "match — how confident the NEW photo is the SAME person as the "
    "REFERENCE: no = clearly a different person, uncertain = genuinely "
    "can't tell, probable = likely the same person, definite = confident "
    "it is the same person.\n"
    "frame_quality — whether the NEW photo alone is good enough to later "
    "use as a reference image itself: poor if too dark, blurry, an extreme "
    "angle, or the face is partially obscured; good otherwise."
)


def _call_arbiter(new_frame_path: str, reference_photo_path: str) -> dict:
    """Ask Claude to compare a fresh capture against a reference photo.

    Fails safe on any error or malformed response — never {"match":
    "definite", ...} unless Claude genuinely said so. The caller must
    never act on anything but this function's return value (never on raw
    API text), per the guardrail: malformed output -> no action, no save.
    """
    fail_safe = {"match": "no", "frame_quality": "poor", "notes": "arbiter call failed"}
    api_key = os.environ.get(ARBITER_ANTHROPIC_KEY_ENV, "").strip()
    if not api_key:
        logger.info("arbiter: no API key configured, skipping")
        return fail_safe

    try:
        with open(reference_photo_path, "rb") as f:
            ref_b64 = base64.b64encode(f.read()).decode("ascii")
        with open(new_frame_path, "rb") as f:
            new_b64 = base64.b64encode(f.read()).decode("ascii")
    except Exception:
        logger.exception("arbiter: failed to read comparison images")
        return fail_safe

    body = json.dumps({
        "model": ARBITER_MODEL,
        "max_tokens": 200,
        "system": ARBITER_SYSTEM_PROMPT,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "REFERENCE photo:"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": ref_b64}},
                {"type": "text", "text": "NEW photo:"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": new_b64}},
            ],
        }],
    }).encode()
    req = urllib.request.Request(
        ANTHROPIC_URL, data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = json.loads(resp.read())
        parts = data.get("content", [])
        text = " ".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
        # Despite the system prompt saying "no markdown", Claude sometimes
        # wraps the JSON in a ```json ... ``` fence anyway (confirmed live
        # 2026-07-02) — strip it before parsing rather than failing safe on
        # every single call.
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        verdict = json.loads(text)
        match = verdict.get("match")
        quality = verdict.get("frame_quality")
        if match not in ("no", "uncertain", "probable", "definite"):
            raise ValueError(f"bad match value: {match!r}")
        if quality not in ("poor", "good"):
            raise ValueError(f"bad frame_quality value: {quality!r}")
        result = {"match": match, "frame_quality": quality, "notes": verdict.get("notes", "")}
        logger.info("arbiter verdict: %r", result)
        return result
    except Exception:
        logger.exception("arbiter call or verdict parsing failed")
        return fail_safe


# ─── ambient object-location memory (see ARUCO_* config above) ─────────────

def _load_marker_registry() -> dict:
    """{marker_id:int -> item name} from the shared marker_registry.json.
    Same lenient parse as find_item.py's load_registry: string keys that
    aren't ints ("_readme") are skipped, a bare-string value is a name.
    Missing/unreadable file -> {} — the pass just has nothing to match."""
    reg: dict = {}
    try:
        with open(MARKER_REGISTRY_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return reg
    if not isinstance(raw, dict):
        return reg
    for key, val in raw.items():
        try:
            mid = int(key)
        except (TypeError, ValueError):
            continue  # "_readme" and friends
        if isinstance(val, str):
            val = {"name": val}
        if isinstance(val, dict) and val.get("name"):
            reg[mid] = str(val["name"])
    return reg


def _detect_aruco_ids(img) -> list[int]:
    """All DICT_4X4_250 marker ids visible in the frame (sorted). Same 4.7+/
    legacy API fallback as _detect_marker_rotations / find_item.py. ArUco
    detection is rotation-invariant, so the oriented frame is fine either
    way up (find_item runs on the raw frame for the same reason). Detects at
    native res then on a 2x upscale (union) to recover small/distant tags on
    the 320x240 sensor -- this is how project-tray tags (IDs 100-249, raised on
    L-brackets) get spotted ambiently."""
    import cv2

    adict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_250)

    def _ids(im):
        try:  # OpenCV >= 4.7 API (this venv has 4.11)
            detector = cv2.aruco.ArucoDetector(adict, cv2.aruco.DetectorParameters())
            _corners, ids, _ = detector.detectMarkers(im)
        except AttributeError:  # older API
            _corners, ids, _ = cv2.aruco.detectMarkers(im, adict)
        return set() if ids is None else {int(i) for i in ids.flatten()}

    found = _ids(img)
    found |= _ids(cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC))
    return sorted(found)


def _rail_station_mm(sess: MCPSession) -> float | None:
    """The rail carriage's current pos_mm, or None unless it's TRUSTWORTHY:
    status fresh (< ARUCO_RAIL_FRESH_MS), homed (pos is anchored to a real
    origin), and not mid-move (a gliding carriage would stamp a station the
    frame wasn't taken at — the head-settle gate only covers the first
    ~1.2s of a multi-second rail glide). Any failure -> None: better to
    remember nothing than a station find_item would drive to and miss at.
    self.rail.status reads the gateway's bridge cache — no device wake."""
    try:
        resp = sess.call_tool("self.rail.status", {}, timeout=6)
        text = (((resp or {}).get("result") or {}).get("content") or [{}])[0].get("text", "{}")
        st = json.loads(text)
    except Exception:
        return None
    if not isinstance(st, dict):
        return None
    age = st.get("status_age_ms")
    pos = st.get("pos_mm")
    if not isinstance(age, (int, float)) or age > ARUCO_RAIL_FRESH_MS:
        return None
    if not st.get("homed") or st.get("moving") is not False:
        return None
    if not isinstance(pos, (int, float)):
        return None
    return float(pos)


def _load_locations() -> dict:
    """object_locations.json as a dict, {} on any problem — find_item.py's
    load_locations semantics (a missing file just means no memory yet)."""
    try:
        with open(OBJECT_LOCATIONS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_locations(mem: dict) -> None:
    """Atomic write (temp + os.replace), same file shape find_item.py writes
    (indent=2, sorted keys). The temp name is pid-suffixed so a concurrent
    find_item.py save (fixed ".tmp") can never interleave inside OUR temp
    file on Windows; whichever os.replace lands last wins — fine for a
    slow-moving inventory."""
    tmp = f"{OBJECT_LOCATIONS_PATH}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(mem, f, indent=2, sort_keys=True)
    os.replace(tmp, OBJECT_LOCATIONS_PATH)


def _ambient_marker_pass(sess: MCPSession, img, aruco_state: dict,
                         every_n_gate: bool = True) -> None:
    """Ambient object-location memory: upsert object_locations.json for every
    registry-known ArUco marker visible in this tick's frame (see the ARUCO_*
    config comment). On the per-tick identify path it runs every ARUCO_EVERY_N-th
    executed tick; the slow idle ambient scan passes every_n_gate=False (it's
    already rate-limited by AMBIENT_SCAN_S, so every scan should count). Re-checks
    the busy/head-moved markers at pass time (they can appear mid-tick).
    Upserts bump seen_count and refresh ts/station/method (find_item.py's
    remember() semantics, method="ambient"); a NEW or MOVED (>
    ARUCO_SAME_STATION_MM) item gets one INFO line, same-place re-sightings
    stay silent. An existing entry's pitch is preserved but never invented —
    this loop does no servo control (module docstring) so it does not know
    the head's pitch; find_item defaults a missing pitch to its desk pitch.
    Never raises — any failure just means no memory update this tick."""
    if not ARUCO_ENABLED:
        return
    try:
        if every_n_gate:
            aruco_state["tick"] = aruco_state.get("tick", 0) + 1
            if aruco_state["tick"] % ARUCO_EVERY_N:
                return
        if _should_skip_tick() or _head_settling():
            return
        registry = _load_marker_registry()
        if not registry:
            return
        hits = [(mid, registry[mid]) for mid in _detect_aruco_ids(img) if mid in registry]
        if not hits:
            return
        station = _rail_station_mm(sess)
        if station is None:
            return  # no trustworthy station -> nothing worth remembering
        mem = _load_locations()
        changed = False
        for mid, name in hits:
            key = str(name).strip().lower()
            if not key or key.startswith("_"):
                continue  # find_item.py remember() guard
            old = mem.get(key) if isinstance(mem.get(key), dict) else {}
            entry = {
                "station_mm": int(round(station)),
                "ts": time.time(),
                "method": "ambient",
                "seen_count": int(old.get("seen_count", 0) or 0) + 1,
            }
            if isinstance(old.get("pitch"), (int, float)):
                entry["pitch"] = int(old["pitch"])
            mem[key] = entry
            changed = True
            old_station = old.get("station_mm")
            if not isinstance(old_station, (int, float)):
                logger.info("ambient: %s spotted at %dmm (marker %d, first sighting)",
                            key, entry["station_mm"], mid)
            elif abs(float(old_station) - station) > ARUCO_SAME_STATION_MM:
                logger.info("ambient: %s moved %dmm -> %dmm (marker %d)",
                            key, int(old_station), entry["station_mm"], mid)
        if changed:
            _save_locations(mem)
    except Exception:
        if aruco_state.get("warned"):
            logger.debug("ambient marker pass failed", exc_info=True)
        else:
            aruco_state["warned"] = True
            logger.warning("ambient marker pass failed (first occurrence — "
                           "further failures logged at DEBUG)", exc_info=True)


def _ambient_scan(sess: MCPSession, object_model, object_last_seen: dict,
                  aruco_state: dict) -> bool:
    """Idle empty-room scan (radar healthy + no person): ONE photo, no face, no
    speech. Learns tool/tag locations (ArUco -> object_locations.json) and keeps
    object-novelty warm so a returning user doesn't get a stale 'new object!'
    burst. Head-settle gated (never a moving/blurred frame); returns True only if
    a photo was actually captured (a still-moving head returns False so the
    caller retries next cycle rather than burning the whole slow AMBIENT_SCAN_S).
    Never raises out to the loop."""
    import cv2

    # Same "don't shoot mid-move" gate as _tick — wait briefly for the head to
    # settle, else bail (the idle wander moves it; a blurred tag won't decode).
    _settle_waited = 0.0
    while _head_settling() and _settle_waited < HEAD_SETTLE_MAX_WAIT_S:
        time.sleep(0.1)
        _settle_waited += 0.1
    if _head_settling():
        return False

    path = _take_photo(sess, "idle ambient scan")
    if path is None:
        return False
    img = cv2.imread(path)
    if img is None:
        try:
            os.remove(path)
        except OSError:
            pass
        return False
    if _is_upside_down():
        img = cv2.rotate(img, cv2.ROTATE_180)

    # The point of the idle scan: tags -> object-location memory, on EVERY scan
    # (not gated by ARUCO_EVERY_N — AMBIENT_SCAN_S already paces it).
    _ambient_marker_pass(sess, img, aruco_state, every_n_gate=False)
    # Keep object novelty fresh without speaking to an empty room, so the first
    # tick after the user returns doesn't re-announce a desk that never changed.
    if object_model is not None:
        try:
            detected_objects, _ = _detect_objects(object_model, img)
            _pick_new_object(detected_objects, object_last_seen, time.time())
        except Exception:
            logger.debug("ambient object pass failed", exc_info=True)

    try:
        os.remove(path)
    except OSError:
        pass
    return True


# ─── main polling loop ──────────────────────────────────────────────────────

def _tick(
    detector, recognizer, sess: MCPSession, known: dict,
    last_reaction_ts: dict, last_learn_ts: dict, prev_small_box: list,
    last_seen_ts: dict, missed_looks: dict, last_encourage: dict,
    object_model, object_last_seen: dict, last_object_comment: list,
    messy_state: dict, gesture_model, gesture_state: dict, guard_state: dict,
    aruco_state: dict,
) -> bool:
    """One perception step: capture, detect/classify, write state, maybe react.
    Returns True if a photo was actually captured & processed (so the caller
    advances the identify clock only on a real capture), False on an early skip
    (head still moving, or the capture/decode failed).

    No servo calls here — see module docstring. `prev_small_box` is a
    single-element list used as a mutable "out param" so motion-diff has
    the previous tick's downscaled frame to compare against.

    Three-band classification (2026-07-02, arbiter design): SFace's cosine
    score alone has no middle ground, so scores are split into confidently-
    known / genuinely-uncertain / confidently-unknown. Only the uncertain
    band calls the Claude arbiter (_call_arbiter) — this is what keeps the
    arbiter from becoming a per-tick API cost, the whole reason the local
    loop exists in the first place.
    """
    import cv2

    # Don't capture mid-move — wait for the head to settle (idle flags moves),
    # else the frame is motion-blurred and gives bad face/gesture/object reads.
    # If it's still moving after the max wait (a sustained sweep), skip the tick
    # entirely rather than trust a blur (also saves a wasted take_photo).
    _settle_waited = 0.0
    while _head_settling() and _settle_waited < HEAD_SETTLE_MAX_WAIT_S:
        time.sleep(0.1)
        _settle_waited += 0.1
    if _head_settling():
        return False

    path = _take_photo(sess, "ambient scan")
    if path is None:
        return False
    img = cv2.imread(path)
    if img is None:
        logger.warning("could not decode captured image at %s", path)
        try:
            os.remove(path)
        except OSError:
            pass
        return False

    # When hung upside-down the raw frame is rotated 180 — undo it so YuNet
    # sees an upright face and dx/dy come out in world-upright coordinates
    # (a face to the user's right reads dx>0, etc.). Single flag, shared with
    # the companion server + idle loop. See SETTINGS_PATH above.
    if _is_upside_down():
        img = cv2.rotate(img, cv2.ROTATE_180)
        # Keep the on-disk frame consistent with the corrected in-memory one:
        # the arbiter (_call_arbiter), the pending-learn marker, and
        # _confirm_learn all reference this file BY PATH and need the upright
        # orientation (Claude AND YuNet both fail on an inverted face). Only
        # pay this write when actually inverted — the common upright path is free.
        try:
            cv2.imwrite(path, img)
        except Exception:
            logger.debug("failed to rewrite oriented frame to %s", path, exc_info=True)

    # Ambient object-location memory: every Nth tick, a near-free ArUco pass
    # on this same frame updates the shared object map (never raises — see
    # _ambient_marker_pass and the ARUCO_* config comment).
    _ambient_marker_pass(sess, img, aruco_state)

    # Local object pass on the SAME oriented frame (no extra capture). Runs
    # every executed tick so novelty tracking (_pick_new_object stamps every
    # detected label's last-seen) stays accurate; actually SPEAKING is gated
    # separately below on the global cooldown and on not talking over a
    # greeting/encourage. edge_frac + the clutter streak feed the messy-desk
    # remark's (b) trigger; both are maintained every tick regardless of
    # whether anything is said, so the streak reflects real persistence.
    obj_now = time.time()
    detected_objects, person_present = _detect_objects(object_model, img)
    new_object = _pick_new_object(detected_objects, object_last_seen, obj_now)
    edge_frac = _edge_fraction(img)
    if edge_frac >= CLUTTER_EDGE_THRESHOLD:
        messy_state["streak"] += 1
    else:
        messy_state["streak"] = 0

    # Hand gesture pass on the same frame. A gesture is a DELIBERATE user act,
    # so it takes priority over ambient object/messy commentary this tick
    # (gesture_fired gates those below); it still shares the reactor lock, so
    # a 409 just means he was mid-say.
    gesture_fired = _maybe_fire_gesture(
        _detect_gesture(gesture_model, img), gesture_state, obj_now
    )

    small = _small_gray(img)
    motion_score = _motion_score(prev_small_box[0], small)
    prev_small_box[0] = small
    motion_detected = motion_score > MOTION_DIFF_THRESHOLD
    # Non-camera presence evidence (recent input / motion) — lets a face-less
    # tick avoid being mis-counted as the owner having left (e.g. low light).
    present_hint = _user_present_hint(motion_detected)
    # Broad "user is here" for the idle absence clock (see _write_vision_state):
    # a person in frame (YOLO — turned-to-the-side working) OR recent keyboard/
    # mouse input. NOT raw motion (his own head movement dominates that). Face
    # visibility is OR'd in at each _write_vision_state call.
    _idle_s = _seconds_since_user_input()
    input_recent = _idle_s is not None and _idle_s <= PRESENCE_INPUT_WINDOW_S
    user_present = person_present or input_recent
    # "Hot" = something is happening in front of the camera (motion, or a
    # gesture just fired) — the loop polls FAST while hot so held gestures
    # actually get caught between the slow ambient snapshots. Set here, before
    # the face branch, so every return path below carries it. Read by _loop.
    gesture_state["hot"] = motion_detected or bool(gesture_fired)

    # Lights-out is now sensor-only (LTR-553 lux) and handled in the loop's
    # _check_lights_out_lux poll — the camera no longer guesses it here.
    face = _detect_largest_face(detector, img)
    if face is None:
        logger.info(
            "tick: no face in frame%s%s",
            " (motion detected)" if motion_detected else "",
            " (present: recent input/motion — not counting absence)" if present_hint else "",
        )
        _note_look(None, last_seen_ts, missed_looks, suppress_absence=present_hint)
        # Guard bookkeeping still runs on face-less ticks: a YOLO 'person'
        # box keeps an episode alive (face turned away ≠ gone); a genuinely
        # empty bench lets the 60s episode reset tick down.
        _guard_note_tick(guard_state, obj_now, person_visible=person_present,
                         known_seen=False)
        _write_vision_state(False, None, None, 0.0, 0.0, motion_detected, present=user_present)
        # Nobody's here to greet, but a new object / a fridge-hallucination /
        # a cluttered desk still earns a remark — unless a gesture already
        # fired this tick (that takes the reactor).
        if not gesture_fired:
            _comment_on_scene(new_object, edge_frac, messy_state, last_object_comment, obj_now)
        try:
            os.remove(path)
        except OSError:
            pass
        return True

    frame_h, frame_w = img.shape[:2]
    fx = face[0] + face[2] / 2
    fy = face[1] + face[3] / 2
    # face[] is a numpy row (float32) — cast to native float, or json.dump
    # in _write_vision_state throws "Object of type float32 is not JSON
    # serializable" on every tick a face IS detected (confirmed live
    # 2026-07-01: this silently broke tracking entirely, since the crash
    # happened before _fire_reaction too — vision-state.json never updated
    # past face_visible=false, so idle.py had nothing to track).
    dx = float((fx - frame_w / 2) / (frame_w / 2))
    dy = float((fy - frame_h / 2) / (frame_h / 2))

    embedding = _embed_face(recognizer, img, face)
    candidate_name, score = _best_match(recognizer, embedding, known)

    propose_learn = False

    if candidate_name is not None and score >= UNCERTAIN_HIGH:
        person, key, name = "known", candidate_name, candidate_name
    elif candidate_name is None or score < UNCERTAIN_LOW:
        person, key, name = "unknown", "unknown", None
    else:
        # Genuinely uncertain — worth a second opinion, but only if there's
        # actually a reference photo for the best candidate to compare
        # against (older enrollments predating this feature won't have one).
        ref_photo = os.path.join(REFERENCE_PHOTOS_DIR, f"{candidate_name}.jpg")
        if not os.path.exists(ref_photo):
            logger.info(
                "tick: uncertain score=%.3f for %r but no reference photo — treating as unknown",
                score, candidate_name,
            )
            person, key, name = "unknown", "unknown", None
        else:
            verdict = _call_arbiter(path, ref_photo)
            match, quality = verdict["match"], verdict["frame_quality"]
            logger.info(
                "tick: uncertain score=%.3f candidate=%r -> arbiter match=%s quality=%s",
                score, candidate_name, match, quality,
            )
            if match == "no":
                person, key, name = "unknown", "unknown", None
            elif match == "uncertain":
                # Decision table: no greet, no learn, just stay quiet and
                # try again next tick — don't guess either way.
                person, key, name = None, None, None
            else:  # probable or definite
                person, key, name = "known", candidate_name, candidate_name
                if match == "definite" and quality == "good":
                    existing_count = len(known.get(candidate_name, []))
                    if (
                        existing_count < LEARN_SAMPLE_CAP
                        and time.time() - last_learn_ts.get(candidate_name, 0.0) >= LEARN_CONFIRM_COOLDOWN_S
                    ):
                        propose_learn = True

    _write_vision_state(True, person, name, dx, dy, False, present=True)

    now = time.time()
    reacted = False
    absent_for = 0.0
    # Guard bookkeeping: any face this tick = a person is visible; a "known"
    # match refreshes the owner clock and stands down any active episode.
    # (The arbiter's stay-quiet verdict — person None — still counts as a
    # person being visible, just not a known one.)
    _guard_note_tick(guard_state, now, person_visible=True,
                     known_seen=(person == "known"))
    if person == "known" and key:
        absent_for = now - last_seen_ts.get(key, now)
        # Greet on FIRST sight since process start, or on return from a real
        # absence (wall-clock gap AND enough looks that actually missed them
        # — see ABSENCE_GREET_S comment).
        should_react = key not in last_seen_ts or (
            absent_for >= ABSENCE_GREET_S
            and missed_looks.get(key, 0) >= ABSENCE_MIN_MISSED_LOOKS
        )
        last_seen_ts[key] = now
    elif key:
        should_react = now - last_reaction_ts.get(key, 0.0) >= COOLDOWN_S
    else:
        should_react = False
    # A recognized owner resets their own counter (real sighting). An
    # unrecognized/uncertain face still means a human is here, so with a
    # presence hint don't let it accrue as the owner's absence either.
    _note_look(
        key if person == "known" else None, last_seen_ts, missed_looks,
        suppress_absence=(person != "known" and present_hint),
    )

    # Guard mode: an unknown face while the owner is away gets the challenge
    # (photo + visitor-log entry + /react/guard) INSTEAD of the friendly
    # who-are-you ask below — and while owner-away holds, guard OWNS the
    # unknown-face interaction entirely, so a cooldown-blocked challenge
    # doesn't fall through to a chummy "tap the screen and introduce
    # yourself" mid-episode. Busy/voice-chat ticks never get this far
    # (_should_skip_tick), so a challenge can't interrupt a conversation.
    guard_owns_unknown = False
    if GUARD_ENABLED and person == "unknown" and _guard_owner_away(guard_state, now):
        guard_owns_unknown = True
        if _maybe_fire_guard(guard_state, recognizer, img, face, score, now):
            reacted = True

    if key and should_react and not guard_owns_unknown:
        last_reaction_ts[key] = now
        logger.info(
            "face detected: person=%s key=%s score=%.3f absent_for=%.0fs",
            person, key, score, absent_for,
        )
        _fire_reaction(person, name, propose_learn=propose_learn)
        _log_visitor_safe(recognizer, img, face, name, person == "known", score)
        reacted = True
        if person == "unknown":
            _write_pending_enrollment_marker()

    # Ambient work-nudge: only while the owner-ish person is continuously
    # present (seen recently, didn't just get greeted). Busy/paused ticks
    # never reach this point, so it can't talk over Claude or a voice chat.
    encouraged = False
    if (
        ENCOURAGE_COOLDOWN_S > 0
        and person == "known"
        and not reacted
        and absent_for < ABSENCE_GREET_S
        and now - last_encourage["ts"] >= last_encourage["gap"]
    ):
        last_encourage["ts"] = now
        last_encourage["gap"] = ENCOURAGE_COOLDOWN_S * random.uniform(1.0, 1.5)
        logger.info(
            "firing encourage nudge for %s (next gap >= %.0fs)", name, last_encourage["gap"]
        )
        _fire_encourage(name)
        encouraged = True

    # Object / messy-desk commentary, lowest priority: only if he didn't just
    # greet, encourage, OR react to a gesture this tick, so he never doubles
    # up. Novelty/streak were already updated above regardless, so a thing
    # seen during a greeting tick won't re-trigger next tick either.
    if not reacted and not encouraged and not gesture_fired:
        _comment_on_scene(new_object, edge_frac, messy_state, last_object_comment, obj_now)

    # Learning proposal is piggybacked on the greet firing (simplification:
    # the greet's own hourly cooldown already bounds this, on top of its
    # own daily cooldown, so it can't nag even though it isn't a fully
    # independent trigger path). If nothing fired this tick, the frame is
    # just discarded below like any other ambient tick.
    if propose_learn and reacted:
        last_learn_ts[candidate_name] = now
        _write_pending_learn_confirm_marker(candidate_name, path)
        path = None  # ownership transferred to the marker — don't delete it

    if path is not None:
        try:
            os.remove(path)
        except OSError:
            pass
    return True


def _loop(once: bool) -> None:
    detector = _load_detector()
    recognizer = _load_recognizer()
    object_model = _load_object_model()
    gesture_model = _load_gesture_model()
    gesture_state: dict = {"last": None, "ts": 0.0}
    last_reaction_ts: dict = {}
    last_learn_ts: dict = {}
    prev_small_box: list = [None]
    last_seen_ts: dict = {}
    missed_looks: dict = {}
    object_last_seen: dict = {}
    # 1-element list = mutable timestamp of the last object comment. Seeded
    # to 0.0 so the very first new object can speak immediately (unlike the
    # encourage nudge, an object remark on startup is welcome, not a lecture).
    last_object_comment: list = [0.0]
    # Messy-desk remark state: shared long cooldown timestamp + the running
    # count of consecutive high-edge-density ticks (the clutter-streak). ts=0.0
    # so the first genuinely cluttered/confused view can speak straight away.
    messy_state: dict = {"ts": 0.0, "streak": 0}
    # First nudge no earlier than one full (stretched) cooldown after start —
    # boot-time gets the greeting, not a productivity lecture.
    last_encourage: dict = {
        "ts": time.time(),
        "gap": ENCOURAGE_COOLDOWN_S * random.uniform(1.0, 1.5),
    }
    # Guard mode state. last_known_seen_ts starts at PROCESS START —
    # conservatively "the owner might have just been here" — so a fresh boot
    # can never challenge anyone during its first GUARD_OWNER_AWAY_S window.
    guard_state: dict = {
        "last_known_seen_ts": time.time(),
        "last_person_seen_ts": 0.0,
        "episode_active": False,
        "challenges": 0,
        "last_challenge_ts": 0.0,
    }
    # Ambient ArUco object-location memory: executed-tick counter (the pass
    # runs every ARUCO_EVERY_N-th) + one-shot failure-warned flag.
    aruco_state: dict = {"tick": 0}
    # Sensor-only lights-out state: rolling lux history + last-fire + last-poll.
    lux_state: dict = {"history": [], "ts": 0.0, "poll_ts": 0.0}
    logger.info(
        "starting vision loop (poll=%.0fs unknown_cooldown=%.0fs absence_greet=%.0fs "
        "encourage=%.0fs objects=%s obj_cooldown=%.0fs clutter=%s edge_thr=%.3f "
        "gestures=%s guard=%s(away=%.0fs cd=%.0fs) aruco=%s(every_n=%d) "
        "radar_gate=%s recheck=%.0fs ambient_scan=%s(%.0fs) lux_lightsout=%s(%.0fs) "
        "threshold=%.3f uncertain=%.2f-%.2f)",
        POLL_INTERVAL_S, COOLDOWN_S, ABSENCE_GREET_S, ENCOURAGE_COOLDOWN_S,
        object_model is not None, OBJECT_COOLDOWN_S,
        CLUTTER_ENABLED and object_model is not None, CLUTTER_EDGE_THRESHOLD,
        gesture_model is not None,
        GUARD_ENABLED, GUARD_OWNER_AWAY_S, GUARD_COOLDOWN_S,
        ARUCO_ENABLED, ARUCO_EVERY_N,
        RADAR_GATE, CAMERA_RECHECK_S, AMBIENT_SCAN_ENABLED, AMBIENT_SCAN_S,
        LIGHTS_LUX_ENABLED, LIGHTS_LUX_POLL_S,
        MATCH_THRESHOLD, UNCERTAIN_LOW, UNCERTAIN_HIGH,
    )
    # Boot-time orientation auto-detect: figure out which way up he is now
    # (he may have been flipped while powered off) and set the shared flag.
    if not once and AUTO_ORIENT_ENABLED:
        try:
            _auto_orient_on_boot(detector)
        except Exception:
            logger.exception("auto-orient on boot failed")
    hot_ticks = 0  # >0 = poll fast (recent motion/gesture); decays each tick
    last_photo_ts = 0.0    # radar-gate: when we last took an IDENTIFY photo
    last_ambient_ts = 0.0  # when we last took an idle AMBIENT scan photo
    while True:
        if _should_skip_tick():
            time.sleep(POLL_INTERVAL_S)
            if once:
                return
            continue
        sess = MCPSession(GATEWAY_MCP)
        try:
            sess.initialize()
            # Sensor-only lights-out (moved off the camera 2026-07-16): a slow,
            # independent lux poll so it works even in an empty dark room.
            _check_lights_out_lux(sess, lux_state)
            # Radar gate (TRACKING_PLAN §0.5) + idle ambient scans: a full identify
            # photo only when radar sees someone AND we're due (or on request); a
            # passive no-face tag/item scan when the room's empty but radar's healthy
            # (slow, head-settle gated); otherwise just the cheap radar poll. This is
            # the #36 camera-load hard-lockup fix at SOURCE — the old every-8s photo
            # poll dropped his ping to 2567 ms and crash-looped him (2026-07-16).
            mode = _camera_decision(sess, last_photo_ts, last_ambient_ts, once)
            if mode is None:
                time.sleep(IDLE_POLL_S)
                continue
            if mode == "ambient":
                # Empty-room passive scan: learn tool/tag locations, no face, no
                # speech. Stamp last_ambient_ts only on a real capture (a
                # head-moving skip retries next cycle) so the slow cadence holds.
                if _ambient_scan(sess, object_model, object_last_seen, aruco_state):
                    last_ambient_ts = time.time()
                time.sleep(IDLE_POLL_S)
                continue
            # mode == "identify": full face/greeting tick.
            # Reloaded every tick (cheap) so `--enroll` mid-run takes effect next tick.
            known = _load_known_faces()
            # Advance the identify clock only on a REAL capture — a head-settle
            # skip / take_photo failure returns False and is retried promptly
            # rather than costing a full CAMERA_RECHECK_S of blindness.
            if _tick(
                detector, recognizer, sess, known, last_reaction_ts, last_learn_ts,
                prev_small_box,
                last_seen_ts, missed_looks, last_encourage,
                object_model, object_last_seen, last_object_comment,
                messy_state, gesture_model, gesture_state, guard_state,
                aruco_state,
            ):
                now_ts = time.time()
                last_photo_ts = now_ts
                last_ambient_ts = now_ts  # a full identify tick also refreshes tags
        except Exception:
            logger.exception("tick failed")
        if once:
            return
        # Adaptive cadence: a "hot" tick (motion/gesture) keeps the loop fast
        # for the next FAST_POLL_HOT_TICKS ticks so held gestures get caught;
        # it relaxes back to the slow ambient poll once nothing's happening.
        if gesture_state.pop("hot", False):
            hot_ticks = FAST_POLL_HOT_TICKS
        interval = FAST_POLL_INTERVAL_S if hot_ticks > 0 else POLL_INTERVAL_S
        if hot_ticks > 0:
            hot_ticks -= 1
        time.sleep(interval)


# ─── ArUco scan-tray orientation calibration (2026-07-06) ───────────────────
# There is no readable orientation sensor over MCP (no IMU tool; the i2c_*
# tools only reach the external Grove Port A, not the internal bus the BMI270
# sits on — see firmware/TODO.md). So we work orientation out from the CAMERA
# using the scan tray Wheatley hangs over: it has four DICT_4X4_50 ArUco
# markers (IDs 0-3), one per corner, printed upright. We read the markers'
# rotation in the RAW (un-rotated) frame — printed-upright markers read ~0 deg
# when the body is upright and ~180 deg when it's inverted (confirmed live).
# The verdict is written to the shared companion_settings.json `upside_down`
# flag — the single source of truth the vision loop, idle loop, and companion
# server all read. No face needed; run it any time the tray is in view.
CALIB_PITCH_POINTS = (65, 75, 55, 85, 45)  # sweep until the tray markers appear
CALIB_MIN_MARKERS = 2                       # need at least this many before deciding


def _detect_marker_rotations(img) -> list[float]:
    """Rotation (deg, -180..180) of each DICT_4X4_250 marker's top edge in the
    frame. ~0 = marker upright, ~±180 = rotated 180 (body inverted). Native res
    only -- this reads the 50mm drop-zone corners (which decode fine natively)
    and needs true corner coordinates, so no upscale pass here."""
    import cv2
    import numpy as np

    adict = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_250)
    try:  # OpenCV >= 4.7 API
        detector = cv2.aruco.ArucoDetector(adict, cv2.aruco.DetectorParameters())
        corners, ids, _ = detector.detectMarkers(img)
    except AttributeError:  # older API
        corners, ids, _ = cv2.aruco.detectMarkers(img, adict)
    rots: list[float] = []
    if ids is not None:
        for c in corners:
            pts = c.reshape(-1, 2)  # marker's own TL, TR, BR, BL
            top = pts[1] - pts[0]
            rots.append(float(np.degrees(np.arctan2(top[1], top[0]))))
    return rots


def _rotations_inverted(rots: list[float]) -> bool:
    """True if the markers read as rotated ~180 deg (body inverted). Robust to
    angle wrap by averaging unit vectors."""
    import numpy as np
    mx = float(np.mean([np.cos(np.radians(a)) for a in rots]))
    my = float(np.mean([np.sin(np.radians(a)) for a in rots]))
    return abs(float(np.degrees(np.arctan2(my, mx)))) > 90.0


def _set_upside_down(flag: bool) -> None:
    """Merge the verdict into companion_settings.json (shared source of truth)."""
    data = {}
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    data["upside_down"] = bool(flag)
    tmp = SETTINGS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, SETTINGS_PATH)


# Boot sweep poses (yaw, pitch): a broad spread so the tray or a face turns up
# whichever way he's currently mounted — we don't yet know which way is up.
AUTO_ORIENT_POSES = ((0, 60), (0, 45), (0, 30), (0, 75), (-40, 45), (40, 45), (0, 15), (0, 85))


def _auto_orient_on_boot(detector) -> None:
    """Work out which way up he is at startup and set the shared upside_down
    flag, MOVING the camera to hunt for a cue. Priority of cues:
      1. the scan-tray ArUco codes (rotation-invariant, unambiguous), then
      2. a face (detected in the RAW frame => upright; only in the 180-rotated
         frame => inverted).
    If neither is seen anywhere in the sweep, LEAVE the flag as it was before
    boot — user's rule: "if unsure, assume the same way round as before boot."
    (Generic "other object" orientation would need a DNN/OCR model; not built —
    an unreliable guess that flips the wrong way is worse than keeping prev.)
    Disable with STACKCHAN_AUTO_ORIENT=0.
    """
    import cv2

    prev = _is_upside_down()
    sess = MCPSession(GATEWAY_MCP)
    try:
        sess.initialize()
        sess.call_tool("set_servo_torque", {"yaw_enabled": True, "pitch_enabled": True})
    except Exception:
        logger.info("auto-orient: device not ready; keeping previous upside_down=%s", prev)
        return

    _touch_marker(ORIENTING_MARKER)  # tell the idle loop to hold still
    try:
        for yaw, pitch in AUTO_ORIENT_POSES:
            try:
                sess.call_tool("move_head", {"yaw": yaw, "pitch": pitch})
            except Exception:
                continue
            time.sleep(1.1)
            path = _take_photo(sess, "boot orientation check")
            if not path:
                continue
            img = cv2.imread(path)
            try:
                os.remove(path)
            except OSError:
                pass
            if img is None:
                continue

            rots = _detect_marker_rotations(img)
            if rots:
                inverted = _rotations_inverted(rots)
                _set_upside_down(inverted)
                logger.info("auto-orient: tray markers (%d) -> upside_down=%s (was %s)",
                            len(rots), inverted, prev)
                return

            raw_face = _detect_largest_face(detector, img) is not None
            rot_face = _detect_largest_face(detector, cv2.rotate(img, cv2.ROTATE_180)) is not None
            if raw_face != rot_face:  # decisive only when exactly one orientation sees it
                inverted = rot_face
                _set_upside_down(inverted)
                logger.info("auto-orient: face (raw=%s rot=%s) -> upside_down=%s (was %s)",
                            raw_face, rot_face, inverted, prev)
                return

        logger.info("auto-orient: no tray/face cue found in sweep; keeping previous upside_down=%s", prev)
    finally:
        try:
            _perp = 25
            try:
                with open(SETTINGS_PATH, encoding="utf-8") as _f:
                    _perp = int(json.load(_f).get("perpendicular_yaw", 25))
            except Exception:
                pass
            sess.call_tool("move_head", {"yaw": _perp, "pitch": 45})
        except Exception:
            pass
        _clear_marker(ORIENTING_MARKER)


def _calibrate_flip() -> None:
    import numpy as np
    import cv2

    sess = MCPSession(GATEWAY_MCP)
    sess.initialize()
    sess.call_tool("set_servo_torque", {"yaw_enabled": True, "pitch_enabled": True})

    print("Calibrating orientation from the scan-tray ArUco markers...")
    rots: list[float] = []
    for pitch in CALIB_PITCH_POINTS:
        sess.call_tool("move_head", {"yaw": 0, "pitch": pitch})
        time.sleep(1.2)
        path = _take_photo(sess, "tray orientation calibration")
        if not path:
            continue
        img = cv2.imread(path)
        try:
            os.remove(path)
        except OSError:
            pass
        if img is None:
            continue
        found = _detect_marker_rotations(img)
        if found:
            logger.info("calibrate-flip: pitch %d saw %d marker(s): %s", pitch, len(found), found)
            rots.extend(found)
        if len(rots) >= CALIB_MIN_MARKERS:
            break

    if len(rots) < CALIB_MIN_MARKERS:
        print(f"Only found {len(rots)} tray marker(s) (need {CALIB_MIN_MARKERS}). "
              "Point him at the scan tray (the 4 corner codes) and retry. Nothing changed.")
        return

    # Robust mean angle (angles wrap at ±180): average unit vectors.
    mx = float(np.mean([np.cos(np.radians(a)) for a in rots]))
    my = float(np.mean([np.sin(np.radians(a)) for a in rots]))
    mean_abs = abs(float(np.degrees(np.arctan2(my, mx))))  # 0..180
    inverted = mean_abs > 90.0
    _set_upside_down(inverted)
    print(f"Read {len(rots)} marker rotation(s), mean |angle| {mean_abs:.0f} deg -> "
          f"{'UPSIDE DOWN' if inverted else 'UPRIGHT'}. "
          f"Set companion_settings.json upside_down={inverted}. "
          "Restart the idle loop to apply (stackchan-idle-start.vbs).")
    logger.info("calibrate-flip(aruco): rots=%s mean_abs=%.1f inverted=%s", rots, mean_abs, inverted)


# ─── enrollment CLI ─────────────────────────────────────────────────────────

def _next_available_name(base: str, known: dict) -> str:
    n = 2
    while f"{base} ({n})" in known:
        n += 1
    return f"{base} ({n})"


def _resolve_enroll_name(name: str, img, face, recognizer, known: dict) -> str:
    """If `name` is unused, return it as-is — no collision.

    If it's already enrolled, only split into a new "Name (2)" identity
    when BOTH the local score AND the Claude arbiter agree this is clearly
    a different person. Otherwise default to treating it as another
    sample of the existing person — deliberately biased against creating
    spurious duplicate identities from bad lighting/angle on a legitimate
    second enrollment (2026-07-03 design discussion: "avoid it adding more
    Dominics that are just me in bad light"). This is the same asymmetric
    bar as the ongoing-recognition decision table: cheap to treat two
    genuinely different people as one (a false merge just means one
    profile has mixed samples, degrading match quality a bit) is still
    less disruptive than fracturing one real person into several partial
    identities every time the lighting's bad — worth revisiting if merges
    ever turn out to be the bigger problem in practice.
    """
    existing = known.get(name)
    if not existing:
        return name

    embedding = _embed_face(recognizer, img, face)
    _, local_score = _best_match(recognizer, embedding, {name: existing})
    if local_score >= UNCERTAIN_HIGH:
        return name  # confidently the same person

    ref_photo = os.path.join(REFERENCE_PHOTOS_DIR, f"{name}.jpg")
    if not os.path.exists(ref_photo):
        # No reference image to check against and the local score alone is
        # inconclusive — no evidence either way, default to same-person
        # rather than guessing.
        return name

    import cv2
    tmp_path = os.path.join(TEMP, f"stackchan-collision-check-{int(time.time() * 1000)}.jpg")
    cv2.imwrite(tmp_path, img)
    try:
        verdict = _call_arbiter(tmp_path, ref_photo)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    if verdict["match"] == "no":
        new_name = _next_available_name(name, known)
        logger.info(
            "enroll: local score=%.3f + arbiter agree %r is a different person from existing %r -> %r",
            local_score, name, name, new_name,
        )
        return new_name
    return name  # uncertain/probable/definite -> treat as the same person


def _enroll(name: str, samples: int, interval: float) -> None:
    detector = _load_detector()
    recognizer = _load_recognizer()
    sess = MCPSession(GATEWAY_MCP)
    sess.initialize()
    known = _load_known_faces()

    resolved_name = None  # determined from the first successful capture
    collected: list = []

    print(f"Enrolling '{name}' — look at the camera. Capturing {samples} sample(s)...")
    got = 0
    attempts = 0
    max_attempts = samples * 5  # generous retry budget for missed detections
    best_crop = None
    best_area = -1.0
    while got < samples and attempts < max_attempts:
        attempts += 1
        path = _take_photo(sess, "enrollment capture")
        if path is None:
            print("  capture failed, retrying...")
            time.sleep(interval)
            continue

        import cv2
        img = cv2.imread(path)
        try:
            os.remove(path)
        except OSError:
            pass
        if img is None:
            continue
        # Orientation-correct before detection (same as the live loop) so
        # enrolling while inverted (e.g. on the rail) still finds the face and
        # stores an upright reference crop / arbiter comparison.
        if _is_upside_down():
            img = cv2.rotate(img, cv2.ROTATE_180)

        face = _detect_largest_face(detector, img)
        if face is None:
            print(f"  [{got}/{samples}] no face found, try again")
            time.sleep(interval)
            continue

        if resolved_name is None:
            resolved_name = _resolve_enroll_name(name, img, face, recognizer, known)
            collected = list(known.get(resolved_name, []))
            if resolved_name != name:
                print(f"  '{name}' is already someone else — enrolling separately as '{resolved_name}'")

        feat = _embed_face(recognizer, img, face)
        collected.append(feat.flatten().tolist())
        got += 1
        # Keep the biggest (closest/clearest) face as the reference photo —
        # cheap proxy for "best quality" without a separate quality check.
        area = float(face[2] * face[3])
        if area > best_area:
            best_area = area
            best_crop = recognizer.alignCrop(img, face)
        print(f"  [{got}/{samples}] captured")
        time.sleep(interval)

    if got == 0 or resolved_name is None:
        print("No samples captured — nothing saved.")
        return

    known[resolved_name] = collected
    _save_known_faces(known)
    if best_crop is not None:
        _save_reference_photo(resolved_name, best_crop)
    print(f"Enrolled '{resolved_name}' with {len(collected)} sample(s) total in {KNOWN_FACES_PATH}.")


def _confirm_learn(name: str, frame_path: str) -> bool:
    """Append a learning sample from an already-captured frame (the exact
    one the arbiter judged "definite + good") rather than taking a fresh,
    possibly worse shot. Called by stackchan-voice-bridge.py's tap-to-talk
    confirmation flow. Cleans up the frame file regardless of outcome — it's
    single-use, referenced only by the now-consumed marker.
    """
    detector = _load_detector()
    recognizer = _load_recognizer()
    try:
        import cv2
        img = cv2.imread(frame_path)
        if img is None:
            print(f"Could not read frame at {frame_path}")
            return False
        face = _detect_largest_face(detector, img)
        if face is None:
            print("No face found in the saved frame")
            return False

        known = _load_known_faces()
        existing = list(known.get(name, []))
        if len(existing) >= LEARN_SAMPLE_CAP:
            print(f"'{name}' already has {len(existing)} samples (cap {LEARN_SAMPLE_CAP}) — skipping")
            return False

        feat = _embed_face(recognizer, img, face)
        existing.append(feat.flatten().tolist())
        known[name] = existing
        _save_known_faces(known)
        # This frame was already judged "definite + good" by the arbiter —
        # at least as good as whatever reference photo exists, so refresh it.
        _save_reference_photo(name, recognizer.alignCrop(img, face))
        print(f"Learned a new sample for '{name}' ({len(existing)} total).")
        return True
    finally:
        try:
            os.remove(frame_path)
        except OSError:
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enroll", metavar="NAME", help="teach a new/additional face sample")
    parser.add_argument("--samples", type=int, default=3, help="samples to capture when enrolling")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between enroll samples")
    parser.add_argument(
        "--confirm-learn", nargs=2, metavar=("NAME", "FRAME_PATH"),
        help="append a learning sample from an already-captured frame",
    )
    parser.add_argument(
        "--calibrate-flip", action="store_true",
        help="work out the pitch orientation (up/down) from the camera and save it",
    )
    parser.add_argument("--once", action="store_true", help="run a single tick then exit (testing)")
    args = parser.parse_args()

    if args.calibrate_flip:
        # Foreground one-shot, like --enroll — skip the single-instance lock
        # so it runs even while the background loop is active.
        _calibrate_flip()
        return

    if args.enroll:
        # Enrollment is an explicit, foreground, one-shot CLI action — skip
        # the single-instance lock so it can run even while the background
        # loop is already active.
        _enroll(args.enroll, args.samples, args.interval)
        return

    if args.confirm_learn:
        _confirm_learn(*args.confirm_learn)
        return

    _acquire_lock()
    _loop(once=args.once)


if __name__ == "__main__":
    main()
