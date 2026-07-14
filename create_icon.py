"""
Odoo Backup Tool — Icon generator.

Converts new_image.png (512x512 RGBA) into icon.ico with multiple embedded
sizes for the Windows executable and the application window.

Quality strategy:
  - LANCZOS resampling for all downscale steps (best edge preservation).
  - Light UnsharpMask pass on sizes <= 32 px to recover fine detail lost
    when reducing to very small icons (standard technique in professional
    icon pipelines — avoids the soft/blurry look without visible halos).
  - All sizes embedded in a single ICO file so Windows picks the right one
    per context (taskbar, explorer, title bar, Alt+Tab, etc.).

Source image requirements:
  - new_image.png in the same directory as this script.
  - 512x512 px or larger, RGBA (transparency supported).
  - Minimum recommended: 256x256.

Run once before building:
    python create_icon.py
"""
from __future__ import annotations

import os
import sys

try:
    from PIL import Image, ImageFilter
except ImportError:
    sys.exit(
        "Pillow no esta instalado. Ejecuta: pip install Pillow\n"
        "O usa: pip install -r requirements.txt"
    )

# ── Configuration ─────────────────────────────────────────────────────────────

# Source PNG relative to this script
_SOURCE_NAME = "new_image.png"

# Sizes embedded in the final .ico
# Windows uses different sizes in different contexts:
#   16  — title bar, small taskbar icons
#   24  — some UI contexts in Win11
#   32  — standard desktop icon (small view)
#   48  — standard desktop icon (medium view)
#   64  — large icon views, some file manager contexts
#   128 — extra-large icon view
#   256 — jumbo icon view, modern Windows contexts
_SIZES = [16, 24, 32, 48, 64, 128, 256]

# Threshold below which UnsharpMask is applied to recover fine detail
_SHARPEN_THRESHOLD_PX = 32

# UnsharpMask parameters (conservative — avoids visible halos)
# radius: spread of the sharpening effect (px in the downscaled image)
# percent: strength (100 = subtle, 200 = strong)
# threshold: only sharpen where pixel difference > this value (0-255)
_USM_RADIUS    = 0.6
_USM_PERCENT   = 120
_USM_THRESHOLD = 3


def _resize_to(source: Image.Image, size: int) -> Image.Image:
    """
    Resize `source` to (size x size) with LANCZOS + optional UnsharpMask.

    The source is always treated as RGBA to preserve transparency.

    Args:
        source: Source image (should be RGBA, any size >= target).
        size:   Target square size in pixels.

    Returns:
        RGBA Image of exactly (size x size) pixels.
    """
    # Ensure RGBA so transparent areas survive the resize correctly
    img = source.convert("RGBA")

    # LANCZOS (formerly ANTIALIAS) gives the sharpest edges on downscale
    img = img.resize((size, size), Image.LANCZOS)

    # For very small sizes, add a subtle sharpening pass to compensate
    # for the softening effect of aggressive downscaling.
    if size <= _SHARPEN_THRESHOLD_PX:
        img = img.filter(
            ImageFilter.UnsharpMask(
                radius=_USM_RADIUS,
                percent=_USM_PERCENT,
                threshold=_USM_THRESHOLD,
            )
        )

    return img


def main() -> None:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    source_path = os.path.join(script_dir, _SOURCE_NAME)
    out_path    = os.path.join(script_dir, "icon.ico")

    # ── Load source image ─────────────────────────────────────────────────
    if not os.path.isfile(source_path):
        sys.exit(
            f"No se encontro la imagen fuente: {source_path}\n"
            f"Coloca '{_SOURCE_NAME}' en el mismo directorio que este script."
        )

    source = Image.open(source_path)
    print(f"Fuente: {source_path}  ({source.size[0]}x{source.size[1]}, {source.mode})")

    if source.size[0] < 256 or source.size[1] < 256:
        print(
            f"  ADVERTENCIA: imagen fuente menor a 256x256. "
            f"Los tamanios grandes del icono podrian verse pixelados."
        )

    # ── Generate each size ────────────────────────────────────────────────
    frames = [_resize_to(source, s) for s in _SIZES]
    print(f"Tamanios generados: {_SIZES}")

    # ── Save as multi-size ICO ────────────────────────────────────────────
    # Pillow embeds all frames in one ICO when append_images is used.
    # Windows Explorer and the PE loader pick the best size automatically.
    frames[0].save(
        out_path,
        format="ICO",
        sizes=[(s, s) for s in _SIZES],
        append_images=frames[1:],
    )

    print(f"Icono guardado: {out_path}")


if __name__ == "__main__":
    main()
