"""
Scheduled-backup rules page (Automatización) and the persistent history page.

Part of BackupApp (gui/app.py), moved here unchanged.
"""
from __future__ import annotations
import datetime
import tkinter as tk
from tkinter import messagebox, ttk
from core.scheduler import request_rule_run
from gui.constants import APP_TITLE, _PAD, _HISTORY_TYPE_LABELS
from gui.dialogs import _ScheduleDialog


class AutomationMixin:
    """
    Scheduled-backup rules page (Automatización) and the persistent history page.

    Mixin of gui.app.BackupApp: every method runs with `self` being the
    application object, so widgets and state created by other mixins are
    reachable exactly as before the split.
    """

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
        if not self._scheduler_owner:
            # This window does not run the schedule. Starting the job here
            # would be the double-run the scheduler lock exists to prevent.
            if self._no_scheduler:
                messagebox.showinfo(
                    APP_TITLE,
                    "Esta ventana se abrió en modo sin programador: no ejecuta reglas.",
                )
                return
            request_rule_run(rule_id)
            self._sched_append_log(
                f"[{label}] Ejecucion solicitada al monitor en segundo plano (inicia en menos de un minuto)."
            )
            return
        # Delegate to the scheduler's own guarded entry point — run_rule_now()
        # checks the same _active_jobs lock _tick() uses, so this can never
        # collide with an automatic run of the same rule firing at the same
        # moment (both used to be able to run _run_rule concurrently for the
        # same rule_id, racing on the same /tmp remote file paths).
        if not self._scheduler.run_rule_now(rule):
            messagebox.showinfo(
                APP_TITLE,
                f'El backup "{label}" ya se esta ejecutando — espere a que termine.',
            )
            return
        self._sched_append_log(f"[{label}] Ejecucion manual iniciada...")

    def _sched_append_log(self, message: str) -> None:
        """
        Write a scheduler-related line to the log panel (GUI thread).

        _sched_run_now() always called this, but it was never defined: the
        job started and then the click handler died with AttributeError.
        """
        self._append_log(message)

    # ── Tab Historial ──────────────────────────────────────────────────────

    def _tab_history(self) -> None:
        """
        Tab Historial: persistent, cross-restart log of every backup/restore/
        addons-sync (manual or scheduled) and terminal session — see
        core/history_manager.py. Unlike self.log_widget (cleared on every app
        close by design), this survives closing the app so a client's
        activity can be audited later.
        """
        outer = ttk.Frame(self.nb, padding=_PAD)
        self.nb.add(outer, text="  Historial  ")
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)

        # ── Filter bar ──────────────────────────────────────────────────────
        bar = ttk.Frame(outer)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        ttk.Label(bar, text="Tipo:").pack(side="left", padx=(0, 4))
        self._v_hist_filter = tk.StringVar(value="Todos")
        cb = ttk.Combobox(
            bar,
            textvariable=self._v_hist_filter,
            values=["Todos"] + list(_HISTORY_TYPE_LABELS.keys()),
            state="readonly",
            width=20,
        )
        cb.pack(side="left", padx=(0, 12))
        cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_history_tree())

        ttk.Button(
            bar, text="Actualizar", command=self._refresh_history_tree,
        ).pack(side="left")
        ttk.Button(
            bar, text="Ver detalle", command=self._history_show_detail,
        ).pack(side="left", padx=(4, 0))

        # ── Tree ──────────────────────────────────────────────────────────
        lf = ttk.LabelFrame(outer, text="Conexiones y acciones registradas", padding=_PAD)
        lf.grid(row=1, column=0, sticky="nsew")
        lf.columnconfigure(0, weight=1)
        lf.rowconfigure(0, weight=1)

        cols = ("Fecha", "Tipo", "Servidor", "Resultado", "Resumen")
        self._hist_tree = ttk.Treeview(
            lf, columns=cols, show="headings", height=18, selectmode="browse",
        )
        for col, w in zip(cols, (140, 130, 160, 90, 380)):
            self._hist_tree.heading(col, text=col)
            self._hist_tree.column(col, width=w, anchor="w")
        self._hist_tree.grid(row=0, column=0, sticky="nsew")

        vsb = ttk.Scrollbar(lf, orient="vertical", command=self._hist_tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        self._hist_tree.configure(yscrollcommand=vsb.set)

        self._hist_tree.tag_configure("ok",        foreground="#2E8B57")
        self._hist_tree.tag_configure("error",     foreground="#C0392B")
        self._hist_tree.tag_configure("cancelled", foreground="#888888")
        self._hist_tree.tag_configure("warning",   foreground="#E67E22")

        self._hist_tree.bind("<Double-1>", lambda e: self._history_show_detail())

        # Cache of entries currently shown in the tree, keyed by Treeview iid,
        # so "Ver detalle" can find the full log text without re-reading disk.
        self._hist_entries_by_iid: dict[str, dict] = {}

        self._refresh_history_tree()

    def _refresh_history_tree(self) -> None:
        """Reload the history table from disk, applying the current filter."""
        tree = getattr(self, "_hist_tree", None)
        if tree is None:
            return
        children = tree.get_children()
        if children:
            tree.delete(*children)
        self._hist_entries_by_iid = {}

        filt = self._v_hist_filter.get()
        for i, entry in enumerate(self._history.list_entries(limit=500)):
            if filt != "Todos" and entry.get("action_type") != filt:
                continue
            iid = entry.get("id") or str(i)
            self._hist_entries_by_iid[iid] = entry

            ended_at = entry.get("ended_at")
            ts = (
                datetime.datetime.fromtimestamp(ended_at).strftime("%Y-%m-%d %H:%M:%S")
                if ended_at else "—"
            )
            type_label = _HISTORY_TYPE_LABELS.get(
                entry.get("action_type", ""), entry.get("action_type", "")
            )
            status = entry.get("status", "")
            server = entry.get("server_label") or entry.get("host") or "—"
            summary = (entry.get("summary") or "")[:150]

            tree.insert(
                "", "end", iid=iid,
                values=(ts, type_label, server, status, summary),
                tags=(status,) if status in ("ok", "error", "cancelled", "warning") else (),
            )

    def _history_show_detail(self) -> None:
        """Open a window with the full captured log for the selected history row."""
        tree = getattr(self, "_hist_tree", None)
        if tree is None:
            return
        sel = tree.selection()
        if not sel:
            messagebox.showinfo(APP_TITLE, "Seleccione una fila del historial primero.")
            return
        entry = self._hist_entries_by_iid.get(sel[0])
        if not entry:
            return

        top = tk.Toplevel(self.root)
        top.title(f"Detalle — {entry.get('server_label', '')}")
        top.geometry("800x500")

        type_label = _HISTORY_TYPE_LABELS.get(
            entry.get("action_type", ""), entry.get("action_type", "")
        )
        header = (
            f"Tipo: {type_label}\n"
            f"Servidor: {entry.get('server_label', '')}  ({entry.get('host', '')})\n"
            f"Resultado: {entry.get('status', '')}\n"
            f"Resumen: {entry.get('summary', '')}\n"
            f"{'-' * 80}\n"
        )
        txt = tk.Text(top, wrap="word")
        txt.pack(fill="both", expand=True, padx=8, pady=8)
        txt.insert("end", header + (entry.get("log") or "(sin log detallado)"))
        txt.config(state="disabled")

        ttk.Button(top, text="Cerrar", command=top.destroy).pack(pady=(0, 8))
