"""
Odoo Backup Tool — Icon generator.

Produces icon.ico with multiple sizes for the Windows executable and
the application window.

Design:
  - Odoo purple (#714B67) rounded-square background
  - White database cylinder (3 horizontal platters)
  - Teal (#00A09D) accent stripe on the top platter (echoes Odoo's brand stripe)

Uses 4x supersampling + LANCZOS downscale for smooth edges at all sizes.

Run once before building:
    python create_icon.py
"""
from __future__ import annotations

import os
import sys

try:
    from PIL import Image, ImageDraw
except ImportError:
    sys.exit(
        "Pillow no esta instalado. Ejecuta: pip install Pillow\n"
        "O usa: pip install -r requirements.txt"
    )

# ── Palette ───────────────────────────────────────────────────────────────────
_PURPLE     = (113, 75, 103, 255)   # Odoo brand purple  #714B67
_PURPLE_DRK = (80,  50,  72, 255)   # Darker shade for inner shadow
_TEAL       = (0,  160, 157, 255)   # Odoo teal          #00A09D
_WHITE      = (255, 255, 255, 255)
_PLATTER_MID= (228, 212, 223, 255)  # Mid platter — slightly purple-tinted
_PLATTER_BOT= (196, 174, 190, 255)  # Bottom platter — more muted


def _draw_icon(size: int) -> Image.Image:
    """
    Draw a single icon frame at the given pixel size.

    Renders internally at 4× resolution then scales down with LANCZOS
    for antialiased curves without requiring a third-party AA library.

    Args:
        size: Target icon size in pixels (e.g. 32, 128).

    Returns:
        RGBA Image of exactly (size × size) pixels.
    """
    S = size * 4  # supersampled canvas

    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)

    # ── Background: rounded square in Odoo purple ─────────────────────────
    pad    = max(S // 16, 4)
    radius = S // 5
    d.rounded_rectangle(
        [pad, pad, S - pad - 1, S - pad - 1],
        radius=radius,
        fill=_PURPLE,
    )

    # ── Database cylinder geometry ────────────────────────────────────────
    cx = S // 2
    ew = int(S * 0.54)          # total ellipse width
    eh = int(S * 0.15)          # ellipse height (flat = looks like a disc)
    eh = max(eh, 6)

    left  = cx - ew // 2
    right = cx + ew // 2

    # Three platters from top to bottom
    top_y   = int(S * 0.18)
    spacing = int(S * 0.215)

    platters_y    = [top_y, top_y + spacing, top_y + spacing * 2]
    platter_fills = [_WHITE, _PLATTER_MID, _PLATTER_BOT]

    # Draw cylinder bodies (rectangles between consecutive platter centers),
    # bottom to top so the upper platter renders on top.
    for i in range(len(platters_y) - 1, 0, -1):
        y0 = platters_y[i - 1] + eh // 2
        y1 = platters_y[i]     + eh // 2
        d.rectangle([left, y0, right, y1], fill=platter_fills[i])

    # Draw each platter ellipse, bottom to top
    for i in range(len(platters_y) - 1, -1, -1):
        py = platters_y[i]
        d.ellipse([left, py, right, py + eh], fill=platter_fills[i])

    # ── Teal accent stripe on the top platter ─────────────────────────────
    # Mirrors Odoo's brand stripe concept — a thin contrasting band.
    stripe_h = max(S // 28, 3)
    inset    = ew // 5
    ty       = platters_y[0] + eh // 2 - stripe_h // 2
    d.ellipse(
        [left + inset, ty, right - inset, ty + stripe_h],
        fill=_TEAL,
    )

    # ── Scale down with LANCZOS for antialiasing ──────────────────────────
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    sizes   = [16, 24, 32, 48, 64, 128, 256]
    images  = [_draw_icon(s) for s in sizes]

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")

    images[0].save(
        out_path,
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=images[1:],
    )

    print(f"Icono generado correctamente: {out_path}")
    print(f"Tamanios incluidos: {sizes}")


if __name__ == "__main__":
    main()
