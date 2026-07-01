#!/usr/bin/env python3
"""Generate a Wheatley (Portal 2) optic avatar set for StackChan (layered mode).

Wheatley's eye is a glowing BLUE gradient optic (bright cyan core -> deep blue
rim) inside a dark aperture housing, with metallic eyelid shutters that do the
emoting (wide = shocked, angled = worried, half = his nervous blink). This is
the GLaDOS optic treatment recoloured blue, plus mechanical lids — which the
reference video confirms is correct (it is NOT a realistic eyeball).

The iris becomes the Claude starburst for the "thinking" face; errors turn the
optic red and snap the eye wide (panic). 14 frames (6 faces + 3 eyes +
5 mouths), 160x120 RGB565 little-endian, 537,600 bytes total.

Frame order:
    faces : idle, happy, thinking, sad, surprised, embarrassed
    eyes  : eyes_open, eyes_half, eyes_closed
    mouths: mouth_closed, mouth_half, mouth_open, mouth_e, mouth_u
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


# (mood) -> dict(pal, scale, bright, ox, oy, claude, top, bot, angle, worried)
def _spec(mood):
    S = SS
    P = dict(pal=BLUE, scale=1.0, bright=1.0, ox=0, oy=0, claude=False,
             top=0.0, bot=0.0, angle=0.0, worried=False)
    if mood == "idle":
        P.update(top=0.0, bot=0.0, oy=-2 * S)
    # --- WANDER gaze frames (only ever set by the idle wander, never by hooks
    #     or firmware touch reactions, so they can safely move the optic) ---
    elif mood == "happy":        # WANDER: look RIGHT — optic darts toward the edge
        P.update(ox=12 * S, scale=0.74, bright=1.06, top=0.15, bot=0.09)
    elif mood == "thinking":     # WANDER: look LEFT — optic darts toward the edge
        P.update(ox=-12 * S, scale=0.74, bright=1.06, top=0.15, bot=0.09)
    elif mood == "sad":          # WANDER: EXAMINE — zoom in + squint (lids lowered)
        P.update(scale=1.30, bright=1.12, top=0.30, bot=0.24)
    # --- CENTERED frames (safe for firmware touch reactions + work hooks) ---
    elif mood == "surprised":    # wide reaction, centered (touch-tap / notification)
        P.update(scale=1.20, bright=1.28)
    elif mood == "embarrassed":  # CENTERED mild reaction (firmware fires on head-stroke);
        P.update(bright=1.0, top=0.20, bot=0.14)                   # MUST stay centered so phantom touches can't dart the eye
    elif mood == "eyes_open":
        P.update(top=0.0, bot=0.0, oy=-2 * S)
    elif mood == "eyes_half":
        P.update(top=0.44, bot=0.36)                               # nervous half-blink
    elif mood == "eyes_closed":
        P.update(top=0.54, bot=0.52)
    elif mood == "mouth_closed":
        P.update(top=0.0, bot=0.0, oy=-2 * S)
    elif mood == "mouth_half":   # lip-sync: pulse via brightness/lids only (no ox dart)
        P.update(bright=1.12, top=0.16, bot=0.10)
    elif mood == "mouth_open":
        P.update(bright=1.3, oy=-2 * S, top=0.06, bot=0.05)
    elif mood == "mouth_e":      # KUNG-FU FLUTTER frame A (repurposed — see below)
        P.update(scale=0.65, bright=1.3, oy=-15 * S, ox=6 * S, top=0.35, bot=0.05)
    elif mood == "mouth_u":      # KUNG-FU FLUTTER frame B (repurposed — see below)
        P.update(scale=0.70, bright=1.15, oy=-10 * S, ox=-6 * S, top=0.22, bot=0.08)
    # mouth_e/mouth_u repurposed 2026-06-30: firmware's own lip-sync
    # auto-cycle only ever walks closed -> half -> open -> half -> closed,
    # so these two slots are never touched by real speech — free to reuse.
    # Triggered via set_mouth_sequence (firmware-local step queue) on each
    # completed tool call during busy/coding stretches: the optic rolls up
    # and tucks toward the top lid, alternating A/B for a fast flutter —
    # the "Neo learning kung fu" download-look. See stackchan-hook.py.
    else:
        raise ValueError(mood)
    return P


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


def render(mood: str) -> Image.Image:
    P = _spec(mood)
    base = Image.new("RGB", (CW, CH), (0, 0, 0))
    d = ImageDraw.Draw(base)
    _housing(d)
    r = int((OPTIC_R * 0.92 if P["claude"] else OPTIC_R) * P["scale"])
    P["ox"], P["oy"] = _clamp_gaze(r, P["ox"], P["oy"], margin=int(3 * SS))
    optic = Image.fromarray(
        _optic_layer(P["pal"], P["scale"], P["bright"], P["ox"], P["oy"],
                     P["claude"]), "RGB")
    base = Image.composite(optic, base, _aperture_mask())
    d = ImageDraw.Draw(base)
    _lids(d, P["top"], P["bot"], P["angle"], P["worried"])
    return base.resize((W, H), Image.LANCZOS)


def to_rgb565(img: Image.Image) -> bytes:
    a = np.asarray(img.convert("RGB"), dtype=np.uint16)
    r = (a[:, :, 0] >> 3) & 0x1F
    g = (a[:, :, 1] >> 2) & 0x3F
    b = (a[:, :, 2] >> 3) & 0x1F
    return (((r << 11) | (g << 5) | b).astype("<u2")).tobytes()


FACES = ["idle", "happy", "thinking", "sad", "surprised", "embarrassed"]
EYES = ["eyes_open", "eyes_half", "eyes_closed"]
MOUTHS = ["mouth_closed", "mouth_half", "mouth_open", "mouth_e", "mouth_u"]
ORDER = FACES + EYES + MOUTHS


def main():
    save_png = "--png" in sys.argv
    payload = bytearray()
    preview = Image.new("RGB", (W * len(ORDER), H), (0, 0, 0))
    for i, name in enumerate(ORDER):
        img = render(name)
        payload += to_rgb565(img)
        preview.paste(img, (i * W, 0))
        if save_png:
            img.save(OUT / f"wheatley_{name}.png")
    assert len(payload) == 14 * 160 * 120 * 2, len(payload)
    (OUT / "wheatley_avatar.bin").write_bytes(payload)
    preview.save(OUT / "wheatley_preview.png")
    print(f"OK: wheatley_avatar.bin = {len(payload)} bytes")


if __name__ == "__main__":
    main()
