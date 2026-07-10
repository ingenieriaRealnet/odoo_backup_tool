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
    root = tk.Tk()
    _set_window_icon(root)
    BackupApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
