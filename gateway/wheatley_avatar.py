#!/usr/bin/env python3
"""Generate a Wheatley (Portal 2) optic avatar set for StackChan.

Wheatley's eye is a glowing BLUE gradient optic (bright cyan core -> deep blue
rim) inside a dark aperture housing, with metallic eyelid shutters that do the
emoting (wide = shocked, angled = worried, half = his nervous blink). This is
the GLaDOS optic treatment recoloured blue, plus mechanical lids.

The iris becomes the Claude starburst for the "thinking" face; errors turn the
optic red and snap the eye wide (panic).

Two output modes, both built from the SAME three-axis model (face / eyes /
mouth) so a look is always the composition of independent parts rather than
14 mutually-exclusive whole-face pictures:

  face  (6): horizontal gaze + expression identity (ox offset, scale, bright,
             a lid-squint baseline for that expression)
  eyes  (3): blink closure amount, ADDED on top of the face's lid baseline
             (firmware auto-cycles this for real blinking)
  mouth (5): closed/half/open drive real lip-sync (firmware auto-cycles these
             during speech — near-neutral here); e/u are the vertical
             "glance up" / kung-fu-flutter frames, carrying an oy (and small
             ox) offset that composites on top of WHATEVER face is active

--matrix (default): all 90 face x eyes x mouth combinations pre-composited,
    so e.g. "thinking" (look left) + "mouth_e" (glance up) really renders as
    looking up-and-left — genuine independent multi-axis gaze, not just 6
    fixed poses. 160x120 RGB565, 3,456,000 bytes, idx = face*15 + eyes*5 + mouth.
--layered: the original 14-frame set (one combo picked per name, see
    LAYERED_COMBOS below) for devices/tests that still want the smaller set.
"""
from __future__ import annotations
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter
import numpy as np

W, H = 160, 120
SS = 4
CW, CH = W * SS, H * SS
CX, CY = CW // 2, CH // 2
OUT = Path(__file__).resolve().parent

APERTURE_R = int(55 * SS)        # dark housing inner radius (optic lives here)
OPTIC_R = int(50 * SS)           # blue gradient outer radius

BLUE = dict(core=(225, 248, 255), bright=(72, 182, 255), mid=(26, 108, 208),
            dark=(8, 30, 82), glow=(46, 150, 255))
RED = dict(core=(255, 226, 208), bright=(255, 80, 50), mid=(206, 28, 18),
           dark=(60, 2, 2), glow=(255, 40, 20))
LID = (26, 28, 34)
LID_HI = (54, 58, 68)
CLAUDE_HOT = (255, 156, 110)

# A device sitting below the user's eyeline reads as more attentive if its
# resting gaze is biased very slightly up — applied to every combo, not a
# per-face choice.
GLOBAL_OY_BIAS = -2 * SS


def _circle(d, cx, cy, r, fill):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)


