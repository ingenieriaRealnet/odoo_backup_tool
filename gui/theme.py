"""
Shared visual constants for the Odoo Backup Tool GUI.

Single source for the palette so the main window (gui/app.py) and the
panels that live in their own modules (sidebar, health dashboard, alert
settings) cannot drift apart.
"""
from __future__ import annotations

APP_TITLE = "Odoo Backup Tool"
PAD = 8

# ── Brand palette ────────────────────────────────────────────────────────────
DARK_BG = "#714B67"     # Odoo brand purple — header bar
LOG_BG = "#1E1B1D"      # Near-black log panel
LOG_FG = "#D4CFCF"      # Warm light gray text in log

C_BG = "#F5F3F0"        # Warm near-white background
C_PURPLE = "#714B67"    # Odoo brand purple
C_PURPLE2 = "#8B6080"   # Lighter purple for hover
C_PURPLE3 = "#5A3A52"   # Darker purple for pressed
C_TEAL = "#00A09D"      # Odoo teal accent (progressbar, stripe)
C_WHITE = "#FFFFFF"
C_TEXT = "#2C2424"      # Warm dark text
C_MUTED = "#7A7075"     # Secondary text
C_BORDER = "#C8BEC5"    # Muted purple-gray border
C_RED = "#C0392B"       # Stop/danger
C_RED2 = "#E74C3C"      # Stop hover

# ── Sidebar ──────────────────────────────────────────────────────────────────
C_SIDEBAR_BG = "#ECE6EA"
C_SIDEBAR_HOVER = "#DDD3D9"
C_SIDEBAR_ACTIVE = "#FFFFFF"
SIDEBAR_WIDTH = 196

# ── Health levels (dashboard, badges) ────────────────────────────────────────
# foreground / soft background pairs, keyed by core.health_eval level.
LEVEL_COLORS = {
    "crit":    ("#A93226", "#FADBD8"),
    "warn":    ("#9A6700", "#FFF3CD"),
    "ok":      ("#1E7E46", "#DFF3E6"),
    "unknown": ("#5F6368", "#E8E6E7"),
    "off":     ("#9A9398", "#F1EEF0"),
}

FONT = ("Segoe UI", 9)
FONT_BOLD = ("Segoe UI", 9, "bold")
FONT_SMALL = ("Segoe UI", 8)
FONT_TITLE = ("Segoe UI", 13, "bold")
