"""
File-type icon generator for FileBrowserPanel.

Generates 16x16 RGBA icons with Pillow and caches them as tk.PhotoImage
objects.  No external image files required — all shapes are drawn
programmatically.

Usage:
    from gui.icons import icon_for_entry
    photo = icon_for_entry(entry_dict)   # pass directly as image= to tree.insert
"""
from __future__ import annotations

import tkinter as tk
from PIL import Image, ImageDraw, ImageTk

_SIZE = 16
_cache: dict[str, tk.PhotoImage] = {}

# ── Palette ───────────────────────────────────────────────────────────────────
_AMBER    = (245, 190,  50, 255)   # folder body
_AMBER_D  = (200, 148,  28, 255)   # folder shadow / tab
_AMBER_L  = (255, 220, 110, 255)   # folder highlight
_CYAN     = ( 20, 190, 205, 255)   # symlink arrow overlay
_BLUE     = ( 55, 115, 210, 255)   # python / info accent
_GREEN    = ( 42, 170,  72, 255)   # executable accent
_ORANGE   = (210, 120,  30, 255)   # archive box
_ORANGE_D = (170,  90,  20, 255)   # archive stripes
_PURPLE   = (113,  75, 103, 255)   # config / theme accent
_PAGE     = (242, 242, 245, 255)   # regular file page
_PAGE_D   = (195, 195, 200, 255)   # fold corner
_PAGE_IMG = (215, 230, 255, 255)   # image file page
_PAGE_PY  = (220, 235, 255, 255)   # python file page
_PAGE_CFG = (235, 228, 250, 255)   # config file page
_PAGE_LOG = (250, 250, 245, 255)   # log file page
_WHITE    = (255, 255, 255, 255)
_TRANS    = (0, 0, 0, 0)


def _canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGBA", (_SIZE, _SIZE), _TRANS)
    return img, ImageDraw.Draw(img)


# ── Shape builders ────────────────────────────────────────────────────────────

def _draw_folder(color=_AMBER, shadow=_AMBER_D, highlight=_AMBER_L) -> Image.Image:
    """Classic folder shape: tab at top-left, rounded body."""
    img, d = _canvas()
    # Tab
    d.rectangle([1, 4, 7, 6], fill=color)
    d.rectangle([1, 4, 6, 5], fill=highlight)   # tab top highlight
    # Body
    d.rectangle([1, 5, 14, 13], fill=color)
    # Body top highlight
    d.line([1, 5, 14, 5], fill=highlight)
    d.line([1, 6, 14, 6], fill=highlight)
    # Bottom shadow
    d.line([1, 13, 14, 13], fill=shadow)
    d.line([14, 5, 14, 13], fill=shadow)
    return img


def _draw_file(page=_PAGE, fold=_PAGE_D) -> Image.Image:
    """Page with top-right corner fold."""
    img, d = _canvas()
    # Page body
    d.polygon([
        (2,  1), (10,  1), (13,  4), (13, 14), (2, 14)
    ], fill=page)
    # Fold
    d.polygon([(10, 1), (13, 4), (10, 4)], fill=fold)
    # Subtle border
    d.line([(2, 1), (10, 1)], fill=fold)
    d.line([(2, 1), (2, 14)], fill=fold)
    d.line([(2, 14), (13, 14)], fill=fold)
    d.line([(13, 4), (13, 14)], fill=fold)
    return img


def _overlay_link(img: Image.Image) -> Image.Image:
    """Overlay a small cyan arrow (bottom-right) to mark symlinks."""
    d = ImageDraw.Draw(img)
    d.polygon([(9, 10), (14, 7), (14, 14), (9, 14)], fill=_CYAN)
    # Small white arrow line
    d.line([(10, 12), (13, 9)], fill=_WHITE, width=1)
    return img


# ── Named icon makers ─────────────────────────────────────────────────────────

