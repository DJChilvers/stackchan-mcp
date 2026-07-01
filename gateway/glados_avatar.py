#!/usr/bin/env python3
"""Generate a GLaDOS-style optic avatar set for StackChan (layered mode).

Draws a glowing aperture/optic in several moods, with the Claude starburst
as the iris for the "thinking" (doing-Claude-things) face and a red optic
for errors. Packs all 14 frames into the raw RGB565 layered payload that
the gateway's load_avatar_set(mode="layered") expects:

    faces (6):  idle, happy, thinking, sad, surprised, embarrassed
    eyes  (3):  eyes_open, eyes_half, eyes_closed
    mouths(5):  mouth_closed, mouth_half, mouth_open, mouth_e, mouth_u

Each frame is 160x120 RGB565 little-endian (38,400 bytes); 14 frames total
= 537,600 bytes. Output: glados_avatar.bin (+ optional PNG previews).
"""
from __future__ import annotations
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter
import numpy as np

W, H = 160, 120
SS = 4                      # supersample factor for smooth edges
CW, CH = W * SS, H * SS
CX, CY = CW // 2, CH // 2

OUT = Path(__file__).resolve().parent

# ----- palettes -------------------------------------------------------------
# Each: (core, bright, mid, dark, glow) as RGB tuples.
AMBER = dict(core=(255, 245, 215), bright=(255, 190, 70), mid=(225, 130, 18),
             dark=(70, 32, 0), glow=(255, 150, 30))
RED   = dict(core=(255, 225, 205), bright=(255, 78, 48), mid=(205, 26, 16),
             dark=(60, 0, 0),     glow=(255, 36, 18))
CLAUDE = (217, 119, 87)    # Claude clay/terracotta
CLAUDE_HOT = (255, 156, 110)


def _circle(d, cx, cy, r, fill):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=fill)


