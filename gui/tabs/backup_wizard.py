"""
Manual backup wizard: pages 1-5 (connection, database, filestore,
destination, execute), server profiles, validation, the backup worker and
the transfer-retry panel.

Part of BackupApp (gui/app.py), moved here unchanged.
"""
from __future__ import annotations
import datetime
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from core.db_manager import DBManager
from core.docker_manager import DockerManager
from core.gdrive import DriveUploader
from core.inventory_manager import InventoryManager
from core.filestore_manager import FilestoreManager
from core.ssh_client import SSHClient
from core.transfer import TransferManager
from core.bundle_manager import BundleManager
from gui.constants import APP_TITLE, _PAD, _PROFILE_NEW


class BackupWizardMixin:
    """
    Manual backup wizard: pages 1-5 (connection, database, filestore,
    destination, execute), server profiles, validation, the backup worker and
    the transfer-retry panel.

    Mixin of gui.app.BackupApp: every method runs with `self` being the
    application object, so widgets and state created by other mixins are
    reachable exactly as before the split.
    """

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
        self._history_begin(
            "backup_manual",
            self._server_label_for(self._cb_profile.get(), self._ssh.host),
            self._ssh.host or "",
        )

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

        db_mgr = DBManager(self._ssh, target=self._pg_target_source)
        src_host = self._retry_conn_params.get("src_host", self._ssh.host)
        for path in self._retry_remote_tmp:
            try:
                self._log(f"Limpiando {path} del servidor ...")
                db_mgr.cleanup_remote(path)
                self._temp_registry.unregister_path(src_host, path)
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
            values=[_PROFILE_NEW] + self._profiles.names(),
        )
        self._cb_profile.set(_PROFILE_NEW)
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
            values=[_PROFILE_NEW] + self._profiles.names(),
        )
        self._cb_r_profile.set(_PROFILE_NEW)
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

        # ── Docker (opcional) — BD dentro de un contenedor en vez de bare-metal ──
        lf_docker = ttk.LabelFrame(f, text="Docker (opcional)", padding=6)
        lf_docker.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(_PAD, 0))
        lf_docker.columnconfigure(1, weight=1)

        ttk.Label(
            lf_docker,
            text="Vacio = PostgreSQL directo en el servidor. Complete solo si la BD "
                 "vive dentro de un contenedor Docker.",
            foreground="#666666", font=("Segoe UI", 8), wraplength=420,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 4))

        ttk.Label(lf_docker, text="Contenedor:").grid(row=1, column=0, sticky="e", padx=(0, _PAD))
        self._cb_docker_container = ttk.Combobox(lf_docker, textvariable=self._v_docker_container)
        self._cb_docker_container.grid(row=1, column=1, sticky="ew", padx=(0, _PAD))
        ttk.Button(
            lf_docker, text="Detectar contenedores", command=self._action_detect_docker_source,
        ).grid(row=1, column=2)

        ttk.Label(lf_docker, text="Usuario OS (peer auth):").grid(
            row=2, column=0, sticky="e", padx=(0, _PAD), pady=(4, 0))
        ttk.Entry(lf_docker, textvariable=self._v_docker_exec_user).grid(
            row=2, column=1, sticky="w", padx=(0, _PAD), pady=(4, 0))
        ttk.Label(
            lf_docker,
            text="Solo si el contenedor exige 'docker exec -u <usuario>' (autenticacion peer, "
                 "ej. despliegues \"todo en uno\"). Dejar vacio para el caso normal.",
            foreground="#666666", font=("Segoe UI", 8), wraplength=420,
        ).grid(row=3, column=0, columnspan=3, sticky="w")

        def _sync_pg_target_source(*_):
            container = self._v_docker_container.get().strip()
            self._pg_target_source.container = container
            self._pg_target_source.pg_user = self._docker_pg_users_source.get(container, "postgres")
            self._pg_target_source.docker_exec_user = self._v_docker_exec_user.get().strip()
        self._v_docker_container.trace_add("write", _sync_pg_target_source)
        self._v_docker_exec_user.trace_add("write", _sync_pg_target_source)

        ttk.Button(f, text="Siguiente ->", style="Nav.TButton", command=self._goto_filestore).grid(
            row=8, column=0, columnspan=3, pady=_PAD * 2
        )

    def _action_detect_docker_source(self) -> None:
        """Tab 2: detecta contenedores Postgres en el servidor origen (Tab 1)."""
        if not self._ssh.connected:
            self._log("[ERROR] Docker: conectese primero en Tab 1.")
            return
        self._log("Docker — detectando contenedores Postgres ...")

        def _run():
            try:
                containers = DockerManager(self._ssh).list_postgres_containers()
                def _fill():
                    names = [c["name"] for c in containers]
                    self._cb_docker_container["values"] = names
                    self._docker_pg_users_source = {c["name"]: c["pg_user"] for c in containers}
                    if not names:
                        self._log("  No se encontraron contenedores Postgres (o Docker no esta disponible).")
                    for c in containers:
                        self._log(f"  {c['name']}  ({c['image']})  usuario pg: {c['pg_user']}  {c['ports']}")
                self.root.after(0, _fill)
            except Exception as exc:
                self.root.after(0, lambda exc=exc: self._log(f"[ERROR] Docker — detectar: {exc}"))

        threading.Thread(target=_run, daemon=True).start()

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
            values=[_PROFILE_NEW] + self._profiles.names(),
        )
        self._cb_d_profile.set(_PROFILE_NEW)
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
        names  = self._profiles.names()
        values = [_PROFILE_NEW] + names
        self._cb_profile["values"]   = values
        self._cb_d_profile["values"] = values
        self._cb_r_profile["values"] = values

    def _load_profile(self) -> None:
        """Fill Tab-1 connection fields from the selected profile."""
        name = self._cb_profile.get()
        if not name or name == _PROFILE_NEW:
            return
        p = self._profiles.get(name)
        if p:
            self._cv["host"].set(p["host"])
            self._cv["port"].set(str(p["port"]))
            self._cv["user"].set(p["user"])
            self._cv["pass"].set(p["password"])
            self._v_prof_gdrive_creds.set(p.get("gdrive_creds_path", ""))
            self._v_prof_gdrive_folder.set(p.get("gdrive_folder_id", ""))
            self._v_docker_container.set(p.get("docker_container", ""))
            self._v_docker_exec_user.set(p.get("docker_exec_user", ""))

    def _save_profile(self) -> None:
        """Save current Tab-1 connection fields as a named profile.

        If a profile is already selected, updates it directly (no name prompt).
        If nothing is selected, asks for a name to create a new profile.
        """
        host = self._cv["host"].get().strip()
        if not host:
            messagebox.showwarning(APP_TITLE, "Ingrese los datos de conexion primero.")
            return
        existing = self._cb_profile.get()
        if existing == _PROFILE_NEW:
            existing = ""
        if existing:
            # Editing an existing profile — confirm and overwrite directly
            if not messagebox.askyesno(
                APP_TITLE, f'¿Actualizar el perfil "{existing}" con los datos actuales?'
            ):
                return
            name = existing
        else:
            # New profile — ask for a name
            default_name = f"{self._cv['user'].get()}@{host}:{self._cv['port'].get()}"
            name = simpledialog.askstring(
                "Nuevo perfil", "Nombre del perfil:",
                initialvalue=default_name, parent=self.root,
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
                docker_container=self._v_docker_container.get(),
                docker_exec_user=self._v_docker_exec_user.get(),
            )
            self._refresh_profile_combos()
            self._cb_profile.set(name)
            messagebox.showinfo(APP_TITLE, f'Perfil "{name}" guardado.')
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _delete_profile(self) -> None:
        """Delete the selected profile from the backup profile combobox."""
        name = self._cb_profile.get()
        if not name or name == _PROFILE_NEW:
            messagebox.showwarning(APP_TITLE, "Seleccione un perfil para eliminar.")
            return
        if not messagebox.askyesno(APP_TITLE, f'¿Eliminar el perfil "{name}"?'):
            return
        self._profiles.delete(name)
        self._refresh_profile_combos()
        self._cb_profile.set(_PROFILE_NEW)

    def _load_d_profile(self) -> None:
        """Fill Tab-4 destination server fields from the selected profile."""
        name = self._cb_d_profile.get()
        if not name or name == _PROFILE_NEW:
            return
        p = self._profiles.get(name)
        if p:
            self._dv["host"].set(p["host"])
            self._dv["port"].set(str(p["port"]))
            self._dv["user"].set(p["user"])
            self._dv["pass"].set(p["password"])
            # Keep 'dir' as-is — it's specific to backup paths, not the server profile

    def _save_d_profile(self) -> None:
        """Save current Tab-4 destination fields as a named profile.

        Updates the selected profile directly; asks for name only when creating new.
        """
        host = self._dv["host"].get().strip()
        if not host:
            messagebox.showwarning(APP_TITLE, "Ingrese los datos de conexion primero.")
            return
        existing = self._cb_d_profile.get()
        if existing == _PROFILE_NEW:
            existing = ""
        if existing:
            if not messagebox.askyesno(
                APP_TITLE, f'¿Actualizar el perfil "{existing}" con los datos actuales?'
            ):
                return
            name = existing
        else:
            default_name = f"{self._dv['user'].get()}@{host}:{self._dv['port'].get()}"
            name = simpledialog.askstring(
                "Nuevo perfil", "Nombre del perfil:",
                initialvalue=default_name, parent=self.root,
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
        if not name or name == _PROFILE_NEW:
            messagebox.showwarning(APP_TITLE, "Seleccione un perfil para eliminar.")
            return
        if not messagebox.askyesno(APP_TITLE, f'¿Eliminar el perfil "{name}"?'):
            return
        self._profiles.delete(name)
        self._refresh_profile_combos()
        self._cb_d_profile.set(_PROFILE_NEW)

    def _load_r_profile(self) -> None:
        """Fill Servidor B connection fields from the selected profile."""
        name = self._cb_r_profile.get()
        if not name or name == _PROFILE_NEW:
            return
        p = self._profiles.get(name)
        if p:
            self._r_conn_vars["host"].set(p["host"])
            self._r_conn_vars["port"].set(str(p["port"]))
            self._r_conn_vars["user"].set(p["user"])
            self._r_conn_vars["pass"].set(p["password"])
            trial_container_var = getattr(self, "_v_trial_container", None)
            if trial_container_var is not None:
                trial_container_var.set(p.get("docker_container", ""))
            trial_exec_user_var = getattr(self, "_v_trial_exec_user", None)
            if trial_exec_user_var is not None:
                trial_exec_user_var.set(p.get("docker_exec_user", ""))

    def _save_r_profile(self) -> None:
        """Save current restore 'otro servidor' fields as a named profile.

        Updates the selected profile directly; asks for name only when creating new.
        """
        host = self._r_conn_vars["host"].get().strip()
        if not host:
            messagebox.showwarning(APP_TITLE, "Ingrese los datos de conexion primero.")
            return
        existing = self._cb_r_profile.get()
        if existing == _PROFILE_NEW:
            existing = ""
        if existing:
            if not messagebox.askyesno(
                APP_TITLE, f'¿Actualizar el perfil "{existing}" con los datos actuales?'
            ):
                return
            name = existing
        else:
            default_name = (
                f"{self._r_conn_vars['user'].get()}@{host}:"
                f"{self._r_conn_vars['port'].get()}"
            )
            name = simpledialog.askstring(
                "Nuevo perfil", "Nombre del perfil:",
                initialvalue=default_name, parent=self.root,
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
                docker_container=getattr(self, "_v_trial_container", tk.StringVar()).get(),
                docker_exec_user=getattr(self, "_v_trial_exec_user", tk.StringVar()).get(),
            )
            self._refresh_profile_combos()
            self._cb_r_profile.set(name)
            messagebox.showinfo(APP_TITLE, f'Perfil "{name}" guardado.')
        except ValueError as exc:
            messagebox.showerror(APP_TITLE, str(exc))

    def _delete_r_profile(self) -> None:
        """Delete the selected profile from the restore profile combobox."""
        name = self._cb_r_profile.get()
        if not name or name == _PROFILE_NEW:
            messagebox.showwarning(APP_TITLE, "Seleccione un perfil para eliminar.")
            return
        if not messagebox.askyesno(APP_TITLE, f'¿Eliminar el perfil "{name}"?'):
            return
        self._profiles.delete(name)
        self._refresh_profile_combos()
        self._cb_r_profile.set(_PROFILE_NEW)

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

    def _action_load_dbs(self) -> None:
        def _run():
            try:
                dbs = DBManager(self._ssh, target=self._pg_target_source).list_databases()
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

        # Warn before a manual local download while a scheduled Drive upload
        # is in flight: both compete for the source server's network egress.
        # In the 2026-07-15 log, a local SFTP download (ARANZAZU) degraded a
        # concurrent Drive upload (mega) from ~2 MB/s to 0.3 MB/s.
        if self._v_dest_type.get() == "local" and self._scheduler.has_active_uploads():
            labels = ", ".join(self._scheduler.active_upload_labels())
            if not messagebox.askyesno(
                "Subida en curso",
                "Hay un backup programado subiendo a Google Drive ahora mismo "
                f"({labels}).\n\n"
                "Descargar en paralelo por SFTP puede competir por el ancho de "
                "banda del servidor origen y degradar ambas transferencias.\n\n"
                "¿Desea continuar de todas formas?",
                parent=self.root,
            ):
                return

        self._btn_run.config(state="disabled")
        self._v_progress.set(0)
        self._lbl_progress.config(text="")
        self._begin_operation()
        self._history_begin(
            "backup_manual",
            self._server_label_for(self._cb_profile.get(), self._ssh.host),
            self._ssh.host or "",
        )

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
            # Origin server credentials — needed to register remote /tmp
            # files with self._temp_registry (SSHClient itself only keeps
            # host/port, not user/password, after connecting).
            "src_host": self._cv["host"].get().strip(),
            "src_port": int(self._cv["port"].get().strip() or 22),
            "src_user": self._cv["user"].get().strip(),
            "src_pass": self._cv["pass"].get(),
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
        db_mgr = DBManager(self._ssh, target=self._pg_target_source)
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

        # Tracks remote /tmp files this run creates so they get cleaned up
        # even if the run is interrupted before reaching its own cleanup
        # code — see core/temp_registry.py. Mirrors the same pattern used
        # by BackupScheduler._run_rule for scheduled backups. Unregistering
        # by (host, path) rather than by id means the retry flow
        # (_worker_transfer_only, a separate thread/closure reusing the same
        # `p` dict) and the manual "delete without transferring" action can
        # unregister these files too without needing this closure's state.
        def _register(path: str, kind: str) -> None:
            self._temp_registry.register(
                p["src_host"], p["src_port"], p["src_user"], p["src_pass"],
                path, kind, p["db"],
            )

        def _unregister(path: str) -> None:
            self._temp_registry.unregister_path(p["src_host"], path)

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
                _register(dump_path, "dump")
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
                _register(fs_path, "filestore")
                advance(f"Filestore empaquetado: {fs_path}")

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
                    _register(inv_remote_path, "inventory")
                    self._log(f"Inventario escrito en servidor: {inv_remote_path}")

                all_files = remote_tmp + ([inv_remote_path] if inv_remote_path else [])
                bm.create(bundle_path_remote, all_files, log_callback=self._log)
                _register(bundle_path_remote, "bundle")

                # Remove individual files — they are now inside the .tar
                db_mgr_cleanup = DBManager(self._ssh, target=self._pg_target_source)
                for f in all_files:
                    db_mgr_cleanup.cleanup_remote(f)
                    _unregister(f)

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
            self._exec_transfer_and_finish(p, remote_tmp, dump_path, inventory, advance, _unregister)

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
        unregister_fn=None,
    ) -> None:
        """
        Steps 3-5: transfer files to destination, server cleanup, save inventory.

        Raises RuntimeError / Exception on failure — callers wrap this in try/except
        and decide whether to show the retry panel or report a fatal error.

        Args:
            p:            Full backup params dict (dest_type, connection info, etc.).
            remote_tmp:   List of absolute paths on the source server ready to transfer.
            dump_path:    Path of the DB dump on the server (used to name the inventory).
            inventory:    Inventory dict collected before the dump, or None.
            advance_fn:   Callable(label) that increments the progress bar step counter.
            unregister_fn: Callable(path) to drop a file from the orphan-cleanup
                registry once deleted (core/temp_registry.py). Defaults to
                unregistering by (p["src_host"], path) directly so callers
                that didn't build their own closure (the retry flow) still
                get correct cleanup tracking.
        """
        db_mgr   = DBManager(self._ssh, target=self._pg_target_source)
        transfer = TransferManager(self._ssh)

        if unregister_fn is None:
            unregister_fn = lambda path: self._temp_registry.unregister_path(p["src_host"], path)

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
                unregister_fn(remote_file)
        else:
            # User opted out of cleanup — these files are being kept on the
            # server on purpose, so stop tracking them: the orphan sweep
            # must never delete something intentionally left in place.
            for remote_file in remote_tmp:
                unregister_fn(remote_file)

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
