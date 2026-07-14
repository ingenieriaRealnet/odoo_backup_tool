"""
Main application window for Odoo Backup Tool.

Implements a wizard with two sections:
  Backup  (tabs 1-5): connect, select DB, select filestore, destination, execute
  Restore (tab 6)  : upload/locate files, create DB, restore, neutralize
"""
from __future__ import annotations
import datetime
import json
import os
import queue
import tempfile
import threading
import tkinter as tk
import re
from tkinter import filedialog, messagebox, simpledialog, ttk

from core.addons_manager import AddonsManager, scan_ssh_directory
from core.file_browser import RemoteBrowser
from core.db_manager import DBManager
from core.gdrive import DriveUploader
from core.inventory_manager import InventoryManager
from core.filestore_manager import FilestoreManager
from core.profiles import ProfileManager
from core.restore_manager import RestoreManager
from core.ssh_client import SSHClient
from core.transfer import TransferManager
from core.version import APP_VERSION, GITHUB_REPO
from core.updater import check_for_update
from gui.help_window import HelpWindow
from gui.file_browser_panel import FileBrowserPanel
from gui.ssh_terminal_panel import SshTerminalPanel
from core.trial_manager import TrialManager
from core.scheduler import ScheduleManager, BackupScheduler
from core.bundle_manager import BundleManager

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


# Valid PostgreSQL identifier: starts with letter/underscore, max 63 chars
_DB_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')

# Keyword pattern for word-level log highlighting: INFO / WARNING / WARN / ERROR
# Matches both bare words and bracket-wrapped forms like [INFO], [ERROR]
_KW_LOG_RE = re.compile(
    r'\[(?:INFO|WARNING|WARN|ERROR)\]|\b(?:INFO|WARNING|WARN|ERROR)\b',
    re.IGNORECASE,
)

APP_TITLE = "Odoo Backup Tool"
_PAD = 8

# Persistent settings file (geometry, sash position)
_SETTINGS_FILE = os.path.join(
    os.path.expanduser("~"), ".odoo_backup_tool", "settings.json"
)
_DARK_BG  = "#714B67"   # Odoo brand purple — header bar
_LOG_BG   = "#1E1B1D"   # Near-black log panel
_LOG_FG   = "#D4CFCF"   # Warm light gray text in log

# ── Theme palette (used by _apply_styles) ────────────────────────────────────
_C_BG       = "#F5F3F0"   # Warm near-white background
_C_PURPLE   = "#714B67"   # Odoo brand purple
_C_PURPLE2  = "#8B6080"   # Lighter purple for hover
_C_PURPLE3  = "#5A3A52"   # Darker purple for pressed
_C_TEAL     = "#00A09D"   # Odoo teal accent (progressbar, stripe)
_C_WHITE    = "#FFFFFF"
_C_TEXT     = "#2C2424"   # Warm dark text
_C_BORDER   = "#C8BEC5"   # Muted purple-gray border
_C_RED      = "#C0392B"   # Stop/danger
_C_RED2     = "#E74C3C"   # Stop hover


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
        }
        self.destroy()

    def show(self) -> dict | None:
        """Block until the dialog closes and return the rule dict or None."""
        self.wait_window()
        return self._result