def _glow_layer(draw_fn):
    """Render bright content on black, blur it, return as RGB array to add."""
    g = Image.new("RGB", (CW, CH), (0, 0, 0))
    dg = ImageDraw.Draw(g)
    draw_fn(dg)
    g = g.filter(ImageFilter.GaussianBlur(radius=22 * SS // 4))
    return np.asarray(g, dtype=np.int16)


def _housing(d):
    """Dark optic housing + metallic rim, common to every frame."""
    # subtle outer rim
    _circle(d, CX, CY, int(57 * SS), (26, 26, 30))
    _circle(d, CX, CY, int(54 * SS), (12, 12, 14))
    _circle(d, CX, CY, int(50 * SS), (6, 6, 7))


def _draw_iris(d, pal, scale=1.0, bright=1.0):
    """Concentric glowing iris rings + hot core."""
    def mul(c, k):
        return tuple(min(255, int(v * k)) for v in c)
    r_out = int(44 * SS * scale)
    _circle(d, CX, CY, r_out,              mul(pal["dark"], bright))
    _circle(d, CX, CY, int(r_out * 0.80),  mul(pal["mid"], bright))
    _circle(d, CX, CY, int(r_out * 0.52),  mul(pal["bright"], bright))
    _circle(d, CX, CY, int(r_out * 0.24),  mul(pal["core"], bright))


def _draw_claude_iris(d, scale=1.0):
    """The Claude starburst as the iris."""
    r_out = int(40 * SS * scale)
    # amber backing ring so it reads as an optic
    _circle(d, CX, CY, int(r_out * 1.05), (90, 48, 6))
    _circle(d, CX, CY, int(r_out * 0.92), (30, 16, 2))
    n = 11
    inner = r_out * 0.20
    outer = r_out * 0.98
    half_w = math.radians(360 / n / 2 * 0.62)
    for i in range(n):
        a = math.radians(i * 360 / n - 90)
        pts = []
        for sgn in (-1, 1):
            aa = a + sgn * half_w
            pts.append((CX + inner * math.cos(aa), CY + inner * math.sin(aa)))
        for sgn in (1, -1):
            aa = a + sgn * (half_w * 0.55)
            pts.append((CX + outer * math.cos(aa), CY + outer * math.sin(aa)))
        d.polygon(pts, fill=CLAUDE_HOT)
    _circle(d, CX, CY, int(inner * 1.15), CLAUDE_HOT)


def _eyelids(d, frac):
    """Dark lids closing from top & bottom by `frac` of half-height (0..1)."""
    if frac <= 0:
        return
    lid = int(CH * 0.5 * frac)
    d.rectangle((0, 0, CW, lid), fill=(0, 0, 0))
    d.rectangle((0, CH - lid, CW, CH), fill=(0, 0, 0))


def render(mood: str) -> Image.Image:
    base = Image.new("RGB", (CW, CH), (0, 0, 0))
    d = ImageDraw.Draw(base)
    _housing(d)

    glow = None
    if mood == "idle":
        glow = _glow_layer(lambda g: _draw_iris(g, AMBER, 1.0, 1.0))
        _draw_iris(d, AMBER, 1.0, 1.0)
    elif mood == "happy":
        glow = _glow_layer(lambda g: _draw_iris(g, AMBER, 1.08, 1.25))
        _draw_iris(d, AMBER, 1.08, 1.18)
    elif mood == "surprised":
        glow = _glow_layer(lambda g: _draw_iris(g, AMBER, 1.18, 1.4))
        _draw_iris(d, AMBER, 1.18, 1.3)
    elif mood == "sad":            # RED error optic, contracted + dim
        glow = _glow_layer(lambda g: _draw_iris(g, RED, 0.82, 0.95))
        _draw_iris(d, RED, 0.82, 0.9)
    elif mood == "thinking":       # Claude starburst iris
        glow = _glow_layer(lambda g: _draw_claude_iris(g, 1.0))
        _draw_claude_iris(d, 1.0)
    elif mood == "embarrassed":    # wry half-lidded amber
        glow = _glow_layer(lambda g: _draw_iris(g, AMBER, 0.95, 1.0))
        _draw_iris(d, AMBER, 0.95, 1.0)
        _eyelids(d, 0.42)
    # ----- eye (blink) frames ------------------------------------------
    elif mood == "eyes_open":
        glow = _glow_layer(lambda g: _draw_iris(g, AMBER, 1.0, 1.0))
        _draw_iris(d, AMBER, 1.0, 1.0)
    elif mood == "eyes_half":
        glow = _glow_layer(lambda g: _draw_iris(g, AMBER, 1.0, 0.8))
        _draw_iris(d, AMBER, 1.0, 0.85)
        _eyelids(d, 0.5)
    elif mood == "eyes_closed":
        _eyelids(d, 0.93)          # aperture irised shut, faint line remains
        d.line((int(CW * 0.30), CY, int(CW * 0.70), CY), fill=(80, 40, 4),
               width=int(2 * SS))
    # ----- mouth (speech pulse) frames: optic flickers while talking ---
    elif mood == "mouth_closed":
        glow = _glow_layer(lambda g: _draw_iris(g, AMBER, 1.0, 1.0))
        _draw_iris(d, AMBER, 1.0, 1.0)
    elif mood == "mouth_half":
        glow = _glow_layer(lambda g: _draw_iris(g, AMBER, 1.05, 1.2))
        _draw_iris(d, AMBER, 1.05, 1.15)
    elif mood == "mouth_open":
        glow = _glow_layer(lambda g: _draw_iris(g, AMBER, 1.12, 1.45))
        _draw_iris(d, AMBER, 1.12, 1.35)
    elif mood == "mouth_e":
        glow = _glow_layer(lambda g: _draw_iris(g, AMBER, 1.0, 1.1))
        _draw_iris(d, AMBER, 1.0, 1.05)
    elif mood == "mouth_u":
        glow = _glow_layer(lambda g: _draw_iris(g, AMBER, 0.95, 1.15))
        _draw_iris(d, AMBER, 0.95, 1.1)
    else:
        raise ValueError(mood)

    arr = np.asarray(base, dtype=np.int16)
    if glow is not None:
        arr = np.clip(arr + (glow * 0.55).astype(np.int16), 0, 255)
    img = Image.fromarray(arr.astype(np.uint8), "RGB")
    return img.resize((W, H), Image.LANCZOS)


def to_rgb565(img: Image.Image) -> bytes:
    a = np.asarray(img.convert("RGB"), dtype=np.uint16)
    r = (a[:, :, 0] >> 3) & 0x1F
    g = (a[:, :, 1] >> 2) & 0x3F
    b = (a[:, :, 2] >> 3) & 0x1F
    v = ((r << 11) | (g << 5) | b).astype("<u2")
    return v.tobytes()


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
            img.save(OUT / f"glados_{name}.png")
    assert len(payload) == 14 * 160 * 120 * 2, len(payload)
    (OUT / "glados_avatar.bin").write_bytes(payload)
    preview.save(OUT / "glados_preview.png")
    print(f"OK: glados_avatar.bin = {len(payload)} bytes")
    print(f"preview: {OUT / 'glados_preview.png'}")


if __name__ == "__main__":
    main()
