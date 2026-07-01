# StackChan / Wheatley — Handover

Working state as of 2026-07-01. This is a personal fork
(`DJChilvers/stackchan-mcp`, private, `origin`) of
`kisaragi-mochi/stackchan-mcp` (`upstream`), running a custom Wheatley
(Portal 2) personality on an M5Stack CoreS3 desk robot. Read this before
picking the project back up — several pieces of state live outside git
(background processes, PSRAM contents, temp marker files) and aren't
obvious from the code alone.

## What's running, and how to (re)start it

Four independent processes, all launched hidden via `.vbs` wrappers in
`gateway/`, each single-instance-locked via a `%TEMP%\*.lock` file:

| Process | Script | Launcher | Purpose |
|---|---|---|---|
| Gateway daemon | `stackchan_mcp` (via `stackchan-daemon.bat`) | `stackchan-daemon-start.vbs` | MCP server on `http://127.0.0.1:8767`, owns the device WebSocket |
| Idle wander | `stackchan-idle.py` | `stackchan-idle-start.vbs` | Ambient head/eye movement when truly idle |
| LED chase | `stackchan-led-chase.py` | `stackchan-led-chase-start.vbs` | Animates the 12-LED ring based on marker files |
| Voice bridge | `stackchan-voice-bridge.py` | `stackchan-voice-bridge-start.vbs` | Touch-to-talk → faster-whisper → Claude API → speech |

To restart any of them: kill the `pythonw.exe`/`python.exe` process(es)
running that script, then run the matching `.vbs` (e.g.
`cscript //nologo stackchan-idle-start.vbs` from `gateway/`).

**Every one of these shows up as TWO OS processes for ONE logical
instance** — a `.venv\Scripts\pythonw.exe` parent and a
`C:\Users\<user>\AppData\Roaming\uv\python\...\pythonw.exe` child (the venv
python is a trampoline). Kill both PIDs when restarting; this is normal,
not a duplicate-instance bug. The gateway daemon additionally has a
watchdog loop in `stackchan-daemon.bat` that auto-relaunches it in ~5s if
it crashes (there's a recurring `uv`-managed-interpreter-goes-missing bug —
fix is `uv python install 3.12.13 --reinstall`, ignore the "Missing
expected target directory" error it prints, verify with
`.venv\Scripts\python.exe --version`).

## The avatar (matrix mode)

`gateway/wheatley_avatar.py` composites the avatar from three independent
axes instead of 14 fixed pictures:

- **face** (`FACE_SPECS`): horizontal gaze + expression identity + a small
  fixed lid `tilt` (referencing real Wheatley footage — his prop rides at a
  slight canted roll, not dead-level; faked here since the servos have no
  roll axis).
- **eyes** (`EYES_SPECS`): blink closure, added on top of the face's lid
  baseline. Firmware auto-cycles this; we don't control timing directly.
- **mouth** (`MOUTH_SPECS`): `closed`/`half`/`open` stay near-neutral
  (firmware auto-cycles these during real lip-sync); `e`/`u` carry a big
  vertical offset — the "glance up" cue, and also the kung-fu-flutter pair
  used during busy/coding (`set_mouth_sequence` in the hook).

`render_combo(face, eyes, mouth)` sums the three and renders one of 90
frames; `build_matrix()` generates all of them into
`wheatley_avatar_matrix.bin` (3,456,000 bytes). `.env` points
`STACKCHAN_DEFAULT_AVATAR`/`_MODE` at this file in matrix mode — set
`load_avatar_set(mode="matrix")` to push a fresh build.

**Gotcha: PSRAM double-buffering.** The firmware allocates the *incoming*
avatar buffer before freeing the old one
(`firmware/main/boards/stackchan/avatar_set_fetcher.cc:80`), so pushing a
new matrix set while one is already loaded needs ~2x the buffer size in
PSRAM momentarily and can OOM. First push after a power-cycle always
works; a second push to an already-loaded device sometimes needs a
power-cycle first. See `firmware/TODO.md`.

**Gaze sign convention:** `ox` (horizontal) is negated inside
`render_combo()` — confirmed via a live eye-only test that the naive sign
rendered backwards from the viewer's perspective. `oy` (vertical) is NOT
negated; that one was already correct. If you ever see a face looking the
"wrong" way again, check this convention hasn't drifted, and confirm with
an eye-only test (no head movement) before touching anything, so you're
not chasing a servo-side bug that isn't there.

