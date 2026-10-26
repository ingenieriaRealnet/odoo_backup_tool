"""
Modal dialogs of the main window: file-overwrite confirmation and the
scheduled-backup rule editor. Moved out of gui/app.py unchanged.
"""
from __future__ import annotations
import datetime
import os
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


def _add_timestamp(filename: str) -> str:
    """Insert a timestamp before the file extension: file.dump → file_2026-06-25_14-03.dump"""
    ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
    base, ext = os.path.splitext(filename)
    return f"{base}_{ts}{ext}"


class _OverwriteDialog(tk.Toplevel):
    """
    Modal dialog shown when a backup file already exists at the destination.

    Returns (action, final_filename) where action is:
      'rename_ts'  — use auto timestamp name  (default)
      'rename_custom' — use the name typed by the user
      'overwrite'  — keep the original name and overwrite
      'cancel'     — abort the entire backup
    """

    def __init__(self, parent: tk.Tk, filename: str, dest_desc: str) -> None:
        super().__init__(parent)
        self.title("Archivo ya existe")
        self.resizable(False, False)
        self.grab_set()          # modal
        self.focus_set()

        self._action = "cancel"
        self._filename = filename
        self._ts_name = _add_timestamp(filename)

        self._choice = tk.StringVar(value="rename_ts")
        self._custom_name = tk.StringVar(value=filename)

        pad = 10
        tk.Label(
            self,
            text=f'El archivo ya existe en el destino:\n"{dest_desc}/{filename}"',
            justify="left", wraplength=420,
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=pad, pady=(pad, 4))

        tk.Label(self, text="¿Qué desea hacer?", font=("Segoe UI", 9, "bold")).grid(
            row=1, column=0, columnspan=2, sticky="w", padx=pad, pady=(4, 2)
        )

        # Option 1: rename with timestamp (default)
        ttk.Radiobutton(
            self, text=f"Renombrar con timestamp  →  {self._ts_name}",
            variable=self._choice, value="rename_ts",
            command=self._toggle,
        ).grid(row=2, column=0, columnspan=2, sticky="w", padx=pad + 4, pady=2)

        # Option 2: custom name
        ttk.Radiobutton(
            self, text="Nombre personalizado:",
            variable=self._choice, value="rename_custom",
            command=self._toggle,
        ).grid(row=3, column=0, sticky="w", padx=pad + 4, pady=2)
        self._custom_entry = ttk.Entry(self, textvariable=self._custom_name, width=32, state="disabled")
        self._custom_entry.grid(row=3, column=1, sticky="w", padx=(0, pad), pady=2)

        # Option 3: overwrite
        ttk.Radiobutton(
            self, text="Sobreescribir el archivo existente",
            variable=self._choice, value="overwrite",
            command=self._toggle,
        ).grid(row=4, column=0, columnspan=2, sticky="w", padx=pad + 4, pady=2)

        # Buttons
        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=5, column=0, columnspan=2, pady=pad)
        ttk.Button(btn_frame, text="Aceptar", command=self._ok).pack(side="left", padx=6)
        ttk.Button(btn_frame, text="Cancelar backup", command=self._cancel).pack(side="left", padx=6)

        # Center over parent
        self.update_idletasks()
        px = parent.winfo_x() + (parent.winfo_width()  - self.winfo_width())  // 2
        py = parent.winfo_y() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{px}+{py}")

    def _toggle(self) -> None:
        state = "normal" if self._choice.get() == "rename_custom" else "disabled"
        self._custom_entry.config(state=state)
        if state == "normal":
            self._custom_entry.focus_set()

    def _ok(self) -> None:
        choice = self._choice.get()
        if choice == "rename_ts":
            self._action, self._filename = "rename_ts", self._ts_name
        elif choice == "rename_custom":
            name = self._custom_name.get().strip()
            if not name:
                messagebox.showwarning("Nombre requerido", "Ingrese un nombre de archivo.", parent=self)
                return
            self._action, self._filename = "rename_custom", name
        else:
            self._action, self._filename = "overwrite", self._filename
        self.destroy()

    def _cancel(self) -> None:
        self._action = "cancel"
        self.destroy()

    def show(self) -> tuple[str, str]:
        """Block until the user closes the dialog and return (action, filename)."""
        self.wait_window()
        return self._action, self._filename


