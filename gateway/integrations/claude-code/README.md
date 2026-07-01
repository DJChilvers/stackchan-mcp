# Claude Code integration (personal setup snapshot)

This folder is a backup of the Claude Code hook wiring used to give this
StackChan a "busy/idle/need-you" personality synced to an actual coding
session — not a generic install target. Paths inside both files are
absolute to this machine (`C:\Users\domin\...`) and would need editing to
reuse elsewhere.

- `stackchan-hook.py` — reacts to Claude Code's PreToolUse/PostToolUse/
  Notification/Stop hooks: sets the avatar face, head pitch, LED color/chase
  marker, and occasional speech depending on whether Claude is busy, done,
  or needs the user's attention. See the module docstring for the mode list.
- `claude-settings.json` — the `.claude/settings.json` that wires the above
  hooks into a Claude Code project (goes in the project's `.claude/` folder).

See the gateway's own `stackchan-led-chase.py`, `stackchan-idle.py`, and
`stackchan-voice-bridge.py` (one level up) for the rest of the personality
system this hook coordinates with.
