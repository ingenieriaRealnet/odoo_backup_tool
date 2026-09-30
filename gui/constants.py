"""
Module-level constants of the main window, shared by gui/app.py, the dialog
classes (gui/dialogs.py) and the page mixins (gui/tabs/*).

They used to live at the top of gui/app.py. Names keep their leading
underscore so the code that uses them did not have to change when that file
was split.
"""
from __future__ import annotations
import os
import re

from gui import theme

# Valid PostgreSQL identifier: starts with letter/underscore, max 63 chars
_DB_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

# Keyword pattern for word-level log highlighting: INFO / WARNING / WARN / ERROR
# Matches both bare words and bracket-wrapped forms like [INFO], [ERROR]
_KW_LOG_RE = re.compile(
    r'\[(?:INFO|WARNING|WARN|ERROR)\]|\b(?:INFO|WARNING|WARN|ERROR)\b',
    re.IGNORECASE,
)

APP_TITLE = theme.APP_TITLE
_PAD = theme.PAD

# _on_close(): a backup/restore "in progress" whose last queue activity (log
# line, progress update, ...) is older than this is treated as frozen/stuck
# rather than genuinely active — closing skips the confirmation prompt in
# that case since there is nothing real to interrupt. Comfortably above the
# normal gap between progress lines (heartbeats every ~15s, Drive % updates
# every few seconds to a couple minutes even when degraded) but well under
# the 180s socket inactivity timeout in ssh_client.py, so the user gets an
# immediate, informed choice instead of waiting for the network layer to
# eventually time out on its own.
_FROZEN_ACTIVITY_THRESHOLD_SECS = 90

# How often a viewer window retries taking over the scheduler from the
# headless monitor (see BackupApp._retry_scheduler_ownership).
_OWNERSHIP_RETRY_MS = 60_000

# Notebook page indexes of the monitoring pages. They are appended AFTER the
# original twelve pages on purpose: the wizard and several handlers address
# pages by number (self.nb.select(4), idx == 7, ...), so inserting "Panel" at
# position 0 would silently shift every one of them. The sidebar, not the
# notebook order, decides what the user sees first.
_TAB_PANEL = 12
_TAB_ALERTS = 13

# Sidebar layout: (section, [(label, notebook page index), ...]).
_SIDEBAR_SECTIONS = [
    ("Monitoreo", [("Panel de clientes", _TAB_PANEL), ("Configuración", _TAB_ALERTS)]),
    ("Respaldo manual", [
        ("1. Conexión", 0), ("2. Base de datos", 1), ("3. Filestore", 2),
        ("4. Destino", 3), ("5. Ejecutar", 4),
    ]),
    ("Automatización", [("Reglas programadas", 10), ("Historial", 11)]),
    ("Servidor", [
        ("Restaurar", 5), ("Addons", 6), ("Explorador", 7),
        ("Terminal", 8), ("Trial", 9),
    ]),
]

# Interval of the sidebar<->notebook state sync (enabled/disabled pages).
_SIDEBAR_SYNC_MS = 800

# Lowest share of the window height the pages keep when a saved log-panel
# sash position is restored (see BackupApp._restore_sash).
_MIN_PAGES_FRACTION = 0.66

# Sentinel value shown as the first option in every profile combobox.
# Selecting it is equivalent to "no profile selected" — triggers the
# create-new-profile path when the user clicks Guardar.
_PROFILE_NEW = "— Nuevo perfil —"

# Display labels for HistoryManager action_type values — see core/history_manager.py
_HISTORY_TYPE_LABELS = {
    "backup_manual":    "Backup manual",
    "backup_scheduled": "Backup programado",
    "restore":          "Restauracion",
    "addons_sync":      "Sync addons",
    "terminal":         "Terminal SSH",
}

# Persistent settings file (geometry, sash position)
_SETTINGS_FILE = os.path.join(
    os.path.expanduser("~"), ".odoo_backup_tool", "settings.json"
)

# ── Palette ──────────────────────────────────────────────────────────────────
# The values live in gui/theme.py (single source, also used by the sidebar
# and the monitoring panels); these are the names the window code has
# always used.
_DARK_BG  = theme.DARK_BG     # Odoo brand purple — header bar
_LOG_BG   = theme.LOG_BG      # Near-black log panel
_LOG_FG   = theme.LOG_FG      # Warm light gray text in log

_C_BG       = theme.C_BG        # Warm near-white background
_C_PURPLE   = theme.C_PURPLE    # Odoo brand purple
_C_PURPLE2  = theme.C_PURPLE2   # Lighter purple for hover
_C_PURPLE3  = theme.C_PURPLE3   # Darker purple for pressed
_C_TEAL     = theme.C_TEAL      # Odoo teal accent (progressbar, stripe)
_C_WHITE    = theme.C_WHITE
_C_TEXT     = theme.C_TEXT      # Warm dark text
_C_BORDER   = theme.C_BORDER    # Muted purple-gray border
_C_RED      = theme.C_RED       # Stop/danger
_C_RED2     = theme.C_RED2      # Stop hover