class _ScheduleDialog(tk.Toplevel):
    """
    Modal dialog for creating or editing a scheduled-backup rule.

    Args:
        parent:       Parent Tk window.
        profile_mgr:  ProfileManager instance (to populate server dropdown).
        rule:         Existing rule dict to edit, or None to create a new one.
    """

    def __init__(self, parent, profile_mgr, rule: dict | None) -> None:
        super().__init__(parent)
        self.title("Regla de backup" if rule is None else "Editar regla de backup")
        self.resizable(True, True)
        # No grab_set() — diálogo no modal para que el usuario pueda consultar
        # otros tabs de la herramienta mientras llena los campos de la regla.
        self.focus_set()

        self._profile_mgr = profile_mgr
        self._result: dict | None = None

        # Pre-fill from rule or use defaults
        r = rule or {}
        _PAD = 8

        # ── Variables ─────────────────────────────────────────────────────
        self._v_label        = tk.StringVar(value=r.get("label", ""))
        self._v_server       = tk.StringVar(value=r.get("server_profile", ""))
        self._v_db_name      = tk.StringVar(value=r.get("db_name", ""))
        self._v_db_fmt       = tk.StringVar(value=r.get("db_format", "dump"))
        self._v_inc_db       = tk.BooleanVar(value=r.get("include_db", True))
        self._v_inc_fs       = tk.BooleanVar(value=r.get("include_filestore", True))
        self._v_fs_root      = tk.StringVar(value=r.get("filestore_root", ""))
        self._v_fs_db        = tk.StringVar(value=r.get("filestore_db", ""))
        self._v_dest_type    = tk.StringVar(value=r.get("dest_type", "gdrive"))
        self._v_local_dir    = tk.StringVar(value=r.get("dest_local_dir", ""))
        self._v_rem_profile  = tk.StringVar(value=r.get("dest_remote_profile", ""))
        self._v_rem_dir      = tk.StringVar(value=r.get("dest_remote_dir", "/opt/backups"))
        self._v_gdrive_creds = tk.StringVar(value=r.get("dest_gdrive_creds", ""))
        self._v_gdrive_folder= tk.StringVar(value=r.get("dest_gdrive_folder_id", ""))
        self._v_hour         = tk.StringVar(value=str(r.get("schedule_hour", 2)))
        self._v_minute       = tk.StringVar(value=str(r.get("schedule_minute", 0)))
        self._v_retention    = tk.StringVar(value=str(r.get("retention_days", 90)))
        self._v_cleanup      = tk.BooleanVar(value=r.get("cleanup_server", True))
        self._v_enabled      = tk.BooleanVar(value=r.get("enabled", True))
        self._v_upload_direct= tk.BooleanVar(value=r.get("upload_mode") == "direct")

        # ── Layout ────────────────────────────────────────────────────────
        self.columnconfigure(0, weight=1)
        content = ttk.Frame(self, padding=_PAD)
        content.grid(row=0, column=0, sticky="nsew")
        content.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        row = 0

        # Nombre
        ttk.Label(content, text="Nombre de la regla:").grid(row=row, column=0, sticky="e", padx=(0, _PAD), pady=4)
        ttk.Entry(content, textvariable=self._v_label).grid(row=row, column=1, sticky="ew", pady=4)
        row += 1

        # Habilitado
        ttk.Checkbutton(content, text="Regla habilitada", variable=self._v_enabled).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=4
        )
        row += 1

        ttk.Separator(content, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=6)
        row += 1

        # Servidor
        ttk.Label(content, text="Servidor (perfil):", font=("Segoe UI", 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 2)
        )
        row += 1
        ttk.Label(content, text="Perfil SSH:").grid(row=row, column=0, sticky="e", padx=(0, _PAD), pady=4)
        self._cb_server = ttk.Combobox(
            content, textvariable=self._v_server,
            values=profile_mgr.names(), state="readonly",
        )
        self._cb_server.grid(row=row, column=1, sticky="ew", pady=4)
        self._cb_server.bind("<<ComboboxSelected>>", self._on_profile_selected)
        row += 1

        ttk.Separator(content, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=6)
        row += 1

        # Base de datos
        ttk.Label(content, text="Base de datos:", font=("Segoe UI", 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 2)
        )
        row += 1
        ttk.Label(content, text="Nombre de la BD:").grid(row=row, column=0, sticky="e", padx=(0, _PAD), pady=4)
        ttk.Entry(content, textvariable=self._v_db_name).grid(row=row, column=1, sticky="ew", pady=4)
        row += 1
        ttk.Label(content, text="Formato:").grid(row=row, column=0, sticky="e", padx=(0, _PAD), pady=4)
        fmt_frame = ttk.Frame(content)
        fmt_frame.grid(row=row, column=1, sticky="w")
        ttk.Radiobutton(fmt_frame, text=".dump (recomendado)", variable=self._v_db_fmt, value="dump").pack(side="left", padx=(0, 12))
        ttk.Radiobutton(fmt_frame, text=".sql (texto plano)", variable=self._v_db_fmt, value="sql").pack(side="left")
        row += 1

        ttk.Separator(content, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=6)
        row += 1

        # Filestore
        ttk.Label(content, text="Filestore:", font=("Segoe UI", 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 2)
        )
        row += 1
        ttk.Checkbutton(content, text="Incluir dump de BD", variable=self._v_inc_db).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=2
        )
        row += 1
        ttk.Checkbutton(content, text="Incluir filestore", variable=self._v_inc_fs).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=2
        )
        row += 1
        ttk.Label(content, text="Ruta raiz filestore:").grid(row=row, column=0, sticky="e", padx=(0, _PAD), pady=4)
        ttk.Entry(content, textvariable=self._v_fs_root).grid(row=row, column=1, sticky="ew", pady=4)
        row += 1
        ttk.Label(content, text="Carpeta BD filestore:").grid(row=row, column=0, sticky="e", padx=(0, _PAD), pady=4)
        ttk.Entry(content, textvariable=self._v_fs_db).grid(row=row, column=1, sticky="ew", pady=4)
        row += 1

        ttk.Separator(content, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=6)
        row += 1

        # Destino
        ttk.Label(content, text="Destino:", font=("Segoe UI", 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 2)
        )
        row += 1
        dest_rb_frame = ttk.Frame(content)
        dest_rb_frame.grid(row=row, column=0, columnspan=2, sticky="w")
        ttk.Radiobutton(dest_rb_frame, text="Google Drive", variable=self._v_dest_type, value="gdrive",
                        command=self._toggle_dest).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(dest_rb_frame, text="Local", variable=self._v_dest_type, value="local",
                        command=self._toggle_dest).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(dest_rb_frame, text="Otro servidor", variable=self._v_dest_type, value="remote",
                        command=self._toggle_dest).pack(side="left")
        row += 1

        # Drive panel
        self._pnl_gdrive = ttk.Frame(content)
        self._pnl_gdrive.columnconfigure(1, weight=1)
        ttk.Label(self._pnl_gdrive, text="Credenciales JSON:").grid(row=0, column=0, sticky="e", padx=(0, _PAD), pady=4)
        _gdrive_row = ttk.Frame(self._pnl_gdrive)
        _gdrive_row.grid(row=0, column=1, sticky="ew")
        _gdrive_row.columnconfigure(0, weight=1)
        ttk.Entry(_gdrive_row, textvariable=self._v_gdrive_creds).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(_gdrive_row, text="...", width=3,
                   command=lambda: self._v_gdrive_creds.set(
                       filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("Todos", "*.*")]) or self._v_gdrive_creds.get()
                   )).grid(row=0, column=1)
        ttk.Label(self._pnl_gdrive, text="Carpeta Drive (ID):").grid(row=1, column=0, sticky="e", padx=(0, _PAD), pady=4)
        ttk.Entry(self._pnl_gdrive, textvariable=self._v_gdrive_folder).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Checkbutton(
            self._pnl_gdrive,
            text="Subida directa servidor -> Drive (rclone, evita el relé por esta máquina)",
            variable=self._v_upload_direct,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 4))
        ttk.Label(
            self._pnl_gdrive,
            text="Si el servidor no tiene salida a Google o no se puede instalar rclone,\n"
                 "esta regla usa el modo relé (streaming) de todos modos.",
            foreground="#666666",
        ).grid(row=3, column=0, columnspan=2, sticky="w")

        # Local panel
        self._pnl_local = ttk.Frame(content)
        self._pnl_local.columnconfigure(1, weight=1)
        ttk.Label(self._pnl_local, text="Directorio local:").grid(row=0, column=0, sticky="e", padx=(0, _PAD), pady=4)
        _local_row = ttk.Frame(self._pnl_local)
        _local_row.grid(row=0, column=1, sticky="ew")
        _local_row.columnconfigure(0, weight=1)
        ttk.Entry(_local_row, textvariable=self._v_local_dir).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(_local_row, text="...", width=3,
                   command=lambda: self._v_local_dir.set(
                       filedialog.askdirectory(title="Seleccionar directorio destino") or self._v_local_dir.get()
                   )).grid(row=0, column=1)

        # Remote panel
        self._pnl_remote = ttk.Frame(content)
        self._pnl_remote.columnconfigure(1, weight=1)
        ttk.Label(self._pnl_remote, text="Perfil servidor:").grid(row=0, column=0, sticky="e", padx=(0, _PAD), pady=4)
        ttk.Combobox(self._pnl_remote, textvariable=self._v_rem_profile,
                     values=profile_mgr.names(), state="readonly").grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Label(self._pnl_remote, text="Directorio destino:").grid(row=1, column=0, sticky="e", padx=(0, _PAD), pady=4)
        ttk.Entry(self._pnl_remote, textvariable=self._v_rem_dir).grid(row=1, column=1, sticky="ew", pady=4)

        self._dest_row = row
        self._content = content
        self._toggle_dest()
        row += 1

        ttk.Separator(content, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=6)
        row += 1

        # Programacion
        ttk.Label(content, text="Programacion:", font=("Segoe UI", 9, "bold")).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(0, 2)
        )
        row += 1
        time_frame = ttk.Frame(content)
        time_frame.grid(row=row, column=0, columnspan=2, sticky="w")
        ttk.Label(time_frame, text="Hora de ejecucion (HH MM):").pack(side="left", padx=(0, _PAD))
        ttk.Spinbox(time_frame, textvariable=self._v_hour, from_=0, to=23, width=4, format="%02.0f").pack(side="left", padx=(0, 4))
        ttk.Label(time_frame, text=":").pack(side="left")
        ttk.Spinbox(time_frame, textvariable=self._v_minute, from_=0, to=59, width=4, format="%02.0f").pack(side="left", padx=(4, 0))
        row += 1

        ret_frame = ttk.Frame(content)
        ret_frame.grid(row=row, column=0, columnspan=2, sticky="w")
        ttk.Label(ret_frame, text="Retener backups (dias, 0 = sin limite):").pack(side="left", padx=(0, _PAD))
        ttk.Spinbox(ret_frame, textvariable=self._v_retention, from_=0, to=3650, width=6).pack(side="left")
        row += 1

        ttk.Separator(content, orient="horizontal").grid(row=row, column=0, columnspan=2, sticky="ew", pady=6)
        row += 1

        # Opciones
        ttk.Checkbutton(content, text="Limpiar /tmp/ del servidor al terminar", variable=self._v_cleanup).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=4
        )
        row += 1

        # Buttons
        btn_frame = ttk.Frame(self, padding=(_PAD, 0, _PAD, _PAD))
        btn_frame.grid(row=1, column=0, sticky="ew")
        ttk.Button(btn_frame, text="Guardar", style="Primary.TButton" if hasattr(ttk.Style(), "theme_use") else "TButton",
                   command=self._save).pack(side="right", padx=4)
        ttk.Button(btn_frame, text="Cancelar", command=self.destroy).pack(side="right", padx=4)

        self.update_idletasks()
        px = parent.winfo_x() + max(0, (parent.winfo_width()  - self.winfo_width())  // 2)
        py = parent.winfo_y() + max(0, (parent.winfo_height() - self.winfo_height()) // 2)
        self.geometry(f"+{px}+{py}")

    def _on_profile_selected(self, event=None) -> None:
        """Auto-fill Drive credentials from the selected server profile."""
        name = self._v_server.get()
        p = self._profile_mgr.get(name)
        if p:
            if p.get("gdrive_creds_path") and not self._v_gdrive_creds.get():
                self._v_gdrive_creds.set(p["gdrive_creds_path"])
            if p.get("gdrive_folder_id") and not self._v_gdrive_folder.get():
                self._v_gdrive_folder.set(p["gdrive_folder_id"])
            # Auto-fill filestore root hint (user may need to adjust)
            if not self._v_fs_root.get():
                self._v_fs_root.set("/var/lib/odoo/filestore")

    def _toggle_dest(self) -> None:
        """Show/hide destination sub-panels based on the selected radio."""
        dest = self._v_dest_type.get()
        row  = self._dest_row
        for pnl in (self._pnl_gdrive, self._pnl_local, self._pnl_remote):
            pnl.grid_remove()
        if dest == "gdrive":
            self._pnl_gdrive.grid(row=row, column=0, columnspan=2, sticky="ew", padx=(20, 0))
        elif dest == "local":
            self._pnl_local.grid(row=row, column=0, columnspan=2, sticky="ew", padx=(20, 0))
        else:
            self._pnl_remote.grid(row=row, column=0, columnspan=2, sticky="ew", padx=(20, 0))

    def _save(self) -> None:
        """Validate and collect the rule dict from dialog fields."""
        label   = self._v_label.get().strip()
        db_name = self._v_db_name.get().strip()
        if not label:
            messagebox.showwarning("Validacion", "Ingrese un nombre para la regla.", parent=self)
            return
        if not db_name:
            messagebox.showwarning("Validacion", "Ingrese el nombre de la base de datos.", parent=self)
            return
        if not self._v_server.get():
            messagebox.showwarning("Validacion", "Seleccione un perfil de servidor.", parent=self)
            return
        try:
            hour   = int(self._v_hour.get())
            minute = int(self._v_minute.get())
        except ValueError:
            messagebox.showwarning("Validacion", "Hora o minuto invalidos.", parent=self)
            return

        self._result = {
            "label":                self._v_label.get().strip(),
            "enabled":              self._v_enabled.get(),
            "server_profile":       self._v_server.get(),
            "db_name":              db_name,
            "db_format":            self._v_db_fmt.get(),
            "include_db":           self._v_inc_db.get(),
            "include_filestore":    self._v_inc_fs.get(),
            "filestore_root":       self._v_fs_root.get().strip(),
            "filestore_db":         self._v_fs_db.get().strip() or db_name,
            "dest_type":            self._v_dest_type.get(),
            "dest_local_dir":       self._v_local_dir.get().strip(),
            "dest_remote_profile":  self._v_rem_profile.get(),
            "dest_remote_dir":      self._v_rem_dir.get().strip(),
            "dest_gdrive_creds":    self._v_gdrive_creds.get().strip(),
            "dest_gdrive_folder_id":self._v_gdrive_folder.get().strip(),
            "schedule_hour":        hour,
            "schedule_minute":      minute,
            "retention_days":       int(self._v_retention.get() or 90),
            "cleanup_server":       self._v_cleanup.get(),
            "upload_mode":          "direct" if self._v_upload_direct.get() else "relay",
        }
        self.destroy()

    def show(self) -> dict | None:
        """Block until the dialog closes and return the rule dict or None."""
        self.wait_window()
        return self._result
