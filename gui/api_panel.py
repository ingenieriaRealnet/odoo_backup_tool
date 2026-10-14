"""
"Conexiones API" page: create, edit, delete and test the external API
connections (core/api/connections.py). The API key is typed in a masked dialog
and goes straight to the Windows Credential Manager; it is never shown again
nor written to api_connections.json.
"""
from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from core.api.connections import ApiConnectionManager
from core.api.connectors import CONNECTOR_TYPES
from gui import theme

_TYPE_LABELS = {cls.LABEL: key for key, cls in CONNECTOR_TYPES.items()}
# Union of the extra fields of every connector type: (key, label) in a stable order.
_EXTRA_FIELDS = list(dict.fromkeys(
    (key, label) for cls in CONNECTOR_TYPES.values() for key, label, _required in cls.FIELDS
))


class ApiPanel(ttk.Frame):
    """
    Args:
        parent:    Scrollable page container.
        manager:   ApiConnectionManager shared by the app.
        ui_queue:  The app's thread->GUI queue (("ui_call", fn) events).
    """

    def __init__(self, parent: tk.Widget, manager: ApiConnectionManager, ui_queue: queue.Queue) -> None:
        super().__init__(parent)
        self._manager = manager
        self._q = ui_queue
        self._editing: str | None = None  # name of the connection loaded in the form
        self.columnconfigure(0, weight=1)

        self._v_name = tk.StringVar()
        self._v_type = tk.StringVar()
        self._v_url = tk.StringVar()
        self._v_user = tk.StringVar()
        self._v_notes = tk.StringVar()
        self._v_extra = {key: tk.StringVar() for key, _label in _EXTRA_FIELDS}
        self._extra_entries: dict[str, ttk.Entry] = {}

        self._build_list()
        self._build_form()
        self.refresh()
        self._new()

    # ── Construction ─────────────────────────────────────────────────────────

    def _build_list(self) -> None:
        box = ttk.LabelFrame(self, text="Conexiones", padding=theme.PAD)
        box.grid(row=0, column=0, sticky="ew", pady=(0, theme.PAD))
        box.columnconfigure(0, weight=1)
        ttk.Label(
            box, wraplength=680, justify="left", foreground=theme.C_MUTED,
            text="Sistemas externos a los que la herramienta se conecta por API (p. ej. el Odoo de Realnet para "
                 "el tablero de tareas y la mesa de ayuda). Las claves se guardan cifradas en el Administrador "
                 "de credenciales de Windows, no en archivos.",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self._tree = ttk.Treeview(box, columns=("type", "url", "key"), height=5, selectmode="browse")
        self._tree.heading("#0", text="Nombre")
        self._tree.heading("type", text="Tipo")
        self._tree.heading("url", text="URL")
        self._tree.heading("key", text="Clave")
        self._tree.column("#0", width=140)
        self._tree.column("type", width=170)
        self._tree.column("url", width=260)
        self._tree.column("key", width=70, anchor="center")
        self._tree.grid(row=1, column=0, sticky="ew")
        self._tree.bind("<<TreeviewSelect>>", self._on_select)
        buttons = ttk.Frame(box)
        buttons.grid(row=1, column=1, sticky="n", padx=(8, 0))
        ttk.Button(buttons, text="Nueva", command=self._new).pack(fill="x")
        ttk.Button(buttons, text="Eliminar", command=self._delete).pack(fill="x", pady=(4, 0))

    def _build_form(self) -> None:
        box = ttk.LabelFrame(self, text="Detalle", padding=theme.PAD)
        box.grid(row=1, column=0, sticky="ew", pady=(0, theme.PAD))
        box.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(box, text="Nombre:").grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(box, textvariable=self._v_name, width=30).grid(row=row, column=1, sticky="w", pady=2)
        row += 1
        ttk.Label(box, text="Tipo:").grid(row=row, column=0, sticky="w", pady=2)
        combo = ttk.Combobox(box, textvariable=self._v_type, values=list(_TYPE_LABELS), state="readonly", width=32)
        combo.grid(row=row, column=1, sticky="w", pady=2)
        combo.bind("<<ComboboxSelected>>", lambda _e: self._sync_type_fields())
        row += 1
        ttk.Label(box, text="URL base:").grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(box, textvariable=self._v_url).grid(row=row, column=1, sticky="ew", pady=2)
        row += 1
        ttk.Label(box, text="Usuario de la clave:").grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(box, textvariable=self._v_user, width=40).grid(row=row, column=1, sticky="w", pady=2)
        for key, label in _EXTRA_FIELDS:
            row += 1
            ttk.Label(box, text=f"{label}:").grid(row=row, column=0, sticky="w", pady=2)
            entry = ttk.Entry(box, textvariable=self._v_extra[key], width=48)
            entry.grid(row=row, column=1, sticky="w", pady=2)
            self._extra_entries[key] = entry
        row += 1
        ttk.Label(box, text="Notas:").grid(row=row, column=0, sticky="w", pady=2)
        ttk.Entry(box, textvariable=self._v_notes).grid(row=row, column=1, sticky="ew", pady=2)
        row += 1
        ttk.Label(box, text="Parámetros (JSON):").grid(row=row, column=0, sticky="nw", pady=2)
        self._txt_settings = tk.Text(box, height=9, font=("Consolas", 9), wrap="none")
        self._txt_settings.grid(row=row, column=1, sticky="ew", pady=2)

        row += 1
        actions = ttk.Frame(box)
        actions.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(actions, text="Guardar", style="Primary.TButton", command=self._save).pack(side="left")
        ttk.Button(actions, text="Guardar clave...", command=self._set_key).pack(side="left", padx=(6, 0))
        self._btn_test = ttk.Button(actions, text="Probar conexión", command=self._test)
        self._btn_test.pack(side="left", padx=(6, 0))
        self._lbl_result = ttk.Label(actions, text="", foreground=theme.C_MUTED)
        self._lbl_result.pack(side="left", padx=(10, 0))

    # ── Data ─────────────────────────────────────────────────────────────────

    def refresh(self, select: str | None = None) -> None:
        self._tree.delete(*self._tree.get_children())
        for connection in self._manager.list():
            cls = CONNECTOR_TYPES.get(connection["type"])
            self._tree.insert("", "end", iid=connection["name"], text=connection["name"], values=(
                cls.LABEL if cls else connection["type"], connection["base_url"],
                "Sí" if self._manager.has_secret(connection["name"]) else "No"))
        if select and self._tree.exists(select):
            self._tree.selection_set(select)

    def _on_select(self, _event=None) -> None:
        selection = self._tree.selection()
        if selection:
            self._load(self._manager.get(selection[0]))

    def _load(self, connection: dict | None) -> None:
        if not connection:
            return
        self._editing = connection["name"]
        self._v_name.set(connection["name"])
        cls = CONNECTOR_TYPES.get(connection["type"])
        self._v_type.set(cls.LABEL if cls else "")
        self._v_url.set(connection.get("base_url", ""))
        self._v_user.set(connection.get("user", ""))
        self._v_notes.set(connection.get("notes", ""))
        for key, var in self._v_extra.items():
            var.set(connection.get(key, ""))
        self._txt_settings.delete("1.0", "end")
        self._txt_settings.insert("1.0", json.dumps(connection.get("settings", {}), indent=2, ensure_ascii=False))
        self._sync_type_fields()
        self._lbl_result.config(text="")

    def _new(self) -> None:
        self._editing = None
        self._tree.selection_remove(*self._tree.selection())
        for var in (self._v_name, self._v_url, self._v_user, self._v_notes, *self._v_extra.values()):
            var.set("")
        self._v_type.set(next(iter(_TYPE_LABELS)))
        self._txt_settings.delete("1.0", "end")
        self._txt_settings.insert("1.0", "{}")
        self._sync_type_fields()
        self._lbl_result.config(text="")

    def _sync_type_fields(self) -> None:
        """Enable only the extra fields the selected connector type uses."""
        cls = CONNECTOR_TYPES.get(_TYPE_LABELS.get(self._v_type.get(), ""))
        used = {key for key, _label, _req in cls.FIELDS} if cls else set()
        for key, entry in self._extra_entries.items():
            entry.config(state="normal" if key in used else "disabled")

    def _form_connection(self) -> dict:
        try:
            settings = json.loads(self._txt_settings.get("1.0", "end").strip() or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"Parámetros: JSON inválido ({exc.msg}, línea {exc.lineno}).") from exc
        type_key = _TYPE_LABELS.get(self._v_type.get(), "")
        connection = {"name": self._v_name.get(), "type": type_key, "base_url": self._v_url.get(),
                      "user": self._v_user.get().strip(), "notes": self._v_notes.get().strip(), "settings": settings}
        cls = CONNECTOR_TYPES.get(type_key)
        for key, _label, _req in (cls.FIELDS if cls else ()):
            connection[key] = self._v_extra[key].get().strip()
        return connection

    # ── Actions ──────────────────────────────────────────────────────────────

    def _save(self) -> bool:
        try:
            connection = self._form_connection()
            self._manager.save(connection, original_name=self._editing)
        except (ValueError, OSError) as exc:
            messagebox.showwarning(theme.APP_TITLE, str(exc), parent=self)
            return False
        self._editing = connection["name"].strip()
        self.refresh(select=self._editing)
        self._lbl_result.config(text="Conexión guardada.", foreground=theme.LEVEL_COLORS["ok"][0])
        return True

    def _set_key(self) -> None:
        if not self._editing and not self._save():
            return
        secret = simpledialog.askstring(
            theme.APP_TITLE, f"Clave API de '{self._editing}'.\nSe guarda en el Administrador de credenciales de Windows.",
            show="*", parent=self)
        if not secret:
            return
        try:
            self._manager.set_secret(self._editing, secret)
        except (ValueError, OSError, RuntimeError) as exc:
            messagebox.showerror(theme.APP_TITLE, str(exc), parent=self)
            return
        self.refresh(select=self._editing)
        self._lbl_result.config(text="Clave guardada.", foreground=theme.LEVEL_COLORS["ok"][0])

    def _test(self) -> None:
        if not self._editing:
            messagebox.showinfo(theme.APP_TITLE, "Guarde la conexión antes de probarla.", parent=self)
            return
        name = self._editing
        self._btn_test.config(state="disabled")
        self._lbl_result.config(text="Probando...", foreground=theme.C_MUTED)

        def _work() -> None:
            try:
                outcome = (True, self._manager.connector(name).test())
            except Exception as exc:  # noqa: BLE001 — shown to the user verbatim
                outcome = (False, f"Falló: {exc}")
            self._q.put(("ui_call", lambda: self._test_finished(*outcome)))
        threading.Thread(target=_work, name="api-test", daemon=True).start()

    def _test_finished(self, ok: bool, message: str) -> None:
        self._btn_test.config(state="normal")
        self._lbl_result.config(text=message[:160], foreground=theme.LEVEL_COLORS["ok" if ok else "crit"][0])

    def _delete(self) -> None:
        if not self._editing:
            return
        if not messagebox.askyesno(theme.APP_TITLE, f"¿Eliminar la conexión '{self._editing}' y su clave guardada?",
                                   parent=self):
            return
        self._manager.delete(self._editing)
        self.refresh()
        self._new()
