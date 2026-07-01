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
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request

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


def _fire_reaction(person: str) -> None:
    url = f"{REACT_URL}?person={person}"
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


# ─── main polling loop ──────────────────────────────────────────────────────

def _tick(
    detector, recognizer, sess: MCPSession, known: dict,
    last_reaction_ts: dict, prev_small_box: list,
) -> None:
    """One perception step: capture, detect/classify, write state, maybe react.

    No servo calls here — see module docstring. `prev_small_box` is a
    single-element list used as a mutable "out param" so motion-diff has
    the previous tick's downscaled frame to compare against.
    """
    import cv2

    path = _take_photo(sess, "ambient scan")
    if path is None:
        return
    try:
        img = cv2.imread(path)
    finally:
        try:
            os.remove(path)  # don't accumulate captures forever — this loop
        except OSError:      # snaps one every POLL_INTERVAL_S indefinitely.
            pass
    if img is None:
        logger.warning("could not decode captured image at %s", path)
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
    name, score = _best_match(recognizer, embedding, known)

    if name is not None and score >= MATCH_THRESHOLD:
        person, key = "known", name
    else:
        person, key = "unknown", "unknown"

    _write_vision_state(True, person, name if person == "known" else None, dx, dy, False)

    now = time.time()
    if now - last_reaction_ts.get(key, 0.0) >= COOLDOWN_S:
        last_reaction_ts[key] = now
        logger.info("face detected: person=%s key=%s score=%.3f", person, key, score)
        _fire_reaction(person)
        if person == "unknown":
            _write_pending_enrollment_marker()


def _loop(once: bool) -> None:
    detector = _load_detector()
    recognizer = _load_recognizer()
    last_reaction_ts: dict = {}
    prev_small_box: list = [None]
    logger.info(
        "starting vision loop (poll=%.0fs cooldown=%.0fs threshold=%.3f)",
        POLL_INTERVAL_S, COOLDOWN_S, MATCH_THRESHOLD,
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
            _tick(detector, recognizer, sess, known, last_reaction_ts, prev_small_box)
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
        print(f"  [{got}/{samples}] captured")
        time.sleep(interval)

    if got == 0:
        print("No samples captured — nothing saved.")
        return

    known[name] = collected
    _save_known_faces(known)
    print(f"Enrolled '{name}' with {len(collected)} sample(s) total in {KNOWN_FACES_PATH}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enroll", metavar="NAME", help="teach a new/additional face sample")
    parser.add_argument("--samples", type=int, default=3, help="samples to capture when enrolling")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between enroll samples")
    parser.add_argument("--once", action="store_true", help="run a single tick then exit (testing)")
    args = parser.parse_args()

    if args.enroll:
        # Enrollment is an explicit, foreground, one-shot CLI action — skip
        # the single-instance lock so it can run even while the background
        # loop is already active.
        _enroll(args.enroll, args.samples, args.interval)
        return

    _acquire_lock()
    _loop(once=args.once)


if __name__ == "__main__":
    main()