def _optic_layer(pal, scale, bright, ox, oy, claude=False):
    """Render the glowing optic (with glow halo) on its own RGB array."""
    lens = Image.new("RGB", (CW, CH), (0, 0, 0))
    d = ImageDraw.Draw(lens)
    cx, cy = CX + ox, CY + oy

    def mul(c, k):
        return tuple(min(255, int(v * k)) for v in c)

    if claude:
        r = int(OPTIC_R * 0.92 * scale)
        _circle(d, cx, cy, int(r * 1.12), (34, 20, 6))
        _circle(d, cx, cy, r, (12, 8, 2))
        n = 11
        inner, outer = r * 0.20, r * 0.98
        hw = math.radians(360 / n / 2 * 0.62)
        for i in range(n):
            a = math.radians(i * 360 / n - 90)
            pts = []
            for s in (-1, 1):
                pts.append((cx + inner * math.cos(a + s * hw),
                            cy + inner * math.sin(a + s * hw)))
            for s in (1, -1):
                pts.append((cx + outer * math.cos(a + s * hw * 0.55),
                            cy + outer * math.sin(a + s * hw * 0.55)))
            d.polygon(pts, fill=CLAUDE_HOT)
        _circle(d, cx, cy, int(inner * 1.15), CLAUDE_HOT)
    else:
        r = int(OPTIC_R * scale)
        _circle(d, cx, cy, r,              mul(pal["dark"], bright))
        _circle(d, cx, cy, int(r * 0.80),  mul(pal["mid"], bright))
        _circle(d, cx, cy, int(r * 0.50),  mul(pal["bright"], bright))
        _circle(d, cx, cy, int(r * 0.22),  mul(pal["core"], bright))
        # specular glint, upper-left — the bit that makes him feel alive
        _circle(d, cx - int(r * 0.34), cy - int(r * 0.40), int(r * 0.12),
                (245, 252, 255))

    # glow halo
    glow = lens.filter(ImageFilter.GaussianBlur(radius=20 * SS // 4))
    arr = np.clip(np.asarray(lens, np.int16)
                  + (np.asarray(glow, np.int16) * 0.5).astype(np.int16), 0, 255)
    return arr.astype(np.uint8)


def _aperture_mask():
    m = Image.new("L", (CW, CH), 0)
    ImageDraw.Draw(m).ellipse(
        (CX - APERTURE_R, CY - APERTURE_R, CX + APERTURE_R, CY + APERTURE_R),
        fill=255)
    return m


def _housing(d):
    _circle(d, CX, CY, APERTURE_R + int(6 * SS), (24, 25, 30))   # socket
    _circle(d, CX, CY, APERTURE_R + int(2 * SS), (40, 42, 50))   # rim
    _circle(d, CX, CY, APERTURE_R, (6, 7, 9))                    # dark backing
    # iris-diaphragm blade hints around the rim
    for i in range(12):
        a = math.radians(i * 30)
        x1 = CX + (APERTURE_R - int(2 * SS)) * math.cos(a)
        y1 = CY + (APERTURE_R - int(2 * SS)) * math.sin(a)
        x2 = CX + (APERTURE_R - int(9 * SS)) * math.cos(a)
        y2 = CY + (APERTURE_R - int(9 * SS)) * math.sin(a)
        d.line((x1, y1, x2, y2), fill=(30, 32, 40), width=int(2 * SS))


def _lids(d, top=0.0, bot=0.0, angle=0.0, worried=False):
    """Metallic eyelid shutters, full screen width edge-to-edge. top/bot =
    fraction closed; angle tilts lid. Anchored at the canvas top/bottom edge
    (not the housing rim) so top=0/bot=0 retracts the shutter completely off
    -screen — no sliver visible when "open"."""
    R = APERTURE_R + int(8 * SS)
    if top > 0 or worried:
        t = max(top, 0.0)
        yl = int(2 * R * t)
        dx = math.tan(angle) * R
        if worried:
            pts = [(0, 0), (CW, 0),
                   (CW, yl - dx), (CX, yl + int(R * 0.30)), (0, yl - dx)]
        else:
            pts = [(0, 0), (CW, 0),
                   (CW, yl - dx), (0, yl + dx)]
        d.polygon(pts, fill=LID)
        d.line(pts[2:] if not worried else [pts[2], pts[3], pts[4]],
               fill=LID_HI, width=int(2 * SS))
    if bot > 0:
        yl = CH - int(2 * R * bot)
        d.polygon([(0, CH), (CW, CH),
                   (CW, yl), (0, yl)], fill=LID)
        d.line([(0, yl), (CW, yl)], fill=LID_HI, width=int(2 * SS))


def _clamp_gaze(r: int, ox: int, oy: int, margin: int) -> tuple[int, int]:
    """Cap the optic's (ox, oy) offset so it never crosses the aperture mask
    — without this, a big enough gaze offset pushes the glowing disc past
    the socket rim and it looks like the eye is overflowing/leaving the
    screen rather than glancing within its housing."""
    max_d = max(APERTURE_R - r - margin, 0)
    d = math.hypot(ox, oy)
    if d > max_d and d > 0:
        k = max_d / d
        return int(ox * k), int(oy * k)
    return ox, oy


# ── three independent axes ───────────────────────────────────────────────────
# face: horizontal gaze/expression identity. lid_top/lid_bot are that
# expression's OWN baseline squint — eyes axis adds blink closure on top.
# tilt: a small fixed lid-cant (radians) baked per expression — reference
# footage of the physical Wheatley prop shows he's almost never dead level;
# the whole housing rides at a slight roll while swinging on his rail. Our
# servos have no roll axis, so this fakes it by tilting the lid line instead
# of the (rotationally-symmetric) optic — cheapest way to read as "canted".
FACE_SPECS = {
    "idle":        dict(ox=0,       scale=1.00, bright=1.00, claude=False, lid_top=0.00, lid_bot=0.00, tilt=0.00),
    "happy":       dict(ox=12 * SS, scale=0.74, bright=1.06, claude=False, lid_top=0.15, lid_bot=0.09, tilt=0.08),   # look RIGHT, leans into it
    "thinking":    dict(ox=-12 * SS, scale=0.74, bright=1.06, claude=False, lid_top=0.15, lid_bot=0.09, tilt=-0.08),  # look LEFT, leans into it
    "sad":         dict(ox=0,       scale=1.30, bright=1.12, claude=False, lid_top=0.30, lid_bot=0.24, tilt=0.00),   # EXAMINE: zoom+squint
    "surprised":   dict(ox=0,       scale=1.20, bright=1.28, claude=False, lid_top=0.00, lid_bot=0.00, tilt=0.00),   # WIDE
    "embarrassed": dict(ox=0,       scale=1.00, bright=1.00, claude=False, lid_top=0.20, lid_bot=0.14, tilt=0.10),   # MILD, worried cant
}

# eyes: blink closure, additive on top of the face's lid baseline.
EYES_SPECS = {
    "eyes_open":   dict(dtop=0.00, dbot=0.00),
    "eyes_half":   dict(dtop=0.30, dbot=0.24),   # nervous half-blink / downcast-thoughtful look
    "eyes_closed": dict(dtop=0.50, dbot=0.48),
}

# mouth: closed/half/open are real lip-sync (firmware auto-cycles these
# during speech) so they stay near-neutral. e/u are the two matrix-mode
# vertical gaze cues, composited onto whatever face is currently active —
# giving a genuine diagonal look (e.g. thinking + mouth_e = look
# up-and-left) rather than a fixed pose.
#
# 2026-07-01: mouth_u used to be a SECOND "up" variant (paired with mouth_e
# for the kung-fu flutter, both rolling upward). User wanted a real,
# independently-controllable "look down" to pair with head pitch — the
# matrix's 90 frames are a fixed 6x3x5 cross-product (firmware-hardcoded,
# not expandable without a reflash), so there's no literal free slot to
# add; the only slot not already committed to real lip-sync is this one.
# Flipped mouth_u's oy sign so it's a genuine down-glance instead of a
# second up-glance. The kung-fu flutter (stackchan-hook.py busy-continue)
# was reworked to alternate mouth_e with mouth_closed instead of e/u, so it
# still flutters using only the up cue, freeing this one for down.
# clip_margin: how much of the optic is ALLOWED to clip against the
# aperture edge (fed to _clamp_gaze as its margin). Real eyes don't shrink
# when they roll up/down — the iris just partially disappears behind the
# lid/socket rim. closed/half/open keep the protective default (never
# clip — they're driven by real speech, need to stay legible); e/u use a
# deliberately negative margin so the same-size optic is allowed to push
# past the aperture boundary and clip naturally instead of shrinking.
MOUTH_SPECS = {
    "mouth_closed": dict(oy=0,        ox=0, bright_mult=1.00, scale_mult=1.00, clip_margin=3 * SS),
    "mouth_half":   dict(oy=0,        ox=0, bright_mult=1.12, scale_mult=1.00, clip_margin=3 * SS),
    "mouth_open":   dict(oy=0,        ox=0, bright_mult=1.30, scale_mult=1.00, clip_margin=3 * SS),
    "mouth_e":      dict(oy=-24 * SS, ox=0, bright_mult=1.30, scale_mult=1.00, clip_margin=-14 * SS),  # glance UP
    "mouth_u":      dict(oy=24 * SS,  ox=0, bright_mult=1.00, scale_mult=1.00, clip_margin=-14 * SS),  # glance DOWN
}

FACES = ["idle", "happy", "thinking", "sad", "surprised", "embarrassed"]
EYES = ["eyes_open", "eyes_half", "eyes_closed"]
MOUTHS = ["mouth_closed", "mouth_half", "mouth_open", "mouth_e", "mouth_u"]

# Legacy 14-frame layered set: one fixed (face, eyes, mouth) combo per name,
# chosen to reproduce the original single-picture look for each slot.
LAYERED_COMBOS = {
    "idle":         ("idle", "eyes_open", "mouth_closed"),
    "happy":        ("happy", "eyes_open", "mouth_closed"),
    "thinking":     ("thinking", "eyes_open", "mouth_closed"),
    "sad":          ("sad", "eyes_open", "mouth_closed"),
    "surprised":    ("surprised", "eyes_open", "mouth_closed"),
    "embarrassed":  ("embarrassed", "eyes_open", "mouth_closed"),
    "eyes_open":    ("idle", "eyes_open", "mouth_closed"),
    "eyes_half":    ("idle", "eyes_half", "mouth_closed"),
    "eyes_closed":  ("idle", "eyes_closed", "mouth_closed"),
    "mouth_closed": ("idle", "eyes_open", "mouth_closed"),
    "mouth_half":   ("idle", "eyes_open", "mouth_half"),
    "mouth_open":   ("idle", "eyes_open", "mouth_open"),
    "mouth_e":      ("idle", "eyes_open", "mouth_e"),
    "mouth_u":      ("idle", "eyes_open", "mouth_u"),
}
LAYERED_ORDER = FACES + EYES + MOUTHS


def render_combo(face: str, eyes: str, mouth: str) -> Image.Image:
    f = FACE_SPECS[face]
    e = EYES_SPECS[eyes]
    m = MOUTH_SPECS[mouth]

    # Horizontal sign is negated here — confirmed 2026-07-01 via an
    # eye-only (no head movement) live test that FACE_SPECS's "thinking"
    # (ox negative) rendered on the VIEWER'S right, not left. The spec
    # values below are unchanged/self-consistent; only the direction they
    # map to on screen was backwards. Vertical (oy) was confirmed correct
    # by the same test (mouth_e rendered up, as intended) so it is NOT
    # negated. Do not also compensate for this in wander()'s LOOK_LEFT/
    # LOOK_RIGHT pairing — that earlier swap was masking this same bug
    # and must be reverted now that the root cause is fixed here instead.
    ox = -(f["ox"] + m["ox"])
    oy = GLOBAL_OY_BIAS + m["oy"]
    scale = f["scale"] * m["scale_mult"]
    bright = f["bright"] * m["bright_mult"]
    top = min(f["lid_top"] + e["dtop"], 0.62)
    bot = min(f["lid_bot"] + e["dbot"], 0.62)
    claude = f["claude"]

    base = Image.new("RGB", (CW, CH), (0, 0, 0))
    d = ImageDraw.Draw(base)
    _housing(d)
    r = int((OPTIC_R * 0.92 if claude else OPTIC_R) * scale)
    ox, oy = _clamp_gaze(r, ox, oy, margin=m["clip_margin"])
    optic = Image.fromarray(_optic_layer(BLUE, scale, bright, ox, oy, claude), "RGB")
    base = Image.composite(optic, base, _aperture_mask())
    d = ImageDraw.Draw(base)
    _lids(d, top, bot, angle=f["tilt"])
    return base.resize((W, H), Image.LANCZOS)


def to_rgb565(img: Image.Image) -> bytes:
    a = np.asarray(img.convert("RGB"), dtype=np.uint16)
    r = (a[:, :, 0] >> 3) & 0x1F
    g = (a[:, :, 1] >> 2) & 0x3F
    b = (a[:, :, 2] >> 3) & 0x1F
    return (((r << 11) | (g << 5) | b).astype("<u2")).tobytes()


def build_matrix(save_png: bool = False) -> bytes:
    payload = bytearray()
    preview = Image.new("RGB", (W * 15, H * 6), (0, 0, 0))
    for fi, face in enumerate(FACES):
        for ei, eyes in enumerate(EYES):
            for mi, mouth in enumerate(MOUTHS):
                img = render_combo(face, eyes, mouth)
                payload += to_rgb565(img)
                col = ei * 5 + mi
                preview.paste(img, (col * W, fi * H))
                if save_png:
                    img.save(OUT / f"wheatley_matrix_{face}_{eyes}_{mouth}.png")
    assert len(payload) == 90 * 160 * 120 * 2, len(payload)
    (OUT / "wheatley_avatar_matrix.bin").write_bytes(payload)
    preview.save(OUT / "wheatley_matrix_preview.png")
    return bytes(payload)


def build_layered(save_png: bool = False) -> bytes:
    payload = bytearray()
    preview = Image.new("RGB", (W * len(LAYERED_ORDER), H), (0, 0, 0))
    for i, name in enumerate(LAYERED_ORDER):
        img = render_combo(*LAYERED_COMBOS[name])
        payload += to_rgb565(img)
        preview.paste(img, (i * W, 0))
        if save_png:
            img.save(OUT / f"wheatley_{name}.png")
    assert len(payload) == 14 * 160 * 120 * 2, len(payload)
    (OUT / "wheatley_avatar.bin").write_bytes(payload)
    preview.save(OUT / "wheatley_preview.png")
    return bytes(payload)


def main():
    save_png = "--png" in sys.argv
    layered_only = "--layered" in sys.argv
    if layered_only:
        build_layered(save_png)
        print(f"OK: wheatley_avatar.bin = {14 * 160 * 120 * 2} bytes (layered)")
    else:
        build_matrix(save_png)
        print(f"OK: wheatley_avatar_matrix.bin = {90 * 160 * 120 * 2} bytes (matrix)")


if __name__ == "__main__":
    main()
