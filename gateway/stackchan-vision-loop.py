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
import json
import logging
import os
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
# Debounce: don't re-fire the greet/notice reaction for the same identity
# more often than this. 1 hour by default (user: "repeat if it hasn't seen
# me for more than one hour") — long enough that sitting at the desk all
# day only gets one welcome-back, short enough to re-greet after a real
# absence (lunch, overnight, etc.).
COOLDOWN_S = float(os.environ.get("STACKCHAN_VISION_COOLDOWN_S", str(60 * 60)))

# Written every tick — stackchan-idle.py reads this to decide whether to
# track a face, search for one, or glance toward motion. See the module
# docstring for why movement lives there and not here.
VISION_STATE_PATH = os.path.join(TEMP, "stackchan-vision-state.json")

# Motion detection: cheap frame-differencing (grayscale, downscaled, mean
# absolute difference) — no extra model needed. Only meaningful as a signal
# when no face is in frame (a face already gives idle.py plenty to work
# with); mainly for "something moved, glance over" when nobody recognizable
# is currently visible.
MOTION_DIFF_THRESHOLD = float(os.environ.get("STACKCHAN_VISION_MOTION_THRESHOLD", "12.0"))

# Set when an unrecognized face triggers the "who are you?" prompt (see
# sensor_reactor.py's _behavior_recognize unknown branch); stackchan-voice-
# bridge.py checks this to know a tap-to-answer transcript is probably a
# name introduction rather than a normal question.
PENDING_ENROLLMENT_MARKER = os.path.join(TEMP, "stackchan-pending-enrollment")

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

# Shared with the idle loop / led-chase / voice bridge — skip a tick rather
# than fight Claude Code's active work or an in-progress voice exchange.
BUSY_MARKER = os.path.join(TEMP, "stackchan-busy")
BUSY_STALE_S = 30 * 60
VOICE_THINKING_MARKER = os.path.join(TEMP, "stackchan-voice-thinking")
VOICE_STALE_S = 90
# Convenience pause switch — touch this file to stop captures without
# killing the process (e.g. for privacy), remove it to resume.
PAUSE_MARKER = os.path.join(TEMP, "stackchan-vision-paused")


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


def _should_skip_tick() -> bool:
    if os.path.exists(PAUSE_MARKER):
        return True
    if _marker_active(BUSY_MARKER, BUSY_STALE_S):
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


def _write_vision_state(
    face_visible: bool, person: str | None, name: str | None,
    dx: float, dy: float, motion_detected: bool,
) -> None:
    state = {
        "ts": time.time(),
        "face_visible": face_visible,
        "person": person,
        "name": name,
        "dx": dx,
        "dy": dy,
        "motion_detected": motion_detected,
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


# ─── face detection / recognition ──────────────────────────────────────────

def _load_detector():
    import cv2
    return cv2.FaceDetectorYN.create(YUNET_MODEL, "", (320, 320))


def _load_recognizer():
    import cv2
    return cv2.FaceRecognizerSF.create(SFACE_MODEL, "")


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


# ─── main polling loop ──────────────────────────────────────────────────────

def _tick(
    detector, recognizer, sess: MCPSession, known: dict,
    last_reaction_ts: dict, last_learn_ts: dict, prev_small_box: list,
) -> None:
    """One perception step: capture, detect/classify, write state, maybe react.

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

    path = _take_photo(sess, "ambient scan")
    if path is None:
        return
    img = cv2.imread(path)
    if img is None:
        logger.warning("could not decode captured image at %s", path)
        try:
            os.remove(path)
        except OSError:
            pass
        return

    small = _small_gray(img)
    motion_score = _motion_score(prev_small_box[0], small)
    prev_small_box[0] = small
    motion_detected = motion_score > MOTION_DIFF_THRESHOLD

    face = _detect_largest_face(detector, img)
    if face is None:
        logger.info(
            "tick: no face in frame%s", " (motion detected)" if motion_detected else ""
        )
        _write_vision_state(False, None, None, 0.0, 0.0, motion_detected)
        try:
            os.remove(path)
        except OSError:
            pass
        return

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

    _write_vision_state(True, person, name, dx, dy, False)

    now = time.time()
    reacted = False
    if key and now - last_reaction_ts.get(key, 0.0) >= COOLDOWN_S:
        last_reaction_ts[key] = now
        logger.info("face detected: person=%s key=%s score=%.3f", person, key, score)
        _fire_reaction(person, name, propose_learn=propose_learn)
        reacted = True
        if person == "unknown":
            _write_pending_enrollment_marker()

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


def _loop(once: bool) -> None:
    detector = _load_detector()
    recognizer = _load_recognizer()
    last_reaction_ts: dict = {}
    last_learn_ts: dict = {}
    prev_small_box: list = [None]
    logger.info(
        "starting vision loop (poll=%.0fs cooldown=%.0fs threshold=%.3f uncertain=%.2f-%.2f)",
        POLL_INTERVAL_S, COOLDOWN_S, MATCH_THRESHOLD, UNCERTAIN_LOW, UNCERTAIN_HIGH,
    )
    while True:
        if _should_skip_tick():
            time.sleep(POLL_INTERVAL_S)
            if once:
                return
            continue
        # Reloaded every tick (cheap — a small JSON file) rather than once
        # at startup, so `--enroll` while the loop is already running takes
        # effect on the next tick instead of needing a restart.
        known = _load_known_faces()
        sess = MCPSession(GATEWAY_MCP)
        try:
            sess.initialize()
            _tick(detector, recognizer, sess, known, last_reaction_ts, last_learn_ts, prev_small_box)
        except Exception:
            logger.exception("tick failed")
        if once:
            return
        time.sleep(POLL_INTERVAL_S)


# ─── enrollment CLI ─────────────────────────────────────────────────────────

def _enroll(name: str, samples: int, interval: float) -> None:
    detector = _load_detector()
    recognizer = _load_recognizer()
    sess = MCPSession(GATEWAY_MCP)
    sess.initialize()
    known = _load_known_faces()
    collected = list(known.get(name, []))

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

        face = _detect_largest_face(detector, img)
        if face is None:
            print(f"  [{got}/{samples}] no face found, try again")
            time.sleep(interval)
            continue

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

    if got == 0:
        print("No samples captured — nothing saved.")
        return

    known[name] = collected
    _save_known_faces(known)
    if best_crop is not None:
        _save_reference_photo(name, best_crop)
    print(f"Enrolled '{name}' with {len(collected)} sample(s) total in {KNOWN_FACES_PATH}.")


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
    parser.add_argument("--once", action="store_true", help="run a single tick then exit (testing)")
    args = parser.parse_args()

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
