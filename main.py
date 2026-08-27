"""
Odoo Backup Tool — entry point.

Run directly:
    python main.py

Or build the Windows executable:
    build.bat
"""
from __future__ import annotations

import os
import sys
import tkinter as tk

from gui.app import BackupApp

# tkinterdnd2 requires the root window itself to be created via its own
# TkinterDnD.Tk() subclass — a plain tk.Tk() root would make every panel's
# drop_target_register() call fail silently. Falls back to a normal Tk root
# if the dependency isn't installed (dev environment) — the Explorer tab's
# drag-and-drop just won't register in that case; everything else still works.
try:
    from tkinterdnd2 import TkinterDnD
    _DND_ROOT_CLS = TkinterDnD.Tk
except ImportError:
    _DND_ROOT_CLS = tk.Tk


def _set_window_icon(root: tk.Tk) -> None:
    """
    Load icon.ico and apply it to the window titlebar and taskbar.

    Searches next to the script (dev mode) and next to the frozen
    executable (PyInstaller onefile mode).
    """
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico"),
        os.path.join(getattr(sys, "_MEIPASS", ""), "icon.ico"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                root.iconbitmap(path)
            except Exception:
                pass
            return


def main() -> None:
    root = _DND_ROOT_CLS()
    _set_window_icon(root)
    BackupApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