def _make(name: str) -> Image.Image:
    if name == "folder":
        return _draw_folder()

    if name == "folder_link":
        return _overlay_link(_draw_folder())

    if name == "file":
        return _draw_file()

    if name == "file_link":
        return _overlay_link(_draw_file())

    if name == "file_exec":
        # Green page (executable)
        img = _draw_file(page=(228, 248, 230), fold=(150, 210, 155))
        d = ImageDraw.Draw(img)
        d.rectangle([2, 1, 4, 14], fill=_GREEN)     # green left bar
        d.line([5, 6, 11, 6],  fill=_GREEN, width=1)
        d.line([5, 9, 9,  9],  fill=_GREEN, width=1)
        return img

    if name == "file_image":
        # Blue-tinted page with mini landscape scene
        img = _draw_file(page=_PAGE_IMG, fold=(160, 185, 235))
        d = ImageDraw.Draw(img)
        d.rectangle([3, 5, 12, 12], fill=(200, 218, 248))   # sky
        d.ellipse([4, 5, 7, 8],     fill=(255, 210, 50))    # sun
        d.polygon([(3, 12), (6, 8), (9, 11), (12, 7), (12, 12)],
                  fill=(80, 160, 90))                         # hills
        return img

    if name == "file_archive":
        # Box / package
        img, d = _canvas()
        d.rectangle([2,  1, 13, 13], fill=_ORANGE)
        # Lid
        d.rectangle([2,  1, 13,  4], fill=_ORANGE_D)
        # Strap
        d.rectangle([6,  1,  9, 13], fill=_ORANGE_D)
        # Handle/knot
        d.rectangle([6,  2,  9,  3], fill=_AMBER_L)
        # Border
        d.rectangle([2,  1, 13, 13], outline=_ORANGE_D)
        return img

    if name == "file_config":
        # Purple-tinted page with horizontal "config lines"
        img = _draw_file(page=_PAGE_CFG, fold=(185, 175, 220))
        d = ImageDraw.Draw(img)
        for y, w in [(6, 8), (8, 6), (10, 9), (12, 5)]:
            d.line([(4, y), (4 + w, y)], fill=_PURPLE, width=1)
        return img

    if name == "file_python":
        # Blue-accent page with Python snake hint
        img = _draw_file(page=_PAGE_PY, fold=(155, 190, 240))
        d = ImageDraw.Draw(img)
        d.rectangle([2, 1, 13, 3], fill=_BLUE)      # blue top bar
        # Two interlocked C shapes (Python logo hint)
        d.arc([4, 5, 9,  9],  start=180, end=0,   fill=_BLUE, width=2)
        d.arc([6, 8, 11, 12], start=0,   end=180, fill=(255, 190, 40, 255), width=2)
        return img

    if name == "file_log":
        # Plain page with dotted log lines
        img = _draw_file(page=_PAGE_LOG, fold=_PAGE_D)
        d = ImageDraw.Draw(img)
        colors = [(150, 150, 150), (200, 120, 40), (180, 60, 60)]
        for i, (y, c) in enumerate([(5, colors[0]), (7, colors[1]),
                                     (9, colors[2]), (11, colors[0])]):
            d.line([(4, y), (11, y)], fill=c, width=1)
        return img

    if name == "file_shell":
        # Dark page with > prompt
        img = _draw_file(page=(230, 240, 230), fold=(160, 200, 160))
        d = ImageDraw.Draw(img)
        d.polygon([(4, 7), (8, 9), (4, 11)], fill=_GREEN)   # > arrow
        d.line([(9, 11), (12, 11)], fill=(100, 160, 100), width=1)
        return img

    # Fallback
    return _draw_file()


# ── Public API ────────────────────────────────────────────────────────────────

def get(name: str) -> tk.PhotoImage:
    """Return a cached PhotoImage for the given icon name."""
    if name not in _cache:
        _cache[name] = ImageTk.PhotoImage(_make(name))
    return _cache[name]


# Extension → icon name mapping
_EXT_MAP: dict[str, str] = {
    # Archives
    "zip": "file_archive", "tar": "file_archive", "gz":  "file_archive",
    "bz2": "file_archive", "xz":  "file_archive", "7z":  "file_archive",
    "rar": "file_archive", "tgz": "file_archive", "tbz2":"file_archive",
    "deb": "file_archive", "rpm": "file_archive",
    # Images
    "png": "file_image", "jpg":  "file_image", "jpeg": "file_image",
    "gif": "file_image", "bmp":  "file_image", "svg":  "file_image",
    "webp":"file_image", "ico":  "file_image", "tiff": "file_image",
    # Config / data
    "conf":"file_config", "ini": "file_config", "yaml": "file_config",
    "yml": "file_config", "toml":"file_config", "env":  "file_config",
    "cfg": "file_config", "json":"file_config", "xml":  "file_config",
    "properties":"file_config",
    # Python
    "py":  "file_python", "pyc": "file_python", "pyo":  "file_python",
    # Shell scripts
    "sh":  "file_shell",  "bash":"file_shell",  "zsh":  "file_shell",
    # Logs
    "log": "file_log",
}


def icon_for_entry(entry: dict) -> tk.PhotoImage:
    """Choose the right PhotoImage icon for a remote filesystem entry dict."""
    is_link = entry.get("is_link", False)

    if entry["is_dir"]:
        return get("folder_link" if is_link else "folder")

    if is_link:
        return get("file_link")

    name = entry.get("name", "")
    ext  = name.rsplit(".", 1)[-1].lower() if "." in name else ""

    if ext in _EXT_MAP:
        return get(_EXT_MAP[ext])

    # Detect executables via permissions string (e.g. "-rwxr-xr-x")
    perms = entry.get("permissions", "")
    if len(perms) >= 4 and "x" in perms[1:4]:
        return get("file_exec")

    return get("file")
