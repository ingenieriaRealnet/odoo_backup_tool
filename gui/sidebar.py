"""
Left navigation for the main window.

Replaces the single row of 12+ notebook tabs — which no longer fit and gave
the wizard steps, the server tools and the monitoring views the same visual
weight — with grouped sections. The ttk.Notebook is still what holds and
switches the pages (its tab strip is hidden); the sidebar only drives
notebook.select(), so every existing `self.nb.select(i)` / `self.nb.tab(i,
state=...)` call in gui/app.py keeps working unchanged.
"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from gui import theme


class Sidebar(tk.Frame):
    """
    Args:
        parent:    Container widget.
        notebook:  The ttk.Notebook whose pages this sidebar selects.
        sections:  [(section title, [(label, notebook tab index), ...]), ...]
    """

    def __init__(
        self,
        parent: tk.Widget,
        notebook: ttk.Notebook,
        sections: list[tuple[str, list[tuple[str, int]]]],
    ) -> None:
        super().__init__(parent, bg=theme.C_SIDEBAR_BG, width=theme.SIDEBAR_WIDTH)
        self.pack_propagate(False)
        self._nb = notebook
        self._rows: dict[int, dict] = {}
        self._active: int | None = None

        for title, items in sections:
            tk.Label(
                self, text=title.upper(), anchor="w",
                font=("Segoe UI", 7, "bold"), fg=theme.C_MUTED, bg=theme.C_SIDEBAR_BG,
            ).pack(fill="x", padx=(14, 8), pady=(12, 2))
            for label, index in items:
                self._add_item(label, index)

    def _add_item(self, label: str, index: int) -> None:
        row = tk.Frame(self, bg=theme.C_SIDEBAR_BG, cursor="hand2")
        row.pack(fill="x")
        # Accent bar: only visible (purple) on the active item.
        bar = tk.Frame(row, bg=theme.C_SIDEBAR_BG, width=4)
        bar.pack(side="left", fill="y")
        text = tk.Label(
            row, text=label, anchor="w", font=theme.FONT,
            fg=theme.C_TEXT, bg=theme.C_SIDEBAR_BG, padx=10, pady=5,
        )
        text.pack(side="left", fill="x", expand=True)
        badge = tk.Label(row, text="", font=("Segoe UI", 7, "bold"), fg="white", bg=theme.C_SIDEBAR_BG, padx=5)

        entry = {"row": row, "bar": bar, "text": text, "badge": badge, "enabled": True, "badge_bg": None}
        self._rows[index] = entry
        for widget in (row, text, badge):
            widget.bind("<Button-1>", lambda _e, i=index: self._on_click(i))
            widget.bind("<Enter>", lambda _e, i=index: self._paint(i, hover=True))
            widget.bind("<Leave>", lambda _e, i=index: self._paint(i, hover=False))

    def _on_click(self, index: int) -> None:
        if self._rows[index]["enabled"]:
            self._nb.select(index)   # fires <<NotebookTabChanged>> -> sync()

    def _paint(self, index: int, hover: bool = False) -> None:
        entry = self._rows[index]
        active = index == self._active
        if not entry["enabled"]:
            bg, fg, font = theme.C_SIDEBAR_BG, "#B3A9AF", theme.FONT
        elif active:
            bg, fg, font = theme.C_SIDEBAR_ACTIVE, theme.C_PURPLE, theme.FONT_BOLD
        elif hover:
            bg, fg, font = theme.C_SIDEBAR_HOVER, theme.C_TEXT, theme.FONT
        else:
            bg, fg, font = theme.C_SIDEBAR_BG, theme.C_TEXT, theme.FONT
        entry["row"].config(bg=bg, cursor="hand2" if entry["enabled"] else "arrow")
        entry["text"].config(bg=bg, fg=fg, font=font)
        entry["bar"].config(bg=theme.C_PURPLE if active else bg)
        entry["badge"].config(bg=entry["badge_bg"] or bg)

    def sync(self) -> None:
        """
        Mirror the notebook: highlight its selected page and grey out the
        pages it has disabled (wizard steps 2-5 before connecting).
        """
        try:
            self._active = self._nb.index(self._nb.select())
        except tk.TclError:
            self._active = None
        for index, entry in self._rows.items():
            try:
                entry["enabled"] = str(self._nb.tab(index, "state")) != "disabled"
            except tk.TclError:
                entry["enabled"] = False
            self._paint(index)

    def set_badge(self, index: int, text: str, color: str | None) -> None:
        """Show a small counter next to an item (empty text hides it)."""
        entry = self._rows.get(index)
        if not entry:
            return
        entry["badge_bg"] = color if text else None
        entry["badge"].config(text=text)
        if text:
            entry["badge"].pack(side="right", padx=(0, 8))
        else:
            entry["badge"].pack_forget()
        self._paint(index)