class BackupApp:
    """Root window and controller for the Odoo Backup Tool."""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._configure_window()
        self._apply_styles()

        # SSH client shared across backup tabs
        self._ssh = SSHClient()
        # SSH client for restore destination (used when target != backup origin)
        self._ssh_restore = SSHClient()
        # SSH client for the backup destination server (Step 4 "otro servidor remoto")
        self._ssh_dest = SSHClient()

        # Persistent connection profiles (loaded from ~/.odoo_backup_tool/servers.json)
        self._profiles = ProfileManager()

        # Schedule rules manager (shared with BackupScheduler)
        self._sched_mgr = ScheduleManager()

        # ── Backup state variables ────────────────────────────────────────
        self._v_db = tk.StringVar()
        self._v_dump_fmt = tk.StringVar(value="dump")
        self._v_fs_root = tk.StringVar()
        self._v_fs_db = tk.StringVar()
        self._v_dest_type = tk.StringVar(value="local")
        self._v_local_dir = tk.StringVar(
            value=os.path.join(os.path.expanduser("~"), "Downloads")
        )
        # Google Drive destination fields
        self._v_gdrive_creds  = tk.StringVar()   # path to service account JSON
        self._v_gdrive_folder = tk.StringVar()   # Drive folder ID

        # Drive fields stored inside the server profile (Tab 1)
        self._v_prof_gdrive_creds  = tk.StringVar()
        self._v_prof_gdrive_folder = tk.StringVar()

        # Bundle options (Tab 5)
        self._v_bundle = tk.BooleanVar(value=True)

        # Restore-from-bundle state (Tab 6)
        self._v_r_restore_mode  = tk.StringVar(value="individual")   # "bundle" | "individual"
        self._v_r_bundle_local  = tk.StringVar(value="")
        self._v_r_bundle_src    = tk.StringVar(value="local")        # "local" | "server"
        self._v_r_bundle_srv    = tk.StringVar(value="")

        # ── Restore state variables ───────────────────────────────────────
        # 'origin' | 'dest' | 'other'
        self._v_r_conn_type  = tk.StringVar(value="origin")
        self._v_r_dump_src   = tk.StringVar(value="local") # 'local' | 'server'
        self._v_r_dump_local = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads"))
        self._v_r_dump_srv   = tk.StringVar(value="/tmp/odoo_bancasa_prod.dump")
        self._v_r_fs_src     = tk.StringVar(value="local") # 'local' | 'server' | 'none'
        self._v_r_fs_local   = tk.StringVar(value=os.path.join(os.path.expanduser("~"), "Downloads"))
        self._v_r_fs_srv     = tk.StringVar(value="/tmp/filestore_bancasa_prod.zip")
        self._v_r_db_name    = tk.StringVar()
        self._v_r_fs_root    = tk.StringVar()
        self._v_r_jobs       = tk.StringVar(value="4")
        self._v_r_neutralize = tk.BooleanVar(value=False)
        self._v_r_conf       = tk.StringVar(value="/etc/odoo/odoo.conf")
        self._v_r_cleanup    = tk.BooleanVar(value=True)
        self._v_r_inventory  = tk.StringVar()   # path to companion inventory JSON

        # ── Addons sync state variables ───────────────────────────────────
        self._v_a_conn_type  = tk.StringVar(value="origin")
        self._v_a_repo_url   = tk.StringVar()
        self._v_a_branch     = tk.StringVar(value="main")
        self._v_a_target     = tk.StringVar(value="/opt/odoo/addons_custom")
        self._v_a_odoo_user  = tk.StringVar(value="odoo")
        self._v_a_restart      = tk.BooleanVar(value=False)
        self._v_a_service      = tk.StringVar(value="odoo")
        self._v_a_submodules   = tk.BooleanVar(value=False)  # use git submodule update sequence

        # SSH key source priority cascade:
        #   "server"   — key already on the remote server's ~/.ssh/
        #   "local"    — key from this local machine (upload temporarily)
        #   "generate" — generate a new Ed25519 key on this machine
        self._v_a_key_source   = tk.StringVar(value="server")
        self._v_a_server_key   = tk.StringVar()    # remote path selected from scan
        self._v_a_ssh_key      = tk.StringVar()    # local key path (mode "local")
        self._v_a_gen_key_name = tk.StringVar(value="odoo_github_key")
        self._v_a_pub_key_text = tk.StringVar()    # display-only public key after generate

        # Per-session passphrase cache keyed by key path — NEVER written to disk.
        # Cleared automatically when the app closes.
        self._key_passphrases: dict[str, str] = {}

        # Cancellation flag shared between GUI and worker threads
        self._cancel_event = threading.Event()

        # Thread -> GUI message queue
        self._q: queue.Queue = queue.Queue()

        # Retry state — set when a transfer fails after files are ready on the server.
        # Allows re-running only the transfer step with same or different destination.
        self._retry_remote_tmp:  list[str]   = []
        self._retry_conn_params: dict        = {}
        self._retry_dump_path:   str         = ""
        self._retry_inventory:   dict | None = None

        self._build_ui()
        self._poll_queue()

        # Kick off the background update check after the UI is ready.
        # The callback schedules the banner display on the main thread via root.after.
        check_for_update(GITHUB_REPO, APP_VERSION, self._on_update_check_result)

        # Start the backup scheduler daemon thread.
        # It shares self._q so schedule events land in the same poll loop.
        self._scheduler = BackupScheduler(self._sched_mgr, self._profiles, self._q)
        self._scheduler.start()

    # ── Theme / Style ─────────────────────────────────────────────────────

    def _apply_styles(self) -> None:
        """
        Configure the ttk visual theme with Odoo brand colors.

        Uses 'clam' as base theme (most customizable cross-platform) and
        defines:
          - TButton            default action buttons
          - Primary.TButton    main CTA buttons (Conectar, Iniciar, etc.)
          - Stop.TButton       cancel/danger buttons (red)
          - TNotebook.Tab      purple when selected, neutral otherwise
          - TLabelframe        purple border + purple label
          - TProgressbar       teal fill bar
        """
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass  # Fallback to whatever theme is available

        self.root.configure(bg=_C_BG)

        # Base containers
        style.configure("TFrame",     background=_C_BG)
        style.configure("TPanedwindow", background=_C_BG)

        # Labels
        style.configure("TLabel", background=_C_BG, foreground=_C_TEXT, font=("Segoe UI", 9))
        style.configure("Bold.TLabel", background=_C_BG, foreground=_C_TEXT, font=("Segoe UI", 10, "bold"))

        # LabelFrame — purple border and title
        style.configure(
            "TLabelframe",
            background=_C_BG, bordercolor=_C_PURPLE,
            relief="groove", borderwidth=1,
        )
        style.configure(
            "TLabelframe.Label",
            background=_C_BG, foreground=_C_PURPLE,
            font=("Segoe UI", 9, "bold"),
        )

        # Default button
        style.configure(
            "TButton",
            background="#E6DEE3", foreground=_C_TEXT,
            bordercolor=_C_BORDER, focuscolor=_C_PURPLE,
            padding=(8, 4), font=("Segoe UI", 9), relief="flat",
        )
        style.map("TButton",
            background=[
                ("active",   "#D4C8D0"),
                ("pressed",  _C_BORDER),
                ("disabled", "#EAE5E8"),
            ],
            foreground=[("disabled", "#AAAAAA")],
        )

        # Primary action button
        style.configure(
            "Primary.TButton",
            background=_C_PURPLE, foreground=_C_WHITE,
            bordercolor=_C_PURPLE, padding=(12, 6),
            font=("Segoe UI", 10, "bold"), relief="flat",
        )
        style.map("Primary.TButton",
            background=[
                ("active",   _C_PURPLE2),
                ("pressed",  _C_PURPLE3),
                ("disabled", "#B099A8"),
            ],
            foreground=[
                ("active", _C_WHITE), ("pressed", _C_WHITE), ("disabled", "#DDDDDD"),
            ],
        )

        # Navigation / flow button  (Siguiente ->, back, step-advance)
        style.configure(
            "Nav.TButton",
            background=_C_TEAL, foreground=_C_WHITE,
            bordercolor=_C_TEAL, padding=(10, 5),
            font=("Segoe UI", 9, "bold"), relief="flat",
        )
        style.map("Nav.TButton",
            background=[
                ("active",   "#00B8B5"),
                ("pressed",  "#007A78"),
                ("disabled", "#90CECE"),
            ],
            foreground=[
                ("active", _C_WHITE), ("pressed", _C_WHITE), ("disabled", "#DDDDDD"),
            ],
        )

        # Stop / danger button
        style.configure(
            "Stop.TButton",
            background=_C_RED, foreground=_C_WHITE,
            bordercolor=_C_RED, padding=(10, 5),
            font=("Segoe UI", 9, "bold"), relief="flat",
        )
        style.map("Stop.TButton",
            background=[
                ("active",   _C_RED2),
                ("pressed",  "#922B21"),
                ("disabled", "#E0A89E"),
            ],
            foreground=[
                ("active", _C_WHITE), ("pressed", _C_WHITE), ("disabled", "#FFFFFF"),
            ],
        )

        # Notebook — purple active tab
        style.configure("TNotebook", background=_C_BG, tabmargins=[2, 5, 0, 0])
        style.configure(
            "TNotebook.Tab",
            background="#DDD5DA", foreground="#555050",
            padding=(14, 6), font=("Segoe UI", 9),
        )
        style.map("TNotebook.Tab",
            background=[
                ("disabled", "#C8C0C5"),
                ("selected", _C_PURPLE),
                ("active",   _C_PURPLE2),
            ],
            foreground=[
                ("disabled", "#999090"),
                ("selected", _C_WHITE),
                ("active",   _C_WHITE),
            ],
            font=[
                ("disabled", ("Segoe UI", 9)),
            ],
        )

        # Entry and Combobox
        style.configure(
            "TEntry",
            fieldbackground=_C_WHITE, bordercolor=_C_BORDER,
            selectbackground=_C_PURPLE, selectforeground=_C_WHITE,
        )
        style.configure(
            "TCombobox",
            fieldbackground=_C_WHITE, selectbackground=_C_PURPLE,
            selectforeground=_C_WHITE, arrowcolor=_C_PURPLE,
        )

        # Progressbar — teal fill
        style.configure(
            "TProgressbar",
            background=_C_TEAL, troughcolor="#DDD5DA",
            bordercolor=_C_BORDER, thickness=12,
        )

        # Checkbutton and Radiobutton
        for w in ("TCheckbutton", "TRadiobutton"):
            style.configure(w, background=_C_BG, foreground=_C_TEXT, font=("Segoe UI", 9))
            style.map(w, background=[("active", _C_BG)], indicatorcolor=[("selected", _C_PURPLE)])

        # Scrollbar
        style.configure(
            "TScrollbar",
            background=_C_BORDER, troughcolor=_C_BG, arrowcolor=_C_PURPLE,
        )

    # ── Window setup ─────────────────────────────────────────────────────

    def _configure_window(self) -> None:
        self.root.title(APP_TITLE)
        self.root.geometry("960x740")
        self.root.minsize(700, 500)
        self.root.configure(bg="#f0f0f0")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # Restore previous geometry if available; otherwise center on screen
        if not self._load_geometry():
            self.root.update_idletasks()
            w = self.root.winfo_width()
            h = self.root.winfo_height()
            x = (self.root.winfo_screenwidth()  - w) // 2
            y = (self.root.winfo_screenheight() - h) // 2
            self.root.geometry(f"+{x}+{y}")

    # ── UI construction ──────────────────────────────────────────────────

    def _build_ui(self) -> None:
        """Assemble status bar, header, resizable notebook+log paned area."""

        # ── Status bar — packed first so it stays at the bottom ──────────
        self._build_status_bar()

        # ── Header bar ───────────────────────────────────────────────────
        header = tk.Frame(self.root, bg=_DARK_BG, height=54)
        header.pack(fill="x")
        header.pack_propagate(False)

        tk.Label(
            header,
            text="  ⛃  ",
            font=("Segoe UI", 16),
            bg=_DARK_BG, fg="#E8D5E0",
        ).pack(side="left", pady=8)

        tk.Label(
            header,
            text=APP_TITLE,
            font=("Segoe UI", 13, "bold"),
            bg=_DARK_BG, fg="white",
        ).pack(side="left", pady=8)

        tk.Frame(header, bg=_C_TEAL, width=3).pack(side="left", fill="y", padx=(6, 0), pady=10)

        tk.Label(
            header,
            text=f"v{APP_VERSION}  —  Realnet  ",
            font=("Segoe UI", 9),
            bg=_DARK_BG, fg="#C4AAB8",
        ).pack(side="right", pady=8)

        # Help button — opens the documentation window
        tk.Button(
            header,
            text=" ? ",
            font=("Segoe UI", 10, "bold"),
            bg=_C_TEAL, fg="white",
            relief="flat", cursor="hand2",
            padx=6, pady=2,
            command=self._open_help,
        ).pack(side="right", padx=(0, 6), pady=10)

        # ── Update notification banner (hidden; shown by _show_update_banner) ──
        # Created here so it sits between header and paned in the pack order.
        _BNR_BG   = "#FFF3CD"   # amber warning background
        _BNR_FG   = "#664D03"   # dark amber text
        _BNR_BTN  = "#664D03"   # download button background

        self._frm_banner = tk.Frame(self.root, bg=_BNR_BG, pady=5)
        # Not packed yet — _show_update_banner() positions it via pack(before=).

        tk.Label(
            self._frm_banner,
            text="⚠",
            font=("Segoe UI", 11),
            bg=_BNR_BG, fg=_BNR_FG,
        ).pack(side="left", padx=(10, 4))

        self._lbl_banner_text = tk.Label(
            self._frm_banner,
            text="",
            font=("Segoe UI", 9),
            bg=_BNR_BG, fg=_BNR_FG,
        )
        self._lbl_banner_text.pack(side="left")

        self._btn_banner_dl = tk.Button(
            self._frm_banner,
            text=" Descargar actualización ",
            font=("Segoe UI", 8, "bold"),
            bg=_BNR_BTN, fg="white",
            relief="flat", cursor="hand2",
            padx=6, pady=2,
        )
        self._btn_banner_dl.pack(side="left", padx=(12, 4))

        tk.Button(
            self._frm_banner,
            text=" × ",
            font=("Segoe UI", 11, "bold"),
            bg=_BNR_BG, fg=_BNR_FG,
            activebackground=_BNR_BG, activeforeground="#3D2B02",
            relief="flat", cursor="hand2",
            padx=4, pady=0,
            command=self._dismiss_update_banner,
        ).pack(side="right", padx=(0, 8))

        # ── Resizable PanedWindow: notebook (top) + log (bottom) ─────────
        # tk.PanedWindow gives a visible, draggable sash the user can resize.
        self._paned = tk.PanedWindow(
            self.root,
            orient=tk.VERTICAL,
            sashwidth=7,
            sashpad=1,
            sashrelief="flat",
            bg=_C_BORDER,
            bd=0,
        )
        self._paned.pack(fill="both", expand=True, padx=_PAD, pady=(_PAD, _PAD))

        # Top pane: Notebook
        self.nb = ttk.Notebook(self._paned)
        self._paned.add(self.nb, minsize=280, stretch="always")

        self._tab_connection()
        self._tab_database()
        self._tab_filestore()
        self._tab_destination()
        self._tab_execute()
        self._tab_restore()
        self._tab_addons()
        self._tab_explorer()
        self._tab_terminal()
        self._tab_trial()
        self._tab_automation()

        # Lock backup tabs 2-5 until connected
        for i in range(1, 5):
            self.nb.tab(i, state="disabled")

        # Bottom pane: Log panel
        log_outer = ttk.Frame(self._paned)
        self._paned.add(log_outer, minsize=80, stretch="always")
        self._build_log_panel(log_outer)

        # Restore saved sash position (or default 70/30 split)
        self.root.after(80, self._restore_sash)

        # Auto-connect explorer/terminal panels when the user switches to those tabs
        self.nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # ── Keyboard shortcuts ────────────────────────────────────────────
        # Ctrl+L: clear log;  F5: reload database list
        self.root.bind("<Control-l>", lambda e: self._clear_log())
        self.root.bind("<Control-L>", lambda e: self._clear_log())
        self.root.bind("<F5>",        lambda e: self._action_load_dbs())

    # ── Tab auto-connect ─────────────────────────────────────────────────

    def _on_tab_changed(self, event=None) -> None:
        """Auto-connect explorer/terminal/trial panels when their tab is selected.

        Called by <<NotebookTabChanged>>.  Uses after(100) so the tab frame
        has finished rendering before triggering network I/O.
        """
        try:
            idx = self.nb.index(self.nb.select())
        except Exception:
            return

        if idx == 7:  # Tab 8 — Explorador
            self.root.after(100, self._auto_connect_explorer)
        elif idx == 8:  # Tab 9 — Terminal
            self.root.after(100, self._auto_connect_terminal)
        elif idx == 9:  # Tab Trial
            self.root.after(100, self._auto_refresh_trial)

    def _auto_connect_explorer(self) -> None:
        """Connect explorer panels that are not yet connected, if SSH is active."""
        panel_l = getattr(self, "_panel_l", None)
        panel_r = getattr(self, "_panel_r", None)
        if panel_l and panel_l._browser is None and self._ssh.connected:
            panel_l.connect_and_navigate("/")
        if panel_r and panel_r._browser is None:
            if self._ssh_restore.connected or self._ssh_dest.connected:
                panel_r.connect_and_navigate("/")

    def _auto_connect_terminal(self) -> None:
        """Connect terminal panels that are not yet connected, if SSH is active."""
        term_l = getattr(self, "_term_l", None)
        term_r = getattr(self, "_term_r", None)
        if term_l and not term_l._connected and self._ssh.connected:
            term_l.connect()
        if term_r and not term_r._connected:
            if self._ssh_restore.connected or self._ssh_dest.connected:
                term_r.connect()

    def _auto_refresh_trial(self) -> None:
        """Refresh Trial tab server status label when the tab is selected."""
        fn = getattr(self, "_trial_refresh_srv", None)
        if fn:
            fn()

    # ── Status bar ───────────────────────────────────────────────────────

    def _build_status_bar(self) -> None:
        """Fixed thin bar at the bottom: SSH connection state + operation status."""
        _BAR_BG = "#E0D8DC"
        bar = tk.Frame(self.root, bg=_BAR_BG, height=22, relief="sunken", bd=1)
        bar.pack(side="bottom", fill="x")
        bar.pack_propagate(False)

        # Connection indicator dot
        self._lbl_status_conn = tk.Label(
            bar, text="  ● Desconectado",
            font=("Segoe UI", 8), bg=_BAR_BG, fg="#888888",
        )
        self._lbl_status_conn.pack(side="left", padx=(4, 2))

        tk.Frame(bar, bg=_C_BORDER, width=1).pack(side="left", fill="y", padx=6, pady=3)

        # Current operation state
        self._lbl_status_op = tk.Label(
            bar, text="Listo",
            font=("Segoe UI", 8), bg=_BAR_BG, fg="#666666",
        )
        self._lbl_status_op.pack(side="left")

        # Keyboard shortcut hints (right-aligned)
        tk.Label(
            bar,
            text="Ctrl+L: limpiar log   F5: recargar BDs  ",
            font=("Segoe UI", 7), bg=_BAR_BG, fg="#AAAAAA",
        ).pack(side="right")

    def _set_status_conn(self, text: str, ok: bool = False) -> None:
        """Update the connection indicator in the status bar (GUI thread only)."""
        color = "#2E8B57" if ok else "#888888"
        self._lbl_status_conn.config(text=f"  ● {text}", fg=color)

    def _set_status_op(self, text: str, color: str = "#555555") -> None:
        """Update the operation label in the status bar (GUI thread only)."""
        self._lbl_status_op.config(text=text, fg=color)

    # ── Log panel ─────────────────────────────────────────────────────────

    def _build_log_panel(self, parent: ttk.Frame) -> None:
        """Build the log area with toolbar and color-coded text widget."""
        # ── Toolbar ───────────────────────────────────────────────────────
        toolbar = tk.Frame(parent, bg=_C_BG, pady=2)
        toolbar.pack(fill="x", side="top")

        tk.Label(
            toolbar, text=" Log de operaciones",
            font=("Segoe UI", 9, "bold"),
            bg=_C_BG, fg=_C_PURPLE,
        ).pack(side="left", padx=(4, 0))

        # Auto-scroll toggle: when enabled, new messages auto-scroll to the end
        self._v_autoscroll = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            toolbar, text="Auto-scroll",
            variable=self._v_autoscroll,
        ).pack(side="left", padx=10)

        # Right-side action buttons
        ttk.Button(toolbar, text="Exportar", command=self._export_log).pack(side="right", padx=(2, 6))
        ttk.Button(toolbar, text="Copiar",   command=self._copy_log).pack(side="right", padx=2)
        ttk.Button(toolbar, text="Limpiar",  command=self._clear_log).pack(side="right", padx=2)

        ttk.Separator(parent, orient="horizontal").pack(fill="x", side="top")

        # ── Text widget ───────────────────────────────────────────────────
        self.log_widget = tk.Text(
            parent,
            font=("Consolas", 9),
            bg=_LOG_BG,
            fg=_LOG_FG,
            wrap="word",
            state="disabled",
            cursor="arrow",      # read-only appearance
        )
        log_sb = ttk.Scrollbar(parent, orient="vertical", command=self.log_widget.yview)
        self.log_widget.configure(yscrollcommand=log_sb.set)
        log_sb.pack(side="right", fill="y")
        self.log_widget.pack(fill="both", expand=True)

        # ── Color tags (line-level) ───────────────────────────────────────
        self.log_widget.tag_configure("timestamp", foreground="#6E6466")
        self.log_widget.tag_configure("normal",    foreground=_LOG_FG)
        self.log_widget.tag_configure("error",     foreground="#FF6B6B",
                                                   font=("Consolas", 9, "bold"))
        self.log_widget.tag_configure("success",   foreground="#7FDB7F")
        self.log_widget.tag_configure("warning",   foreground="#FFB347")
        self.log_widget.tag_configure("process",   foreground="#64B4D4")
        # ── Keyword highlight tags (word-level, raised above line tags) ───
        self.log_widget.tag_configure("kw_info",    foreground="#58ADEF",
                                                    font=("Consolas", 9, "bold"))
        self.log_widget.tag_configure("kw_warning", foreground="#FFD700",
                                                    font=("Consolas", 9, "bold"))
        self.log_widget.tag_configure("kw_error",   foreground="#FF4444",
                                                    font=("Consolas", 9, "bold"))
        # Raise keyword tags above all line-level tags so they take visual priority
        self.log_widget.tag_raise("kw_info")
        self.log_widget.tag_raise("kw_warning")
        self.log_widget.tag_raise("kw_error")

    def _clear_log(self) -> None:
        """Clear all content from the log widget."""
        self.log_widget.config(state="normal")
        self.log_widget.delete("1.0", "end")
        self.log_widget.config(state="disabled")

    def _copy_log(self) -> None:
        """Copy the full log content to the system clipboard."""
        content = self.log_widget.get("1.0", "end").strip()
        if not content:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self._set_status_op("Log copiado al portapapeles.", color=_C_TEAL)
        self.root.after(2500, lambda: self._set_status_op("Listo"))

    def _export_log(self) -> None:
        """Save log content to a .txt file chosen by the user."""
        content = self.log_widget.get("1.0", "end").strip()
        if not content:
            messagebox.showinfo(APP_TITLE, "El log esta vacio.")
            return
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
        path = filedialog.asksaveasfilename(
            title="Exportar log",
            defaultextension=".txt",
            initialfile=f"odoo_backup_log_{ts}.txt",
            filetypes=[("Texto", "*.txt"), ("Todos", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
            self._set_status_op(f"Log exportado: {os.path.basename(path)}", color=_C_TEAL)
            self.root.after(3000, lambda: self._set_status_op("Listo"))
        except OSError as exc:
            messagebox.showerror(APP_TITLE, f"Error exportando log:\n{exc}")

    # ── Geometry / sash persistence ───────────────────────────────────────

    def _load_geometry(self) -> bool:
        """
        Restore window size and position from the previous session.

        Returns True if geometry was successfully restored so the caller
        can skip the default centering step.
        """
        try:
            with open(_SETTINGS_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            geo = data.get("geometry", "")
            if geo:
                self.root.geometry(geo)
                return True
        except Exception:
            pass
        return False

    def _save_geometry(self) -> None:
        """Persist window geometry and log-panel sash position to disk."""
        try:
            os.makedirs(os.path.dirname(_SETTINGS_FILE), exist_ok=True)
            sash_y: int | None = None
            try:
                sash_y = self._paned.sash_coord(0)[1]
            except Exception:
                pass
            settings = {
                "geometry": self.root.geometry(),
                "log_sash": sash_y,
            }
            with open(_SETTINGS_FILE, "w", encoding="utf-8") as fh:
                json.dump(settings, fh, indent=2)
        except Exception:
            pass

    def _restore_sash(self) -> None:
        """
        Position the PanedWindow sash after the window is fully rendered.

        Tries to restore the last saved position; falls back to a 70/30
        (notebook/log) default split.
        """
        try:
            with open(_SETTINGS_FILE, encoding="utf-8") as fh:
                data = json.load(fh)
            sash_y = data.get("log_sash")
            if isinstance(sash_y, (int, float)) and sash_y > 0:
                self._paned.sash_place(0, 0, int(sash_y))
                return
        except Exception:
            pass

        # Default: 70% notebook, 30% log
        h = self._paned.winfo_height()
        if h > 10:
            self._paned.sash_place(0, 0, int(h * 0.70))
        else:
            # Window not yet rendered — retry
            self.root.after(100, self._restore_sash)

    # ── Scroll isolation helper ───────────────────────────────────────────

    @staticmethod
    def _bind_mousewheel(canvas: tk.Canvas) -> None:
        """
        Scope mousewheel scrolling to `canvas` only while the pointer is inside it.

        Replaces canvas.bind_all("<MouseWheel>", ...) which would fire on ALL
        canvases simultaneously whenever any scroll event occurs anywhere in the
        window (a known tkinter gotcha when multiple scrollable panels exist).

        Using <Enter>/<Leave> events to install/remove the binding limits the
        handler to the canvas currently under the mouse pointer.
        """
        def _on_scroll(event: tk.Event) -> None:
            canvas.yview_scroll(-1 * (event.delta // 120), "units")

        def _enter(_event: tk.Event) -> None:
            canvas.bind_all("<MouseWheel>", _on_scroll)

        def _leave(_event: tk.Event) -> None:
            canvas.unbind_all("<MouseWheel>")

        canvas.bind("<Enter>", _enter)
        canvas.bind("<Leave>", _leave)

    # ── Help window ──────────────────────────────────────────────────────

    def _open_help(self) -> None:
        """Open (or bring to front) the documentation window."""
        # Reuse existing window instead of opening duplicates
        if hasattr(self, "_help_win") and self._help_win.winfo_exists():
            self._help_win.lift()
            self._help_win.focus_set()
            return
        self._help_win = HelpWindow(self)

    # ── Update banner ────────────────────────────────────────────────────

    def _on_update_check_result(
        self, new_version: str | None, download_url: str | None
    ) -> None:
        """
        Callback invoked from the background update-checker thread.

        Schedules the banner display on the Tkinter main thread so it is
        always safe to call from any thread.
        """
        if new_version:
            self.root.after(
                0, lambda: self._show_update_banner(new_version, download_url or "")
            )

    def _show_update_banner(self, version: str, download_url: str) -> None:
        """
        Reveal the update banner below the header bar.

        Uses pack(before=self._paned) so the banner is always positioned
        between the header and the main content area regardless of when it
        is called.
        """
        self._lbl_banner_text.config(
            text=f"Nueva version {version} disponible — version instalada: {APP_VERSION}"
        )
        self._btn_banner_dl.config(
            command=lambda: self._open_update_download(download_url)
        )
        self._frm_banner.pack(fill="x", before=self._paned)

    def _dismiss_update_banner(self) -> None:
        """Hide the update banner (user dismissed it)."""
        self._frm_banner.pack_forget()

    def _open_update_download(self, url: str) -> None:
        """Open the download URL in the system default browser."""
        import webbrowser
        if url:
            webbrowser.open(url)

    # ── Transfer retry ────────────────────────────────────────────────────

    def _show_retry_panel(self, data: dict) -> None:
        """
        Reveal the retry panel below the execute buttons.

        Called from the queue handler on a 'transfer_failed' event.
        Stores retry state so _action_retry_transfer can re-use it.
        """
        self._retry_remote_tmp  = data.get("remote_tmp", [])
        self._retry_conn_params = data.get("conn_params", {})
        self._retry_dump_path   = data.get("dump_path", "")
        self._retry_inventory   = data.get("inventory")

        error_msg = data.get("error", "")
        if error_msg:
            self._append_log(f"[ERROR] Error durante el traslado: {error_msg}")
            self._set_status_op("✗ Fallo traslado", color="#C0392B")
        messagebox.showerror(
            APP_TITLE,
            f"El traslado fallo:\n\n{error_msg}\n\n"
            "Los archivos del servidor siguen disponibles.\n"
            "Puede reintentar el traslado con el mismo u otro destino."
        )

        files_text = "\n".join(f"  • {f}" for f in self._retry_remote_tmp) or "(ninguno)"
        self._lbl_retry_files.config(text=files_text)

        # Grid the retry panel below the progress/button area (row 7)
        self._frm_retry.grid(
            row=7, column=0, columnspan=2, sticky="ew",
            padx=0, pady=(_PAD, 0),
        )
        # Scroll to show it if the tab is in a scrollable frame
        try:
            self._frm_retry.update_idletasks()
            self.nb.select(4)  # ensure Tab 5 is visible
        except Exception:
            pass

    def _hide_retry_panel(self) -> None:
        """Hide the retry panel and clear stored retry state."""
        self._frm_retry.grid_remove()
        self._retry_remote_tmp  = []
        self._retry_conn_params = {}
        self._retry_dump_path   = ""
        self._retry_inventory   = None

    def _action_retry_transfer(self) -> None:
        """
        Re-run only the transfer step using current Tab 4 destination settings.

        The dump / filestore files already exist on the server — only the transfer
        (and subsequent cleanup + inventory) is repeated.
        """
        if not self._retry_remote_tmp:
            messagebox.showwarning(APP_TITLE, "No hay archivos pendientes de traslado.")
            return

        # Rebuild params: keep connection info from the original run, but override
        # destination with whatever is currently selected in Tab 4.
        p = dict(self._retry_conn_params)
        p["dest_type"] = self._v_dest_type.get()
        p["local_dir"] = self._v_local_dir.get()

        if p["dest_type"] == "remote":
            p["dest_host"] = self._dv["host"].get()
            p["dest_port"] = self._dv["port"].get()
            p["dest_user"] = self._dv["user"].get()
            p["dest_pass"] = self._dv["pass"].get()
            p["dest_dir"]  = self._dv["dir"].get()
        elif p["dest_type"] == "gdrive":
            p["gdrive_creds"]  = self._v_gdrive_creds.get()
            p["gdrive_folder"] = self._v_gdrive_folder.get()

        # Snapshot retry state BEFORE _hide_retry_panel clears it
        remote_tmp    = list(self._retry_remote_tmp)
        dump_path_ret = self._retry_dump_path
        inventory_ret = self._retry_inventory

        self._hide_retry_panel()
        self._btn_run.config(state="disabled")
        self._v_progress.set(0)
        self._lbl_progress.config(text="")
        self._begin_operation()

        threading.Thread(
            target=self._worker_transfer_only,
            args=(p, remote_tmp, dump_path_ret, inventory_ret),
            daemon=True,
        ).start()

    def _action_cleanup_server_retry(self) -> None:
        """Delete the pending server temp files and close the retry panel."""
        if not self._retry_remote_tmp:
            self._hide_retry_panel()
            return
        if not messagebox.askyesno(
            APP_TITLE,
            "Esto eliminará los archivos del servidor sin transferirlos:\n\n"
            + "\n".join(f"  • {f}" for f in self._retry_remote_tmp)
            + "\n\n¿Continuar?",
        ):
            return

        db_mgr = DBManager(self._ssh)
        for path in self._retry_remote_tmp:
            try:
                self._log(f"Limpiando {path} del servidor ...")
                db_mgr.cleanup_remote(path)
            except Exception as exc:
                self._log(f"[aviso] No se pudo limpiar {path}: {exc}")

        self._hide_retry_panel()
        self._log("Archivos del servidor eliminados.")

    def _worker_transfer_only(
        self,
        p: dict,
        remote_tmp: list[str],
        dump_path: str,
        inventory: dict | None,
    ) -> None:
        """
        Background worker that runs only the transfer step (Steps 3-5).

        Used by the retry flow when files are already on the server and only
        the destination needs to be re-attempted.
        """
        n    = max(len(remote_tmp), 1)
        step = [0]

        def advance_fn(label: str) -> None:
            step[0] += 1
            pct = min(int(step[0] / n * 90), 90)
            self._q.put(("progress", (pct, label)))

        try:
            self._exec_transfer_and_finish(p, remote_tmp, dump_path, inventory, advance_fn)

        except RuntimeError as exc:
            if str(exc) == "__CANCELLED__":
                self._q.put(("cancelled", "Traslado detenido por el usuario."))
            else:
                self._q.put(("transfer_failed", {
                    "remote_tmp":  remote_tmp,
                    "dump_path":   dump_path,
                    "inventory":   inventory,
                    "conn_params": p,
                    "error":       str(exc),
                }))
                self._q.put(("btn_enable", None))
        except Exception as exc:
            self._q.put(("transfer_failed", {
                "remote_tmp":  remote_tmp,
                "dump_path":   dump_path,
                "inventory":   inventory,
                "conn_params": p,
                "error":       str(exc),
            }))
            self._q.put(("btn_enable", None))

    # ── Shared helper ────────────────────────────────────────────────────

    def _scrollable_tab(self, tab_text: str) -> ttk.Frame:
        """
        Create a scrollable tab and return its inner content frame.

        Wraps the tab in a Canvas + Scrollbar so the content survives small
        window sizes.  Scroll is isolated to this canvas via _bind_mousewheel
        so sibling canvas tabs are not affected.

        Args:
            tab_text: Label displayed on the notebook tab.

        Returns:
            A ttk.Frame with padding already applied.  Add child widgets to it.
        """
        outer = ttk.Frame(self.nb, padding=0)
        self.nb.add(outer, text=tab_text)

        canvas = tk.Canvas(outer, highlightthickness=0, bg=_C_BG)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        f = ttk.Frame(canvas, padding=_PAD * 2)
        win_id = canvas.create_window((0, 0), window=f, anchor="nw")
        f.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfig(win_id, width=e.width),
        )
        self._bind_mousewheel(canvas)
        return f

    # ── Tab 1: SSH Connections (Servidor A + Servidor B) ─────────────────

    def _tab_connection(self) -> None:
        f = self._scrollable_tab("  1. Conexiones  ")

        # Column 0: labels (fixed); column 1: inputs (expand with window)
        f.columnconfigure(0, minsize=130)
        f.columnconfigure(1, weight=1)

        # ── Servidor A — Emisor / Origen ──────────────────────────────────
        ttk.Label(f, text="Servidor A — Emisor / Origen", font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, _PAD)
        )

        # Saved profiles panel
        pnl_prof = ttk.LabelFrame(f, text="Perfiles guardados", padding=_PAD)
        pnl_prof.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, _PAD))
        pnl_prof.columnconfigure(0, weight=1)

        self._cb_profile = ttk.Combobox(
            pnl_prof, state="readonly",
            values=self._profiles.names(),
        )
        self._cb_profile.grid(row=0, column=0, padx=(0, _PAD), pady=2, sticky="ew")
        self._cb_profile.bind("<<ComboboxSelected>>", lambda e: self._load_profile())

        ttk.Button(
            pnl_prof, text="Guardar",
            command=self._save_profile,
        ).grid(row=0, column=1, padx=2)
        ttk.Button(
            pnl_prof, text="Eliminar",
            command=self._delete_profile,
        ).grid(row=0, column=2, padx=2)

        # Drive config fields inside the profile panel (for automation)
        pnl_prof.columnconfigure(1, weight=1)
        ttk.Separator(pnl_prof, orient="horizontal").grid(
            row=1, column=0, columnspan=3, sticky="ew", pady=(_PAD, 4)
        )
        ttk.Label(pnl_prof, text="Google Drive (automatizacion):",
                  font=("Segoe UI", 8, "bold")).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(0, 2)
        )
        ttk.Label(pnl_prof, text="Credenciales JSON:").grid(
            row=3, column=0, sticky="e", padx=(0, _PAD), pady=2
        )
        _prof_creds_row = ttk.Frame(pnl_prof)
        _prof_creds_row.grid(row=3, column=1, columnspan=2, sticky="ew", pady=2)
        _prof_creds_row.columnconfigure(0, weight=1)
        ttk.Entry(_prof_creds_row, textvariable=self._v_prof_gdrive_creds).grid(
            row=0, column=0, sticky="ew", padx=(0, 4)
        )
        ttk.Button(
            _prof_creds_row, text="...",
            command=lambda: self._v_prof_gdrive_creds.set(
                filedialog.askopenfilename(
                    title="Seleccionar credenciales de Drive",
                    filetypes=[("JSON", "*.json"), ("Todos", "*.*")],
                ) or self._v_prof_gdrive_creds.get()
            ),
            width=3,
        ).grid(row=0, column=1)

        ttk.Label(pnl_prof, text="Carpeta Drive (ID):").grid(
            row=4, column=0, sticky="e", padx=(0, _PAD), pady=2
        )
        ttk.Entry(pnl_prof, textvariable=self._v_prof_gdrive_folder).grid(
            row=4, column=1, columnspan=2, sticky="ew", pady=2
        )

        # Connection fields — Servidor A
        self._cv: dict[str, tk.StringVar] = {
            "host": tk.StringVar(),
            "port": tk.StringVar(value="22"),
            "user": tk.StringVar(value="root"),
            "pass": tk.StringVar(),
        }
        labels_a = [
            ("IP / Hostname:", "host"),
            ("Puerto SSH:", "port"),
            ("Usuario:", "user"),
            ("Contrasena:", "pass"),
        ]
        for row, (lbl, key) in enumerate(labels_a, start=2):
            ttk.Label(f, text=lbl).grid(row=row, column=0, sticky="e", padx=(0, _PAD), pady=5)
            show = "*" if key == "pass" else ""
            ttk.Entry(f, textvariable=self._cv[key], show=show).grid(
                row=row, column=1, sticky="ew", pady=5, padx=(0, _PAD * 2)
            )

        btn_row_a = ttk.Frame(f)
        btn_row_a.grid(row=6, column=0, columnspan=2, pady=_PAD * 2)

        self._btn_connect = ttk.Button(
            btn_row_a, text="Conectar",
            style="Primary.TButton", command=self._action_connect,
        )
        self._btn_connect.pack(side="left", padx=6)

        self._btn_disconnect = ttk.Button(
            btn_row_a, text="Desconectar",
            style="Stop.TButton", state="disabled",
            command=self._action_disconnect,
        )
        self._btn_disconnect.pack(side="left", padx=6)

        self._lbl_conn_status = ttk.Label(f, text="", foreground="gray")
        self._lbl_conn_status.grid(row=7, column=0, columnspan=2)

        # ── Separador ─────────────────────────────────────────────────────
        ttk.Separator(f, orient="horizontal").grid(
            row=8, column=0, columnspan=2, sticky="ew", pady=(_PAD * 2, _PAD)
        )

        # ── Servidor B — Receptor ─────────────────────────────────────────
        ttk.Label(f, text="Servidor B — Receptor", font=("Segoe UI", 11, "bold")).grid(
            row=9, column=0, columnspan=2, sticky="w", pady=(0, _PAD)
        )

        pnl_b_prof = ttk.LabelFrame(f, text="Perfiles guardados", padding=_PAD)
        pnl_b_prof.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(0, _PAD))
        pnl_b_prof.columnconfigure(0, weight=1)

        self._cb_r_profile = ttk.Combobox(
            pnl_b_prof, state="readonly",
            values=self._profiles.names(),
        )
        self._cb_r_profile.grid(row=0, column=0, padx=(0, _PAD), pady=2, sticky="ew")
        self._cb_r_profile.bind("<<ComboboxSelected>>", lambda e: self._load_r_profile())

        ttk.Button(
            pnl_b_prof, text="Guardar",
            command=self._save_r_profile,
        ).grid(row=0, column=1, padx=2)
        ttk.Button(
            pnl_b_prof, text="Eliminar",
            command=self._delete_r_profile,
        ).grid(row=0, column=2, padx=2)

        # Connection fields — Servidor B
        self._r_conn_vars: dict[str, tk.StringVar] = {
            "host": tk.StringVar(),
            "port": tk.StringVar(value="22"),
            "user": tk.StringVar(value="root"),
            "pass": tk.StringVar(),
        }
        labels_b = [
            ("IP / Hostname:", "host"),
            ("Puerto SSH:", "port"),
            ("Usuario:", "user"),
            ("Contrasena:", "pass"),
        ]
        for row, (lbl, key) in enumerate(labels_b, start=11):
            ttk.Label(f, text=lbl).grid(row=row, column=0, sticky="e", padx=(0, _PAD), pady=5)
            show = "*" if key == "pass" else ""
            ttk.Entry(f, textvariable=self._r_conn_vars[key], show=show).grid(
                row=row, column=1, sticky="ew", pady=5, padx=(0, _PAD * 2)
            )

        btn_row_b = ttk.Frame(f)
        btn_row_b.grid(row=15, column=0, columnspan=2, pady=_PAD * 2)

        self._btn_r_connect = ttk.Button(
            btn_row_b, text="Conectar",
            style="Primary.TButton", command=self._action_r_connect,
        )
        self._btn_r_connect.pack(side="left", padx=6)

        self._btn_r_disconnect = ttk.Button(
            btn_row_b, text="Desconectar",
            style="Stop.TButton", state="disabled",
            command=self._action_r_disconnect,
        )
        self._btn_r_disconnect.pack(side="left", padx=6)

        self._lbl_b_conn_status = ttk.Label(f, text="", foreground="gray")
        self._lbl_b_conn_status.grid(row=16, column=0, columnspan=2)

    # ── Tab 2: Database ──────────────────────────────────────────────────

    def _tab_database(self) -> None:
        f = self._scrollable_tab("  2. Base de Datos  ")

        f.columnconfigure(0, minsize=130)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Base de Datos", font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, _PAD)
        )

        ttk.Label(f, text="Base de datos:").grid(row=1, column=0, sticky="e", padx=(0, _PAD), pady=5)
        self._cb_db = ttk.Combobox(f, textvariable=self._v_db, state="readonly")
        self._cb_db.grid(row=1, column=1, sticky="ew", pady=5, padx=(0, _PAD))
        ttk.Button(f, text="Recargar", command=self._action_load_dbs).grid(
            row=1, column=2, padx=_PAD
        )

        ttk.Label(f, text="Formato dump:").grid(row=2, column=0, sticky="e", padx=(0, _PAD), pady=5)
        fmt_f = ttk.Frame(f)
        fmt_f.grid(row=2, column=1, sticky="w", columnspan=2)
        ttk.Radiobutton(
            fmt_f, text=".dump  —  pg_dump -Fc  (recomendado)", variable=self._v_dump_fmt, value="dump"
        ).pack(anchor="w")
        ttk.Radiobutton(
            fmt_f, text=".sql   —  texto plano", variable=self._v_dump_fmt, value="sql"
        ).pack(anchor="w")

        ttk.Button(f, text="Siguiente ->", style="Nav.TButton", command=self._goto_filestore).grid(
            row=8, column=0, columnspan=3, pady=_PAD * 2
        )

    # ── Tab 3: Filestore ─────────────────────────────────────────────────

    def _tab_filestore(self) -> None:
        f = self._scrollable_tab("  3. Filestore  ")

        f.columnconfigure(0, minsize=130)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Filestore", font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, _PAD)
        )

        # Filestore root selection
        ttk.Label(f, text="Ruta raiz:").grid(row=1, column=0, sticky="e", padx=(0, _PAD), pady=5)
        self._cb_fs_root = ttk.Combobox(f, textvariable=self._v_fs_root)
        self._cb_fs_root.grid(row=1, column=1, sticky="ew", pady=5, padx=(0, _PAD))
        ttk.Button(f, text="Buscar en servidor", command=self._action_search_fs).grid(
            row=1, column=2, padx=(0, _PAD)
        )

        # DB subfolder selection
        ttk.Label(f, text="Carpeta de BD:").grid(row=2, column=0, sticky="e", padx=(0, _PAD), pady=5)
        self._cb_fs_db = ttk.Combobox(f, textvariable=self._v_fs_db, state="readonly")
        self._cb_fs_db.grid(row=2, column=1, sticky="ew", pady=5, padx=(0, _PAD))
        ttk.Button(f, text="Cargar carpetas", command=self._action_load_fs_folders).grid(
            row=2, column=2, padx=(0, _PAD)
        )

        # Remote directory browser
        tree_lf = ttk.LabelFrame(f, text="Explorador de directorios (servidor remoto)", padding=4)
        tree_lf.grid(row=3, column=0, columnspan=3, sticky="ew", pady=_PAD)
        tree_lf.columnconfigure(0, weight=1)

        # height=16 rows makes the tree comfortably readable without shrinking
        self._fs_tree = ttk.Treeview(tree_lf, columns=("size",), height=16)
        self._fs_tree.heading("#0", text="Nombre")
        self._fs_tree.heading("size", text="Tamano")
        # "#0" stretches to fill available space; "size" column stays narrow
        self._fs_tree.column("#0", minwidth=300, stretch=True)
        self._fs_tree.column("size", width=110, minwidth=80, anchor="e", stretch=False)
        sb = ttk.Scrollbar(tree_lf, orient="vertical", command=self._fs_tree.yview)
        self._fs_tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._fs_tree.pack(fill="both", expand=True)

        ttk.Button(f, text="Siguiente ->", style="Nav.TButton", command=self._goto_destination).grid(
            row=4, column=0, columnspan=3, pady=_PAD * 2
        )

    # ── Tab 4: Destination ───────────────────────────────────────────────

    def _tab_destination(self) -> None:
        f = self._scrollable_tab("  4. Destino  ")

        # Column 0: labels/radios (fixed), column 1: inputs (expand)
        f.columnconfigure(0, minsize=130)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="Destino del Backup", font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, _PAD)
        )

        ttk.Radiobutton(
            f, text="Esta maquina (local)",
            variable=self._v_dest_type, value="local",
            command=self._toggle_dest_panels,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=4)

        ttk.Radiobutton(
            f, text="Otro servidor remoto",
            variable=self._v_dest_type, value="remote",
            command=self._toggle_dest_panels,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=4)

        ttk.Radiobutton(
            f, text="Google Drive (Service Account)",
            variable=self._v_dest_type, value="gdrive",
            command=self._toggle_dest_panels,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=4)

        # Local panel
        self._pnl_local = ttk.LabelFrame(f, text="Destino local", padding=_PAD)
        self._pnl_local.grid(row=4, column=0, columnspan=3, sticky="ew", pady=_PAD)
        self._pnl_local.columnconfigure(1, weight=1)
        ttk.Label(self._pnl_local, text="Carpeta destino:").grid(row=0, column=0, sticky="e", padx=(0, _PAD))
        ttk.Entry(self._pnl_local, textvariable=self._v_local_dir).grid(row=0, column=1, sticky="ew")
        ttk.Button(self._pnl_local, text="Examinar...", command=self._browse_local).grid(
            row=0, column=2, padx=_PAD
        )

        # Remote destination panel
        self._dv: dict[str, tk.StringVar] = {
            "host": tk.StringVar(),
            "port": tk.StringVar(value="22"),
            "user": tk.StringVar(value="root"),
            "pass": tk.StringVar(),
            "dir": tk.StringVar(value="/opt/backups"),
        }
        self._pnl_remote = ttk.LabelFrame(f, text="Servidor destino", padding=_PAD)
        self._pnl_remote.grid(row=5, column=0, columnspan=3, sticky="ew", pady=_PAD)
        # Column 0: labels (fixed); column 1: inputs (expand)
        self._pnl_remote.columnconfigure(0, minsize=120)
        self._pnl_remote.columnconfigure(1, weight=1)

        # ── Saved profiles for destination server ─────────────────────────
        pnl_d_prof = ttk.LabelFrame(self._pnl_remote, text="Perfiles guardados", padding=_PAD)
        pnl_d_prof.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, _PAD))
        pnl_d_prof.columnconfigure(0, weight=1)

        self._cb_d_profile = ttk.Combobox(
            pnl_d_prof, state="readonly",
            values=self._profiles.names(),
        )
        self._cb_d_profile.grid(row=0, column=0, padx=(0, _PAD), pady=2, sticky="ew")
        self._cb_d_profile.bind("<<ComboboxSelected>>", lambda e: self._load_d_profile())

        ttk.Button(
            pnl_d_prof, text="Guardar",
            command=self._save_d_profile,
        ).grid(row=0, column=1, padx=2)
        ttk.Button(
            pnl_d_prof, text="Eliminar",
            command=self._delete_d_profile,
        ).grid(row=0, column=2, padx=2)

        # ── Destination connection fields ─────────────────────────────────
        dest_labels = [
            ("IP / Hostname:", "host"),
            ("Puerto SSH:", "port"),
            ("Usuario:", "user"),
            ("Contrasena:", "pass"),
            ("Ruta remota:", "dir"),
        ]
        for row, (lbl, key) in enumerate(dest_labels, start=1):
            ttk.Label(self._pnl_remote, text=lbl).grid(row=row, column=0, sticky="e", padx=(0, _PAD), pady=3)
            show = "*" if key == "pass" else ""
            ttk.Entry(self._pnl_remote, textvariable=self._dv[key], show=show).grid(
                row=row, column=1, sticky="ew", pady=3, padx=(0, _PAD)
            )
        self._pnl_remote.grid_remove()  # Hidden by default

        # ── Google Drive destination panel ────────────────────────────────
        self._pnl_gdrive = ttk.LabelFrame(f, text="Google Drive", padding=_PAD)
        self._pnl_gdrive.grid(row=6, column=0, columnspan=3, sticky="ew", pady=_PAD)
        self._pnl_gdrive.columnconfigure(1, weight=1)

        # Info note
        ttk.Label(
            self._pnl_gdrive,
            text=(
                "Autenticacion via Service Account. Descargue el JSON desde\n"
                "Google Cloud Console > IAM > Cuentas de servicio > Claves."
            ),
            foreground="#666666",
            font=("Segoe UI", 8),
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, _PAD))

        # Row 1: Service Account JSON
        ttk.Label(self._pnl_gdrive, text="Archivo JSON:").grid(
            row=1, column=0, sticky="e", padx=(0, _PAD), pady=4
        )
        ttk.Entry(self._pnl_gdrive, textvariable=self._v_gdrive_creds).grid(
            row=1, column=1, sticky="ew", pady=4
        )
        ttk.Button(
            self._pnl_gdrive, text="Examinar...",
            command=self._browse_gdrive_creds,
        ).grid(row=1, column=2, padx=_PAD, pady=4)

        # Row 2: Folder ID
        ttk.Label(self._pnl_gdrive, text="ID de carpeta:").grid(
            row=2, column=0, sticky="e", padx=(0, _PAD), pady=4
        )
        ttk.Entry(self._pnl_gdrive, textvariable=self._v_gdrive_folder).grid(
            row=2, column=1, sticky="ew", pady=4
        )
        ttk.Button(
            self._pnl_gdrive, text="Verificar conexion",
            command=self._verify_gdrive_conn,
        ).grid(row=2, column=2, padx=_PAD, pady=4)

        # Row 3: Help hint for folder ID
        ttk.Label(
            self._pnl_gdrive,
            text="El ID aparece al final de la URL de la carpeta en Drive.",
            foreground="#888888",
            font=("Segoe UI", 8),
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 4))

        self._pnl_gdrive.grid_remove()  # Hidden by default

        ttk.Button(f, text="Siguiente ->", style="Nav.TButton", command=self._goto_execute).grid(
            row=7, column=0, columnspan=3, pady=_PAD * 2
        )

    # ── Tab 5: Execute ───────────────────────────────────────────────────

    def _tab_execute(self) -> None:
        f = self._scrollable_tab("  5. Ejecutar  ")

        ttk.Label(f, text="Resumen y Ejecucion", font=("Segoe UI", 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, _PAD)
        )

        self._summary = tk.Text(
            f, height=6, font=("Consolas", 9), wrap="word",
            state="disabled", bg="#f8f8f8", relief="sunken",
        )
        self._summary.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, _PAD))

        # Execution options
        opt = ttk.Frame(f)
        opt.grid(row=2, column=0, columnspan=2, sticky="w", pady=4)
        self._v_inc_db = tk.BooleanVar(value=True)
        self._v_inc_fs = tk.BooleanVar(value=True)
        self._v_cleanup = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt, text="Incluir dump de BD", variable=self._v_inc_db).pack(side="left", padx=_PAD)
        ttk.Checkbutton(opt, text="Incluir filestore", variable=self._v_inc_fs).pack(side="left", padx=_PAD)
        ttk.Checkbutton(opt, text="Limpiar /tmp/ al terminar", variable=self._v_cleanup).pack(side="left", padx=_PAD)
        ttk.Checkbutton(opt, text="Crear bundle unificado (.tar)", variable=self._v_bundle).pack(side="left", padx=_PAD)

        # Progress
        ttk.Label(f, text="Progreso:").grid(row=3, column=0, sticky="w", pady=(_PAD, 2))
        self._v_progress = tk.DoubleVar()
        self._progressbar = ttk.Progressbar(f, variable=self._v_progress, maximum=100)
        self._progressbar.grid(row=4, column=0, columnspan=2, sticky="ew", pady=4)

        self._lbl_progress = ttk.Label(f, text="")
        self._lbl_progress.grid(row=5, column=0, columnspan=2)

        btn_row = ttk.Frame(f)
        btn_row.grid(row=6, column=0, columnspan=2, pady=_PAD * 2)
        self._btn_run = ttk.Button(
            btn_row, text="Iniciar Backup",
            style="Primary.TButton", command=self._action_start_backup,
        )
        self._btn_run.pack(side="left", padx=6)
        self._btn_stop_backup = ttk.Button(
            btn_row, text="Detener", state="disabled",
            style="Stop.TButton", command=self._action_stop,
        )
        self._btn_stop_backup.pack(side="left", padx=6)

        # ── Retry panel (hidden; shown when a transfer fails after files are ready) ──
        self._frm_retry = tk.LabelFrame(
            f, text="  Reanudar traslado  ",
            font=("Segoe UI", 9, "bold"),
            bg="#FFF3CD", fg="#664D03",
            bd=1, relief="solid", padx=10, pady=8,
        )
        # Not gridded yet — _show_retry_panel() calls grid() when needed.

        tk.Label(
            self._frm_retry,
            text="⚠  El traslado fallo pero los archivos siguen disponibles en el servidor.",
            font=("Segoe UI", 9, "bold"),
            bg="#FFF3CD", fg="#664D03",
            anchor="w",
        ).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))

        tk.Label(
            self._frm_retry,
            text="Archivos listos:",
            font=("Segoe UI", 8),
            bg="#FFF3CD", fg="#664D03",
        ).grid(row=1, column=0, sticky="nw", padx=(0, 6))

        self._lbl_retry_files = tk.Label(
            self._frm_retry,
            text="",
            font=("Consolas", 8),
            bg="#FFF3CD", fg="#3D2B02",
            justify="left", anchor="w",
        )
        self._lbl_retry_files.grid(row=1, column=1, sticky="w")

        tk.Label(
            self._frm_retry,
            text="Cambia el destino en Tab 4 si lo necesitas, luego haz clic en Reintentar.",
            font=("Segoe UI", 8, "italic"),
            bg="#FFF3CD", fg="#664D03",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 4))

        retry_btns = tk.Frame(self._frm_retry, bg="#FFF3CD")
        retry_btns.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 0))

        tk.Button(
            retry_btns,
            text=" ↺  Reintentar traslado ",
            font=("Segoe UI", 9, "bold"),
            bg="#664D03", fg="white",
            activebackground="#3D2B02", activeforeground="white",
            relief="flat", cursor="hand2", padx=8, pady=4,
            command=self._action_retry_transfer,
        ).pack(side="left", padx=(0, 6))

        tk.Button(
            retry_btns,
            text=" ⇆  Ir a Tab 4 (cambiar destino) ",
            font=("Segoe UI", 9),
            bg="#856404", fg="white",
            activebackground="#664D03", activeforeground="white",
            relief="flat", cursor="hand2", padx=8, pady=4,
            command=lambda: self.nb.select(3),
        ).pack(side="left", padx=(0, 6))

        tk.Button(
            retry_btns,
            text=" ✕  Limpiar archivos del servidor ",
            font=("Segoe UI", 9),
            bg="#6c757d", fg="white",
            activebackground="#495057", activeforeground="white",
            relief="flat", cursor="hand2", padx=8, pady=4,
            command=self._action_cleanup_server_retry,
        ).pack(side="left")

        self._frm_retry.columnconfigure(1, weight=1)

        f.columnconfigure(0, weight=1)

    # ── Navigation helpers ────────────────────────────────────────────────

    def _goto_filestore(self) -> None:
        if not self._v_db.get():
            messagebox.showwarning(APP_TITLE, "Seleccione una base de datos primero.")
            return
        self.nb.tab(2, state="normal")
        self.nb.select(2)

    def _goto_destination(self) -> None:
        self.nb.tab(3, state="normal")
        self.nb.select(3)

    def _goto_execute(self) -> None:
        # Warn (non-blocking) if remote dest is chosen but fields are incomplete
        dest = self._v_dest_type.get()
        if dest == "remote":
            missing = [
                label for key, label in [
                    ("host", "IP / Hostname"),
                    ("user", "Usuario"),
                    ("pass", "Contrasena"),
                    ("dir",  "Ruta remota"),
                ]
                if not self._dv[key].get().strip()
            ]
            if missing:
                if not messagebox.askyesno(
                    APP_TITLE,
                    "Faltan campos en la configuracion del servidor destino:\n  • "
                    + "\n  • ".join(missing)
                    + "\n\n¿Continuar de todas formas?"
                ):
                    return
        elif dest == "gdrive":
            missing_gd = []
            if not self._v_gdrive_creds.get().strip():
                missing_gd.append("Archivo JSON de Service Account")
            if not self._v_gdrive_folder.get().strip():
                missing_gd.append("ID de carpeta en Drive")
            if missing_gd:
                if not messagebox.askyesno(
                    APP_TITLE,
                    "Faltan campos para Google Drive:\n  • "
                    + "\n  • ".join(missing_gd)
                    + "\n\n¿Continuar de todas formas?"
                ):
                    return
        self.nb.tab(4, state="normal")
        self.nb.select(4)
        self._refresh_summary()

    def _toggle_dest_panels(self) -> None:
        dest = self._v_dest_type.get()
        self._pnl_local.grid_remove()
        self._pnl_remote.grid_remove()
        self._pnl_gdrive.grid_remove()
        if dest == "local":
            self._pnl_local.grid()
        elif dest == "remote":
            self._pnl_remote.grid()
        else:  # gdrive
            self._pnl_gdrive.grid()

    def _browse_local(self) -> None:
        path = filedialog.askdirectory(title="Seleccionar carpeta destino")
        if path:
            self._v_local_dir.set(path)

    def _browse_gdrive_creds(self) -> None:
        """Open a file picker for the Service Account JSON key file."""
        path = filedialog.askopenfilename(
            title="Seleccionar archivo de credenciales de Service Account",
            filetypes=[("JSON", "*.json"), ("Todos", "*.*")],
        )
        if path:
            self._v_gdrive_creds.set(path)

    def _verify_gdrive_conn(self) -> None:
        """Test Drive credentials and folder access, show result in a dialog."""
        creds = self._v_gdrive_creds.get().strip()
        folder = self._v_gdrive_folder.get().strip()
        if not creds or not folder:
            messagebox.showwarning(
                APP_TITLE,
                "Complete los campos 'Archivo JSON' e 'ID de carpeta' antes de verificar."
            )
            return
        # Run in thread to avoid freezing the GUI
        def _run() -> None:
            uploader = DriveUploader(creds, folder)
            ok, msg = uploader.verify_connection()
            if ok:
                self._q.put(("log", f"[Drive] Conexion verificada. Carpeta: '{msg}'"))
                self.root.after(
                    0,
                    lambda: messagebox.showinfo(
                        APP_TITLE,
                        f"Conexion exitosa.\nCarpeta de destino: \"{msg}\"",
                    ),
                )
            else:
                self._q.put(("log", f"[Drive] Error de conexion: {msg}"))
                self.root.after(
                    0,
                    lambda: messagebox.showerror(
                        APP_TITLE,
                        f"No se pudo conectar a Google Drive:\n\n{msg}",
                    ),
                )

        threading.Thread(target=_run, daemon=True).start()

    def _refresh_summary(self) -> None:
        """Rebuild the summary text from current selections."""
        fs_db = self._v_fs_db.get() or self._v_db.get()
        dest_type = self._v_dest_type.get()
        if dest_type == "local":
            dest_str = f"Local -> {self._v_local_dir.get()}"
        elif dest_type == "remote":
            dest_str = f"Remoto -> {self._dv['user'].get()}@{self._dv['host'].get()}:{self._dv['dir'].get()}"
        else:
            folder = self._v_gdrive_folder.get() or "(sin carpeta)"
            dest_str = f"Google Drive -> carpeta {folder}"

        lines = [
            f"Servidor origen  : {self._ssh.host}:{self._ssh.port}",
            f"Base de datos    : {self._v_db.get()} (formato: {self._v_dump_fmt.get()})",
            f"Filestore        : {self._v_fs_root.get()}/{fs_db}",
            f"Destino          : {dest_str}",
        ]
        self._summary.config(state="normal")
        self._summary.delete("1.0", "end")
        self._summary.insert("end", "\n".join(lines))
        self._summary.config(state="disabled")

    # ── Profile helpers ──────────────────────────────────────────────────

    def _refresh_profile_combos(self) -> None:
        """Sync all profile comboboxes with the current profile list."""
        names = self._profiles.names()
        self._cb_profile["values"]   = names
        self._cb_d_profile["values"] = names
        self._cb_r_profile["values"] = names

    def _load_profile(self) -> None:
        """Fill Tab-1 connection fields from the selected profile."""
        name = self._cb_profile.get()
        if not name:
            return
        p = self._profiles.get(name)
        if p:
            self._cv["host"].set(p["host"])
            self._cv["port"].set(str(p["port"]))
            self._cv["user"].set(p["user"])
            self._cv["pass"].set(p["password"])
            self._v_prof_gdrive_creds.set(p.get("gdrive_creds_path", ""))
            self._v_prof_gdrive_folder.set(p.get("gdrive_folder_id", ""))

    def _save_profile(self) -> None:
        """Save current Tab-1 connection fields as a named profile."""
        host = self._cv["host"].get().strip()
        if not host:
            messagebox.showwarning(APP_TITLE, "Ingrese los datos de conexion primero.")
            return
        default_name = f"{self._cv['user'].get()}@{host}:{self._cv['port'].get()}"
        name = simpledialog.askstring(
            "Guardar perfil",
            "Nombre del perfil:",
            initialvalue=self._cb_profile.get() or default_name,
            parent=self.root,
        )
        if not name:
            return
        try:
            self._profiles.save(
                name=name,
                host=host,
                port=int(self._cv["port"].get() or 22),
                user=self._cv["user"].get(),
                password=self._cv["pass"].get(),
                gdrive_creds_path=self._v_prof_gdrive_creds.get(),
                gdrive_folder_id=self._v_prof_gdrive_folder.get(),
            )
            self._refresh_profile_combos()
            self._cb_profile.set(name)
            messagebox.showinfo(APP_TITLE, f'Perfil "{name}" guardado.')
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _delete_profile(self) -> None:
        """Delete the selected profile from the backup profile combobox."""
        name = self._cb_profile.get()
        if not name:
            messagebox.showwarning(APP_TITLE, "Seleccione un perfil para eliminar.")
            return
        if not messagebox.askyesno(APP_TITLE, f'¿Eliminar el perfil "{name}"?'):
            return
        self._profiles.delete(name)
        self._refresh_profile_combos()
        self._cb_profile.set("")

    def _load_d_profile(self) -> None:
        """Fill Tab-4 destination server fields from the selected profile."""
        name = self._cb_d_profile.get()
        if not name:
            return
        p = self._profiles.get(name)
        if p:
            self._dv["host"].set(p["host"])
            self._dv["port"].set(str(p["port"]))
            self._dv["user"].set(p["user"])
            self._dv["pass"].set(p["password"])
            # Keep 'dir' as-is — it's specific to backup paths, not the server profile

    def _save_d_profile(self) -> None:
        """Save current Tab-4 destination fields as a named profile."""
        host = self._dv["host"].get().strip()
        if not host:
            messagebox.showwarning(APP_TITLE, "Ingrese los datos de conexion primero.")
            return
        default_name = f"{self._dv['user'].get()}@{host}:{self._dv['port'].get()}"
        name = simpledialog.askstring(
            "Guardar perfil",
            "Nombre del perfil:",
            initialvalue=self._cb_d_profile.get() or default_name,
            parent=self.root,
        )
        if not name:
            return
        try:
            self._profiles.save(
                name=name,
                host=host,
                port=int(self._dv["port"].get() or 22),
                user=self._dv["user"].get(),
                password=self._dv["pass"].get(),
            )
            self._refresh_profile_combos()
            self._cb_d_profile.set(name)
            messagebox.showinfo(APP_TITLE, f'Perfil "{name}" guardado.')
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _delete_d_profile(self) -> None:
        """Delete the selected profile from the destination profile combobox."""
        name = self._cb_d_profile.get()
        if not name:
            messagebox.showwarning(APP_TITLE, "Seleccione un perfil para eliminar.")
            return
        if not messagebox.askyesno(APP_TITLE, f'¿Eliminar el perfil "{name}"?'):
            return
        self._profiles.delete(name)
        self._refresh_profile_combos()
        self._cb_d_profile.set("")

    def _load_r_profile(self) -> None:
        """Fill Servidor B connection fields from the selected profile."""
        name = self._cb_r_profile.get()
        if not name:
            return
        p = self._profiles.get(name)
        if p:
            self._r_conn_vars["host"].set(p["host"])
            self._r_conn_vars["port"].set(str(p["port"]))
            self._r_conn_vars["user"].set(p["user"])
            self._r_conn_vars["pass"].set(p["password"])

    def _save_r_profile(self) -> None:
        """Save current restore 'otro servidor' fields as a named profile."""
        host = self._r_conn_vars["host"].get().strip()
        if not host:
            messagebox.showwarning(APP_TITLE, "Ingrese los datos de conexion primero.")
            return
        default_name = (
            f"{self._r_conn_vars['user'].get()}@{host}:"
            f"{self._r_conn_vars['port'].get()}"
        )
        name = simpledialog.askstring(
            "Guardar perfil",
            "Nombre del perfil:",
            initialvalue=self._cb_r_profile.get() or default_name,
            parent=self.root,
        )
        if not name:
            return
        try:
            self._profiles.save(
                name=name,
                host=host,
                port=int(self._r_conn_vars["port"].get() or 22),
                user=self._r_conn_vars["user"].get(),
                password=self._r_conn_vars["pass"].get(),
            )
            self._refresh_profile_combos()
            self._cb_r_profile.set(name)
            messagebox.showinfo(APP_TITLE, f'Perfil "{name}" guardado.')
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _delete_r_profile(self) -> None:
        """Delete the selected profile from the restore profile combobox."""
        name = self._cb_r_profile.get()
        if not name:
            messagebox.showwarning(APP_TITLE, "Seleccione un perfil para eliminar.")
            return
        if not messagebox.askyesno(APP_TITLE, f'¿Eliminar el perfil "{name}"?'):
            return
        self._profiles.delete(name)
        self._refresh_profile_combos()
        self._cb_r_profile.set("")

    # ── Pre-flight validation helpers ────────────────────────────────────

    def _check_ssh_alive(self, ssh: SSHClient, label: str = "origen") -> bool:
        """
        Verify the SSH connection is still responsive before starting an operation.

        Runs a trivial remote command with a short timeout. If it fails the
        user is warned and the caller should abort.

        Args:
            ssh: The SSH client to test.
            label: Human-readable server label for the warning message.

        Returns:
            True if the connection is alive, False otherwise.
        """
        try:
            code, _, _ = ssh.execute("echo ping", timeout=4)
            return code == 0
        except Exception:
            pass

        messagebox.showwarning(
            APP_TITLE,
            f"La conexion SSH con el servidor de {label} no responde.\n\n"
            "Es posible que la sesion haya expirado por inactividad.\n"
            "Por favor, reconectate antes de continuar."
        )
        return False

    def _validate_backup_params(self) -> bool:
        """
        Validate all user inputs required for a backup operation.

        Checks are ordered from most obvious to most specific so the user
        sees the first problem first. Returns True only when everything
        is ready to proceed.
        """
        inc_db = self._v_inc_db.get()
        inc_fs = self._v_inc_fs.get()

        # At least one thing to back up
        if not inc_db and not inc_fs:
            messagebox.showwarning(
                APP_TITLE,
                "Seleccione al menos una opcion para incluir en el backup:\n"
                "  • Dump de base de datos\n"
                "  • Filestore"
            )
            return False

        # DB selection required when dump is enabled
        if inc_db and not self._v_db.get():
            messagebox.showwarning(
                APP_TITLE,
                "Debe seleccionar una base de datos (Tab 2) antes de iniciar el backup."
            )
            return False

        # Filestore configuration required when filestore is enabled
        if inc_fs:
            if not self._v_fs_root.get().strip():
                messagebox.showwarning(
                    APP_TITLE,
                    "Debe configurar la ruta raiz del filestore (Tab 3) "
                    "para poder incluirlo en el backup."
                )
                return False

        dest_type = self._v_dest_type.get()

        # Destination: local
        if dest_type == "local":
            if not self._v_local_dir.get().strip():
                messagebox.showwarning(
                    APP_TITLE, "Seleccione una carpeta destino local (Tab 4)."
                )
                return False

        # Destination: remote server
        elif dest_type == "remote":
            missing = [
                label for key, label in [
                    ("host", "IP / Hostname"),
                    ("user", "Usuario"),
                    ("pass", "Contrasena"),
                    ("dir",  "Ruta remota"),
                ]
                if not self._dv[key].get().strip()
            ]
            if missing:
                messagebox.showwarning(
                    APP_TITLE,
                    "Faltan datos del servidor remoto destino (Tab 4):\n  • "
                    + "\n  • ".join(missing)
                )
                return False

            try:
                int(self._dv["port"].get())
            except ValueError:
                messagebox.showerror(
                    APP_TITLE,
                    "El puerto del servidor destino debe ser un numero entero."
                )
                return False

        # Destination: Google Drive
        else:
            creds  = self._v_gdrive_creds.get().strip()
            folder = self._v_gdrive_folder.get().strip()
            if not creds:
                messagebox.showwarning(
                    APP_TITLE,
                    "Seleccione el archivo JSON de Service Account para Google Drive (Tab 4)."
                )
                return False
            if not os.path.isfile(creds):
                messagebox.showerror(
                    APP_TITLE,
                    f"El archivo de credenciales no existe:\n{creds}\n\n"
                    "Verifique la ruta o seleccione otro archivo."
                )
                return False
            if not folder:
                messagebox.showwarning(
                    APP_TITLE,
                    "Ingrese el ID de la carpeta de Google Drive destino (Tab 4)."
                )
                return False

        return True

    def _validate_restore_params(self) -> bool:
        """
        Validate all user inputs required for a restore operation.

        Returns True only when all fields are present and well-formed.
        """
        # ── Database name ─────────────────────────────────────────────────
        db_name = self._v_r_db_name.get().strip()
        if not db_name:
            messagebox.showwarning(APP_TITLE, "Ingrese el nombre de la nueva base de datos.")
            return False

        if not _DB_NAME_RE.match(db_name):
            messagebox.showerror(
                APP_TITLE,
                f'Nombre de base de datos invalido: "{db_name}"\n\n'
                "Reglas PostgreSQL:\n"
                "  • Solo letras, numeros y guiones bajos (_)\n"
                "  • No puede empezar con un numero\n"
                "  • Sin espacios ni caracteres especiales"
            )
            return False

        if len(db_name) > 63:
            messagebox.showerror(
                APP_TITLE,
                f"El nombre de la BD supera los 63 caracteres permitidos "
                f"por PostgreSQL (actual: {len(db_name)})."
            )
            return False

        # ── Dump source ───────────────────────────────────────────────────
        dump_src = self._v_r_dump_src.get()
        if dump_src == "local":
            path = self._v_r_dump_local.get().strip()
            if not path:
                messagebox.showwarning(
                    APP_TITLE, "Seleccione el archivo de dump local a restaurar."
                )
                return False
            if not os.path.isfile(path):
                messagebox.showerror(
                    APP_TITLE,
                    f"El archivo de dump no existe en la ruta indicada:\n{path}"
                )
                return False
        elif dump_src == "server":
            if not self._v_r_dump_srv.get().strip():
                messagebox.showwarning(
                    APP_TITLE, "Ingrese la ruta del archivo de dump en el servidor."
                )
                return False

        # ── Filestore source ──────────────────────────────────────────────
        fs_src = self._v_r_fs_src.get()
        if fs_src == "local":
            path = self._v_r_fs_local.get().strip()
            if not path:
                messagebox.showwarning(
                    APP_TITLE, "Seleccione el archivo ZIP del filestore a restaurar."
                )
                return False
            if not os.path.isfile(path):
                messagebox.showerror(
                    APP_TITLE,
                    f"El archivo ZIP del filestore no existe en la ruta indicada:\n{path}"
                )
                return False
        elif fs_src == "server":
            if not self._v_r_fs_srv.get().strip():
                messagebox.showwarning(
                    APP_TITLE, "Ingrese la ruta del ZIP del filestore en el servidor."
                )
                return False

        # Filestore destination root required unless skipping filestore
        if fs_src != "none" and not self._v_r_fs_root.get().strip():
            messagebox.showwarning(
                APP_TITLE,
                "Ingrese la ruta raiz del filestore en el servidor destino."
            )
            return False

        # ── Workers (parallel jobs) ───────────────────────────────────────
        jobs_str = self._v_r_jobs.get().strip()
        try:
            jobs = int(jobs_str)
            if jobs < 1 or jobs > 16:
                messagebox.showwarning(
                    APP_TITLE,
                    f"El numero de workers debe estar entre 1 y 16 (actual: {jobs})."
                )
                return False
        except ValueError:
            messagebox.showerror(
                APP_TITLE,
                f'Numero de workers invalido: "{jobs_str}".\n'
                "Ingrese un numero entero (ejemplo: 4)."
            )
            return False

        return True

    # ── Actions (start background threads) ───────────────────────────────

    def _action_connect(self) -> None:
        host = self._cv["host"].get().strip()
        port_s = self._cv["port"].get().strip()
        user = self._cv["user"].get().strip()
        pwd = self._cv["pass"].get()

        if not all([host, port_s, user, pwd]):
            messagebox.showwarning(APP_TITLE, "Complete todos los campos de conexion.")
            return
        try:
            port = int(port_s)
        except ValueError:
            messagebox.showerror(APP_TITLE, "El puerto debe ser un numero entero.")
            return

        self._btn_connect.config(state="disabled")
        self._lbl_conn_status.config(text="Conectando...", foreground="gray")

        def _run():
            try:
                if self._ssh.connected:
                    self._ssh.close()
                self._ssh.connect(host, port, user, pwd)
                self._q.put(("conn_ok", f"Conectado a {host}:{port}"))
            except ConnectionError as exc:
                self._q.put(("conn_fail", str(exc)))

        threading.Thread(target=_run, daemon=True).start()

    def _action_disconnect(self) -> None:
        """Close the backup origin SSH connection and reset Tab 1 state."""
        self._ssh.close()
        self._lbl_conn_status.config(text="  Desconectado", foreground="gray")
        self._btn_connect.config(state="normal")
        self._btn_disconnect.config(state="disabled")
        # Lock backup tabs 2-5 — they depend on this connection
        for i in range(1, 5):
            self.nb.tab(i, state="disabled")
        self.nb.select(0)
        self._set_status_conn("Desconectado", ok=False)
        self._append_log("Desconectado del servidor de origen.")

    def _action_r_disconnect(self) -> None:
        """Close the Servidor B (Receptor) SSH connection and reset its state in Tab 1."""
        self._ssh_restore.close()
        self._lbl_b_conn_status.config(text="  Desconectado", foreground="gray")
        self._btn_r_connect.config(state="normal")
        self._btn_r_disconnect.config(state="disabled")
        # Refresh Tab 6 status label if it's showing Servidor B
        if self._v_r_conn_type.get() == "receptor":
            self._toggle_restore_conn()
        self._append_log("Desconectado del Servidor B (Receptor).")

    def _action_load_dbs(self) -> None:
        def _run():
            try:
                dbs = DBManager(self._ssh).list_databases()
                self._q.put(("db_list", dbs))
            except Exception as exc:
                self._q.put(("error", f"Error cargando bases de datos: {exc}"))

        threading.Thread(target=_run, daemon=True).start()

    def _action_search_fs(self) -> None:
        self._log("Buscando rutas de filestore en el servidor ...")

        def _run():
            try:
                paths = FilestoreManager(self._ssh).find_filestore_roots()
                self._q.put(("fs_roots", paths))
            except Exception as exc:
                self._q.put(("error", f"Error buscando filestore: {exc}"))

        threading.Thread(target=_run, daemon=True).start()

    def _action_load_fs_folders(self) -> None:
        root = self._v_fs_root.get().strip()
        if not root:
            messagebox.showwarning(APP_TITLE, "Ingrese o seleccione una ruta de filestore.")
            return

        def _run():
            try:
                mgr = FilestoreManager(self._ssh)
                folders = mgr.list_db_folders(root)
                self._q.put(("fs_folders", folders))
                entries = mgr.browse_directory(root)
                self._q.put(("fs_tree", entries))
            except Exception as exc:
                self._q.put(("error", f"Error cargando carpetas: {exc}"))

        threading.Thread(target=_run, daemon=True).start()

    def _action_start_backup(self) -> None:
        # ── Pre-flight checks (run in GUI thread before disabling anything) ──
        if not self._validate_backup_params():
            return
        if not self._check_ssh_alive(self._ssh, "origen"):
            return

        self._btn_run.config(state="disabled")
        self._v_progress.set(0)
        self._lbl_progress.config(text="")
        self._begin_operation()

        # Snapshot all parameters before entering the thread
        params = {
            "db": self._v_db.get(),
            "fmt": self._v_dump_fmt.get(),
            "fs_root": self._v_fs_root.get(),
            "fs_db": self._v_fs_db.get() or self._v_db.get(),
            "dest_type": self._v_dest_type.get(),
            "local_dir": self._v_local_dir.get(),
            "inc_db": self._v_inc_db.get(),
            "inc_fs": self._v_inc_fs.get(),
            "cleanup": self._v_cleanup.get(),
            "bundle": self._v_bundle.get(),
        }
        if params["dest_type"] == "remote":
            params["dest_host"] = self._dv["host"].get()
            params["dest_port"] = self._dv["port"].get()
            params["dest_user"] = self._dv["user"].get()
            params["dest_pass"] = self._dv["pass"].get()
            params["dest_dir"] = self._dv["dir"].get()
        elif params["dest_type"] == "gdrive":
            params["gdrive_creds"]  = self._v_gdrive_creds.get()
            params["gdrive_folder"] = self._v_gdrive_folder.get()

        threading.Thread(target=self._worker_backup, args=(params,), daemon=True).start()

    # ── Background worker ─────────────────────────────────────────────────

    def _worker_backup(self, p: dict) -> None:
        """Orchestrates DB dump, filestore compression and file transfer."""
        db_mgr = DBManager(self._ssh)
        fs_mgr = FilestoreManager(self._ssh)
        transfer = TransferManager(self._ssh)

        # Determine how many major steps so we can drive the progress bar
        # +1 for inventory collection at the start
        major_steps = sum([p["inc_db"], p["inc_fs"]]) * 2 + 1
        step = 0

        def advance(label: str) -> None:
            nonlocal step
            step += 1
            pct = min((step / max(major_steps, 1)) * 100, 99)
            self._q.put(("progress", (pct, label)))

        remote_tmp: list[str] = []
        inventory: dict | None = None
        final_dump_fname: str = ""   # resolved after overwrite dialog
        dump_path: str = ""          # set only when inc_db=True

        # ── Phase A: create files on the server (not retryable) ──────────
        try:
            # ── 0. Collect inventory BEFORE dump starts ───────────────────
            # Read-only queries — safe while Odoo is still running.
            # Captures the live state that the dump will preserve.
            try:
                fs_path = (
                    f"{p['fs_root']}/{p['fs_db']}"
                    if p["inc_fs"] and p.get("fs_root")
                    else None
                )
                inv_mgr = InventoryManager(self._ssh)
                inventory = inv_mgr.collect(
                    db_name=p["db"],
                    filestore_path=fs_path,
                    source_host=self._ssh.host or "",
                    log_callback=self._log,
                )
            except Exception as exc:
                # Non-fatal: backup continues without inventory
                self._log(f"[aviso] No se pudo recopilar inventario: {exc}")
                inventory = None
            advance("Inventario recopilado.")

            # ── 1. DB dump
            if p["inc_db"]:
                default_dump = db_mgr.default_dump_path(p["db"], p["fmt"])
                dump_fname   = os.path.basename(default_dump)

                if db_mgr.remote_file_exists(default_dump):
                    action, dump_fname = self._ask_overwrite(
                        dump_fname, f"{self._ssh.host}:/tmp"
                    )
                    if action == "cancel":
                        self._log("Backup cancelado por el usuario.")
                        self._q.put(("btn_enable", None))
                        return

                dump_path = db_mgr.create_dump(
                    p["db"], p["fmt"],
                    remote_path=f"/tmp/{dump_fname}",
                    log_callback=self._log,
                    cancel_event=self._cancel_event,
                )
                remote_tmp.append(dump_path)
                final_dump_fname = dump_fname   # track resolved filename for inventory naming
                advance(f"Dump listo: {dump_path}")

            # ── 2. Filestore compression
            if p["inc_fs"] and p["fs_root"]:
                default_zip = fs_mgr.default_zip_path(p["fs_db"])
                zip_fname   = os.path.basename(default_zip)

                if fs_mgr.remote_file_exists(default_zip):
                    action, zip_fname = self._ask_overwrite(
                        zip_fname, f"{self._ssh.host}:/tmp"
                    )
                    if action == "cancel":
                        self._log("Backup cancelado por el usuario.")
                        self._q.put(("btn_enable", None))
                        return

                fs_path = fs_mgr.compress_filestore(
                    p["fs_root"], p["fs_db"],
                    remote_zip=f"/tmp/{zip_fname}",
                    log_callback=self._log,
                    cancel_event=self._cancel_event,
                )
                remote_tmp.append(fs_path)
                advance(f"Filestore comprimido: {fs_path}")

        except RuntimeError as exc:
            if str(exc) == "__CANCELLED__":
                self._q.put(("cancelled", "Backup detenido por el usuario."))
            else:
                self._q.put(("error", f"Error creando backup: {exc}"))
                self._q.put(("btn_enable", None))
            return
        except Exception as exc:
            self._q.put(("error", f"Error creando backup: {exc}"))
            self._q.put(("btn_enable", None))
            return

        # ── Bundle step: pack dump + filestore + inventory into one .tar ──
        # Only when bundle=True and there is at least one file to pack.
        if p.get("bundle", True) and remote_tmp:
            try:
                bm = BundleManager(self._ssh)
                ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
                bundle_name = BundleManager.bundle_name_for(p["db"], ts)
                bundle_path_remote = f"/tmp/{bundle_name}"

                # Write inventory JSON to server so it travels inside the bundle
                inv_remote_path = None
                if inventory:
                    inv_json_name   = f"{p['db']}_{ts}_inventory.json"
                    inv_remote_path = f"/tmp/{inv_json_name}"
                    bm.write_json_to_server(inventory, inv_remote_path)
                    self._log(f"Inventario escrito en servidor: {inv_remote_path}")

                all_files = remote_tmp + ([inv_remote_path] if inv_remote_path else [])
                bm.create(bundle_path_remote, all_files, log_callback=self._log)

                # Remove individual files — they are now inside the .tar
                db_mgr_cleanup = DBManager(self._ssh)
                for f in all_files:
                    db_mgr_cleanup.cleanup_remote(f)

                # Replace the file list with just the single bundle
                remote_tmp = [bundle_path_remote]
                dump_path  = bundle_path_remote   # used for inventory naming in _exec_transfer_and_finish
                inventory  = None                 # already inside the bundle — skip Step 5

            except Exception as exc_bundle:
                # Non-fatal: fall back to transferring individual files
                self._log(f"[aviso] No se pudo crear el bundle, se transferiran archivos individuales: {exc_bundle}")

        # ── Phase B: transfer (retryable — files are on the server) ─────
        # Any error here shows the retry panel so the user can change the
        # destination and re-run the transfer without recreating the dump/zip.
        try:
            self._exec_transfer_and_finish(p, remote_tmp, dump_path, inventory, advance)

        except RuntimeError as exc:
            if str(exc) == "__CANCELLED__":
                self._q.put(("cancelled", "Backup detenido por el usuario."))
            else:
                self._q.put(("transfer_failed", {
                    "remote_tmp": list(remote_tmp),
                    "dump_path":  dump_path,
                    "inventory":  inventory,
                    "conn_params": p,
                    "error": str(exc),
                }))
                self._q.put(("btn_enable", None))
        except Exception as exc:
            self._q.put(("transfer_failed", {
                "remote_tmp":  list(remote_tmp),
                "dump_path":   dump_path,
                "inventory":   inventory,
                "conn_params": p,
                "error":       str(exc),
            }))
            self._q.put(("btn_enable", None))

    def _exec_transfer_and_finish(
        self,
        p: dict,
        remote_tmp: list[str],
        dump_path: str,
        inventory: dict | None,
        advance_fn,
    ) -> None:
        """
        Steps 3-5: transfer files to destination, server cleanup, save inventory.

        Raises RuntimeError / Exception on failure — callers wrap this in try/except
        and decide whether to show the retry panel or report a fatal error.

        Args:
            p:           Full backup params dict (dest_type, connection info, etc.).
            remote_tmp:  List of absolute paths on the source server ready to transfer.
            dump_path:   Path of the DB dump on the server (used to name the inventory).
            inventory:   Inventory dict collected before the dump, or None.
            advance_fn:  Callable(label) that increments the progress bar step counter.
        """
        db_mgr   = DBManager(self._ssh)
        transfer = TransferManager(self._ssh)

        # ── 3. Transfer each file ─────────────────────────────────────────
        final_dump_fname: str = ""
        gdrive_uploader = (
            DriveUploader(p["gdrive_creds"], p["gdrive_folder"])
            if p["dest_type"] == "gdrive"
            else None
        )

        for remote_file in remote_tmp:
            fname = os.path.basename(remote_file)

            if p["dest_type"] == "local":
                if transfer.local_file_exists(p["local_dir"], fname):
                    action, fname = self._ask_overwrite(fname, p["local_dir"])
                    if action == "cancel":
                        self._log("Backup cancelado por el usuario.")
                        self._q.put(("btn_enable", None))
                        return

                def _prog(transferred: int, total: int, _f: str = fname) -> None:
                    pct = (transferred / total * 100) if total else 0
                    self._q.put(("progress", (pct, f"Descargando {_f} ... {pct:.0f}%")))

                transfer.download_to_local(
                    remote_file, p["local_dir"],
                    dest_filename=fname,
                    progress_callback=_prog,
                    log_callback=self._log,
                )

            elif p["dest_type"] == "remote":
                if transfer.remote_file_exists(
                    p["dest_host"], int(p["dest_port"]),
                    p["dest_user"], p["dest_pass"],
                    p["dest_dir"], fname,
                ):
                    action, fname = self._ask_overwrite(
                        fname, f"{p['dest_host']}:{p['dest_dir']}"
                    )
                    if action == "cancel":
                        self._log("Backup cancelado por el usuario.")
                        self._q.put(("btn_enable", None))
                        return

                transfer.transfer_to_server(
                    remote_file,
                    p["dest_host"],
                    int(p["dest_port"]),
                    p["dest_user"],
                    p["dest_pass"],
                    p["dest_dir"],
                    dest_filename=fname,
                    log_callback=self._log,
                )

            else:
                # ── Google Drive: stream directly SFTP → Drive (no local disk) ──
                total_size = transfer.get_remote_file_size(remote_file)
                sftp_session, sftp_file = transfer.open_remote_file(remote_file)
                try:
                    def _prog_up(uploaded: int, total: int, _f: str = fname) -> None:
                        pct = (uploaded / total * 100) if total else 0
                        self._q.put(("progress", (pct, f"Drive streaming {_f} ... {pct:.0f}%")))

                    gdrive_uploader.upload_stream(
                        sftp_file,
                        filename=fname,
                        total_size=total_size,
                        progress_callback=_prog_up,
                        log_callback=self._log,
                    )
                finally:
                    try:
                        sftp_file.close()
                    except Exception:
                        pass
                    try:
                        sftp_session.close()
                    except Exception:
                        pass

            if remote_file == dump_path:
                final_dump_fname = fname

            advance_fn(f"Transferido: {fname}")

        # ── 4. Remote cleanup ─────────────────────────────────────────────
        if p.get("cleanup", True):
            for remote_file in remote_tmp:
                self._log(f"Limpiando {remote_file} del servidor ...")
                db_mgr.cleanup_remote(remote_file)

        # ── 5. Save inventory ─────────────────────────────────────────────
        if inventory and final_dump_fname:
            inv_base     = os.path.splitext(final_dump_fname)[0]
            inv_filename = f"{inv_base}_inventory.json"
            try:
                if p["dest_type"] == "local":
                    inv_local = os.path.join(p["local_dir"], inv_filename)
                    InventoryManager.save(inventory, inv_local)
                    self._log(f"Inventario guardado: {inv_local}")
                elif p["dest_type"] == "gdrive":
                    inv_local = os.path.join(
                        InventoryManager.local_inventory_dir(), inv_filename
                    )
                    InventoryManager.save(inventory, inv_local)
                    self._log(f"Inventario guardado localmente: {inv_local}")
                    try:
                        gdrive_uploader.upload_file(
                            inv_local,
                            dest_filename=inv_filename,
                            log_callback=self._log,
                        )
                    except Exception as exc_inv:
                        self._log(f"[aviso] No se pudo subir el inventario a Drive: {exc_inv}")
                else:
                    inv_local = os.path.join(
                        InventoryManager.local_inventory_dir(), inv_filename
                    )
                    InventoryManager.save(inventory, inv_local)
                    self._log(f"Inventario guardado localmente: {inv_local}")
            except Exception as exc:
                self._log(f"[aviso] No se pudo guardar el inventario: {exc}")

        self._q.put(("done", "Backup completado exitosamente."))

    def _action_stop(self) -> None:
        """Signal the running backup, restore, or addons-sync to stop."""
        self._cancel_event.set()
        self._btn_stop_backup.config(state="disabled")
        self._btn_stop_restore.config(state="disabled")
        self._btn_stop_addons.config(state="disabled")
        self._q.put(("log", "Deteniendo... espere mientras se cancela el proceso en el servidor."))

    def _begin_operation(self) -> None:
        """Called at the start of any operation: reset cancel flag, toggle buttons."""
        self._cancel_event.clear()
        self._btn_stop_backup.config(state="normal")
        self._btn_stop_restore.config(state="normal")
        self._btn_stop_addons.config(state="normal")
        self._set_status_op("En curso...", color=_C_PURPLE)

    def _end_operation(self) -> None:
        """Called when any operation ends (success, error or cancel)."""
        self._btn_stop_backup.config(state="disabled")
        self._btn_stop_restore.config(state="disabled")
        self._btn_stop_addons.config(state="disabled")

    # ── Overwrite dialog (called from worker thread) ──────────────────────

    def _ask_overwrite(self, filename: str, dest_desc: str) -> tuple[str, str]:
        """
        Ask the user what to do when a destination file already exists.

        Blocks the calling worker thread until the user responds.
        Must be called from a background thread (never from the GUI thread).

        Returns:
            (action, final_filename)
            action: 'rename_ts' | 'rename_custom' | 'overwrite' | 'cancel'
        """
        event = threading.Event()
        result: dict = {}
        self._q.put(("ask_overwrite", (filename, dest_desc, event, result)))
        event.wait()
        return result.get("action", "cancel"), result.get("filename", filename)

    # ── Thread-safe GUI updates ───────────────────────────────────────────

    def _log(self, msg: str) -> None:
        """Queue a log message from any thread."""
        self._q.put(("log", msg))

    def _poll_queue(self) -> None:
        """Drain the message queue and update the GUI (called every 100 ms)."""
        try:
            while True:
                event, data = self._q.get_nowait()

                if event == "log":
                    self._append_log(data)

                elif event == "conn_ok":
                    self._lbl_conn_status.config(text=f"  {data}", foreground="green")
                    self._btn_connect.config(state="normal")
                    self._btn_disconnect.config(state="normal")
                    self._set_status_conn(data.replace("Conectado a ", ""), ok=True)
                    self._append_log(data)
                    self.nb.tab(1, state="normal")
                    self.nb.select(1)
                    self._action_load_dbs()
                    # If explorer/terminal tab is already visible, auto-connect left panel
                    try:
                        cur = self.nb.index(self.nb.select())
                        if cur == 7:
                            self.root.after(200, self._auto_connect_explorer)
                        elif cur == 8:
                            self.root.after(200, self._auto_connect_terminal)
                    except Exception:
                        pass

                elif event == "conn_fail":
                    self._lbl_conn_status.config(text=f"  {data}", foreground="red")
                    self._btn_connect.config(state="normal")
                    self._btn_disconnect.config(state="disabled")
                    self._set_status_conn("Conexion fallida", ok=False)
                    self._append_log(f"Error de conexion: {data}")

                elif event == "db_list":
                    self._cb_db["values"] = data
                    if data:
                        self._cb_db.current(0)
                    self._append_log(f"Bases de datos disponibles: {', '.join(data)}")

                elif event == "fs_roots":
                    self._cb_fs_root["values"] = data
                    if data:
                        self._v_fs_root.set(data[0])
                        self._append_log(f"Filestore detectado en: {data[0]}")
                    else:
                        self._append_log("No se encontraron rutas de filestore conocidas. Ingresela manualmente.")

                elif event == "fs_folders":
                    self._cb_fs_db["values"] = data
                    db = self._v_db.get()
                    if db in data:
                        self._v_fs_db.set(db)
                    elif data:
                        self._cb_fs_db.current(0)

                elif event == "fs_tree":
                    self._fs_tree.delete(*self._fs_tree.get_children())
                    for entry in data:
                        icon = "[DIR]" if entry["type"] == "d" else "[FILE]"
                        self._fs_tree.insert(
                            "", "end",
                            text=f"{icon}  {entry['name']}",
                            values=(entry["size"],),
                        )

                elif event == "progress":
                    pct, label = data
                    self._v_progress.set(pct)
                    self._lbl_progress.config(text=label)

                elif event == "cancelled":
                    self._v_progress.set(0)
                    self._lbl_progress.config(text=f"  {data}")
                    self._append_log(f"DETENIDO: {data}")
                    self._btn_run.config(state="normal")
                    self._set_status_op("Detenido", color="#888888")
                    self._end_operation()
                    messagebox.showwarning(APP_TITLE, data)

                elif event == "done":
                    self._v_progress.set(100)
                    self._lbl_progress.config(text=f"  {data}")
                    self._append_log(f"COMPLETADO: {data}")
                    self._btn_run.config(state="normal")
                    self._set_status_op("✓ Completado", color="#2E8B57")
                    self._end_operation()
                    messagebox.showinfo(APP_TITLE, data)

                elif event == "error":
                    self._append_log(f"[ERROR] {data}")
                    self._set_status_op("✗ Error (ver log)", color="#C0392B")
                    messagebox.showerror(APP_TITLE, data)

                elif event == "btn_enable":
                    self._btn_run.config(state="normal")

                elif event == "transfer_failed":
                    self._show_retry_panel(data)

                elif event == "ask_overwrite":
                    filename, dest_desc, ev, result = data
                    action, final_name = _OverwriteDialog(self.root, filename, dest_desc).show()
                    result["action"] = action
                    result["filename"] = final_name
                    ev.set()   # unblock the worker thread

                # ── Restore events ────────────────────────────────────────
                elif event == "r_conn_ok":
                    self._lbl_b_conn_status.config(text=f"  {data}", foreground="green")
                    self._btn_r_connect.config(state="normal")
                    self._btn_r_disconnect.config(state="normal")
                    # Refresh Tab 6 label if it shows Servidor B
                    if self._v_r_conn_type.get() == "receptor":
                        self._toggle_restore_conn()
                    self._append_log(data)
                    # If explorer/terminal tab is already visible, auto-connect right panel
                    try:
                        cur = self.nb.index(self.nb.select())
                        if cur == 7:
                            self.root.after(200, self._auto_connect_explorer)
                        elif cur == 8:
                            self.root.after(200, self._auto_connect_terminal)
                    except Exception:
                        pass

                elif event == "r_conn_fail":
                    self._lbl_b_conn_status.config(text=f"  {data}", foreground="red")
                    self._btn_r_connect.config(state="normal")
                    self._btn_r_disconnect.config(state="disabled")
                    self._append_log(f"Error conexion Servidor B: {data}")

                elif event == "r_fs_roots":
                    self._r_fs_root_combo["values"] = data
                    if data:
                        self._v_r_fs_root.set(data[0])
                        self._append_log(f"Filestore destino detectado en: {data[0]}")
                    else:
                        self._append_log("No se detectaron rutas de filestore en el destino. Ingresela manualmente.")

                elif event == "r_progress":
                    pct, label = data
                    self._v_r_progress.set(pct)
                    self._lbl_r_progress.config(text=label)

                elif event == "r_cancelled":
                    self._v_r_progress.set(0)
                    self._lbl_r_progress.config(text=f"  {data}")
                    self._append_log(f"DETENIDO: {data}")
                    self._btn_restore.config(state="normal")
                    self._set_status_op("Detenido", color="#888888")
                    self._end_operation()
                    messagebox.showwarning(APP_TITLE, data)

                elif event == "r_done":
                    self._v_r_progress.set(100)
                    self._lbl_r_progress.config(text=f"  {data}")
                    self._append_log(f"COMPLETADO: {data}")
                    self._btn_restore.config(state="normal")
                    self._set_status_op("✓ Completado", color="#2E8B57")
                    self._end_operation()
                    messagebox.showinfo(APP_TITLE, data)

                elif event == "btn_r_enable":
                    self._btn_restore.config(state="normal")

                elif event == "r_check_failed":
                    # Restore finished but post-restore checks found blocking issues
                    d = data
                    db  = d["db"]
                    err_text  = d["errors"]
                    warn_text = d["warnings"]

                    # Mark progress as 100% — the files ARE there, the issues need attention
                    self._v_r_progress.set(100)
                    self._lbl_r_progress.config(
                        text=f"  Restauracion completada con errores — revise el log"
                    )
                    self._btn_restore.config(state="normal")
                    self._set_status_op("⚠ Verificacion fallida — ver log", color="#E67E22")
                    self._end_operation()

                    body = f"La restauracion de '{db}' finalizo pero las verificaciones detectaron problemas.\n\n"
                    if err_text:
                        body += f"ERRORES CRITICOS (explican por que no se puede iniciar sesion):\n{err_text}\n\n"
                    if warn_text:
                        body += f"AVISOS:\n{warn_text}\n\n"
                    body += "Consulte el log completo para el detalle de cada verificacion."

                    self._append_log(f"[ERROR] Verificacion post-restauracion fallo para '{db}'.")
                    messagebox.showerror(APP_TITLE, body)

                # ── Addons sync events ────────────────────────────────────
                elif event == "addons_progress":
                    pct, label = data
                    self._v_a_progress.set(pct)
                    self._lbl_a_progress.config(text=label)

                elif event == "addons_server_keys":
                    # Result of scanning server's ~/.ssh/ for private keys
                    keys: list[str] = data
                    self._btn_scan_server.config(state="normal")
                    if keys:
                        self._cb_a_server_key["values"] = keys
                        if not self._v_a_server_key.get():
                            self._v_a_server_key.set(keys[0])
                        self._lbl_a_server_key_hint.config(
                            text=f"  {len(keys)} llave(s) encontrada(s) en el servidor.",
                            foreground="#2E8B57",
                        )
                        self._append_log(
                            f"Llaves del servidor escaneadas: {len(keys)} encontrada(s)."
                        )
                    else:
                        self._cb_a_server_key["values"] = []
                        self._lbl_a_server_key_hint.config(
                            text="  No se encontraron llaves en ~/.ssh/ del servidor."
                            "  Use el modo 'Llave de esta maquina'.",
                            foreground="#CC4444",
                        )
                        self._append_log(
                            "No se encontraron llaves SSH en el servidor."
                        )

                elif event == "addons_service":
                    # Auto-detected service name from the remote server
                    self._v_a_service.set(data)
                    self._append_log(f"Servicio Odoo detectado: {data}")

                elif event == "addons_done":
                    self._v_a_progress.set(100)
                    self._lbl_a_progress.config(text=f"  {data}")
                    self._append_log(f"COMPLETADO: {data}")
                    self._btn_sync_addons.config(state="normal")
                    self._set_status_op("✓ Completado", color="#2E8B57")
                    self._end_operation()
                    messagebox.showinfo(APP_TITLE, data)

                elif event == "addons_cancelled":
                    self._v_a_progress.set(0)
                    self._lbl_a_progress.config(text=f"  {data}")
                    self._append_log(f"DETENIDO: {data}")
                    self._btn_sync_addons.config(state="normal")
                    self._set_status_op("Detenido", color="#888888")
                    self._end_operation()
                    messagebox.showwarning(APP_TITLE, data)

                elif event == "btn_addons_enable":
                    self._btn_sync_addons.config(state="normal")

                # ── Scheduler events ──────────────────────────────────────
                elif event == "sched_refresh":
                    self._sched_refresh_tree(data)

                elif event == "sched_log":
                    rule_id, message = data
                    self._append_log(message)

        except queue.Empty:
            pass

        self.root.after(100, self._poll_queue)

    def _append_log(self, msg: str) -> None:
        """Append a color-coded, timestamped line to the log panel (GUI thread only)."""
        ts = datetime.datetime.now().strftime("%H:%M:%S")

        # ── Line-level tag selection ───────────────────────────────────────
        m = msg.strip()
        if m.startswith("[ERROR]") or m.startswith("Rollback:"):
            tag = "error"
        elif (
            m.startswith("COMPLETADO:")
            or m.startswith("Dump restaurado")
            or m.startswith("Filestore restaurado")
            or m.startswith("Permisos de")
            or "correctamente" in m.lower()
            or "completado" in m.lower()
        ):
            tag = "success"
        elif (
            m.startswith("DETENIDO:")
            or "[aviso]" in m.lower()
            or "advertencia" in m.lower()
            or "INCOMPATIBILIDAD" in m
            or m.startswith("Rollback")
        ):
            tag = "warning"
        elif m.startswith("  [") and (
            "en curso" in m or "en proceso" in m
        ):
            # In-progress heartbeat lines like "  [pg_dump en curso] ..."
            tag = "process"
        else:
            tag = "normal"

        self.log_widget.config(state="normal")
        self.log_widget.insert("end", f"[{ts}]  ", "timestamp")
        # Save position just before the message text so we can highlight keywords
        msg_start = self.log_widget.index("end")
        self.log_widget.insert("end", f"{msg}\n", tag)
        # Apply per-word keyword highlighting on top of the line tag
        self._highlight_log_keywords(msg_start, msg)
        if self._v_autoscroll.get():
            self.log_widget.see("end")
        self.log_widget.config(state="disabled")

    def _highlight_log_keywords(self, base_idx: str, msg: str) -> None:
        """
        Apply kw_info / kw_warning / kw_error tags on keyword matches
        within the just-inserted message text.

        base_idx: the tk.Text index where msg starts (right after the timestamp).
        """
        for match in _KW_LOG_RE.finditer(msg):
            word = match.group(0).lower().strip("[]")
            if "error" in word:
                tag_name = "kw_error"
            elif "warn" in word:
                tag_name = "kw_warning"
            else:
                tag_name = "kw_info"
            start = f"{base_idx}+{match.start()}c"
            end   = f"{base_idx}+{match.end()}c"
            self.log_widget.tag_add(tag_name, start, end)

    # ── Tab 6: Restore ───────────────────────────────────────────────────

    def _tab_restore(self) -> None:
        """Build the full restoration tab with scrollable content."""
        outer = ttk.Frame(self.nb, padding=0)
        self.nb.add(outer, text="  6. Restaurar  ")

        # Scrollable inner frame
        canvas = tk.Canvas(outer, highlightthickness=0, bg="#f0f0f0")
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        f = ttk.Frame(canvas, padding=_PAD * 2)
        win_id = canvas.create_window((0, 0), window=f, anchor="nw")
        f.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")),
        )
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfig(win_id, width=e.width),
        )
        # Scoped mousewheel: only scroll this canvas when the pointer is inside it
        self._bind_mousewheel(canvas)

        row = 0

        # ── Sección 1: Servidor destino ───────────────────────────────────
        sec1 = ttk.LabelFrame(f, text="1. ¿En qué servidor desea restaurar?", padding=_PAD)
        sec1.grid(row=row, column=0, sticky="ew", pady=(0, _PAD))
        sec1.columnconfigure(0, weight=1)
        f.columnconfigure(0, weight=1)
        row += 1

        # Radio options
        ttk.Radiobutton(
            sec1,
            text="En Servidor A — Emisor / Origen  (pestaña Conexiones)",
            variable=self._v_r_conn_type, value="origin",
            command=self._toggle_restore_conn,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=2)

        ttk.Radiobutton(
            sec1,
            text="En Servidor B — Receptor  (pestaña Conexiones)",
            variable=self._v_r_conn_type, value="receptor",
            command=self._toggle_restore_conn,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=2)

        ttk.Radiobutton(
            sec1,
            text="En el servidor de destino del backup  (Paso 4 — solo si elegiste 'otro servidor')",
            variable=self._v_r_conn_type, value="dest",
            command=self._toggle_restore_conn,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=2)

        # Status label — shows which server will be used and its connection state
        self._lbl_r_conn = ttk.Label(sec1, text="", foreground="gray")
        self._lbl_r_conn.grid(row=3, column=0, columnspan=3, pady=(4, 0))

        # Apply initial state
        self._toggle_restore_conn()

        # ── Sección 2: Archivos a restaurar ──────────────────────────────
        sec2 = ttk.LabelFrame(f, text="2. Archivos", padding=_PAD)
        sec2.grid(row=row, column=0, sticky="ew", pady=(0, _PAD))
        sec2.columnconfigure(1, weight=1)
        row += 1

        # ── Modo bundle (opcion prioritaria) ────────────────────────────
        ttk.Label(sec2, text="Modo de restauracion:", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 4)
        )
        rb_bundle = ttk.Radiobutton(
            sec2, text="Desde bundle unificado OBT (.tar)  —  recomendado",
            variable=self._v_r_restore_mode, value="bundle",
            command=self._toggle_restore_mode,
        )
        rb_bundle.grid(row=1, column=0, columnspan=3, sticky="w")
        ttk.Radiobutton(
            sec2, text="Archivos individuales",
            variable=self._v_r_restore_mode, value="individual",
            command=self._toggle_restore_mode,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, _PAD))

        # Bundle path panel
        self._pnl_bundle = ttk.Frame(sec2)
        self._pnl_bundle.columnconfigure(1, weight=1)
        ttk.Radiobutton(
            self._pnl_bundle, text="Archivo local:",
            variable=self._v_r_bundle_src, value="local",
            command=self._toggle_bundle_src,
        ).grid(row=0, column=0, sticky="w")
        self._r_bundle_local_entry = ttk.Entry(
            self._pnl_bundle, textvariable=self._v_r_bundle_local, width=38
        )
        self._r_bundle_local_entry.grid(row=0, column=1, sticky="ew", padx=(4, 4))
        ttk.Button(
            self._pnl_bundle, text="...", width=3,
            command=lambda: self._v_r_bundle_local.set(
                filedialog.askopenfilename(
                    title="Seleccionar bundle OBT",
                    filetypes=[("Bundle OBT", "*.tar"), ("Todos", "*.*")],
                ) or self._v_r_bundle_local.get()
            ),
        ).grid(row=0, column=2)
        ttk.Radiobutton(
            self._pnl_bundle, text="Ya en servidor:",
            variable=self._v_r_bundle_src, value="server",
            command=self._toggle_bundle_src,
        ).grid(row=1, column=0, sticky="w", pady=(2, 0))
        self._r_bundle_srv_entry = ttk.Entry(
            self._pnl_bundle, textvariable=self._v_r_bundle_srv, width=38, state="disabled"
        )
        self._r_bundle_srv_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=(4, 0), pady=(2, 0))
        self._pnl_bundle.grid(row=3, column=0, columnspan=3, sticky="ew", padx=(20, 0), pady=(0, _PAD))

        # Separator between modes
        ttk.Separator(sec2, orient="horizontal").grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=(0, _PAD)
        )

        # Individual-file panel (the original sec2 content, now grouped)
        self._pnl_individual = ttk.Frame(sec2)
        self._pnl_individual.columnconfigure(1, weight=1)

        # Dump file
        ttk.Label(self._pnl_individual, text="Dump de BD:", font=("Segoe UI", 9, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 2)
        )
        ttk.Radiobutton(
            self._pnl_individual, text="Archivo local:", variable=self._v_r_dump_src, value="local",
            command=self._toggle_dump_src,
        ).grid(row=1, column=0, sticky="w")
        pi = self._pnl_individual   # shorthand
        self._r_dump_local_entry = ttk.Entry(pi, textvariable=self._v_r_dump_local, width=38)
        self._r_dump_local_entry.grid(row=1, column=1, sticky="ew", padx=(4, 4))
        ttk.Button(pi, text="...", width=3,
            command=lambda: self._browse_file(self._v_r_dump_local, "*.dump *.sql")
        ).grid(row=1, column=2)

        ttk.Radiobutton(
            pi, text="Ya en servidor:", variable=self._v_r_dump_src, value="server",
            command=self._toggle_dump_src,
        ).grid(row=2, column=0, sticky="w", pady=(2, 0))
        self._r_dump_srv_entry = ttk.Entry(pi, textvariable=self._v_r_dump_srv, width=38, state="disabled")
        self._r_dump_srv_entry.grid(row=2, column=1, sticky="ew", padx=(4, 4))

        ttk.Separator(pi, orient="horizontal").grid(
            row=3, column=0, columnspan=3, sticky="ew", pady=_PAD
        )

        # Filestore zip
        ttk.Label(pi, text="Filestore ZIP:", font=("Segoe UI", 9, "bold")).grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(0, 2)
        )
        ttk.Radiobutton(
            pi, text="Archivo local:", variable=self._v_r_fs_src, value="local",
            command=self._toggle_fs_src,
        ).grid(row=5, column=0, sticky="w")
        self._r_fs_local_entry = ttk.Entry(pi, textvariable=self._v_r_fs_local, width=38)
        self._r_fs_local_entry.grid(row=5, column=1, sticky="ew", padx=(4, 4))
        ttk.Button(pi, text="...", width=3,
            command=lambda: self._browse_file(self._v_r_fs_local, "*.zip")
        ).grid(row=5, column=2)

        ttk.Radiobutton(
            pi, text="Ya en servidor:", variable=self._v_r_fs_src, value="server",
            command=self._toggle_fs_src,
        ).grid(row=6, column=0, sticky="w", pady=(2, 0))
        self._r_fs_srv_entry = ttk.Entry(pi, textvariable=self._v_r_fs_srv, width=38, state="disabled")
        self._r_fs_srv_entry.grid(row=6, column=1, sticky="ew", padx=(4, 4))

        ttk.Radiobutton(
            pi, text="No restaurar filestore", variable=self._v_r_fs_src, value="none",
            command=self._toggle_fs_src,
        ).grid(row=7, column=0, columnspan=3, sticky="w", pady=(2, 0))

        ttk.Separator(pi, orient="horizontal").grid(
            row=8, column=0, columnspan=3, sticky="ew", pady=_PAD
        )

        # Inventory file (optional) — companion JSON generated at backup time.
        # When provided, post-restore checks compare against the backup baseline.
        ttk.Label(
            pi, text="Inventario backup:",
            foreground=_C_PURPLE, font=("Segoe UI", 9, "bold"),
        ).grid(row=9, column=0, columnspan=3, sticky="w", pady=(0, 2))

        self._r_inv_entry = ttk.Entry(pi, textvariable=self._v_r_inventory)
        self._r_inv_entry.grid(row=10, column=0, columnspan=2, sticky="ew", padx=(0, 4), pady=2)

        inv_btn_frame = ttk.Frame(pi)
        inv_btn_frame.grid(row=10, column=2, sticky="w")
        ttk.Button(
            inv_btn_frame, text="...", width=3,
            command=self._browse_inventory,
        ).pack(side="left", padx=(0, 2))
        ttk.Button(
            inv_btn_frame, text="Auto",
            command=self._auto_detect_inventory,
        ).pack(side="left")

        self._lbl_inv_status = ttk.Label(
            pi, text="  Opcional — mejora la precision de las verificaciones",
            foreground="gray", font=("Segoe UI", 8),
        )
        self._lbl_inv_status.grid(row=11, column=0, columnspan=3, sticky="w")

        # Auto-detect inventory when dump path changes
        self._v_r_dump_local.trace_add("write", lambda *_: self._auto_detect_inventory(silent=True))

        # Grid the individual panel into sec2 and apply initial visibility
        self._pnl_individual.grid(row=5, column=0, columnspan=3, sticky="ew")
        self._toggle_restore_mode()

        # ── Sección 3: Base de datos destino ─────────────────────────────
        sec3 = ttk.LabelFrame(f, text="3. Base de datos destino", padding=_PAD)
        sec3.grid(row=row, column=0, sticky="ew", pady=(0, _PAD))
        sec3.columnconfigure(1, weight=1)
        row += 1

        ttk.Label(sec3, text="Nombre nueva BD:").grid(
            row=0, column=0, sticky="e", padx=(0, _PAD), pady=4
        )
        ttk.Entry(sec3, textvariable=self._v_r_db_name).grid(
            row=0, column=1, sticky="ew", pady=4, padx=(0, _PAD)
        )

        ttk.Label(sec3, text="Ruta raiz filestore:").grid(
            row=1, column=0, sticky="e", padx=(0, _PAD), pady=4
        )
        self._r_fs_root_combo = ttk.Combobox(sec3, textvariable=self._v_r_fs_root, width=38)
        self._r_fs_root_combo.grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(
            sec3, text="Buscar", command=self._action_r_search_fs
        ).grid(row=1, column=2, padx=_PAD)

        # ── Sección 4: Opciones ───────────────────────────────────────────
        sec4 = ttk.LabelFrame(f, text="4. Opciones", padding=_PAD)
        sec4.grid(row=row, column=0, sticky="ew", pady=(0, _PAD))
        sec4.columnconfigure(1, weight=1)
        row += 1

        ttk.Label(sec4, text="Workers pg_restore (-j):").grid(
            row=0, column=0, sticky="e", padx=(0, _PAD), pady=3
        )
        ttk.Spinbox(sec4, textvariable=self._v_r_jobs, from_=1, to=16, width=5).grid(
            row=0, column=1, sticky="w", pady=3
        )
        ttk.Label(sec4, text="  (solo para formato custom)", foreground="gray").grid(
            row=0, column=2, sticky="w"
        )

        ttk.Checkbutton(
            sec4, text="Neutralizar base de datos al terminar",
            variable=self._v_r_neutralize, command=self._toggle_neutralize,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        ttk.Label(sec4, text="Ruta odoo.conf:").grid(
            row=2, column=0, sticky="e", padx=(0, _PAD), pady=3
        )
        self._r_conf_entry = ttk.Entry(sec4, textvariable=self._v_r_conf, width=38, state="disabled")
        self._r_conf_entry.grid(row=2, column=1, sticky="ew", pady=3)

        ttk.Checkbutton(
            sec4, text="Eliminar archivos subidos de /tmp/ al terminar",
            variable=self._v_r_cleanup,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(4, 0))

        # ── Progreso y ejecución ──────────────────────────────────────────
        sec5 = ttk.LabelFrame(f, text="5. Ejecucion", padding=_PAD)
        sec5.grid(row=row, column=0, sticky="ew", pady=(0, _PAD))
        sec5.columnconfigure(0, weight=1)
        row += 1

        self._v_r_progress = tk.DoubleVar()
        self._r_progressbar = ttk.Progressbar(
            sec5, variable=self._v_r_progress, maximum=100
        )
        self._r_progressbar.grid(row=0, column=0, sticky="ew", pady=(0, 4))

        self._lbl_r_progress = ttk.Label(sec5, text="")
        self._lbl_r_progress.grid(row=1, column=0)

        btn_row_r = ttk.Frame(sec5)
        btn_row_r.grid(row=2, column=0, pady=_PAD)
        self._btn_restore = ttk.Button(
            btn_row_r, text="Iniciar Restauracion",
            style="Primary.TButton", command=self._action_start_restore,
        )
        self._btn_restore.pack(side="left", padx=6)
        self._btn_stop_restore = ttk.Button(
            btn_row_r, text="Detener", state="disabled",
            style="Stop.TButton",
            command=self._action_stop,
        )
        self._btn_stop_restore.pack(side="left", padx=6)

    # ── Restore: inventory helpers ────────────────────────────────────────

    def _browse_inventory(self) -> None:
        """Open a file dialog to pick an inventory JSON file manually."""
        inv_dir = InventoryManager.local_inventory_dir()
        # Start in the dump's directory if a dump is already selected
        dump_dir = os.path.dirname(self._v_r_dump_local.get().strip() or "")
        start_dir = dump_dir if os.path.isdir(dump_dir) else (
            inv_dir if os.path.isdir(inv_dir) else os.path.expanduser("~")
        )
        path = filedialog.askopenfilename(
            title="Seleccionar inventario de backup",
            initialdir=start_dir,
            filetypes=[("Inventario JSON", "*_inventory.json"), ("JSON", "*.json"), ("Todos", "*.*")],
        )
        if path:
            self._v_r_inventory.set(path)
            self._update_inv_status(path)

    def _auto_detect_inventory(self, silent: bool = False) -> None:
        """
        Try to find the companion inventory for the currently selected dump file.

        Search order:
          1. Companion path next to the dump  (odoo_db.dump → odoo_db_inventory.json)
          2. ~/.odoo_backup_tool/inventories/ (used when backup destination was remote)

        When silent=True (called via trace) only fills the field if empty and
        does not show any warning dialogs.
        """
        dump_path = self._v_r_dump_local.get().strip()
        if not dump_path:
            if not silent:
                messagebox.showwarning(
                    APP_TITLE,
                    "Seleccione primero el archivo de dump para auto-detectar el inventario."
                )
            return

        # Only auto-fill if field is currently empty (avoid overwriting manual choice)
        if self._v_r_inventory.get().strip() and silent:
            return

        # Search path 1: companion next to the dump
        companion = InventoryManager.companion_path(dump_path)
        if os.path.isfile(companion):
            self._v_r_inventory.set(companion)
            self._update_inv_status(companion)
            return

        # Search path 2: local inventory directory (remote backups)
        dump_base = os.path.splitext(os.path.basename(dump_path))[0]
        candidate = os.path.join(
            InventoryManager.local_inventory_dir(),
            f"{dump_base}_inventory.json",
        )
        if os.path.isfile(candidate):
            self._v_r_inventory.set(candidate)
            self._update_inv_status(candidate)
            return

        if not silent:
            messagebox.showinfo(
                APP_TITLE,
                "No se encontro un inventario de backup para este archivo de dump.\n\n"
                "Puede seleccionarlo manualmente con el boton '...'.\n\n"
                "Si el backup fue hecho con esta herramienta, el inventario deberia estar "
                "junto al dump o en:\n"
                f"{InventoryManager.local_inventory_dir()}"
            )

    def _update_inv_status(self, path: str) -> None:
        """Update the status label next to the inventory field."""
        if not path or not os.path.isfile(path):
            self._lbl_inv_status.config(
                text="  Opcional — mejora la precision de las verificaciones",
                foreground="gray",
            )
            return
        try:
            inv = InventoryManager.load(path)
            meta = inv.get("meta", {})
            db_info = inv.get("database", {})
            ts   = meta.get("timestamp", "?")[:16].replace("T", " ")
            host = meta.get("source_host", "?")
            tbls = db_info.get("table_count", "?")
            sz   = db_info.get("size_human", "?")
            fs_f = inv.get("filestore", {}).get("total_files", "?")
            self._lbl_inv_status.config(
                text=(
                    f"  Inventario: {ts}  |  origen: {host}  |  "
                    f"{tbls} tablas, {sz}  |  {fs_f} archivos filestore"
                ),
                foreground=_C_TEAL,
            )
        except Exception:
            self._lbl_inv_status.config(
                text="  Archivo seleccionado (no se pudo leer el resumen)",
                foreground="#E67E22",
            )

    # ── Restore: toggle helpers ───────────────────────────────────────────

    def _toggle_restore_conn(self) -> None:
        """
        Update the status label in Tab 6 showing which server will be used
        and its current connection state. Auto-fills file paths for "dest" mode.
        """
        sel = self._v_r_conn_type.get()

        if sel == "origin":
            connected = self._ssh.connected
            host = self._ssh.host or "—"
            dot = "  ● conectado" if connected else "  ● desconectado"
            self._lbl_r_conn.config(
                text=f"Servidor A  ({host}){dot}",
                foreground="green" if connected else "#888888",
            )

        elif sel == "receptor":
            connected = self._ssh_restore.connected
            host = self._ssh_restore.host or "—"
            dot = "  ● conectado" if connected else "  ● desconectado"
            self._lbl_r_conn.config(
                text=f"Servidor B  ({host}){dot}",
                foreground="green" if connected else "#888888",
            )

        elif sel == "dest":
            dest_ok = (
                self._v_dest_type.get() == "remote"
                and self._dv["host"].get().strip()
            )
            if dest_ok:
                host = self._dv["host"].get()
                dest_dir = self._dv["dir"].get().rstrip("/")
                self._lbl_r_conn.config(
                    text=f"Se usara el servidor de destino del backup  ({host})",
                    foreground="gray",
                )

                # Auto-fill server paths using Step-4 remote dir + expected filenames
                db    = self._v_db.get()
                ext   = self._v_dump_fmt.get()    # 'dump' or 'sql'
                fs_db = self._v_fs_db.get() or db

                if db:
                    self._v_r_dump_srv.set(f"{dest_dir}/odoo_{db}.{ext}")
                if fs_db:
                    self._v_r_fs_srv.set(f"{dest_dir}/filestore_{fs_db}.zip")

                # Switch sources to "server" so the auto-filled paths are active
                self._v_r_dump_src.set("server")
                self._v_r_fs_src.set("server")
                self._toggle_dump_src()
                self._toggle_fs_src()
            else:
                self._lbl_r_conn.config(
                    text="Advertencia: en el Paso 4 no hay un servidor remoto configurado.",
                    foreground="orange",
                )

        else:
            self._lbl_r_conn.config(text="", foreground="gray")

    def _get_restore_ssh(self) -> SSHClient:
        """
        Return the SSH client that corresponds to the user's restore-destination choice.

        Raises:
            RuntimeError: If the required connection is not active.
        """
        sel = self._v_r_conn_type.get()

        if sel == "origin":
            if not self._ssh.connected:
                raise RuntimeError(
                    "No hay conexion activa con el Servidor A — Emisor.\n"
                    "Conéctese en la pestaña 'Conexiones'."
                )
            return self._ssh

        if sel == "receptor":
            if not self._ssh_restore.connected:
                raise RuntimeError(
                    "No hay conexion activa con el Servidor B — Receptor.\n"
                    "Conéctese en la pestaña 'Conexiones'."
                )
            return self._ssh_restore

        # sel == "dest" — auto-connect from Step-4 fields
        if self._v_dest_type.get() != "remote":
            raise RuntimeError(
                "El Paso 4 no tiene un servidor remoto configurado.\n"
                "Elige 'Otro servidor' e ingresa las credenciales."
            )
        if not self._ssh_dest.connected:
            self._ssh_dest.connect(
                self._dv["host"].get(),
                int(self._dv["port"].get()),
                self._dv["user"].get(),
                self._dv["pass"].get(),
            )
        return self._ssh_dest

    def _toggle_restore_mode(self) -> None:
        """Show/hide the bundle vs individual-file panels in Tab 6 Sec 2."""
        mode = self._v_r_restore_mode.get()
        if mode == "bundle":
            self._pnl_bundle.grid()
            self._pnl_individual.grid_remove()
        else:
            self._pnl_bundle.grid_remove()
            self._pnl_individual.grid()

    def _toggle_bundle_src(self) -> None:
        local = self._v_r_bundle_src.get() == "local"
        self._r_bundle_local_entry.config(state="normal" if local else "disabled")
        self._r_bundle_srv_entry.config(state="disabled" if local else "normal")

    def _toggle_dump_src(self) -> None:
        local = self._v_r_dump_src.get() == "local"
        self._r_dump_local_entry.config(state="normal" if local else "disabled")
        self._r_dump_srv_entry.config(state="disabled" if local else "normal")

    def _toggle_fs_src(self) -> None:
        src = self._v_r_fs_src.get()
        self._r_fs_local_entry.config(state="normal" if src == "local" else "disabled")
        self._r_fs_srv_entry.config(state="normal" if src == "server" else "disabled")

    def _toggle_neutralize(self) -> None:
        state = "normal" if self._v_r_neutralize.get() else "disabled"
        self._r_conf_entry.config(state=state)

    def _browse_file(self, var: tk.StringVar, filetypes: str) -> None:
        path = filedialog.askopenfilename(
            title="Seleccionar archivo",
            filetypes=[("Archivos", filetypes), ("Todos", "*.*")],
        )
        if path:
            var.set(path)

    # ── Restore: actions ─────────────────────────────────────────────────

    def _action_r_connect(self) -> None:
        """Connect the Servidor B (Receptor) SSH client from the Tab 1 credentials."""
        host = self._r_conn_vars["host"].get().strip()
        port_s = self._r_conn_vars["port"].get().strip()
        user = self._r_conn_vars["user"].get().strip()
        pwd = self._r_conn_vars["pass"].get()

        if not all([host, port_s, user, pwd]):
            messagebox.showwarning(APP_TITLE, "Complete todos los campos del Servidor B (Receptor).")
            return
        try:
            port = int(port_s)
        except ValueError:
            messagebox.showerror(APP_TITLE, "El puerto debe ser un numero entero.")
            return

        self._btn_r_connect.config(state="disabled")
        self._lbl_b_conn_status.config(text="Conectando...", foreground="gray")

        def _run() -> None:
            try:
                if self._ssh_restore.connected:
                    self._ssh_restore.close()
                self._ssh_restore.connect(host, port, user, pwd)
                self._q.put(("r_conn_ok", f"Conectado a {host}:{port}"))
            except ConnectionError as exc:
                self._q.put(("r_conn_fail", str(exc)))
            finally:
                self._q.put(("btn_r_enable", None))

        threading.Thread(target=_run, daemon=True).start()

    def _action_r_search_fs(self) -> None:
        """Search filestore roots on the restore destination server."""
        try:
            ssh = self._get_restore_ssh()
        except RuntimeError as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return

        def _run() -> None:
            try:
                paths = FilestoreManager(ssh).find_filestore_roots()
                self._q.put(("r_fs_roots", paths))
            except Exception as exc:
                self._q.put(("error", f"Error buscando filestore en destino: {exc}"))

        threading.Thread(target=_run, daemon=True).start()

    def _action_start_restore(self) -> None:
        """Validate inputs and launch the restore worker."""
        # ── Pre-flight checks (run in GUI thread before disabling anything) ──
        try:
            ssh = self._get_restore_ssh()
        except RuntimeError as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return

        if not self._validate_restore_params():
            return

        if not self._check_ssh_alive(ssh, "destino de restauracion"):
            return

        self._btn_restore.config(state="disabled")
        self._v_r_progress.set(0)
        self._lbl_r_progress.config(text="")
        self._begin_operation()

        # Safe int conversion — already validated by _validate_restore_params
        try:
            jobs = int(self._v_r_jobs.get().strip())
        except ValueError:
            jobs = 4

        params = {
            "db_name":      self._v_r_db_name.get().strip(),
            "restore_mode": self._v_r_restore_mode.get(),
            "bundle_src":   self._v_r_bundle_src.get(),
            "bundle_local": self._v_r_bundle_local.get().strip(),
            "bundle_srv":   self._v_r_bundle_srv.get().strip(),
            "dump_src":     self._v_r_dump_src.get(),
            "dump_local":   self._v_r_dump_local.get(),
            "dump_srv":     self._v_r_dump_srv.get(),
            "fs_src":       self._v_r_fs_src.get(),
            "fs_local":     self._v_r_fs_local.get(),
            "fs_srv":       self._v_r_fs_srv.get(),
            "fs_root":      self._v_r_fs_root.get(),
            "jobs":         jobs,
            "neutralize":   self._v_r_neutralize.get(),
            "odoo_conf":    self._v_r_conf.get(),
            "cleanup":      self._v_r_cleanup.get(),
            "inventory":    self._v_r_inventory.get().strip(),
        }

        threading.Thread(
            target=self._worker_restore, args=(params, ssh), daemon=True
        ).start()

    # ── Restore: background worker ────────────────────────────────────────

    def _worker_restore(self, p: dict, ssh: SSHClient) -> None:
        """Orchestrates the full restoration sequence in a background thread."""
        mgr = RestoreManager(ssh)
        db = p["db_name"]
        uploaded: list[str] = []   # remote paths to clean up on finish
        _bundle_extract_dir: str = ""  # set when bundle is extracted; cleaned up at end

        # ── Bundle extraction (if mode == "bundle") ───────────────────────
        if p.get("restore_mode") == "bundle":
            try:
                import uuid as _uuid
                bm = BundleManager(ssh)
                extract_dir = f"/tmp/obt_restore_{_uuid.uuid4().hex[:8]}"
                _bundle_extract_dir = extract_dir

                bundle_src = p.get("bundle_src", "local")
                if bundle_src == "local":
                    # Upload the local bundle to the server first
                    bundle_local = p.get("bundle_local", "")
                    if not bundle_local or not os.path.isfile(bundle_local):
                        self._q.put(("error", f"Bundle no encontrado: {bundle_local}"))
                        self._q.put(("btn_r_enable", None))
                        return
                    bundle_fname  = os.path.basename(bundle_local)
                    bundle_remote = f"/tmp/{bundle_fname}"
                    self._log(f"Subiendo bundle al servidor: {bundle_fname}")
                    mgr.upload_file(bundle_local, bundle_remote, log_callback=self._log)
                    uploaded.append(bundle_remote)
                else:
                    bundle_remote = p.get("bundle_srv", "")
                    if not bundle_remote:
                        self._q.put(("error", "Ruta del bundle en servidor no especificada."))
                        self._q.put(("btn_r_enable", None))
                        return

                extracted = bm.extract_on_server(bundle_remote, extract_dir, log_callback=self._log)

                # Override params with extracted paths
                if extracted.get("dump"):
                    p["dump_src"] = "server"
                    p["dump_srv"] = extracted["dump"]
                else:
                    self._q.put(("error", "El bundle no contiene un archivo de dump reconocible."))
                    self._q.put(("btn_r_enable", None))
                    bm.cleanup_extract_dir(extract_dir)
                    return

                if extracted.get("filestore"):
                    p["fs_src"] = "server"
                    p["fs_srv"] = extracted["filestore"]
                else:
                    p["fs_src"] = "none"

                if extracted.get("inventory") and not p.get("inventory"):
                    inv_dict = bm.read_inventory_from_server(extracted["inventory"])
                    if inv_dict:
                        # Save locally so InventoryManager.load() can read it
                        import tempfile as _tempfile
                        inv_tmp = _tempfile.NamedTemporaryFile(
                            suffix="_inventory.json", delete=False, mode="w", encoding="utf-8"
                        )
                        import json as _json
                        _json.dump(inv_dict, inv_tmp, ensure_ascii=False, indent=2)
                        inv_tmp.close()
                        p["inventory"] = inv_tmp.name

            except Exception as exc_bundle:
                self._q.put(("error", f"Error procesando bundle: {exc_bundle}"))
                self._q.put(("btn_r_enable", None))
                return

        # Load backup inventory if provided (optional — enriches post-restore checks)
        inventory: dict | None = None
        inv_path = p.get("inventory", "")
        if inv_path and os.path.isfile(inv_path):
            try:
                inventory = InventoryManager.load(inv_path)
                self._log(
                    f"Inventario de backup cargado: "
                    f"{os.path.basename(inv_path)} "
                    f"(origen: {inventory.get('meta', {}).get('source_host', '?')}, "
                    f"{inventory.get('meta', {}).get('timestamp', '?')[:16]})"
                )
            except Exception as exc:
                self._log(f"[aviso] No se pudo cargar el inventario: {exc}")

        # Determine how many major steps for the progress bar
        steps = 4  # createdb + restore dump + grant privileges + post-restore check
        if p["fs_src"] != "none":
            steps += 1
        if p["neutralize"]:
            steps += 1
        if p["dump_src"] == "local":
            steps += 1
        if p["fs_src"] == "local":
            steps += 1

        current = 0

        def advance(label: str) -> None:
            nonlocal current
            current += 1
            pct = min((current / steps) * 100, 99)
            self._q.put(("r_progress", (pct, label)))

        try:
            # ── 1. Upload dump if local ───────────────────────────────────
            if p["dump_src"] == "local":
                remote_dump = f"/tmp/{os.path.basename(p['dump_local'])}"

                def _dump_prog(t: int, total: int) -> None:
                    pct = (t / total * 100) if total else 0
                    self._q.put(("r_progress", (pct, f"Subiendo dump ... {pct:.0f}%")))

                mgr.upload_file(p["dump_local"], remote_dump, _dump_prog, self._log)
                uploaded.append(remote_dump)
                advance("Dump subido al servidor.")
            else:
                remote_dump = p["dump_srv"]

            # ── 2. Upload filestore if local ──────────────────────────────
            remote_zip = None
            if p["fs_src"] == "local":
                remote_zip = f"/tmp/{os.path.basename(p['fs_local'])}"

                def _fs_prog(t: int, total: int) -> None:
                    pct = (t / total * 100) if total else 0
                    self._q.put(("r_progress", (pct, f"Subiendo filestore ... {pct:.0f}%")))

                mgr.upload_file(p["fs_local"], remote_zip, _fs_prog, self._log)
                uploaded.append(remote_zip)
                advance("Filestore subido al servidor.")
            elif p["fs_src"] == "server":
                remote_zip = p["fs_srv"]

            # ── 3. Create database ────────────────────────────────────────
            mgr.create_database(db, log_callback=self._log)
            advance(f"Base de datos '{db}' creada.")

            # ── 4. Detect format, check version compatibility, restore dump ─
            fmt = mgr.detect_dump_format(remote_dump)
            self._log(
                f"Formato detectado: "
                f"{'custom (pg_restore)' if fmt == 'custom' else 'SQL plano (psql)'}"
            )

            # Version check only matters for custom format
            if fmt == "custom":
                compat = mgr.check_version_compatibility(remote_dump, self._log)
                if not compat["compatible"]:
                    # Raise so the except block drops the just-created DB (rollback)
                    raise RuntimeError(compat["recommendation"])

            mgr.restore_dump(remote_dump, db, fmt, p["jobs"], self._log)
            advance(f"Dump restaurado en '{db}'.")

            # ── 5. Restore filestore ──────────────────────────────────────
            filestore_dest = None
            if remote_zip and p["fs_src"] != "none":
                mgr.restore_filestore(remote_zip, db, p["fs_root"], self._log)
                filestore_dest = f"{p['fs_root']}/{db}"
                advance("Filestore restaurado.")

            # ── 6. Grant Odoo privileges (DB + filestore) — always required
            mgr.grant_odoo_privileges(
                db_name=db,
                filestore_path=filestore_dest,
                log_callback=self._log,
            )
            advance("Permisos de Odoo aplicados.")

            # ── 7. Neutralize ─────────────────────────────────────────────
            if p["neutralize"]:
                mgr.neutralize(db, p["odoo_conf"], self._log)
                advance("Base de datos neutralizada.")

            # ── 8. Post-restore validation ────────────────────────────────
            self._log("─" * 50)
            check = mgr.post_restore_check(
                db_name=db,
                filestore_path=filestore_dest,
                inventory=inventory,
                log_callback=self._log,
            )
            advance("Verificaciones post-restauracion ejecutadas.")

            # ── 9. Cleanup uploaded files ─────────────────────────────────
            if p["cleanup"]:
                for rp in uploaded:
                    self._log(f"Limpiando {rp} del servidor ...")
                    mgr.cleanup_upload(rp)

            # Report result based on check outcome
            if check["ok"] and not check["warnings"]:
                self._q.put(("r_done",
                    f"Restauracion de '{db}' completada exitosamente.\n\n"
                    "Todas las verificaciones pasaron."
                ))
            elif check["ok"] and check["warnings"]:
                # Success but with warnings — show dialog with detail
                warn_text = "\n".join(f"  • {w}" for w in check["warnings"])
                self._q.put(("r_done",
                    f"Restauracion de '{db}' completada con avisos:\n\n"
                    f"{warn_text}\n\n"
                    "Revise el log para mas detalle."
                ))
            else:
                # Errors detected — still report done but highlight problems
                err_text  = "\n".join(f"  ✗ {e}" for e in check["errors"])
                warn_text = "\n".join(f"  ⚠ {w}" for w in check["warnings"])
                self._q.put(("r_check_failed", {
                    "db": db,
                    "errors": err_text,
                    "warnings": warn_text,
                }))

        except RuntimeError as exc:
            if str(exc) == "__CANCELLED__":
                self._q.put(("r_cancelled", "Restauracion detenida por el usuario."))
            else:
                self._log(f"[ERROR] {exc}")
                try:
                    mgr.drop_database(db)
                    self._log(f"Rollback: base de datos '{db}' eliminada.")
                except Exception:
                    pass
                self._q.put(("error", f"Restauracion fallida:\n{exc}"))
                self._q.put(("btn_r_enable", None))
        except Exception as exc:
            self._log(f"[ERROR] {exc}")
            try:
                mgr.drop_database(db)
                self._log(f"Rollback: base de datos '{db}' eliminada.")
            except Exception:
                pass
            self._q.put(("error", f"Restauracion fallida:\n{exc}"))
            self._q.put(("btn_r_enable", None))
        finally:
            # Clean up bundle extraction directory if one was created
            if _bundle_extract_dir:
                try:
                    BundleManager(ssh).cleanup_extract_dir(_bundle_extract_dir)
                    self._log(f"Directorio de extraccion eliminado: {_bundle_extract_dir}")
                except Exception:
                    pass

    # ── Tab 7: Addons Sync ───────────────────────────────────────────────

    def _tab_addons(self) -> None:
        """Build the GitHub/GitLab addons synchronization tab (Tab 7)."""
        outer = ttk.Frame(self.nb, padding=0)
        self.nb.add(outer, text="  7. Addons  ")

        # Scrollable inner frame — isolated mousewheel via _bind_mousewheel
        canvas = tk.Canvas(outer, highlightthickness=0, bg=_C_BG)
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        f = ttk.Frame(canvas, padding=_PAD * 2)
        win_id = canvas.create_window((0, 0), window=f, anchor="nw")
        f.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(win_id, width=e.width))
        # Isolated scroll — does not affect Tab 6 canvas
        self._bind_mousewheel(canvas)

        f.columnconfigure(0, weight=1)
        row = 0

        ttk.Label(
            f, text="Sincronizacion de Addons Personalizados",
            font=("Segoe UI", 11, "bold"),
        ).grid(row=row, column=0, sticky="w", pady=(0, _PAD))
        row += 1

        # ── Seccion 1: Servidor destino ───────────────────────────────────
        sec1 = ttk.LabelFrame(f, text="1. Servidor destino", padding=_PAD)
        sec1.grid(row=row, column=0, sticky="ew", pady=(0, _PAD))
        sec1.columnconfigure(0, weight=1)
        row += 1

        ttk.Radiobutton(
            sec1,
            text="Servidor A — Emisor / Origen  (pestaña Conexiones)",
            variable=self._v_a_conn_type, value="origin",
        ).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Radiobutton(
            sec1,
            text="Servidor B — Receptor  (pestaña Conexiones)",
            variable=self._v_a_conn_type, value="restore",
        ).grid(row=1, column=0, sticky="w", pady=2)

        # ── Seccion 2: Repositorio ────────────────────────────────────────
        sec2 = ttk.LabelFrame(f, text="2. Repositorio Git", padding=_PAD)
        sec2.grid(row=row, column=0, sticky="ew", pady=(0, _PAD))
        sec2.columnconfigure(0, minsize=130)
        sec2.columnconfigure(1, weight=1)
        row += 1

        _fields_s2 = [
            ("URL SSH del repo:",   self._v_a_repo_url,  False,
             "git@github.com:mi-org/mi-repo.git"),
            ("Rama (branch):",      self._v_a_branch,    False, "main"),
            ("Ruta en servidor:",   self._v_a_target,    False, "/opt/odoo/addons_custom"),
            ("Usuario Odoo (OS):",  self._v_a_odoo_user, False, "odoo"),
        ]
        for i, (lbl, var, _show, placeholder) in enumerate(_fields_s2):
            ttk.Label(sec2, text=lbl).grid(
                row=i, column=0, sticky="e", padx=(0, _PAD), pady=3
            )
            e = ttk.Entry(sec2, textvariable=var)
            e.grid(row=i, column=1, sticky="ew", pady=3, padx=(0, _PAD))
            if not var.get():
                # Show placeholder hint text and clear it on first focus
                e.insert(0, placeholder)
                e.config(foreground="gray")
                def _on_focus_in(event, _e=e, _v=var, _ph=placeholder):
                    if _e.get() == _ph:
                        _e.delete(0, "end")
                        _e.config(foreground=_C_TEXT)
                def _on_focus_out(event, _e=e, _v=var, _ph=placeholder):
                    if not _e.get().strip():
                        _e.insert(0, _ph)
                        _e.config(foreground="gray")
                e.bind("<FocusIn>",  _on_focus_in)
                e.bind("<FocusOut>", _on_focus_out)

        # Tipo de sincronización: submódulos o git normal
        n_fields = len(_fields_s2)
        ttk.Separator(sec2, orient="horizontal").grid(
            row=n_fields, column=0, columnspan=2, sticky="ew", pady=(6, 4)
        )
        ttk.Label(
            sec2, text="Tipo de repositorio:",
        ).grid(row=n_fields + 1, column=0, sticky="e", padx=(0, _PAD), pady=3)

        repo_type_frame = ttk.Frame(sec2)
        repo_type_frame.grid(row=n_fields + 1, column=1, sticky="w", pady=3)

        ttk.Radiobutton(
            repo_type_frame,
            text="Git normal  (git pull)",
            variable=self._v_a_submodules, value=False,
        ).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(
            repo_type_frame,
            text="Git con submódulos  (git submodule update --remote --recursive --merge)",
            variable=self._v_a_submodules, value=True,
        ).pack(side="left")

        ttk.Label(
            sec2,
            text="  Usa 'Git con submódulos' si el repo principal enlaza otros repos via .gitmodules",
            font=("Segoe UI", 8), foreground="#888888",
        ).grid(row=n_fields + 2, column=0, columnspan=2, sticky="w", pady=(0, 2))

        # ── Seccion 3: Llave SSH ──────────────────────────────────────────
        sec3 = ttk.LabelFrame(
            f, text="3. Llave SSH para GitHub / GitLab", padding=_PAD
        )
        sec3.grid(row=row, column=0, sticky="ew", pady=(0, _PAD))
        sec3.columnconfigure(0, weight=1)
        row += 1

        # Helper: info label for the cascade order
        ttk.Label(
            sec3,
            text="Orden de prioridad: primero se usa la llave del servidor; "
                 "si no existe, se sube una local; si no hay ninguna, se genera una nueva.",
            font=("Segoe UI", 8), foreground="#888888", wraplength=560, justify="left",
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))

        # ── Priority 1: Server key ────────────────────────────────────────
        p1_frame = ttk.Frame(sec3)
        p1_frame.grid(row=1, column=0, sticky="ew", pady=(0, 2))
        p1_frame.columnconfigure(1, weight=1)

        ttk.Radiobutton(
            p1_frame,
            text="Llave en el servidor  (recomendado — la llave ya esta registrada en GitHub)",
            variable=self._v_a_key_source, value="server",
            command=self._toggle_a_key_source,
        ).grid(row=0, column=0, columnspan=3, sticky="w")

        self._frm_a_server = ttk.Frame(p1_frame)
        self._frm_a_server.grid(row=1, column=0, columnspan=3, sticky="ew", padx=(20, 0))
        self._frm_a_server.columnconfigure(1, weight=1)

        ttk.Label(self._frm_a_server, text="Llave en el servidor:").grid(
            row=0, column=0, sticky="e", padx=(0, _PAD), pady=3
        )
        server_key_row = ttk.Frame(self._frm_a_server)
        server_key_row.grid(row=0, column=1, sticky="ew", pady=3)
        server_key_row.columnconfigure(0, weight=1)

        self._cb_a_server_key = ttk.Combobox(
            server_key_row, textvariable=self._v_a_server_key, state="readonly"
        )
        self._cb_a_server_key.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._btn_scan_server = ttk.Button(
            server_key_row, text="Escanear servidor",
            command=self._action_scan_server_keys,
        )
        self._btn_scan_server.grid(row=0, column=1)

        self._lbl_a_server_key_hint = ttk.Label(
            self._frm_a_server,
            text="  Presione 'Escanear servidor' para listar las llaves disponibles.",
            font=("Segoe UI", 8), foreground="#888888",
        )
        self._lbl_a_server_key_hint.grid(row=1, column=0, columnspan=2, sticky="w")

        ttk.Separator(sec3, orient="horizontal").grid(
            row=2, column=0, sticky="ew", pady=6
        )

        # ── Priority 2: Local key ─────────────────────────────────────────
        p2_frame = ttk.Frame(sec3)
        p2_frame.grid(row=3, column=0, sticky="ew", pady=(0, 2))
        p2_frame.columnconfigure(1, weight=1)

        ttk.Radiobutton(
            p2_frame,
            text="Llave de esta maquina  (se sube temporalmente al servidor)",
            variable=self._v_a_key_source, value="local",
            command=self._toggle_a_key_source,
        ).grid(row=0, column=0, columnspan=3, sticky="w")

        self._frm_a_local = ttk.Frame(p2_frame)
        self._frm_a_local.grid(row=1, column=0, columnspan=3, sticky="ew", padx=(20, 0))
        self._frm_a_local.columnconfigure(1, weight=1)

        ttk.Label(self._frm_a_local, text="Llave local:").grid(
            row=0, column=0, sticky="e", padx=(0, _PAD), pady=3
        )
        local_key_row = ttk.Frame(self._frm_a_local)
        local_key_row.grid(row=0, column=1, sticky="ew", pady=3)
        local_key_row.columnconfigure(0, weight=1)

        self._cb_a_ssh_key = ttk.Combobox(local_key_row, textvariable=self._v_a_ssh_key)
        self._cb_a_ssh_key.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(
            local_key_row, text="Examinar...", command=self._browse_ssh_key
        ).grid(row=0, column=1)

        self._lbl_a_passphrase = tk.Label(
            self._frm_a_local,
            text="  Sin llave seleccionada",
            font=("Segoe UI", 8), bg=_C_BG, fg="#AAAAAA",
        )
        self._lbl_a_passphrase.grid(row=1, column=0, columnspan=2, sticky="w")

        # Populate combo with local ~/.ssh/ keys and hook up passphrase indicator
        self._refresh_ssh_keys()
        self._cb_a_ssh_key.bind("<<ComboboxSelected>>", self._on_a_key_changed)
        self._v_a_ssh_key.trace_add("write", lambda *_: self._on_a_key_changed(None))

        ttk.Separator(sec3, orient="horizontal").grid(
            row=4, column=0, sticky="ew", pady=6
        )

        # ── Priority 3: Generate new key ──────────────────────────────────
        p3_frame = ttk.Frame(sec3)
        p3_frame.grid(row=5, column=0, sticky="ew", pady=(0, 2))
        p3_frame.columnconfigure(1, weight=1)

        ttk.Radiobutton(
            p3_frame,
            text="Generar nueva llave Ed25519 en esta maquina",
            variable=self._v_a_key_source, value="generate",
            command=self._toggle_a_key_source,
        ).grid(row=0, column=0, columnspan=3, sticky="w")

        self._frm_a_generate = ttk.Frame(p3_frame)
        self._frm_a_generate.grid(row=1, column=0, columnspan=3, sticky="ew", padx=(20, 0))
        self._frm_a_generate.columnconfigure(1, weight=1)

        ttk.Label(self._frm_a_generate, text="Nombre del archivo:").grid(
            row=0, column=0, sticky="e", padx=(0, _PAD), pady=3
        )
        gen_row = ttk.Frame(self._frm_a_generate)
        gen_row.grid(row=0, column=1, sticky="ew", pady=3)
        gen_row.columnconfigure(0, weight=1)

        ttk.Entry(gen_row, textvariable=self._v_a_gen_key_name).grid(
            row=0, column=0, sticky="ew", padx=(0, 4)
        )
        ttk.Button(
            gen_row, text="Generar llave", command=self._action_generate_key
        ).grid(row=0, column=1)

        ttk.Label(self._frm_a_generate, text="Llave publica:").grid(
            row=1, column=0, sticky="ne", padx=(0, _PAD), pady=3
        )
        pub_frame = ttk.Frame(self._frm_a_generate)
        pub_frame.grid(row=1, column=1, sticky="ew", pady=3)
        pub_frame.columnconfigure(0, weight=1)

        self._txt_a_pub_key = tk.Text(
            pub_frame, height=3, wrap="word", state="disabled",
            font=("Courier New", 8), bg="#1E1E2E", fg="#A8D8A8",
        )
        self._txt_a_pub_key.grid(row=0, column=0, sticky="ew")

        self._btn_copy_pub_key = ttk.Button(
            pub_frame, text="Copiar", command=self._copy_pub_key, state="disabled"
        )
        self._btn_copy_pub_key.grid(row=1, column=0, sticky="e", pady=(2, 0))

        ttk.Label(
            self._frm_a_generate,
            text="  Registre esta llave publica en GitHub / GitLab como Deploy Key "
                 "antes de sincronizar.",
            font=("Segoe UI", 8), foreground="#E67E22", wraplength=480, justify="left",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(2, 0))

        # Apply initial visibility for all three sub-panels
        self._toggle_a_key_source()

        # ── Seccion 4: Opciones ───────────────────────────────────────────
        sec4 = ttk.LabelFrame(f, text="4. Opciones", padding=_PAD)
        sec4.grid(row=row, column=0, sticky="ew", pady=(0, _PAD))
        sec4.columnconfigure(0, minsize=130)
        sec4.columnconfigure(1, weight=1)
        row += 1

        ttk.Checkbutton(
            sec4,
            text="Reiniciar servicio Odoo al terminar la sincronizacion",
            variable=self._v_a_restart,
            command=self._toggle_addons_restart,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))

        ttk.Label(sec4, text="Nombre del servicio:").grid(
            row=1, column=0, sticky="e", padx=(0, _PAD), pady=3
        )
        svc_row = ttk.Frame(sec4)
        svc_row.grid(row=1, column=1, sticky="ew", pady=3)
        svc_row.columnconfigure(0, weight=1)

        self._e_a_service = ttk.Entry(svc_row, textvariable=self._v_a_service, state="disabled")
        self._e_a_service.grid(row=0, column=0, sticky="ew", padx=(0, 4))

        self._btn_detect_svc = ttk.Button(
            svc_row, text="Auto-detectar", state="disabled",
            command=self._action_detect_service,
        )
        self._btn_detect_svc.grid(row=0, column=1)

        # ── Seccion 5: Progreso y ejecucion ──────────────────────────────
        sec5 = ttk.LabelFrame(f, text="5. Ejecucion", padding=_PAD)
        sec5.grid(row=row, column=0, sticky="ew", pady=(0, _PAD))
        sec5.columnconfigure(0, weight=1)
        row += 1

        self._v_a_progress = tk.DoubleVar()
        self._a_progressbar = ttk.Progressbar(
            sec5, variable=self._v_a_progress, maximum=100
        )
        self._a_progressbar.grid(row=0, column=0, sticky="ew", pady=(0, 4))

        self._lbl_a_progress = ttk.Label(sec5, text="")
        self._lbl_a_progress.grid(row=1, column=0)

        btn_row_a = ttk.Frame(sec5)
        btn_row_a.grid(row=2, column=0, pady=_PAD)
        self._btn_sync_addons = ttk.Button(
            btn_row_a, text="Sincronizar Addons",
            style="Primary.TButton", command=self._action_sync_addons,
        )
        self._btn_sync_addons.pack(side="left", padx=6)
        self._btn_stop_addons = ttk.Button(
            btn_row_a, text="Detener", state="disabled",
            style="Stop.TButton", command=self._action_stop,
        )
        self._btn_stop_addons.pack(side="left", padx=6)

        # Apply initial state for the restart option
        self._toggle_addons_restart()

    # ── Addons: helpers ───────────────────────────────────────────────────

    def _toggle_a_key_source(self) -> None:
        """Show the relevant sub-panel and grey out the other two."""
        source = self._v_a_key_source.get()

        # Enable / disable all children inside each sub-frame
        def _set_state(frame: ttk.Frame, enabled: bool) -> None:
            state = "normal" if enabled else "disabled"
            for child in frame.winfo_children():
                try:
                    child.configure(state=state)
                except tk.TclError:
                    pass
                # Recurse into nested frames
                if isinstance(child, (ttk.Frame, tk.Frame)):
                    _set_state(child, enabled)

        _set_state(self._frm_a_server,   source == "server")
        _set_state(self._frm_a_local,    source == "local")
        _set_state(self._frm_a_generate, source == "generate")

        # The public key Text widget needs special handling (state=disabled = read-only)
        if source == "generate" and self._v_a_pub_key_text.get():
            self._txt_a_pub_key.configure(state="disabled")
            self._btn_copy_pub_key.configure(state="normal")

    def _action_scan_server_keys(self) -> None:
        """Async: scan the destination server's ~/.ssh/ for private keys."""
        try:
            ssh = self._get_addons_ssh()
        except RuntimeError as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return

        self._btn_scan_server.config(state="disabled")
        self._lbl_a_server_key_hint.config(
            text="  Escaneando servidor...", foreground="#E67E22"
        )

        def _worker() -> None:
            try:
                mgr = AddonsManager(ssh)
                keys = mgr.scan_server_ssh_keys()
                self._q.put(("addons_server_keys", keys))
            except Exception as exc:
                self._q.put(("addons_server_keys", []))
                self._log(f"Error escaneando llaves del servidor: {exc}")

        threading.Thread(target=_worker, daemon=True).start()

    def _action_generate_key(self) -> None:
        """Generate a new Ed25519 key pair on the local machine."""
        from core.addons_manager import generate_local_key

        name = self._v_a_gen_key_name.get().strip()
        if not name:
            messagebox.showwarning(APP_TITLE, "Ingrese un nombre para la llave.")
            return

        try:
            private_path, pub_str = generate_local_key(name)
        except FileExistsError as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        except Exception as exc:
            messagebox.showerror(APP_TITLE, f"Error generando llave:\n{exc}")
            return

        # Show public key in the text widget
        self._v_a_pub_key_text.set(pub_str)
        self._txt_a_pub_key.configure(state="normal")
        self._txt_a_pub_key.delete("1.0", "end")
        self._txt_a_pub_key.insert("1.0", pub_str)
        self._txt_a_pub_key.configure(state="disabled")
        self._btn_copy_pub_key.configure(state="normal")

        # Auto-switch to "local" mode so the user can sync right after registering
        self._v_a_ssh_key.set(private_path)
        current = list(self._cb_a_ssh_key["values"])
        if private_path not in current:
            current.insert(0, private_path)
            self._cb_a_ssh_key["values"] = current

        messagebox.showinfo(
            APP_TITLE,
            f"Llave generada en:\n  {private_path}\n\n"
            "La llave publica ya aparece en el recuadro.\n"
            "Registrela en GitHub / GitLab como Deploy Key y luego "
            "seleccione el modo 'Llave de esta maquina' para sincronizar.",
        )

    def _copy_pub_key(self) -> None:
        """Copy the generated public key to the clipboard."""
        pub = self._v_a_pub_key_text.get()
        if pub:
            self.root.clipboard_clear()
            self.root.clipboard_append(pub)
            messagebox.showinfo(APP_TITLE, "Llave publica copiada al portapapeles.")

    def _refresh_ssh_keys(self) -> None:
        """Populate the local SSH key combobox from keys found in ~/.ssh/."""
        keys = scan_ssh_directory()
        self._cb_a_ssh_key["values"] = keys
        if keys and not self._v_a_ssh_key.get():
            self._v_a_ssh_key.set(keys[0])

    def _on_a_key_changed(self, _event) -> None:
        """Update the passphrase status indicator when the selected key changes."""
        path = self._v_a_ssh_key.get().strip()
        if not path:
            self._lbl_a_passphrase.config(
                text="  Sin llave seleccionada", fg="#AAAAAA"
            )
            return

        if path in self._key_passphrases:
            self._lbl_a_passphrase.config(
                text=f"  \U0001f513 Passphrase en cache para esta llave", fg="#2E8B57"
            )
        else:
            try:
                from core.addons_manager import key_needs_passphrase
                needs = key_needs_passphrase(path)
            except Exception:
                needs = False

            if needs:
                self._lbl_a_passphrase.config(
                    text="  \U0001f512 Esta llave requiere passphrase  (se pedira antes de sincronizar)",
                    fg="#E67E22",
                )
            else:
                self._lbl_a_passphrase.config(
                    text="  Sin passphrase requerida", fg="#2E8B57"
                )

    def _browse_ssh_key(self) -> None:
        """Open a file dialog starting at ~/.ssh/ to pick a private key."""
        ssh_dir = os.path.expanduser("~/.ssh")
        path = filedialog.askopenfilename(
            title="Seleccionar llave SSH privada",
            initialdir=ssh_dir if os.path.isdir(ssh_dir) else os.path.expanduser("~"),
            filetypes=[("Llaves SSH", "*"), ("Todos", "*.*")],
        )
        if path:
            self._v_a_ssh_key.set(path)
            # Add to combo if not already present
            current = list(self._cb_a_ssh_key["values"])
            if path not in current:
                current.insert(0, path)
                self._cb_a_ssh_key["values"] = current

    def _toggle_addons_restart(self) -> None:
        """Enable/disable the service name entry and auto-detect button."""
        state = "normal" if self._v_a_restart.get() else "disabled"
        self._e_a_service.config(state=state)
        self._btn_detect_svc.config(state=state)

    def _get_key_passphrase(self, key_path: str) -> str | None:
        """
        Return the passphrase for key_path.

        Checks the session cache first; if not found, asks the user once
        via a simple dialog and caches the result for the rest of the session.
        Returns None if the key needs no passphrase.
        """
        from core.addons_manager import key_needs_passphrase

        if key_path in self._key_passphrases:
            return self._key_passphrases[key_path]

        try:
            needs = key_needs_passphrase(key_path)
        except Exception:
            needs = False

        if not needs:
            return None

        passphrase = simpledialog.askstring(
            "Passphrase de llave SSH",
            f"Ingrese la passphrase para:\n{os.path.basename(key_path)}\n\n"
            "(Solo se pide una vez por sesion)",
            show="*",
            parent=self.root,
        )
        if passphrase is None:
            raise RuntimeError("Se cancelo el ingreso de passphrase. Operacion abortada.")

        # Cache for the rest of this session — never written to disk
        self._key_passphrases[key_path] = passphrase
        return passphrase

    def _get_addons_ssh(self) -> SSHClient:
        """Return the SSH client for the selected addons-sync server."""
        sel = self._v_a_conn_type.get()
        if sel == "origin":
            if not self._ssh.connected:
                raise RuntimeError(
                    "No hay conexion activa con el servidor origen (Paso 1)."
                )
            return self._ssh
        # "restore" — use the restore Tab-6 connection
        conn_type = self._v_r_conn_type.get()
        try:
            return self._get_restore_ssh()
        except RuntimeError:
            raise RuntimeError(
                "No hay conexion activa con el servidor de restauracion (Tab 6)."
            )

    # ── Addons: actions ───────────────────────────────────────────────────

    def _action_sync_addons(self) -> None:
        """Validate inputs and launch the addons sync worker."""
        repo_url   = self._v_a_repo_url.get().strip()
        branch     = self._v_a_branch.get().strip()
        target     = self._v_a_target.get().strip()
        key_source = self._v_a_key_source.get()

        if not repo_url:
            messagebox.showwarning(APP_TITLE, "Ingrese la URL SSH del repositorio.")
            return
        if not branch:
            messagebox.showwarning(APP_TITLE, "Ingrese el nombre de la rama (branch).")
            return
        if not target:
            messagebox.showwarning(APP_TITLE, "Ingrese la ruta destino en el servidor.")
            return

        # Validate the selected key source
        server_key = None
        local_key  = None
        passphrase = None

        if key_source == "server":
            server_key = self._v_a_server_key.get().strip()
            if not server_key:
                messagebox.showwarning(
                    APP_TITLE,
                    "Escanee el servidor y seleccione una llave en el Paso 3\n"
                    "o cambie a uno de los otros modos de llave SSH.",
                )
                return

        elif key_source == "local":
            local_key = self._v_a_ssh_key.get().strip()
            if not local_key:
                messagebox.showwarning(
                    APP_TITLE,
                    "Seleccione una llave SSH local para autenticar con GitHub / GitLab."
                )
                return
            if not os.path.isfile(local_key):
                messagebox.showerror(
                    APP_TITLE, f"El archivo de llave SSH no existe:\n{local_key}"
                )
                return
            # Ask for passphrase now in GUI thread so it's cached before we go async
            try:
                passphrase = self._get_key_passphrase(local_key)
            except RuntimeError as exc:
                messagebox.showwarning(APP_TITLE, str(exc))
                return

        elif key_source == "generate":
            messagebox.showwarning(
                APP_TITLE,
                "Genere la llave con el boton 'Generar llave', regstrela en GitHub / GitLab\n"
                "y luego seleccione el modo 'Llave de esta maquina' para sincronizar.",
            )
            return

        try:
            ssh = self._get_addons_ssh()
        except RuntimeError as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return

        if not self._check_ssh_alive(ssh, "addons"):
            return

        self._btn_sync_addons.config(state="disabled")
        self._v_a_progress.set(0)
        self._lbl_a_progress.config(text="")
        self._begin_operation()

        params = {
            "repo_url":       repo_url,
            "branch":         branch,
            "target":         target,
            "odoo_user":      self._v_a_odoo_user.get().strip() or "odoo",
            "key_source":     key_source,      # "server" | "local"
            "server_key":     server_key,      # remote path  (server mode)
            "local_key":      local_key,       # local path   (local mode)
            "passphrase":     passphrase,      # local passphrase (local mode)
            "use_submodules": self._v_a_submodules.get(),
            "restart":        self._v_a_restart.get(),
            "service":        self._v_a_service.get().strip(),
        }
        threading.Thread(
            target=self._worker_addons, args=(params, ssh), daemon=True
        ).start()

    def _worker_addons(self, p: dict, ssh: SSHClient) -> None:
        """
        Background thread: ensure git, prepare SSH key wrapper, sync repo,
        cleanup, optional restart.

        Handles all three key-source modes:
          server   — write wrapper pointing to existing server key; no upload
          local    — upload passphrase-free copy; delete after sync
        """
        mgr = AddonsManager(ssh)
        # Track whether we uploaded a key file (True) or only wrote a wrapper (False)
        key_uploaded = False
        wrapper_written = False

        try:
            # Step 1: verify git on the server
            self._q.put(("addons_progress", (10, "Verificando git en el servidor...")))
            mgr.ensure_git(log_callback=self._log)

            # Step 2: prepare the GIT_SSH key/wrapper
            if p["key_source"] == "server":
                self._q.put(("addons_progress", (20, "Preparando wrapper con llave del servidor...")))
                mgr.write_server_key_wrapper(
                    p["server_key"], log_callback=self._log
                )
                wrapper_written = True

            elif p["key_source"] == "local":
                self._q.put(("addons_progress", (20, "Subiendo llave SSH temporal...")))
                mgr.upload_key(
                    p["local_key"],
                    passphrase=p["passphrase"],
                    log_callback=self._log,
                )
                key_uploaded = True
                wrapper_written = True

            # Step 3: clone or pull (with optional submodule sequence)
            label = (
                "Sincronizando repositorio y submódulos..."
                if p["use_submodules"]
                else "Sincronizando repositorio..."
            )
            self._q.put(("addons_progress", (35, label)))
            is_clone = mgr.sync(
                repo_url=p["repo_url"],
                branch=p["branch"],
                target_path=p["target"],
                odoo_user=p["odoo_user"],
                use_submodules=p["use_submodules"],
                log_callback=self._log,
                cancel_event=self._cancel_event,
            )
            self._q.put(("addons_progress", (80, "Repositorio sincronizado.")))

            # Step 4: optional service restart
            if p["restart"] and p["service"]:
                self._q.put(("addons_progress", (90, f"Reiniciando {p['service']}...")))
                mgr.restart_odoo(p["service"], log_callback=self._log)

            action = "clonado" if is_clone else "actualizado"
            self._q.put(("addons_done",
                f"Repositorio {action} correctamente en {p['target']}."))

        except RuntimeError as exc:
            if str(exc) == "__CANCELLED__":
                self._q.put(("addons_cancelled", "Sincronizacion detenida por el usuario."))
            else:
                self._q.put(("error", f"Error sincronizando addons:\n{exc}"))
                self._q.put(("btn_addons_enable", None))
        except Exception as exc:
            self._q.put(("error", f"Error sincronizando addons:\n{exc}"))
            self._q.put(("btn_addons_enable", None))
        finally:
            # Remove temp files — only delete the key file if we uploaded one
            if wrapper_written:
                mgr.cleanup_key(uploaded=key_uploaded)

    def _action_detect_service(self) -> None:
        """Auto-detect the Odoo systemd service name on the selected server."""
        try:
            ssh = self._get_addons_ssh()
        except RuntimeError as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return

        self._btn_detect_svc.config(state="disabled")
        self._append_log("Detectando servicio Odoo en el servidor...")

        def _run() -> None:
            try:
                name = AddonsManager(ssh).detect_odoo_service()
                if name:
                    self._q.put(("addons_service", name))
                else:
                    self._q.put(("log", "No se detecto un servicio Odoo activo. Ingreselo manualmente."))
            except Exception as exc:
                self._q.put(("log", f"Error detectando servicio: {exc}"))
            finally:
                self.root.after(0, lambda: self._btn_detect_svc.config(
                    state="normal" if self._v_a_restart.get() else "disabled"
                ))

        threading.Thread(target=_run, daemon=True).start()

    # ── Tab 8: Remote File Explorer (FileZilla-style) ───────────────────

    def _tab_explorer(self) -> None:
        """
        Build the dual-panel remote filesystem explorer (Tab 8).

        Uses FileBrowserPanel for each side, providing FileZilla-level
        navigation: breadcrumbs, history, context menu, multi-select,
        rename, chmod, cross-panel transfer, sortable columns, hidden
        files toggle, and keyboard shortcuts.
        """
        # Tab 8 does NOT use _scrollable_tab because the panels manage
        # their own internal scroll — wrapping in a scrollable canvas
        # would conflict with the Treeview scrollbars inside each panel.
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="  8. Explorador  ")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)

        # ── SSH getters — each panel resolves its own connection ─────────
        def _get_ssh_left() -> "SSHClient | None":
            """Left panel always uses the origin server (Tab 1)."""
            return self._ssh if self._ssh.connected else None

        def _get_ssh_right() -> "SSHClient | None":
            """Right panel: Servidor B (Tab 1) > Tab 4 remote dest."""
            if self._ssh_restore.connected:
                return self._ssh_restore
            if self._ssh_dest.connected:
                return self._ssh_dest
            return None

        # ── Status bar shared between both panels ────────────────────────
        status_bar = ttk.Frame(tab)
        status_bar.grid(row=1, column=0, sticky="ew", pady=(4, 2))
        lbl_status = ttk.Label(
            status_bar,
            text="Los paneles se conectan automaticamente al entrar a esta pestana.",
            foreground="#888888", font=("Segoe UI", 8),
        )
        lbl_status.pack(side="left", padx=6)

        def _on_status(msg: str) -> None:
            self.root.after(0, lambda: lbl_status.config(text=msg))

        # ── Two panels in a resizable PanedWindow ────────────────────────
        # Reconectar vive en el encabezado de cada FileBrowserPanel
        paned = ttk.PanedWindow(tab, orient="horizontal")
        paned.grid(row=0, column=0, sticky="nsew", padx=6, pady=(4, 0))

        panel_l = FileBrowserPanel(
            paned,
            side="l",
            get_ssh=_get_ssh_left,
            title="Servidor A — Origen  (Tab 1)",
            on_status=_on_status,
        )
        panel_r = FileBrowserPanel(
            paned,
            side="r",
            get_ssh=_get_ssh_right,
            title="Servidor B — Receptor  (Tab 1 / Tab 4)",
            on_status=_on_status,
        )

        # Wire panels as peers so cross-panel transfer works
        panel_l.set_peer(panel_r)
        panel_r.set_peer(panel_l)

        paned.add(panel_l, weight=1)
        paned.add(panel_r, weight=1)

        # Save references so _auto_connect_explorer can reach them
        self._panel_l = panel_l
        self._panel_r = panel_r

    # ── Tab 9: SSH Terminals ─────────────────────────────────────────────

    # ── Tab 10: Trial — reseteo de parametros de licencia Odoo ──────────

    def _tab_trial(self) -> None:
        """
        Tab Trial: genera o crea valores frescos de licencia Odoo (database.*)
        y los aplica a una BD objetivo eliminando las fechas de vencimiento.

        Usa _scrollable_tab y self._log como los demas tabs.
        """
        f = self._scrollable_tab("  Trial  ")
        f.columnconfigure(0, weight=1)

        # Estado interno: parametros fuente acumulados entre pasos
        _source_params: list[dict] = []

        def _get_ssh():
            """Retorna el SSH del servidor de restauracion/destino."""
            if self._ssh_restore.connected:
                return self._ssh_restore
            if self._ssh_dest.connected:
                return self._ssh_dest
            return None

        # ══════════════════════════════════════════════════════════════════
        # SECCION: Servidor
        # ══════════════════════════════════════════════════════════════════
        lf_srv = ttk.LabelFrame(f, text="Servidor", padding=_PAD)
        lf_srv.grid(row=0, column=0, sticky="ew", pady=(0, _PAD))
        lf_srv.columnconfigure(1, weight=1)

        lbl_srv = ttk.Label(
            lf_srv,
            text="Sin conexion — conectese en Tab 4 (Destino) o Tab 6 (Restaurar).",
            foreground="#CC4444",
        )
        lbl_srv.grid(row=0, column=0, sticky="w")

        def _refresh_srv(*_):
            ssh = _get_ssh()
            if ssh:
                lbl_srv.config(
                    text=f"Conectado: {ssh.host}:{ssh.port}",
                    foreground="#007B00",
                )
            else:
                lbl_srv.config(
                    text="Sin conexion — conectese en Tab 4 (Destino) o Tab 6 (Restaurar).",
                    foreground="#CC4444",
                )

        # Save reference so _auto_refresh_trial can call it on tab entry
        self._trial_refresh_srv = _refresh_srv
        ttk.Button(lf_srv, text="Refrescar", command=_refresh_srv).grid(
            row=0, column=1, sticky="w", padx=(8, 0))

        def _test_conn():
            ssh = _get_ssh()
            if not ssh:
                self._log("[ERROR] Trial: sin conexion SSH (Tab 4 o Tab 6).")
                return
            self._log(f"Trial — probando conexion con {ssh.host} ...")
            def _run():
                try:
                    _, out, _ = ssh.execute("hostname && id")
                    self._log(f"  OK: {out}")
                except Exception as exc:
                    self._log(f"[ERROR] Trial — conexion: {exc}")
            threading.Thread(target=_run, daemon=True).start()

        ttk.Button(lf_srv, text="Probar conexion", command=_test_conn).grid(
            row=0, column=2, padx=(4, 0))

        _refresh_srv()

        # ══════════════════════════════════════════════════════════════════
        # PASO 1 — Fuente de valores
        # ══════════════════════════════════════════════════════════════════
        lf_src = ttk.LabelFrame(
            f,
            text="Paso 1 — Obtener valores fuente (database.*)",
            padding=_PAD,
        )
        lf_src.grid(row=1, column=0, sticky="ew", pady=(0, _PAD))
        lf_src.columnconfigure(1, weight=1)

        # ── Opcion A: generar UUIDs en Python (rapido, sin BD) ───────────
        lf_quick = ttk.LabelFrame(
            lf_src,
            text="Opcion A — Generar valores frescos (rapido, sin BD)",
            padding=6,
        )
        lf_quick.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        lf_quick.columnconfigure(1, weight=1)

        ttk.Label(
            lf_quick,
            text="Genera UUID4 y fecha actual sin necesidad del binario de Odoo.",
            foreground="#666666", font=("Segoe UI", 8),
        ).grid(row=0, column=0, sticky="w", padx=(0, 12))

        def _gen_quick():
            import uuid as _uuid
            import datetime as _dt
            now   = _dt.datetime.now()
            cdate = now.strftime("%Y-%m-%d %H:%M:%S.%f")
            params = [
                {"key": "database.secret",
                 "value": str(_uuid.uuid4()), "create_date": cdate, "write_date": cdate},
                {"key": "database.uuid",
                 "value": str(_uuid.uuid4()), "create_date": cdate, "write_date": cdate},
                {"key": "database.create_date",
                 "value": now.strftime("%Y-%m-%d %H:%M:%S"),
                 "create_date": cdate, "write_date": cdate},
            ]
            nonlocal _source_params
            _source_params = params
            _populate_src_table(params)
            self._log("Trial — valores frescos generados (Opcion A):")
            for p in params:
                self._log(f"  {p['key']:<28} {p['value']}")

        ttk.Button(
            lf_quick, text="Generar valores frescos",
            style="Primary.TButton", command=_gen_quick,
        ).grid(row=0, column=1, sticky="w")

        # ── Opcion B: BD Odoo completa ────────────────────────────────────
        lf_full = ttk.LabelFrame(
            lf_src,
            text="Opcion B — Crear BD Odoo limpia (requiere binario Odoo, 2-5 min)",
            padding=6,
        )
        lf_full.grid(row=1, column=0, columnspan=3, sticky="ew")
        lf_full.columnconfigure(1, weight=1)

        ttk.Label(lf_full, text="Binario:").grid(
            row=0, column=0, sticky="w", padx=(0, _PAD))
        v_bin = tk.StringVar()
        ttk.Entry(lf_full, textvariable=v_bin).grid(
            row=0, column=1, sticky="ew", padx=(0, _PAD))

        def _detect_odoo():
            ssh = _get_ssh()
            if not ssh:
                self._log("[ERROR] Trial — Detectar Odoo: sin conexion SSH.")
                return
            self._log("Trial — detectando Odoo en el servidor ...")
            def _run():
                try:
                    b, c = TrialManager(ssh).find_odoo()
                    def _fill():
                        v_bin.set(b or "")
                        v_conf.set(c or "")
                        self._log(f"  Binario: {b or '(no encontrado)'}")
                        self._log(f"  Config:  {c or '(no encontrado)'}")
                    self.root.after(0, _fill)
                except Exception as exc:
                    self.root.after(0, lambda: self._log(f"[ERROR] Trial — detectar: {exc}"))
            threading.Thread(target=_run, daemon=True).start()

        ttk.Button(lf_full, text="Detectar Odoo", command=_detect_odoo).grid(
            row=0, column=2)

        ttk.Label(lf_full, text="Config:").grid(
            row=1, column=0, sticky="w", padx=(0, _PAD), pady=(4, 0))
        v_conf = tk.StringVar()
        ttk.Entry(lf_full, textvariable=v_conf).grid(
            row=1, column=1, sticky="ew", padx=(0, _PAD), pady=(4, 0))

        ttk.Label(lf_full, text="Nombre BD:").grid(
            row=2, column=0, sticky="w", padx=(0, _PAD), pady=(4, 0))
        v_tmp = tk.StringVar(value="obt_trial_tmp")
        ttk.Entry(lf_full, textvariable=v_tmp).grid(
            row=2, column=1, sticky="ew", padx=(0, _PAD), pady=(4, 0))

        bb_full = ttk.Frame(lf_full)
        bb_full.grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        def _create_db():
            ssh = _get_ssh()
            if not ssh:
                self._log("[ERROR] Trial — sin conexion SSH.")
                return
            b, c, dn = v_bin.get().strip(), v_conf.get().strip(), v_tmp.get().strip()
            if not b or not c or not dn:
                self._log("[ERROR] Trial — complete Binario, Config y Nombre BD.")
                return
            btn_cdb.config(state="disabled")
            self._log(f"Trial === Creando BD '{dn}' con -i base ===")
            def _run():
                try:
                    mgr = TrialManager(ssh)
                    mgr.create_clean_db(dn, b, c, log_callback=self._log)
                    params = mgr.query_db_params(dn)
                    nonlocal _source_params
                    _source_params = params
                    self.root.after(0, lambda: _populate_src_table(params))
                    self._log(f"Trial === BD '{dn}' lista. {len(params)} parametros obtenidos ===")
                    for p in params:
                        self._log(f"  {p['key']:<28} {p['value']}")
                except Exception as exc:
                    self.root.after(0, lambda: self._log(f"[ERROR] Trial — crear BD: {exc}"))
                finally:
                    self.root.after(0, lambda: btn_cdb.config(state="normal"))
            threading.Thread(target=_run, daemon=True).start()

        def _drop_db():
            ssh = _get_ssh()
            dn  = v_tmp.get().strip()
            if not ssh or not dn:
                return
            if not messagebox.askyesno("Confirmar", f"Eliminar BD '{dn}'?", icon="warning"):
                return
            def _run():
                TrialManager(ssh).drop_db(dn)
                self.root.after(0, lambda: self._log(f"Trial — BD '{dn}' eliminada."))
            threading.Thread(target=_run, daemon=True).start()

        btn_cdb = ttk.Button(
            bb_full, text="Crear BD Odoo limpia",
            style="Primary.TButton", command=_create_db,
        )
        btn_cdb.pack(side="left", padx=(0, 6))
        ttk.Button(
            bb_full, text="Eliminar BD tmp",
            style="Stop.TButton", command=_drop_db,
        ).pack(side="left")

        # ── Tabla de valores fuente (comun a ambas opciones) ──────────────
        ttk.Label(lf_src, text="Valores fuente obtenidos:",
                  font=("Segoe UI", 8)).grid(
            row=2, column=0, columnspan=3, sticky="w", pady=(8, 2))

        cols_p = ("key", "value", "create_date", "write_date")
        tree_src = ttk.Treeview(lf_src, columns=cols_p, show="headings", height=4)
        for col, hdr, w, stretch in [
            ("key", "Clave", 200, False),
            ("value", "Valor", 260, True),
            ("create_date", "create_date", 160, False),
            ("write_date", "write_date", 160, False),
        ]:
            tree_src.heading(col, text=hdr)
            tree_src.column(col, width=w, stretch=stretch)
        tree_src.grid(row=3, column=0, columnspan=3, sticky="ew")

        def _populate_src_table(params: list[dict]) -> None:
            tree_src.delete(*tree_src.get_children())
            for p in params:
                tree_src.insert("", "end",
                    values=(p["key"], p["value"], p["create_date"], p["write_date"]))

        # ══════════════════════════════════════════════════════════════════
        # PASO 2 — BD objetivo
        # ══════════════════════════════════════════════════════════════════
        lf_tgt = ttk.LabelFrame(
            f,
            text="Paso 2 — BD objetivo (aplicar valores Trial)",
            padding=_PAD,
        )
        lf_tgt.grid(row=2, column=0, sticky="ew", pady=(0, _PAD))
        lf_tgt.columnconfigure(0, weight=1)

        def _list_dbs():
            ssh = _get_ssh()
            if not ssh:
                self._log("[ERROR] Trial — sin conexion SSH.")
                return
            self._log("Trial — listando bases de datos ...")
            def _run():
                try:
                    dbs = TrialManager(ssh).list_databases()
                    def _fill():
                        lb_dbs.delete(0, "end")
                        for d in dbs:
                            lb_dbs.insert("end", d)
                        self._log(f"  {len(dbs)} base(s) encontrada(s).")
                    self.root.after(0, _fill)
                except Exception as exc:
                    self.root.after(0, lambda: self._log(f"[ERROR] Trial — listar: {exc}"))
            threading.Thread(target=_run, daemon=True).start()

        ttk.Button(lf_tgt, text="Listar BDs", command=_list_dbs).grid(
            row=0, column=0, sticky="w", pady=(0, 4))

        lb_frame = ttk.Frame(lf_tgt)
        lb_frame.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        lb_frame.columnconfigure(0, weight=1)

        lb_dbs = tk.Listbox(
            lb_frame, height=7, selectmode="single",
            font=("Consolas", 9),
            bg="#FAFAFA", fg="#2D2D2D",
            selectbackground="#714B67", selectforeground="#FFFFFF",
            relief="solid", borderwidth=1,
        )
        lb_vsb = ttk.Scrollbar(lb_frame, orient="vertical", command=lb_dbs.yview)
        lb_dbs.config(yscrollcommand=lb_vsb.set)
        lb_dbs.grid(row=0, column=0, sticky="ew")
        lb_vsb.grid(row=0, column=1, sticky="ns")

        v_sel = tk.StringVar(value="(ninguna seleccionada)")
        ttk.Label(lf_tgt, textvariable=v_sel,
                  font=("Segoe UI", 9, "bold")).grid(
            row=2, column=0, sticky="w", pady=(0, 6))

        lb_dbs.bind("<<ListboxSelect>>", lambda e: v_sel.set(
            f"BD objetivo: {lb_dbs.get(lb_dbs.curselection()[0])}"
            if lb_dbs.curselection() else "(ninguna seleccionada)"))

        bb_tgt = ttk.Frame(lf_tgt)
        bb_tgt.grid(row=3, column=0, sticky="w", pady=(0, 4))

        def _apply():
            ssh = _get_ssh()
            if not ssh:
                self._log("[ERROR] Trial — sin conexion SSH.")
                return
            sel = lb_dbs.curselection()
            if not sel:
                self._log("[ERROR] Trial — seleccione una BD objetivo.")
                return
            target = lb_dbs.get(sel[0])
            if not _source_params:
                self._log("[ERROR] Trial — genere los valores fuente en Paso 1 primero.")
                return
            if not messagebox.askyesno(
                "Confirmar",
                f"Aplicar valores Trial en '{target}'?\n\n"
                "  Actualiza: database.secret, database.uuid, database.create_date\n"
                "  Elimina:   database.expiration_date, database.expiration_reason\n\n"
                "Esta accion NO se puede deshacer.",
                icon="warning",
            ):
                return
            btn_apply.config(state="disabled")
            self._log(f"Trial === Aplicando en '{target}' ===")
            def _run():
                try:
                    TrialManager(ssh).apply_trial_params(
                        target, _source_params, log_callback=self._log)
                    self._log("Trial === Aplicacion completada. Verificando... ===")
                    result = TrialManager(ssh).verify_target_params(target)
                    for p in result:
                        self._log(f"  {p['key']:<28} {p['value']}")
                    self.root.after(0, lambda: _populate_verify_table(result))
                except Exception as exc:
                    self.root.after(0, lambda: self._log(f"[ERROR] Trial — aplicar: {exc}"))
                finally:
                    self.root.after(0, lambda: btn_apply.config(state="normal"))
            threading.Thread(target=_run, daemon=True).start()

        def _verify():
            ssh = _get_ssh()
            if not ssh:
                self._log("[ERROR] Trial — sin conexion SSH.")
                return
            sel = lb_dbs.curselection()
            if not sel:
                self._log("[ERROR] Trial — seleccione una BD objetivo.")
                return
            target = lb_dbs.get(sel[0])
            self._log(f"Trial — consultando '{target}' ...")
            def _run():
                try:
                    result = TrialManager(ssh).verify_target_params(target)
                    self._log(f"  Parametros database.* en '{target}':")
                    for p in result:
                        self._log(f"  {p['key']:<28} {p['value']}")
                    self.root.after(0, lambda: _populate_verify_table(result))
                except Exception as exc:
                    self.root.after(0, lambda: self._log(f"[ERROR] Trial — consultar: {exc}"))
            threading.Thread(target=_run, daemon=True).start()

        btn_apply = ttk.Button(
            bb_tgt, text="Aplicar valores Trial",
            style="Primary.TButton", command=_apply,
        )
        btn_apply.pack(side="left", padx=(0, 8))
        ttk.Button(bb_tgt, text="Consultar BD objetivo", command=_verify).pack(side="left")

        # Tabla de verificacion
        ttk.Label(lf_tgt, text="Valores actuales en BD objetivo:",
                  font=("Segoe UI", 8)).grid(
            row=4, column=0, sticky="w", pady=(8, 2))

        tree_verify = ttk.Treeview(lf_tgt, columns=cols_p, show="headings", height=4)
        for col, hdr, w, stretch in [
            ("key", "Clave", 200, False),
            ("value", "Valor", 260, True),
            ("create_date", "create_date", 160, False),
            ("write_date", "write_date", 160, False),
        ]:
            tree_verify.heading(col, text=hdr)
            tree_verify.column(col, width=w, stretch=stretch)
        tree_verify.grid(row=5, column=0, sticky="ew")

        def _populate_verify_table(params: list[dict]) -> None:
            tree_verify.delete(*tree_verify.get_children())
            for p in params:
                tree_verify.insert("", "end",
                    values=(p["key"], p["value"], p["create_date"], p["write_date"]))

    # ── Tab 9: SSH Terminals ─────────────────────────────────────────────

    def _tab_terminal(self) -> None:
        """
        Construye el Tab 9 con dos terminales SSH interactivas.

        Panel izquierdo → servidor origen (Tab 1 self._ssh).
        Panel derecho   → servidor de restauracion (Tab 6) o
                          servidor de destino remoto (Tab 4).

        Cada panel usa SshTerminalPanel, que abre una sesion PTY real
        via paramiko.invoke_shell() y mantiene el estado del shell entre
        comandos (variables de entorno, directorio actual, etc.).
        """
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="  9. Terminal  ")
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)

        # ── SSH getters — mismo patron que Tab 8 ─────────────────────────
        def _get_ssh_left():
            """Terminal izquierda: servidor origen (Tab 1)."""
            return self._ssh if self._ssh.connected else None

        def _get_ssh_right():
            """Terminal derecha: Tab 6 > Tab 4."""
            if self._ssh_restore.connected:
                return self._ssh_restore
            if self._ssh_dest.connected:
                return self._ssh_dest
            return None

        # ── Barra de estado compartida ────────────────────────────────────
        status_bar = ttk.Frame(tab)
        status_bar.grid(row=1, column=0, sticky="ew", pady=(2, 2))
        lbl_status = ttk.Label(
            status_bar,
            text="Las terminales se conectan automaticamente al entrar a esta pestana.  "
                 "Flecha ↑↓ para historial  |  Ctrl+C para interrumpir.",
            foreground="#888888", font=("Segoe UI", 8),
        )
        lbl_status.pack(side="left", padx=6)

        def _on_status(msg: str) -> None:
            self.root.after(0, lambda: lbl_status.config(text=msg))

        # ── Dos terminales en PanedWindow redimensionable ─────────────────
        # Reconectar/Desconectar viven en el encabezado de cada SshTerminalPanel
        paned = ttk.PanedWindow(tab, orient="horizontal")
        paned.grid(row=0, column=0, sticky="nsew", padx=6, pady=(4, 0))

        term_l = SshTerminalPanel(
            paned,
            get_ssh=_get_ssh_left,
            title="Servidor Origen  (Tab 1)",
            on_status=_on_status,
        )
        term_r = SshTerminalPanel(
            paned,
            get_ssh=_get_ssh_right,
            title="Servidor B / Restauracion  (Tab 1 / Tab 4)",
            on_status=_on_status,
        )

        paned.add(term_l, weight=1)
        paned.add(term_r, weight=1)

        # Guardar referencias para desconectar al cerrar la app
        self._term_l = term_l
        self._term_r = term_r

    # ── Window close ─────────────────────────────────────────────────────

    def _on_close(self) -> None:
        """
        Confirm close if a backup is running, then clean up SSH.

        Because long commands run via nohup on the server, they survive
        the SSH disconnect — the user can safely close and the operation
        continues. We still warn so they don't assume it was cancelled.
        """
        btn_state = str(self._btn_run.cget("state"))
        if btn_state == "disabled":
            # Backup in progress — confirm before closing
            answer = messagebox.askyesno(
                APP_TITLE,
                "Hay un backup en ejecucion.\n\n"
                "Los procesos en el servidor (pg_dump / zip) continuaran "
                "corriendo aunque cierres esta ventana, porque se lanzaron "
                "con nohup.\n\n"
                "Puedes reconectarte mas tarde para descargar los archivos "
                "de /tmp/ cuando terminen.\n\n"
                "Cerrar de todos modos?",
            )
            if not answer:
                return

        self._save_geometry()
        # Cerrar terminales PTY antes de cerrar conexiones SSH
        for term in (
            getattr(self, "_term_l", None),
            getattr(self, "_term_r", None),
        ):
            if term is not None:
                term.disconnect()
        self._ssh.close()
        self._ssh_restore.close()
        self._ssh_dest.close()
        # Stop the background scheduler daemon before destroying the window
        try:
            self._scheduler.stop()
        except Exception:
            pass
        self.root.destroy()

    # ── Tab 11: Automatización ────────────────────────────────────────────

    def _tab_automation(self) -> None:
        """
        Tab Automatizacion: manage scheduled backup rules and monitor the
        background scheduler. Reuses ProfileManager + ScheduleManager.
        """
        outer = ttk.Frame(self.nb)
        self.nb.add(outer, text="  Automatizacion  ")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        _C_AMBER = "#FFF3CD"
        _C_AMBER_FG = "#664D03"

        # ── Status bar ────────────────────────────────────────────────────
        status_bar = ttk.Frame(outer)
        status_bar.grid(row=0, column=0, sticky="ew", padx=_PAD, pady=(_PAD, 4))
        status_bar.columnconfigure(1, weight=1)

        self._lbl_sched_status = ttk.Label(
            status_bar,
            text="● Programador activo",
            foreground="#2E8B57",
            font=("Segoe UI", 9, "bold"),
        )
        self._lbl_sched_status.grid(row=0, column=0, sticky="w")

        btn_toggle = ttk.Button(
            status_bar,
            text="⏸ Pausar",
            command=self._sched_toggle_pause,
        )
        btn_toggle.grid(row=0, column=2, padx=(0, 4))
        self._btn_sched_toggle = btn_toggle

        # ── Rule list (Treeview) ─────────────────────────────────────────
        lf_rules = ttk.LabelFrame(outer, text="Reglas de backup programado", padding=_PAD)
        lf_rules.grid(row=1, column=0, sticky="nsew", padx=_PAD, pady=(0, 4))
        lf_rules.columnconfigure(0, weight=1)
        lf_rules.rowconfigure(0, weight=1)

        cols = ("#", "Habilitado", "Cliente", "BD", "Destino", "Hora", "Proximo", "Ultimo resultado")
        self._sched_tree = ttk.Treeview(
            lf_rules,
            columns=cols,
            show="headings",
            height=8,
            selectmode="browse",
        )
        col_widths = [30, 80, 140, 140, 90, 70, 120, 200]
        for col, w in zip(cols, col_widths):
            self._sched_tree.heading(col, text=col)
            self._sched_tree.column(col, width=w, anchor="center" if col in ("#", "Habilitado", "Hora", "Proximo") else "w")

        self._sched_tree.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(lf_rules, orient="vertical", command=self._sched_tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self._sched_tree.configure(yscrollcommand=vsb.set)

        # Tag colors for last result column
        self._sched_tree.tag_configure("ok",    foreground="#2E8B57")
        self._sched_tree.tag_configure("error", foreground="#C0392B")
        self._sched_tree.tag_configure("none",  foreground="#888888")

        # ── Action buttons ────────────────────────────────────────────────
        btn_row = ttk.Frame(outer)
        btn_row.grid(row=2, column=0, sticky="ew", padx=_PAD, pady=4)

        ttk.Button(
            btn_row, text="+ Agregar",
            style="Primary.TButton",
            command=self._sched_add,
        ).pack(side="left", padx=(0, 4))

        ttk.Button(
            btn_row, text="Editar",
            command=self._sched_edit,
        ).pack(side="left", padx=4)

        ttk.Button(
            btn_row, text="Eliminar",
            style="Stop.TButton",
            command=self._sched_delete,
        ).pack(side="left", padx=4)

        ttk.Button(
            btn_row, text="▶ Ejecutar ahora",
            command=self._sched_run_now,
        ).pack(side="left", padx=4)

        # Populate tree on first display
        self._sched_refresh_tree(self._sched_mgr.list_rules())

    def _sched_refresh_tree(self, rules: list) -> None:
        """Rebuild the automation Treeview from the given rule list."""
        try:
            tree = self._sched_tree
        except AttributeError:
            return
        # Guard: tree.delete() with no args raises TclError ("root item may not be deleted")
        children = tree.get_children()
        if children:
            tree.delete(*children)
        for i, r in enumerate(rules, 1):
            label      = r.get("label") or r.get("db_name", "—")
            db_name    = r.get("db_name", "")
            dest_type  = r.get("dest_type", "gdrive")
            hour       = r.get("schedule_hour", 2)
            minute     = r.get("schedule_minute", 0)
            enabled    = "Si" if r.get("enabled") else "No"
            last_res   = r.get("last_result") or "—"
            last_msg   = r.get("last_message", "")
            last_run   = r.get("last_run_ts", "")
            last_run_d = last_run[:10] if last_run else "Nunca"

            # Next run estimate
            from datetime import datetime as _dt
            try:
                now = _dt.now()
                due = now.replace(hour=int(hour), minute=int(minute), second=0, microsecond=0)
                if now >= due:
                    from datetime import timedelta
                    due = due + timedelta(days=1)
                next_str = due.strftime("%m/%d %H:%M")
            except Exception:
                next_str = f"{hour:02d}:{minute:02d}"

            result_display = last_res if last_res == "—" else last_res
            if last_msg and last_res != "—":
                result_display = f"{last_res} — {last_msg[:60]}"

            tag = "ok" if last_res == "ok" else ("error" if last_res == "error" else "none")
            tree.insert(
                "", "end",
                iid=r["id"],
                values=(i, enabled, label, db_name, dest_type,
                        f"{int(hour):02d}:{int(minute):02d}",
                        next_str, result_display),
                tags=(tag,),
            )

    def _sched_toggle_pause(self) -> None:
        """Toggle the scheduler between paused and active states."""
        if self._scheduler.is_paused:
            self._scheduler.resume()
            self._lbl_sched_status.config(
                text="● Programador activo", foreground="#2E8B57"
            )
            self._btn_sched_toggle.config(text="⏸ Pausar")
        else:
            self._scheduler.pause()
            self._lbl_sched_status.config(
                text="⏸ Programador pausado", foreground="#888888"
            )
            self._btn_sched_toggle.config(text="▶ Reanudar")

    def _sched_selected_id(self) -> str | None:
        """Return the rule ID of the currently selected Treeview row, or None."""
        sel = self._sched_tree.selection()
        return sel[0] if sel else None

    def _sched_add(self) -> None:
        """Open the schedule dialog to create a new rule."""
        dlg = _ScheduleDialog(self.root, self._profiles, None)
        result = dlg.show()
        if result:
            # Set last_run_ts to today so the rule does not fire immediately after
            # creation; it will first execute at the next scheduled time (tomorrow
            # at the earliest). The user can trigger an on-demand run via
            # "Ejecutar ahora" if needed.
            result["last_run_ts"] = datetime.datetime.now().isoformat(timespec="seconds")
            self._sched_mgr.add(result)
            self._sched_refresh_tree(self._sched_mgr.list_rules())

    def _sched_edit(self) -> None:
        """Open the schedule dialog pre-filled with the selected rule."""
        rule_id = self._sched_selected_id()
        if not rule_id:
            messagebox.showwarning(APP_TITLE, "Seleccione una regla para editar.")
            return
        rule = self._sched_mgr.get(rule_id)
        if not rule:
            return
        dlg = _ScheduleDialog(self.root, self._profiles, rule)
        result = dlg.show()
        if result:
            self._sched_mgr.update(rule_id, result)
            self._sched_refresh_tree(self._sched_mgr.list_rules())

    def _sched_delete(self) -> None:
        """Delete the selected rule after confirmation."""
        rule_id = self._sched_selected_id()
        if not rule_id:
            messagebox.showwarning(APP_TITLE, "Seleccione una regla para eliminar.")
            return
        rule = self._sched_mgr.get(rule_id)
        label = rule.get("label") if rule else rule_id
        if not messagebox.askyesno(APP_TITLE, f'¿Eliminar la regla "{label}"?'):
            return
        self._sched_mgr.delete(rule_id)
        self._sched_refresh_tree(self._sched_mgr.list_rules())

    def _sched_run_now(self) -> None:
        """Force-run the selected rule immediately (ignores schedule time)."""
        rule_id = self._sched_selected_id()
        if not rule_id:
            messagebox.showwarning(APP_TITLE, "Seleccione una regla para ejecutar.")
            return
        rule = self._sched_mgr.get(rule_id)
        if not rule:
            return
        label = rule.get("label") or rule.get("db_name", rule_id[:8])
        if not messagebox.askyesno(
            APP_TITLE,
            f'¿Ejecutar ahora el backup "{label}"?\n\n'
            "Se ejecutara en segundo plano. El resultado aparecera en el log.",
        ):
            return
        # Run in a fresh thread via the scheduler's _run_rule logic
        t = threading.Thread(
            target=self._scheduler._run_rule,
            args=(rule,),
            name=f"sched-manual-{rule_id[:8]}",
            daemon=True,
        )
        t.start()
        self._sched_append_log(f"[{label}] Ejecucion manual iniciada...")
