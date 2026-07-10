"""
FileBrowserPanel — panel de explorador de archivos remoto estilo FileZilla.

Cada panel es autónomo: mantiene su propio historial de navegación, barra de
breadcrumbs, menú contextual y operaciones de archivo.  El Tab 8 instancia dos
paneles y los conecta para las transferencias entre servidores.

Atajos de teclado:
    F2         Renombrar elemento seleccionado
    F5         Refrescar directorio actual
    Delete     Eliminar elemento seleccionado
    Backspace  Subir un nivel
    Enter      Entrar al directorio / abrir archivo
    Alt+←      Atrás en el historial
    Alt+→      Adelante en el historial
"""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Callable

from core.file_browser import RemoteBrowser
from core.ssh_client import SSHClient
from gui import icons as _icons

# ── Paleta (coincide con app.py) ─────────────────────────────────────────────
_C_BG      = "#F5F3F0"
_C_PURPLE  = "#714B67"
_C_TEAL    = "#00A09D"
_C_WHITE   = "#FFFFFF"
_C_BORDER  = "#C8BEC5"
_C_TEXT    = "#2D2D2D"
_C_SEL_BG  = "#D4C5D0"

# "Nombre" lives in the #0 tree column (icon + text).
# _COL_DEFS only lists the value columns that follow it.
_COL_DEFS = [
    ("Tamaño",      "size_human",  90,  "e"),
    ("Modificado",  "modified",    130, "center"),
    ("Permisos",    "permissions", 80,  "center"),
    ("Propietario", "owner",       80,  "center"),
]