## Movement (`stackchan-idle.py`)

Ambient idle movement is a library of small vignettes
(`_v_nudge`, `_v_look_up_center`, `_v_diagonal_peek`, `_v_ponder_down`,
rare `_v_big_examine`), picked with real randomness (never the same one
twice in a row), each drifting from the *current* pose rather than
snapping back to a fixed neutral position. Gating: holds still if recent
hook activity (`ACTIVITY_FILE`), any session's busy marker is active
(`is_busy()`), or a needs-attention signal is outstanding
(`needs_attention()`).

Do not reintroduce a big committed left-right-left swing as the default
behaviour — that was explicitly, repeatedly rejected. Small and varied is
the whole point.

## Claude Code hook coordination (multi-session aware)

`gateway/integrations/claude-code/stackchan-hook.py` is the canonical
copy; it must be manually synced to `C:\Users\<user>\tools\stackchan-hook.py`
(the path actually referenced by `.claude/settings.json`) after every edit
— there is no automated sync.

Multiple Claude Code sessions can share this one physical device. State is
coordinated via marker files in `%TEMP%`:

- `stackchan-busy-<session_id>` — one per session, written by `busy-start`,
  removed by that session's own `say-done`. `is_busy()`/`_any_busy()`
  (idle-wander, LED chase) glob for any of these rather than checking one
  fixed name — a global marker previously let one session's completion
  wrongly clear another session's still-active busy state.
- `stackchan-needs-attention` — JSON `{session_id, project, ts}`, written
  by `urgent-say`. Takes priority over any busy chase (LED chase renders a
  slow red breathing pulse instead of amber while this is active). Cleared
  only when the *same* session_id's next `busy-start` or `say-done` fires
  — i.e. once the user has actually gone back and engaged with that
  session. A different session going busy must NOT clear it.
- `stackchan-hook.log` — append-only diagnostic log (mode, raw payload
  preview, constructed message, exceptions). This script used to swallow
  every exception silently with zero trace; if something isn't
  speaking/showing as expected, check this file first.

Spoken messages (both `urgent-say` and `say-done`) name the project
(`cwd` basename from the hook payload) so you can tell which session is
talking when more than one is active.

The urgent head-wobble + red-blink flourish is rate-limited to once per 30s
(`URGENT_MARKER`/`URGENT_COOLDOWN_S`) — repeated Notifications firing close
together was reported as nagging. Speech still fires every time regardless.

## Known issues, not yet fixed

See `firmware/TODO.md` for the full writeup of both. Short version:

1. **Touch sensor false-triggers on charging noise.** The capacitive
   head-touch sensor misreads charging-circuit EMI as a continuous
   head-stroke, firing the firmware's built-in touch-reaction wobble
   (yaw -20°→+20°→-20°→0°) every ~7-8s while charging. Confirmed via live
   log monitoring — stops instantly on unplugging. Current firmware
   source already has `set_touch_sensor_enabled` (the currently-flashed
   binary predates it) — likely fixable with a reflash alone, no new
   threshold tuning needed. Workaround until then: don't charge while
   interacting with the device.
2. **English wake word ("Hey Wheatley").** Currently a fixed Chinese
   acoustic model; would need an English MultiNet model swap
   (`USE_CUSTOM_WAKE_WORD`) + rebuild + reflash. Not urgent — voice control
   already works via touch-to-talk.

Both need the same rebuild+reflash window; batch them together.

## Working alongside another agent/session

This repo is sometimes being worked on by more than one Claude Code
session at once (e.g. a second window planning something in parallel).
`git status` will show files neither of you intentionally means to commit
together — **always check `git diff --stat` on exactly the files you
touched before staging**, and use `git add <specific files>` by name,
never `git add -A`/`.`, so you don't sweep up or interfere with another
session's in-progress work.

## Repo / remotes

- `origin` → `https://github.com/DJChilvers/stackchan-mcp` (private fork,
  personal backup — push here freely)
- `upstream` → `https://github.com/kisaragi-mochi/stackchan-mcp` (original
  project — do not push here)
