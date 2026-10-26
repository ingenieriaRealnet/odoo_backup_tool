"""
Addons page: Git sync of custom addons, SSH key handling and module update.

Part of BackupApp (gui/app.py), moved here unchanged.
"""
from __future__ import annotations
import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk
from core.addons_manager import AddonsManager, scan_ssh_directory
from core.ssh_client import SSHClient
from gui.constants import APP_TITLE, _PAD, _C_BG, _C_TEXT


class AddonsMixin:
    """
    Addons page: Git sync of custom addons, SSH key handling and module update.

    Mixin of gui.app.BackupApp: every method runs with `self` being the
    application object, so widgets and state created by other mixins are
    reachable exactly as before the split.
    """

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
            ("Ruta en servidor:",   self._v_a_target,    False, "/usr/lib/python3/dist-packages/odoo/addons_custom"),
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
            text="Git normal",
            variable=self._v_a_submodules, value=False,
        ).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(
            repo_type_frame,
            text="Git con submódulos",
            variable=self._v_a_submodules, value=True,
        ).pack(side="left")

        # El comando exacto usado por cada modo depende tambien del checkbox
        # "Modo espejo forzado" en la Seccion 4 (Opciones) — no se fija aqui
        # en texto estatico para evitar que quede desactualizado; el log de
        # la Seccion 5 ya distingue el comando real corrido en cada caso.
        ttk.Label(
            sec2,
            text="  Usa 'Git con submódulos' si el repo principal enlaza otros repos via .gitmodules.\n"
                 "  Por defecto trae los ultimos cambios (git pull / --remote --merge); ver 'Modo\n"
                 "  espejo forzado' en Opciones para que el repositorio remoto siempre sobrescriba.",
            font=("Segoe UI", 8), foreground="#888888", justify="left",
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

        # Service name is always enabled now — it's shared between "reiniciar
        # servicio" (below) and the automatic `-u` database update (see
        # _worker_addons), which needs it even when a restart isn't requested.
        ttk.Label(sec4, text="Nombre del servicio:").grid(
            row=0, column=0, sticky="e", padx=(0, _PAD), pady=3
        )
        svc_row = ttk.Frame(sec4)
        svc_row.grid(row=0, column=1, sticky="ew", pady=3)
        svc_row.columnconfigure(0, weight=1)

        self._e_a_service = ttk.Entry(svc_row, textvariable=self._v_a_service)
        self._e_a_service.grid(row=0, column=0, sticky="ew", padx=(0, 4))

        self._btn_detect_svc = ttk.Button(
            svc_row, text="Auto-detectar",
            command=self._action_detect_service,
        )
        self._btn_detect_svc.grid(row=0, column=1)

        ttk.Label(sec4, text="Base(s) de datos:").grid(
            row=1, column=0, sticky="e", padx=(0, _PAD), pady=3
        )
        ttk.Entry(sec4, textvariable=self._v_a_db_name).grid(
            row=1, column=1, sticky="ew", pady=3
        )
        tk.Label(
            sec4,
            text="  Si hay modulos con cambios tras el sync, se ofrece actualizarlas "
                 "(-u) antes de reiniciar — usa el binario/conf del servicio de arriba. "
                 "Varias bases separadas por coma (ej: limatec_test, limatec_prod) se "
                 "actualizan una por una, nunca en paralelo — si una falla, se detiene "
                 "ahi y no continua con las siguientes. Dejar vacio para omitir este paso.",
            font=("Segoe UI", 8), fg="#666666", bg=_C_BG,
            wraplength=480, justify="left",
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(0, 4))

        ttk.Checkbutton(
            sec4,
            text="Reiniciar servicio Odoo al terminar la sincronizacion",
            variable=self._v_a_restart,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 4))

        ttk.Separator(sec4, orient="horizontal").grid(
            row=4, column=0, columnspan=3, sticky="ew", pady=6
        )

        ttk.Checkbutton(
            sec4,
            text="Modo espejo forzado (el repositorio SIEMPRE gana, nunca mezcla)",
            variable=self._v_a_force_mirror,
        ).grid(row=5, column=0, columnspan=3, sticky="w")
        tk.Label(
            sec4,
            text="  Descarta cualquier cambio local en el servidor (git reset --hard + "
                 "git clean -fd). Con submodulos: los fija al commit exacto que el "
                 "repositorio tiene anclado (sin --remote, sin --merge). Producción queda "
                 "identica al repositorio remoto — irreversible.",
            font=("Segoe UI", 8), fg="#E67E22", bg=_C_BG,
            wraplength=480, justify="left",
        ).grid(row=6, column=0, columnspan=3, sticky="w", pady=(0, 2))

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

    # Removed: the service name entry/button are now always enabled — the
    # `-u` database update step (see _worker_addons) needs the service name
    # to resolve the odoo binary/conf even when "reiniciar servicio" isn't
    # checked, so gating them on that checkbox no longer made sense.
    # def _toggle_addons_restart(self) -> None:
    #     """Enable/disable the service name entry and auto-detect button."""
    #     state = "normal" if self._v_a_restart.get() else "disabled"
    #     self._e_a_service.config(state=state)
    #     self._btn_detect_svc.config(state=state)

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

        try:
            ssh = self._get_addons_ssh()
        except RuntimeError as exc:
            messagebox.showwarning(APP_TITLE, str(exc))
            return

        if not self._check_ssh_alive(ssh, "addons"):
            return

        # Validate the selected key source
        server_key = None
        local_key  = None
        passphrase = None
        server_passphrase = None

        if key_source == "server":
            server_key = self._v_a_server_key.get().strip()
            if not server_key:
                messagebox.showwarning(
                    APP_TITLE,
                    "Escanee el servidor y seleccione una llave en el Paso 3\n"
                    "o cambie a uno de los otros modos de llave SSH.",
                )
                return
            # The server key never leaves the server, so the app can't check
            # locally whether it's passphrase-protected like it does for
            # local keys — ask the server itself (single quick ssh-keygen
            # check, same pattern as _check_ssh_alive above). If it is, ask
            # for the passphrase now so it's cached before we go async; the
            # worker unlocks the key into an ssh-agent ON THE SERVER so git
            # can authenticate through it non-interactively (BatchMode=yes
            # can never prompt for a passphrase, which is why this used to
            # just fail silently with a publickey auth error).
            cache_key = f"{ssh.host}:{server_key}"
            if cache_key in self._server_key_passphrases:
                server_passphrase = self._server_key_passphrases[cache_key]
            else:
                try:
                    needs = AddonsManager(ssh).server_key_needs_passphrase(server_key)
                except Exception:
                    needs = False
                if needs:
                    server_passphrase = simpledialog.askstring(
                        "Passphrase de llave SSH",
                        f"La llave del servidor requiere passphrase:\n{server_key}\n\n"
                        "Se usara para desbloquearla en un ssh-agent en el servidor "
                        "(la llave nunca sale del servidor).",
                        show="*",
                        parent=self.root,
                    )
                    if server_passphrase is None:
                        return  # cancelled
                    self._server_key_passphrases[cache_key] = server_passphrase

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

        # Modo espejo forzado descarta cambios locales en el servidor de forma
        # irreversible (git reset --hard + git clean -fd) — requiere confirmacion
        # explicita cada vez, igual que otras acciones destructivas de la app.
        if self._v_a_force_mirror.get():
            answer = messagebox.askyesno(
                APP_TITLE,
                f"MODO ESPEJO FORZADO activado para:\n\n  {target}\n\n"
                "Esto va a DESCARTAR cualquier cambio local en esa ruta del servidor "
                "(archivos modificados o no rastreados) y dejarla identica a la rama "
                f"'{branch}' del repositorio remoto.\n\n"
                "Esta accion no se puede deshacer. ¿Continuar?",
                icon="warning",
            )
            if not answer:
                return

        self._btn_sync_addons.config(state="disabled")
        self._v_a_progress.set(0)
        self._lbl_a_progress.config(text="")
        self._begin_operation()
        profile_combo = (
            self._cb_profile.get() if self._v_a_conn_type.get() == "origin"
            else self._cb_r_profile.get()
        )
        self._history_begin(
            "addons_sync",
            self._server_label_for(profile_combo, ssh.host),
            ssh.host or "",
        )

        params = {
            "repo_url":       repo_url,
            "branch":         branch,
            "target":         target,
            "odoo_user":      self._v_a_odoo_user.get().strip() or "odoo",
            "key_source":     key_source,      # "server" | "local"
            "server_key":     server_key,      # remote path  (server mode)
            "local_key":      local_key,       # local path   (local mode)
            "passphrase":     passphrase,      # local passphrase (local mode)
            "server_passphrase": server_passphrase,  # server key passphrase (server mode)
            "use_submodules": self._v_a_submodules.get(),
            "force_mirror":   self._v_a_force_mirror.get(),
            "restart":        self._v_a_restart.get(),
            "service":        self._v_a_service.get().strip(),
            "db_name":        self._v_a_db_name.get().strip(),
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
                # If the key is passphrase-protected, _action_sync_addons
                # already collected the passphrase — unlock it into an
                # ssh-agent on the server so BatchMode=yes git can
                # authenticate through the agent instead of needing to
                # read the (still encrypted) key file directly.
                if p.get("server_passphrase"):
                    self._q.put(("addons_progress", (25, "Desbloqueando llave del servidor...")))
                    mgr.unlock_server_key(
                        p["server_key"], p["server_passphrase"], log_callback=self._log,
                    )

            elif p["key_source"] == "local":
                self._q.put(("addons_progress", (20, "Subiendo llave SSH temporal...")))
                mgr.upload_key(
                    p["local_key"],
                    passphrase=p["passphrase"],
                    log_callback=self._log,
                )
                key_uploaded = True
                wrapper_written = True

            # Step 2.5: detect submodules removed upstream but still
            # registered in THIS checkout's local .git/config (see
            # AddonsManager.detect_stale_submodules's docstring) — confirm
            # with the user before touching anything, same as every other
            # local-state-changing action in the app.
            if p["use_submodules"]:
                stale = mgr.detect_stale_submodules(p["target"])
                if stale:
                    names_list = "\n".join(f"  • {n}" for n in stale)
                    proceed = self._ask_confirm(
                        "Submodulos huerfanos detectados",
                        "Los siguientes submodulos ya no existen en el repositorio "
                        "remoto, pero siguen registrados localmente en este checkout "
                        "(se retiraron del repo despues de que este servidor ya "
                        f"tenia una copia local):\n\n{names_list}\n\n"
                        "¿Retirar su registro local? Es necesario para continuar "
                        "la sincronizacion de submodulos — no contacta GitHub ni "
                        "modifica el repositorio remoto, solo limpia este checkout.",
                    )
                    if not proceed:
                        self._q.put(("addons_cancelled",
                            "Sincronizacion cancelada — submodulos huerfanos sin resolver."))
                        return
                    mgr.deinit_submodules(p["target"], stale, log_callback=self._log)

            # Snapshot "before" state so we can tell which modules actually
            # changed once the sync lands — see diff_changed_modules(). None/
            # empty on a first clone (nothing was "updated" yet).
            before_head = mgr.get_head_commit(p["target"])
            before_subs = mgr.get_submodule_status(p["target"])

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
                force_mirror=p["force_mirror"],
                log_callback=self._log,
                cancel_event=self._cancel_event,
            )
            self._q.put(("addons_progress", (80, "Repositorio sincronizado.")))

            # Step 3.5: if module code actually changed, offer to update the
            # database(s) before restarting — a git sync alone never applies
            # new fields/views/data/migrations, only `-u` does. Multiple
            # databases (comma-separated) update ONE AT A TIME, never in
            # parallel — each -u is heavy (locks tables, real CPU/IO), and
            # running several against the same Postgres server at once risks
            # contention or lock collisions if they share anything. If one
            # fails, the loop stops there rather than continuing to the rest,
            # so a partial failure never gets masked by "later ones worked".
            db_names = [d.strip() for d in p["db_name"].split(",") if d.strip()]
            if not db_names:
                self._log(
                    "Actualizacion de base de datos omitida: no se indico "
                    "ninguna base de datos en el Tab 7 (campo 'Base(s) de datos')."
                )
            elif not p["service"]:
                self._log(
                    "Actualizacion de base de datos omitida: no hay un "
                    "servicio configurado (necesario para resolver el binario/conf de Odoo)."
                )
            else:
                self._log("Verificando modulos con cambios desde el ultimo sync...")
                after_head = mgr.get_head_commit(p["target"])
                after_subs = mgr.get_submodule_status(p["target"])
                changed = mgr.diff_changed_modules(
                    p["target"], before_head, after_head, before_subs, after_subs,
                )
                if not changed:
                    self._log(
                        "No se detectaron modulos con cambios en esta sincronizacion "
                        f"— se omite la actualizacion de {', '.join(db_names)}."
                    )
                else:
                    names_list = "\n".join(f"  • {n}" for n in changed)
                    dbs_list = "\n".join(f"  • {d}" for d in db_names)
                    proceed = self._ask_confirm(
                        "Modulos modificados detectados",
                        f"Se detectaron {len(changed)} modulo(s) con cambios en esta "
                        f"sincronizacion:\n\n{names_list}\n\n"
                        f"¿Actualizar estas base(s) de datos con estos cambios (-u), "
                        f"una por una?\n\n{dbs_list}\n\n"
                        "Puede tardar varios minutos por cada una.",
                    )
                    if proceed:
                        binary, conf_path = mgr.resolve_service_launcher(p["service"])
                        for i, db in enumerate(db_names, 1):
                            self._q.put(("addons_progress",
                                (85, f"Actualizando '{db}' ({i}/{len(db_names)})...")))
                            mgr.update_db_modules(
                                db, changed, conf_path, binary,
                                odoo_user=p["odoo_user"],
                                log_callback=self._log,
                                cancel_event=self._cancel_event,
                            )
                    else:
                        self._log("Actualizacion de base de datos omitida por el usuario.")

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