class FileBrowserPanel(ttk.Frame):
    """
    Panel de explorador de archivos remoto con funcionalidad FileZilla.

    Parámetros
    ----------
    parent        : widget padre tkinter
    side          : 'l' (izquierdo) o 'r' (derecho) — solo informativo
    get_ssh       : callable() → SSHClient | None
    title         : texto de encabezado del panel
    on_status     : callable(str) → muestra mensajes en la barra de estado
    peer_panel    : referencia al panel opuesto (para transferencias)
    """

    def __init__(
        self,
        parent,
        side: str,
        get_ssh: Callable[[], SSHClient | None],
        title: str = "",
        on_status: Callable[[str], None] | None = None,
        peer_panel: "FileBrowserPanel | None" = None,
    ) -> None:
        super().__init__(parent, style="TFrame")
        self._side       = side
        self._get_ssh    = get_ssh
        self._title      = title
        self._on_status  = on_status or (lambda m: None)
        self._peer       = peer_panel

        self._browser: RemoteBrowser | None = None
        self._current_path  = "/"
        self._history: list[str] = []
        self._fwd_stack: list[str] = []
        self._show_hidden   = tk.BooleanVar(value=False)
        self._sort_col      = "name"
        self._sort_rev      = False
        self._entries: list[dict] = []   # current listing cache

        self._build_ui()
        self._bind_keys()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        # ── Header ───────────────────────────────────────────────────────────
        hdr = tk.Frame(self, bg=_C_PURPLE, height=28)
        hdr.grid(row=0, column=0, sticky="ew")
        hdr.grid_propagate(False)
        tk.Label(
            hdr, text=f"  {self._title}",
            font=("Segoe UI", 9, "bold"),
            bg=_C_PURPLE, fg=_C_WHITE,
        ).pack(side="left", pady=4)

        # Checkbutton y boton Reconectar en el encabezado (derecha → izquierda)
        tk.Checkbutton(
            hdr, text="Mostrar ocultos",
            variable=self._show_hidden,
            command=self._refresh,
            bg=_C_PURPLE, fg=_C_WHITE,
            selectcolor=_C_PURPLE,
            activebackground=_C_PURPLE, activeforeground=_C_WHITE,
            font=("Segoe UI", 8),
        ).pack(side="right", padx=(0, 6))

        tk.Button(
            hdr, text="Reconectar",
            font=("Segoe UI", 8),
            bg="#2D6A4F", fg="#FFFFFF",
            relief="flat", cursor="hand2",
            activebackground="#1B4332", activeforeground="#FFFFFF",
            command=lambda: self.connect_and_navigate("/"),
        ).pack(side="right", padx=(0, 4), pady=2)

        # ── Toolbar ───────────────────────────────────────────────────────────
        tb = tk.Frame(self, bg=_C_BG)
        tb.grid(row=1, column=0, sticky="ew", pady=(2, 0))

        btn_cfg = dict(relief="flat", cursor="hand2", padx=4, pady=2, font=("Segoe UI", 9))

        self._btn_back = tk.Button(
            tb, text="◀", command=self.go_back,
            bg=_C_BG, fg=_C_TEXT, **btn_cfg,
        )
        self._btn_back.pack(side="left", padx=(2, 0))

        self._btn_fwd = tk.Button(
            tb, text="▶", command=self.go_forward,
            bg=_C_BG, fg=_C_TEXT, **btn_cfg,
        )
        self._btn_fwd.pack(side="left")

        tk.Button(
            tb, text="▲", command=self.go_up,
            bg=_C_BG, fg=_C_TEXT, **btn_cfg,
        ).pack(side="left")

        tk.Button(
            tb, text="⌂", command=self.go_home,
            bg=_C_BG, fg=_C_TEXT, **btn_cfg,
        ).pack(side="left")

        tk.Button(
            tb, text="↺ F5", command=self._refresh,
            bg=_C_BG, fg=_C_TEXT, **btn_cfg,
        ).pack(side="left", padx=(0, 6))

        # Breadcrumb bar
        self._bread_frame = tk.Frame(tb, bg=_C_BG)
        self._bread_frame.pack(side="left", fill="x", expand=True)

        # Path entry (hidden by default, shown when user clicks path area)
        self._path_var = tk.StringVar()
        self._path_entry = ttk.Entry(tb, textvariable=self._path_var, width=30)
        self._path_entry.bind("<Return>", lambda e: self._navigate_from_entry())
        self._path_entry.bind("<Escape>", lambda e: self._hide_path_entry())

        # ── Treeview ──────────────────────────────────────────────────────────
        tree_frame = tk.Frame(self, bg=_C_BG)
        tree_frame.grid(row=2, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        cols = [c[0] for c in _COL_DEFS]
        self._tree = ttk.Treeview(
            tree_frame,
            columns=cols,
            show="tree headings",
            selectmode="extended",    # multi-select
        )
        # #0 column: icon + file name
        self._tree.column("#0", width=280, anchor="w", stretch=True, minwidth=120)
        self._tree.heading("#0", text="Nombre",
                           command=lambda: self._sort_by("name"))
        for label, key, width, anchor in _COL_DEFS:
            self._tree.heading(
                label, text=label,
                command=lambda k=key: self._sort_by(k),
            )
            self._tree.column(label, width=width, anchor=anchor, stretch=False)

        vsb = ttk.Scrollbar(tree_frame, orient="vertical",   command=self._tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self._tree.xview)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self._tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")

        # ── Status bar ───────────────────────────────────────────────────────
        self._status_var = tk.StringVar(value="Sin conexion")
        tk.Label(
            self, textvariable=self._status_var,
            font=("Segoe UI", 8), bg=_C_BG, fg="#555", anchor="w",
        ).grid(row=3, column=0, sticky="ew", padx=4)

        # ── Bindings ─────────────────────────────────────────────────────────
        self._tree.bind("<Double-Button-1>", self._on_double_click)
        self._tree.bind("<Button-3>",        self._on_right_click)
        self._tree.bind("<Return>",          self._on_enter_key)
        self._tree.bind("<Delete>",          lambda e: self.delete_selected())
        self._tree.bind("<F2>",              lambda e: self.rename_selected())
        self._tree.bind("<F5>",              lambda e: self._refresh())
        self._tree.bind("<BackSpace>",       lambda e: self.go_up())
        self._tree.bind("<Alt-Left>",        lambda e: self.go_back())
        self._tree.bind("<Alt-Right>",       lambda e: self.go_forward())

        # Mousewheel
        self._tree.bind("<Enter>", lambda e: self._tree.bind_all(
            "<MouseWheel>",
            lambda ev: self._tree.yview_scroll(-1 * (ev.delta // 120), "units"),
        ))
        self._tree.bind("<Leave>", lambda e: self._tree.unbind_all("<MouseWheel>"))

        self._update_nav_buttons()

    def _bind_keys(self) -> None:
        """Bind panel-level keyboard shortcuts after UI is built."""
        pass  # bindings are per-widget in _build_ui

    # ── Connection ────────────────────────────────────────────────────────────

    def connect_and_navigate(self, path: str = "/") -> None:
        """Initialize the browser with the current SSH connection and navigate to path."""
        ssh = self._get_ssh()
        if ssh is None or not ssh.connected:
            self._set_status("Sin conexion SSH")
            return
        self._browser = RemoteBrowser(ssh)
        self._history.clear()
        self._fwd_stack.clear()
        self.navigate(path, record_history=False)

    # ── Navigation ────────────────────────────────────────────────────────────

    def navigate(self, path: str, record_history: bool = True) -> None:
        """Navigate to path, updating history and refreshing the listing."""
        if not self._browser:
            self.connect_and_navigate(path)
            return

        if record_history and self._current_path:
            self._history.append(self._current_path)
            self._fwd_stack.clear()

        self._current_path = path
        self._path_var.set(path)
        self._update_breadcrumbs()
        self._update_nav_buttons()
        self._load_listing()

    def go_up(self) -> None:
        if self._current_path and self._current_path != "/":
            parent = self._current_path.rstrip("/").rsplit("/", 1)[0] or "/"
            self.navigate(parent)

    def go_back(self) -> None:
        if self._history:
            self._fwd_stack.append(self._current_path)
            prev = self._history.pop()
            self.navigate(prev, record_history=False)
            self._update_nav_buttons()

    def go_forward(self) -> None:
        if self._fwd_stack:
            self._history.append(self._current_path)
            nxt = self._fwd_stack.pop()
            self.navigate(nxt, record_history=False)
            self._update_nav_buttons()

    def go_home(self) -> None:
        if not self._browser:
            return
        def _do():
            try:
                home = self._browser.home()
                self.after(0, lambda: self.navigate(home))
            except Exception:
                self.after(0, lambda: self.navigate("/"))
        threading.Thread(target=_do, daemon=True).start()

    def _refresh(self) -> None:
        self._load_listing()

    def _navigate_from_entry(self) -> None:
        path = self._path_var.get().strip()
        if path:
            self.navigate(path)
        self._hide_path_entry()

    def _hide_path_entry(self) -> None:
        self._path_entry.pack_forget()
        self._bread_frame.pack(side="left", fill="x", expand=True)

    # ── Listing ───────────────────────────────────────────────────────────────

    def _load_listing(self) -> None:
        """Fetch directory listing in a background thread and populate the tree."""
        if not self._browser:
            return
        self._set_status(f"Cargando {self._current_path} ...")
        path = self._current_path

        def _worker():
            try:
                entries = self._browser.list_dir(path)
            except FileNotFoundError:
                self.after(0, lambda: self._set_status(f"Ruta no existe: {path}"))
                return
            except PermissionError as e:
                self.after(0, lambda: self._set_status(str(e)))
                return
            except Exception as e:
                self.after(0, lambda: self._set_status(f"Error: {e}"))
                return
            self.after(0, lambda: self._populate(entries))

        threading.Thread(target=_worker, daemon=True).start()

    def _populate(self, entries: list[dict]) -> None:
        """Populate the treeview with directory entries."""
        # Filter hidden files if needed
        if not self._show_hidden.get():
            entries = [e for e in entries if not e["name"].startswith(".")]

        self._entries = entries
        self._apply_sort()

    def _apply_sort(self) -> None:
        """Sort _entries and redraw the tree."""
        col  = self._sort_col
        rev  = self._sort_rev

        def _key(e: dict):
            # Directories always first, then sort by chosen column
            is_dir = not e["is_dir"]  # False for dirs → sorts first
            if col == "size_human":
                val = e.get("size_bytes", 0)
            elif col == "modified":
                val = e.get("modified", "")
            else:
                val = e.get(col, "").lower()
            return (is_dir, val)

        sorted_entries = sorted(self._entries, key=_key, reverse=rev)
        self._redraw(sorted_entries)

    def _redraw(self, entries: list[dict]) -> None:
        """Clear and refill the treeview with sorted entries."""
        self._tree.delete(*self._tree.get_children())
        for e in entries:
            self._tree.insert(
                "", "end",
                iid=e["full_path"],
                text=e["name"],
                image=_icons.icon_for_entry(e),
                values=(
                    e.get("size_human", ""),
                    e.get("modified", ""),
                    e.get("permissions", ""),
                    e.get("owner", ""),
                ),
                tags=("dir",) if e["is_dir"] else ("file",),
            )
        self._tree.tag_configure("dir",  foreground=_C_PURPLE)
        self._tree.tag_configure("file", foreground=_C_TEXT)
        total_dirs  = sum(1 for e in entries if e["is_dir"])
        total_files = sum(1 for e in entries if not e["is_dir"])
        self._set_status(
            f"{self._current_path}   |   {total_dirs} carpetas, {total_files} archivos"
        )

    # ── Sorting ───────────────────────────────────────────────────────────────

    def _sort_by(self, col_key: str) -> None:
        if self._sort_col == col_key:
            self._sort_rev = not self._sort_rev
        else:
            self._sort_col = col_key
            self._sort_rev = False
        self._apply_sort()
        # Update column headers to show sort direction
        marker_name = ("  ↓" if not self._sort_rev else "  ↑") if self._sort_col == "name" else ""
        self._tree.heading("#0", text="Nombre" + marker_name)
        for label, key, _, _ in _COL_DEFS:
            marker = ("  ↓" if not self._sort_rev else "  ↑") if key == self._sort_col else ""
            self._tree.heading(label, text=label + marker)

    # ── Breadcrumbs ───────────────────────────────────────────────────────────

    def _update_breadcrumbs(self) -> None:
        """Rebuild the clickable breadcrumb bar from _current_path."""
        for w in self._bread_frame.winfo_children():
            w.destroy()

        # Click on the breadcrumb area → show path entry
        def _show_entry(e=None):
            self._bread_frame.pack_forget()
            self._path_entry.pack(side="left", fill="x", expand=True)
            self._path_entry.focus_set()
            self._path_entry.select_range(0, "end")

        parts = [p for p in self._current_path.split("/") if p]
        segments = [("/", "/")] + [(p, "/" + "/".join(parts[:i+1])) for i, p in enumerate(parts)]

        for label, full_path in segments:
            btn = tk.Button(
                self._bread_frame,
                text=label,
                font=("Segoe UI", 8),
                bg=_C_BG, fg=_C_PURPLE,
                relief="flat", cursor="hand2",
                padx=2, pady=0,
                command=lambda p=full_path: self.navigate(p),
            )
            btn.pack(side="left")
            if label != segments[-1][0]:
                tk.Label(
                    self._bread_frame, text="/",
                    bg=_C_BG, fg=_C_BORDER,
                    font=("Segoe UI", 8),
                ).pack(side="left")

        # Make the bar clickable to switch to entry mode
        self._bread_frame.bind("<Button-1>", _show_entry)

    # ── Nav buttons ───────────────────────────────────────────────────────────

    def _update_nav_buttons(self) -> None:
        state_back = "normal" if self._history   else "disabled"
        state_fwd  = "normal" if self._fwd_stack else "disabled"
        try:
            self._btn_back.configure(state=state_back)
            self._btn_fwd.configure(state=state_fwd)
        except tk.TclError:
            pass

    # ── Events ────────────────────────────────────────────────────────────────

    def _on_double_click(self, event) -> None:
        item = self._tree.identify_row(event.y)
        if not item:
            return
        entry = self._entry_for(item)
        if entry and entry["is_dir"]:
            self.navigate(entry["full_path"])

    def _on_enter_key(self, event) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        entry = self._entry_for(sel[0])
        if entry and entry["is_dir"]:
            self.navigate(entry["full_path"])

    # ── Context menu ─────────────────────────────────────────────────────────

    def _on_right_click(self, event) -> None:
        """Show FileZilla-style right-click context menu."""
        # Select the clicked row
        row = self._tree.identify_row(event.y)
        if row and row not in self._tree.selection():
            self._tree.selection_set(row)

        sel = self._tree.selection()
        has_sel = len(sel) > 0
        multi   = len(sel) > 1
        entry   = self._entry_for(sel[0]) if sel else None
        is_dir  = entry["is_dir"] if entry else False

        menu = tk.Menu(self, tearoff=0)

        if has_sel and not multi and is_dir:
            menu.add_command(
                label="Abrir / Entrar",
                command=lambda: self.navigate(entry["full_path"]),
            )
            menu.add_separator()

        if has_sel:
            menu.add_command(
                label="Copiar al otro panel →" if self._side == "l" else "← Copiar al otro panel",
                command=self.copy_to_peer,
                state="normal" if self._peer else "disabled",
            )
            menu.add_command(
                label="Mover al otro panel →" if self._side == "l" else "← Mover al otro panel",
                command=self.move_to_peer,
                state="normal" if self._peer else "disabled",
            )
            menu.add_separator()

        # ── Download to local machine ─────────────────────────────────────────
        if has_sel:
            if multi:
                dl_label = f"⬇  Descargar {len(sel)} elemento(s) ..."
            elif is_dir:
                dl_label = "⬇  Descargar carpeta como .zip ..."
            else:
                dl_label = "⬇  Descargar archivo ..."
            menu.add_command(label=dl_label, command=self.download_selected)
            menu.add_separator()

        menu.add_command(
            label="Nueva carpeta",
            command=self.new_folder,
        )

        if has_sel and not multi:
            menu.add_command(label="Renombrar  F2", command=self.rename_selected)

        if has_sel:
            menu.add_command(label="Eliminar  Del", command=self.delete_selected)
            menu.add_separator()

        if has_sel and not multi:
            menu.add_command(label="Permisos (chmod)", command=self.chmod_selected)
            menu.add_command(label="Propiedades",      command=self.show_properties)

        menu.add_separator()
        menu.add_command(label="Copiar nombre al portapapeles", command=self.copy_name)
        menu.add_command(label="Actualizar  F5", command=self._refresh)

        menu.tk_popup(event.x_root, event.y_root)

    # ── File operations ───────────────────────────────────────────────────────

    def new_folder(self) -> None:
        name = simpledialog.askstring(
            "Nueva carpeta",
            "Nombre de la nueva carpeta:",
            parent=self,
        )
        if not name or not self._browser:
            return
        path = f"{self._current_path.rstrip('/')}/{name}"
        try:
            self._browser.mkdir(path)
            self._refresh()
            self._on_status(f"Carpeta creada: {path}")
        except Exception as e:
            messagebox.showerror("Error", str(e), parent=self)

    def rename_selected(self) -> None:
        sel = self._tree.selection()
        if not sel or len(sel) > 1 or not self._browser:
            return
        entry = self._entry_for(sel[0])
        if not entry:
            return
        new_name = simpledialog.askstring(
            "Renombrar",
            f"Nuevo nombre para '{entry['name']}':",
            initialvalue=entry["name"],
            parent=self,
        )
        if not new_name or new_name == entry["name"]:
            return
        new_path = f"{self._current_path.rstrip('/')}/{new_name}"
        try:
            self._browser.rename(entry["full_path"], new_path)
            self._refresh()
            self._on_status(f"Renombrado: {entry['name']} → {new_name}")
        except Exception as e:
            messagebox.showerror("Error al renombrar", str(e), parent=self)

    def delete_selected(self) -> None:
        sel = self._tree.selection()
        if not sel or not self._browser:
            return
        names = [self._entry_for(s)["name"] for s in sel if self._entry_for(s)]
        msg = (
            f"¿Eliminar '{names[0]}'?" if len(names) == 1
            else f"¿Eliminar {len(names)} elementos?"
        )
        if not messagebox.askyesno("Confirmar eliminación", msg, parent=self):
            return
        errors: list[str] = []
        for iid in sel:
            entry = self._entry_for(iid)
            if not entry:
                continue
            try:
                if entry["is_dir"]:
                    self._browser.rmdir(entry["full_path"], recursive=True)
                else:
                    self._browser.rm_file(entry["full_path"])
            except Exception as e:
                errors.append(f"{entry['name']}: {e}")
        self._refresh()
        if errors:
            messagebox.showerror("Errores al eliminar", "\n".join(errors), parent=self)
        else:
            self._on_status(f"{len(sel)} elemento(s) eliminado(s)")

    def chmod_selected(self) -> None:
        sel = self._tree.selection()
        if not sel or len(sel) > 1 or not self._browser:
            return
        entry = self._entry_for(sel[0])
        if not entry:
            return
        mode = simpledialog.askstring(
            "Permisos (chmod)",
            f"Nuevo modo para '{entry['name']}':\n(ej: 755, 644, u+x)",
            initialvalue=entry.get("permissions", "755"),
            parent=self,
        )
        if not mode:
            return
        try:
            self._browser.chmod(entry["full_path"], mode)
            self._refresh()
            self._on_status(f"Permisos cambiados: {entry['name']} → {mode}")
        except Exception as e:
            messagebox.showerror("Error en chmod", str(e), parent=self)

    def show_properties(self) -> None:
        sel = self._tree.selection()
        if not sel or not self._browser:
            return
        entry = self._entry_for(sel[0])
        if not entry:
            return

        def _worker():
            info = self._browser.get_file_info(entry["full_path"])
            self.after(0, lambda: _show(info))

        def _show(info: dict):
            win = tk.Toplevel(self)
            win.title(f"Propiedades — {info['name']}")
            win.resizable(False, False)
            rows = [
                ("Nombre",       info["name"]),
                ("Ruta",         info["full_path"]),
                ("Tipo",         "Directorio" if info["is_dir"] else "Archivo"),
                ("Tamaño",       info.get("size_human") or info.get("size_bytes", "")),
                ("Modificado",   info.get("modified", "")),
                ("Permisos",     info.get("permissions", "")),
                ("Propietario",  info.get("owner", "")),
                ("Grupo",        info.get("group", "")),
            ]
            if info.get("symlink_target"):
                rows.append(("Enlace a", info["symlink_target"]))
            for i, (label, value) in enumerate(rows):
                tk.Label(win, text=label + ":", font=("Segoe UI", 9, "bold"),
                         anchor="e").grid(row=i, column=0, sticky="e", padx=(12, 4), pady=3)
                tk.Label(win, text=value, font=("Segoe UI", 9),
                         anchor="w").grid(row=i, column=1, sticky="w", padx=(0, 12), pady=3)
            ttk.Button(win, text="Cerrar", command=win.destroy).grid(
                row=len(rows), column=0, columnspan=2, pady=10,
            )

        threading.Thread(target=_worker, daemon=True).start()

    def copy_name(self) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        names = [self._entry_for(s)["name"] for s in sel if self._entry_for(s)]
        text = "\n".join(names)
        self.clipboard_clear()
        self.clipboard_append(text)
        self._on_status(f"Copiado: {text[:60]}")

    # ── Download to local machine ──────────────────────────────────────────────

    def download_selected(self) -> None:
        """
        Download selected item(s) from the remote server to the local machine.

        - Single file  → save dialog → SFTP get with progress
        - Single dir   → save dialog (*.zip) → server-side zip + SFTP get + cleanup
        - Multiple     → choose local directory → files direct, dirs as .zip each
        """
        sel = self._tree.selection()
        if not sel or not self._browser:
            return

        entries = [e for iid in sel if (e := self._entry_for(iid)) is not None]
        if not entries:
            return

        if len(entries) == 1:
            entry = entries[0]

            if entry["is_dir"]:
                # ── Carpeta → zip ────────────────────────────────────────────
                local_path = filedialog.asksaveasfilename(
                    parent=self,
                    title=f"Descargar carpeta '{entry['name']}' como ZIP",
                    initialfile=entry["name"] + ".zip",
                    defaultextension=".zip",
                    filetypes=[("ZIP archive", "*.zip"), ("Todos los archivos", "*.*")],
                )
                if not local_path:
                    return
                self._set_status(f"Comprimiendo '{entry['name']}' en el servidor ...")

                def _dl_dir(ent=entry, lp=local_path):
                    try:
                        self._browser.download_dir_as_zip(ent["full_path"], lp)
                        self.after(0, lambda: self._set_status(
                            f"Descargado: {lp}"
                        ))
                    except Exception as exc:
                        err_msg = str(exc)
                        self.after(0, lambda: messagebox.showerror(
                            "Error al descargar", err_msg, parent=self,
                        ))
                        self.after(0, lambda: self._set_status("Error en descarga"))

                threading.Thread(target=_dl_dir, daemon=True).start()

            else:
                # ── Archivo → descarga directa ───────────────────────────────
                local_path = filedialog.asksaveasfilename(
                    parent=self,
                    title=f"Descargar '{entry['name']}'",
                    initialfile=entry["name"],
                )
                if not local_path:
                    return
                self._set_status(f"Descargando '{entry['name']}' ...")

                def _progress(done: int, total: int, ent=entry) -> None:
                    if total > 0:
                        pct = int(done * 100 / total)
                        self.after(0, lambda p=pct: self._set_status(
                            f"Descargando '{ent['name']}' ... {p}%"
                        ))

                def _dl_file(ent=entry, lp=local_path):
                    try:
                        self._browser.download_file(ent["full_path"], lp, _progress)
                        self.after(0, lambda: self._set_status(f"Descargado: {lp}"))
                    except Exception as exc:
                        err_msg = str(exc)
                        self.after(0, lambda: messagebox.showerror(
                            "Error al descargar", err_msg, parent=self,
                        ))
                        self.after(0, lambda: self._set_status("Error en descarga"))

                threading.Thread(target=_dl_file, daemon=True).start()

        else:
            # ── Multiples elementos → elegir carpeta local ────────────────────
            local_dir = filedialog.askdirectory(
                parent=self,
                title=f"Seleccionar carpeta destino para {len(entries)} elemento(s)",
            )
            if not local_dir:
                return
            self._set_status(f"Descargando {len(entries)} elemento(s) ...")

            def _dl_multi(ents=entries, ld=local_dir):
                errors: list[str] = []
                done = 0
                for ent in ents:
                    local_target = os.path.join(ld, ent["name"])
                    try:
                        if ent["is_dir"]:
                            # Carpetas se descargan como zip con sufijo .zip
                            self._browser.download_dir_as_zip(
                                ent["full_path"], local_target + ".zip"
                            )
                        else:
                            self._browser.download_file(ent["full_path"], local_target)
                        done += 1
                        self.after(0, lambda d=done, t=len(ents): self._set_status(
                            f"Descargando ... {d}/{t}"
                        ))
                    except Exception as exc:
                        errors.append(f"{ent['name']}: {exc}")

                def _done(d=done, ld=ld, errs=errors):
                    if errs:
                        messagebox.showerror(
                            "Errores al descargar", "\n".join(errs), parent=self
                        )
                        self._set_status(f"Descarga con errores ({d}/{len(ents)})")
                    else:
                        self._set_status(f"{d} elemento(s) descargados en {ld}")

                self.after(0, _done)

            threading.Thread(target=_dl_multi, daemon=True).start()

    # ── Cross-panel transfer ──────────────────────────────────────────────────

    def copy_to_peer(self) -> None:
        """Copy selected items to the peer panel's current directory."""
        self._transfer_to_peer(move=False)

    def move_to_peer(self) -> None:
        """Move selected items to the peer panel's current directory."""
        self._transfer_to_peer(move=True)

    def _transfer_to_peer(self, move: bool = False) -> None:
        if not self._peer or not self._browser:
            return
        peer_ssh  = self._peer._get_ssh()
        peer_path = self._peer._current_path
        if not peer_ssh or not peer_ssh.connected:
            messagebox.showerror(
                "Sin conexión", "El panel destino no tiene conexión SSH activa.", parent=self,
            )
            return

        sel = self._tree.selection()
        if not sel:
            return
        entries = [self._entry_for(s) for s in sel if self._entry_for(s)]
        op = "Mover" if move else "Copiar"
        names = ", ".join(e["name"] for e in entries[:3])
        if len(entries) > 3:
            names += f" (+{len(entries) - 3} más)"
        if not messagebox.askyesno(
            f"Confirmar {op.lower()}",
            f"{op} {len(entries)} elemento(s) al otro panel?\n\n"
            f"Origen:  {self._current_path}\n"
            f"Destino: {peer_path}\n\n"
            f"Elementos: {names}",
            parent=self,
        ):
            return

        src_ssh  = self._get_ssh()
        same_host = (
            getattr(src_ssh, "host", None) == getattr(peer_ssh, "host", None)
            and getattr(src_ssh, "port", None) == getattr(peer_ssh, "port", None)
        )

        def _worker():
            import tempfile
            errors: list[str] = []
            peer_browser = RemoteBrowser(peer_ssh)
            src_browser  = self._browser

            for entry in entries:
                src = entry["full_path"]
                dst = f"{peer_path.rstrip('/')}/{entry['name']}"
                try:
                    if same_host:
                        # Same server → server-side cp/mv
                        if move:
                            src_browser.rename(src, dst)
                        else:
                            src_browser.copy(src, dst)
                    else:
                        # Different servers → download to temp, upload to peer
                        if entry["is_dir"]:
                            errors.append(
                                f"{entry['name']}: transferencia de carpetas entre servidores "
                                "diferentes no soportada aún (solo archivos)"
                            )
                            continue
                        with tempfile.NamedTemporaryFile(delete=False) as tmp:
                            tmp_path = tmp.name
                        try:
                            src_browser.download_file(src, tmp_path)
                            peer_browser.upload_file(tmp_path, dst)
                            if move:
                                src_browser.rm_file(src)
                        finally:
                            try:
                                os.unlink(tmp_path)
                            except OSError:
                                pass
                except Exception as e:
                    errors.append(f"{entry['name']}: {e}")

            def _done():
                self._refresh()
                self._peer._refresh()
                if errors:
                    messagebox.showerror("Errores en transferencia", "\n".join(errors), parent=self)
                else:
                    self._on_status(
                        f"{op} completado: {len(entries)} elemento(s) → {peer_path}"
                    )
            self.after(0, _done)

        threading.Thread(target=_worker, daemon=True).start()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _entry_for(self, iid: str) -> dict | None:
        """Return the cached entry dict for a treeview item ID (full_path)."""
        for e in self._entries:
            if e["full_path"] == iid:
                return e
        return None

    def _set_status(self, msg: str) -> None:
        try:
            self._status_var.set(msg)
        except tk.TclError:
            pass
        self._on_status(msg)

    # ── Public API ────────────────────────────────────────────────────────────

    def set_peer(self, peer: "FileBrowserPanel") -> None:
        """Wire the opposite panel after both are created."""
        self._peer = peer

    @property
    def current_path(self) -> str:
        return self._current_path

    @property
    def browser(self) -> RemoteBrowser | None:
        return self._browser
