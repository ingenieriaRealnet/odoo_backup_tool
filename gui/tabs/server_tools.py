"""
Server tool pages: remote file explorer, SSH terminals and Trial.

Part of BackupApp (gui/app.py), moved here unchanged.
"""
from __future__ import annotations
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from core.docker_manager import DockerManager
from core.ssh_client import SSHClient
from gui.file_browser_panel import FileBrowserPanel
from gui.ssh_terminal_panel import SshTerminalPanel
from core.trial_manager import TrialManager
from gui.constants import _PAD


class ServerToolsMixin:
    """
    Server tool pages: remote file explorer, SSH terminals and Trial.

    Mixin of gui.app.BackupApp: every method runs with `self` being the
    application object, so widgets and state created by other mixins are
    reachable exactly as before the split.
    """

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

        # ── Docker (opcional) — BD dentro de un contenedor en vez de bare-metal ──
        ttk.Label(lf_srv, text="Contenedor:").grid(row=1, column=0, sticky="e", padx=(0, _PAD), pady=(6, 0))
        self._v_trial_container = tk.StringVar()
        cb_trial_container = ttk.Combobox(lf_srv, textvariable=self._v_trial_container)
        cb_trial_container.grid(row=1, column=1, sticky="ew", pady=(6, 0))

        ttk.Label(lf_srv, text="Usuario OS (peer auth):").grid(
            row=2, column=0, sticky="e", padx=(0, _PAD), pady=(4, 0))
        self._v_trial_exec_user = tk.StringVar()
        ttk.Entry(lf_srv, textvariable=self._v_trial_exec_user).grid(
            row=2, column=1, sticky="w", pady=(4, 0))
        ttk.Label(
            lf_srv,
            text="Solo si el contenedor exige 'docker exec -u <usuario>' (autenticacion peer). "
                 "Dejar vacio para el caso normal.",
            foreground="#666666", font=("Segoe UI", 8), wraplength=420,
        ).grid(row=3, column=0, columnspan=3, sticky="w")

        def _sync_pg_target_trial(*_):
            container = self._v_trial_container.get().strip()
            self._pg_target_trial.container = container
            self._pg_target_trial.pg_user = self._docker_pg_users_trial.get(container, "postgres")
            self._pg_target_trial.docker_exec_user = self._v_trial_exec_user.get().strip()
        self._v_trial_container.trace_add("write", _sync_pg_target_trial)
        self._v_trial_exec_user.trace_add("write", _sync_pg_target_trial)

        def _detect_docker_trial():
            ssh = _get_ssh()
            if not ssh:
                self._log("[ERROR] Trial — Docker: sin conexion SSH.")
                return
            self._log("Trial — detectando contenedores Postgres ...")
            def _run():
                try:
                    containers = DockerManager(ssh).list_postgres_containers()
                    def _fill():
                        names = [c["name"] for c in containers]
                        cb_trial_container["values"] = names
                        self._docker_pg_users_trial = {c["name"]: c["pg_user"] for c in containers}
                        if not names:
                            self._log("  No se encontraron contenedores Postgres (o Docker no esta disponible).")
                        for c in containers:
                            self._log(f"  {c['name']}  ({c['image']})  usuario pg: {c['pg_user']}  {c['ports']}")
                    self.root.after(0, _fill)
                except Exception as exc:
                    self.root.after(0, lambda exc=exc: self._log(f"[ERROR] Trial — Docker: {exc}"))
            threading.Thread(target=_run, daemon=True).start()

        ttk.Button(lf_srv, text="Detectar contenedores", command=_detect_docker_trial).grid(
            row=1, column=2, pady=(6, 0))

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
                    b, c = TrialManager(ssh, target=self._pg_target_trial).find_odoo()
                    def _fill():
                        v_bin.set(b or "")
                        v_conf.set(c or "")
                        self._log(f"  Binario: {b or '(no encontrado)'}")
                        self._log(f"  Config:  {c or '(no encontrado)'}")
                    self.root.after(0, _fill)
                except Exception as exc:
                    self.root.after(0, lambda exc=exc: self._log(f"[ERROR] Trial — detectar: {exc}"))
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
                    mgr = TrialManager(ssh, target=self._pg_target_trial)
                    mgr.create_clean_db(dn, b, c, log_callback=self._log)
                    params = mgr.query_db_params(dn)
                    nonlocal _source_params
                    _source_params = params
                    self.root.after(0, lambda: _populate_src_table(params))
                    self._log(f"Trial === BD '{dn}' lista. {len(params)} parametros obtenidos ===")
                    for p in params:
                        self._log(f"  {p['key']:<28} {p['value']}")
                except Exception as exc:
                    self.root.after(0, lambda exc=exc: self._log(f"[ERROR] Trial — crear BD: {exc}"))
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
                TrialManager(ssh, target=self._pg_target_trial).drop_db(dn)
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
                    dbs = TrialManager(ssh, target=self._pg_target_trial).list_databases()
                    def _fill():
                        lb_dbs.delete(0, "end")
                        for d in dbs:
                            lb_dbs.insert("end", d)
                        self._log(f"  {len(dbs)} base(s) encontrada(s).")
                    self.root.after(0, _fill)
                except Exception as exc:
                    self.root.after(0, lambda exc=exc: self._log(f"[ERROR] Trial — listar: {exc}"))
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
                    TrialManager(ssh, target=self._pg_target_trial).apply_trial_params(
                        target, _source_params, log_callback=self._log)
                    self._log("Trial === Aplicacion completada. Verificando... ===")
                    result = TrialManager(ssh, target=self._pg_target_trial).verify_target_params(target)
                    for p in result:
                        self._log(f"  {p['key']:<28} {p['value']}")
                    self.root.after(0, lambda: _populate_verify_table(result))
                except Exception as exc:
                    self.root.after(0, lambda exc=exc: self._log(f"[ERROR] Trial — aplicar: {exc}"))
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
                    result = TrialManager(ssh, target=self._pg_target_trial).verify_target_params(target)
                    self._log(f"  Parametros database.* en '{target}':")
                    for p in result:
                        self._log(f"  {p['key']:<28} {p['value']}")
                    self.root.after(0, lambda: _populate_verify_table(result))
                except Exception as exc:
                    self.root.after(0, lambda exc=exc: self._log(f"[ERROR] Trial — consultar: {exc}"))
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
            on_session_end=self._on_terminal_session_end,
        )
        term_r = SshTerminalPanel(
            paned,
            get_ssh=_get_ssh_right,
            title="Servidor B / Restauracion  (Tab 1 / Tab 4)",
            on_status=_on_status,
            on_session_end=self._on_terminal_session_end,
        )

        paned.add(term_l, weight=1)
        paned.add(term_r, weight=1)

        # Guardar referencias para desconectar al cerrar la app
        self._term_l = term_l
        self._term_r = term_r
