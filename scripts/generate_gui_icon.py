"""Generate the GUI application icon (scripts/gui_icon.ico).

Concept: a Gemini-style four-point sparkle being ERASED on a modern blue-cyan
gradient — the tool's job in one glyph. Renders at 256px and downscales to the
standard ICO sizes, so the small taskbar sizes stay recognizable.

Re-run `uv run python scripts/generate_gui_icon.py` to regenerate.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 256
OUT = Path(__file__).resolve().parent / "gui_icon.ico"


def _lerp(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))  # type: ignore[return-value]


def _polygon_points(cx: float, cy: float, radius: float, n: int, rotation: float = 0.0) -> list[tuple[float, float]]:
    return [
        (cx + radius * math.cos(2 * math.pi * i / n + rotation), cy + radius * math.sin(2 * math.pi * i / n + rotation))
        for i in range(n)
    ]


def _four_point_sparkle(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    """Eight vertices: an upright diamond plus a rotated square — the classic
    four-point star (Google-style sparkle)."""
    pts = _polygon_points(cx, cy, r, 4, math.pi / 4)  # upright diamond
    pts += _polygon_points(cx, cy, r * 0.62, 4, math.pi / 4 + math.pi / 4)
    return pts


def _rounded_rect(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    radius: int,
    fill: tuple[int, int, int, int],
) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def main() -> None:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── background: rounded square, vertical blue->cyan gradient ─────────────
    top = (15, 42, 94)
    mid = (7, 94, 160)
    bottom = (6, 182, 212)
    bg = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    bgd = ImageDraw.Draw(bg)
    for y in range(SIZE):
        t = y / (SIZE - 1)
        color = _lerp(top, mid, min(1.0, t * 1.6)) if t < 0.62 else _lerp(mid, bottom, (t - 0.62) / 0.38)
        bgd.line([(0, y), (SIZE, y)], fill=(*color, 255))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([8, 8, SIZE - 8, SIZE - 8], radius=52, fill=255)
    img.paste(bg, (0, 0), mask)

    # ── soft top highlight for depth ──────────────────────────────────────────
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for y in range(96):
        gd.line([(16, y + 8), (SIZE - 16, y + 8)], fill=(255, 255, 255, max(0, 26 - y // 4)))
    img.alpha_composite(glow, (0, 0))

    # ── sparkle (the mark being removed) ──────────────────────────────────────
    cx, cy = 128, 118
    r = 84
    pts = _four_point_sparkle(cx, cy, r)
    draw.polygon(pts, fill=(255, 255, 255, 255))
    # thin inner shading so the star reads crisply against the gradient
    shade = _four_point_sparkle(cx, cy, r * 0.86)
    draw.polygon(shade, fill=(235, 248, 255, 255))

    # ── erase stroke sweeping across the sparkle (45 degrees, fading ends) ───
    # The stroke is drawn as stacked translucent quads, heaviest in the middle.
    for alpha, width in ((120, 34), (90, 30), (55, 22)):
        # parallelogram along the (1,1) diagonal through the star centre
        half = width / 2.0
        perp = (-1, 1)
        norm = math.sqrt(2)
        ux, uy = 1 / norm, 1 / norm
        px, py = perp[0] / norm, perp[1] / norm
        # centre of the stroke sits slightly past the star centre
        sx, sy = cx + 8, cy + 8
        quad = [
            (sx - ux * 70 + px * half, sy - uy * 70 + py * half),
            (sx + ux * 70 + px * half, sy + uy * 70 + py * half),
            (sx + ux * 70 - px * half, sy + uy * 70 - py * half),
            (sx - ux * 70 - px * half, sy - uy * 70 - py * half),
        ]
        draw.polygon(quad, fill=(255, 255, 255, alpha))

    # ── top-right accent: a small cyan check dot (done / verified) ───────────
    dot_r = 17
    dot = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(dot).ellipse(
        [SIZE - 40 - dot_r, 40 - dot_r, SIZE - 40 + dot_r, 40 + dot_r], fill=(64, 235, 255, 255)
    )
    img.alpha_composite(dot, (0, 0))

    # ── write the multi-size ICO ──────────────────────────────────────────────
    img.save(OUT, format="ICO", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
