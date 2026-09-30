"""
Restore page: connection target, inventory, validation and the restore worker.

Part of BackupApp (gui/app.py), moved here unchanged.
"""
from __future__ import annotations
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from core.inventory_manager import InventoryManager
from core.filestore_manager import FilestoreManager
from core.restore_manager import RestoreManager
from core.ssh_client import SSHClient
from core.bundle_manager import BundleManager
from gui.constants import _DB_NAME_RE, APP_TITLE, _PAD, _C_PURPLE, _C_TEAL


class RestoreMixin:
    """
    Restore page: connection target, inventory, validation and the restore worker.

    Mixin of gui.app.BackupApp: every method runs with `self` being the
    application object, so widgets and state created by other mixins are
    reachable exactly as before the split.
    """

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
            command=lambda: self._browse_file(self._v_r_fs_local, "*.zip *.tar")
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
                    self._v_r_fs_srv.set(f"{dest_dir}/filestore_{fs_db}.tar")

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
        self._history_begin(
            "restore",
            self._server_label_for(self._cb_r_profile.get(), ssh.host),
            ssh.host or "",
        )

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
