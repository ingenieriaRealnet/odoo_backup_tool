"""
"Panel" page: one-screen status of every monitored client.

This is the view the 2026-09-30 incident lacked. The information existed —
19 days of "Espacio insuficiente" in the Historial tab — but nothing put the
clients side by side with a verdict. Here each scheduled-backup rule is a
row with a level (OK / Alerta / Crítico / Sin datos), its disk usage and
projection, and its backup track record; selecting a row shows why it has
that level and how its disk has evolved.

The panel never talks to a server from the Tk thread: probing and reading
the stores happen in worker threads, results come back through the app's
queue (("health_refresh", summaries) and ("ui_call", callable)).
"""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import ttk
from typing import Callable

from core.alerting import email_is_configured
from core.health_eval import (
    DEFAULT_THRESHOLDS, LEVEL_CRIT, LEVEL_LABELS, LEVEL_OFF, LEVEL_OK,
    LEVEL_UNKNOWN, LEVEL_WARN, human_bytes,
)
from gui import theme

# Display order: what needs attention first.
_LEVEL_ORDER = {LEVEL_CRIT: 0, LEVEL_WARN: 1, LEVEL_UNKNOWN: 2, LEVEL_OK: 3, LEVEL_OFF: 4}

_CHART_DAYS = 30
_BAR_CELLS = 10

# (key, heading, base width, anchor). Every column stretches, so the base
# widths act as proportions and the table always fits the page width.
_COLUMNS = (
    ("estado", "Estado", 82, "w"),
    ("cliente", "Cliente", 140, "w"),
    ("disco", "Disco usado", 128, "w"),
    ("libre", "Libre", 74, "e"),
    ("llena", "Se llena en", 84, "center"),
    ("respaldo", "Último respaldo OK", 140, "w"),
    ("fallos", "Fallos", 56, "center"),
    ("pg", "PostgreSQL", 78, "center"),
    ("odoo", "Odoo", 52, "center"),
    ("sondeo", "Sondeo", 92, "w"),
)


def _ago(ts: float | None, now: float) -> str:
    """Human 'time since' in Spanish ('hace 3 h', 'hace 2 días')."""
    if not ts:
        return "—"
    secs = max(0.0, now - ts)
    if secs < 90:
        return "ahora"
    if secs < 3600:
        return f"hace {secs / 60:.0f} min"
    if secs < 86400:
        return f"hace {secs / 3600:.0f} h"
    return f"hace {secs / 86400:.0f} días"


def _disk_cell(pct: float | None) -> str:
    if pct is None:
        return "—"
    filled = min(_BAR_CELLS, max(0, round(pct / 100 * _BAR_CELLS)))
    return f"{pct:3.0f} %  {'█' * filled}{'·' * (_BAR_CELLS - filled)}"


def _days_cell(days: float | None) -> str:
    if days is None:
        return "—"
    if days > 365:
        return "> 1 año"
    return f"~{days:.0f} días"


class HealthPanel(ttk.Frame):
    """
    Args:
        parent:           Notebook page container.
        monitor:          core.monitor.HealthMonitor.
        ui_queue:         The app's thread->GUI queue.
        on_open_settings: Callback that shows the "Alertas" page.
    """

    def __init__(self, parent: tk.Widget, monitor, ui_queue: queue.Queue,
                 on_open_settings: Callable[[], None]) -> None:
        super().__init__(parent, padding=theme.PAD)
        self._monitor = monitor
        self._q = ui_queue
        self._on_open_settings = on_open_settings
        self._summaries: dict[str, dict] = {}
        self._busy = False
        # (timestamp, % used) of the selected client's fullest mount.
        self._chart_points: list[tuple[float, float]] = []
        self._chart_mount = ""

        self.columnconfigure(0, weight=1)
        # The client table is the point of the page: when the log panel
        # below leaves little height it keeps its rows and the detail area
        # is what gives way.
        self.rowconfigure(3, weight=3, minsize=130)
        self.rowconfigure(4, weight=2, minsize=90)

        self._build_header()
        self._build_counters()
        self._build_notice()
        self._build_table()
        self._build_detail()

    # ── Construction ─────────────────────────────────────────────────────────

    def _build_header(self) -> None:
        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Estado de los clientes", font=theme.FONT_TITLE).grid(row=0, column=0, sticky="w")
        self._lbl_updated = ttk.Label(header, text="Sin datos todavía", foreground=theme.C_MUTED)
        self._lbl_updated.grid(row=1, column=0, sticky="w")
        self._btn_probe = ttk.Button(header, text="Sondear ahora", style="Primary.TButton", command=self.probe_now)
        self._btn_probe.grid(row=0, column=1, rowspan=2, padx=(8, 0))
        ttk.Button(header, text="Configuración", command=self._on_open_settings).grid(
            row=0, column=2, rowspan=2, padx=(6, 0))

    def _build_counters(self) -> None:
        row = ttk.Frame(self)
        row.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        self._counters: dict[str, tk.Label] = {}
        for column, (level, caption) in enumerate((
            (LEVEL_CRIT, "Críticos"), (LEVEL_WARN, "En alerta"),
            (LEVEL_OK, "OK"), (LEVEL_UNKNOWN, "Sin datos"),
        )):
            fg, bg = theme.LEVEL_COLORS[level]
            card = tk.Frame(row, bg=bg, padx=14, pady=6)
            card.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 6, 0))
            row.columnconfigure(column, weight=1)
            number = tk.Label(card, text="0", font=("Segoe UI", 18, "bold"), fg=fg, bg=bg)
            number.pack(side="left")
            tk.Label(card, text=f"  {caption}", font=theme.FONT, fg=fg, bg=bg).pack(side="left")
            self._counters[level] = number

    def _build_notice(self) -> None:
        """Amber strip shown while alerts cannot leave the application."""
        fg, bg = theme.LEVEL_COLORS[LEVEL_WARN]
        self._notice = tk.Frame(self, bg=bg, padx=10, pady=5)
        self._lbl_notice = tk.Label(self._notice, text="", font=theme.FONT, fg=fg, bg=bg, anchor="w", justify="left")
        self._lbl_notice.pack(side="left", fill="x", expand=True)
        # Re-wrap to the real width whenever the page is resized (a fixed
        # wraplength either wastes half the strip or clips the text).
        self._notice.bind("<Configure>", lambda e: self._lbl_notice.config(wraplength=max(300, e.width - 24)))
        # Gridded on demand by _refresh_notice().

    def _build_table(self) -> None:
        box = ttk.Frame(self)
        box.grid(row=3, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)

        style = ttk.Style(self)
        style.configure("Health.Treeview", rowheight=26, font=theme.FONT)
        style.configure("Health.Treeview.Heading", font=theme.FONT_BOLD)

        self._tree = ttk.Treeview(
            box, columns=[c[0] for c in _COLUMNS], show="headings",
            selectmode="browse", style="Health.Treeview", height=7,
        )
        for key, title, width, anchor in _COLUMNS:
            self._tree.heading(key, text=title)
            self._tree.column(key, width=width, minwidth=40, anchor=anchor, stretch=True)
        self._tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(box, orient="vertical", command=self._tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self._tree.configure(yscrollcommand=scrollbar.set)
        for level, (fg, bg) in theme.LEVEL_COLORS.items():
            self._tree.tag_configure(level, foreground=fg, background=bg if level in (LEVEL_CRIT, LEVEL_WARN) else "")
        self._tree.bind("<<TreeviewSelect>>", lambda _e: self._show_detail())

    def _build_detail(self) -> None:
        detail = ttk.Frame(self)
        detail.grid(row=4, column=0, sticky="nsew", pady=(6, 0))
        detail.columnconfigure(0, weight=1, uniform="detail")
        detail.columnconfigure(1, weight=1, uniform="detail")
        detail.rowconfigure(0, weight=1)

        left = ttk.LabelFrame(detail, text="Diagnóstico", padding=6)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        self._txt = tk.Text(left, height=7, wrap="word", font=theme.FONT, relief="flat",
                            bg=theme.C_WHITE, fg=theme.C_TEXT, padx=6, pady=4)
        self._txt.pack(fill="both", expand=True)
        for level, (fg, _bg) in theme.LEVEL_COLORS.items():
            self._txt.tag_configure(level, foreground=fg)
        self._txt.tag_configure("title", font=theme.FONT_BOLD)
        self._txt.tag_configure("muted", foreground=theme.C_MUTED)
        self._txt.config(state="disabled")

        right = ttk.LabelFrame(detail, text=f"Uso de disco, últimos {_CHART_DAYS} días", padding=6)
        right.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        self._chart = tk.Canvas(right, height=140, bg=theme.C_WHITE, highlightthickness=0)
        self._chart.pack(fill="both", expand=True)
        self._chart.bind("<Configure>", lambda _e: self._draw_chart())

    # ── Data in ──────────────────────────────────────────────────────────────

    def request_refresh(self) -> None:
        """Re-evaluate from stored data (no network) in a worker thread."""
        def _work() -> None:
            try:
                summaries = self._monitor.summaries()
            except Exception:  # noqa: BLE001 — a repaint must never crash the app
                return
            self._q.put(("health_refresh", summaries))
        threading.Thread(target=_work, name="health-panel-refresh", daemon=True).start()

    def probe_now(self) -> None:
        """Probe every server immediately, ignoring the configured interval."""
        if self._busy:
            return
        self._busy = True
        self._btn_probe.config(state="disabled", text="Sondeando...")

        def _work() -> None:
            try:
                # run_cycle returns [] if the cycle itself failed; the stored
                # data is still worth showing in that case.
                summaries = self._monitor.run_cycle(force=True) or self._monitor.summaries()
                self._q.put(("health_refresh", summaries))
            finally:
                self._q.put(("ui_call", self._probe_finished))
        threading.Thread(target=_work, name="health-panel-probe", daemon=True).start()

    def _probe_finished(self) -> None:
        self._busy = False
        self._btn_probe.config(state="normal", text="Sondear ahora")

    def update_summaries(self, summaries: list[dict]) -> None:
        """Repaint with fresh summaries (GUI thread only)."""
        now = time.time()
        selected = self._tree.selection()
        self._summaries = {s["rule_id"]: s for s in summaries}

        for item in self._tree.get_children():
            self._tree.delete(item)
        counts = {level: 0 for level in self._counters}
        for s in sorted(summaries, key=lambda s: (_LEVEL_ORDER.get(s["level"], 9), s["label"].lower())):
            level = s["level"]
            if level in counts:
                counts[level] += 1
            self._tree.insert("", "end", iid=s["rule_id"], tags=(level,), values=self._row_values(s, now))
        for level, label in self._counters.items():
            label.config(text=str(counts[level]))

        self._lbl_updated.config(text=f"Actualizado {datetime.fromtimestamp(now).strftime('%H:%M:%S')}"
                                      f"  ·  {len(summaries)} cliente(s) con respaldo programado")
        self._refresh_notice()

        if selected and selected[0] in self._summaries:
            self._tree.selection_set(selected[0])
        elif self._tree.get_children():
            self._tree.selection_set(self._tree.get_children()[0])
        self._show_detail()

    @staticmethod
    def _row_values(s: dict, now: float) -> tuple:
        if s["level"] == LEVEL_OFF:
            return ("○ " + LEVEL_LABELS[LEVEL_OFF], s["label"], "—", "—", "—", "—", "—", "—", "—", "—")
        if s.get("reachable") is False:
            pg = odoo = "—"
        else:
            pg = {True: "OK", False: "Caído", None: "—"}[s.get("pg_ok")]
            odoo = {"200": "OK", "none": "n/d", None: "—"}.get(s.get("odoo_http"), f"HTTP {s.get('odoo_http')}")
        last_ok = "Nunca" if not s.get("last_ok_ts") else (
            f"{_ago(s['last_ok_ts'], now)} ({datetime.fromtimestamp(s['last_ok_ts']).strftime('%d-%b')})")
        return (
            "● " + LEVEL_LABELS.get(s["level"], s["level"]),
            s["label"],
            _disk_cell(s.get("disk_pct")),
            human_bytes(s.get("disk_free")),
            _days_cell(s.get("days_to_full")),
            last_ok,
            s.get("consecutive_failures", 0) or "0",
            pg, odoo,
            _ago(s.get("probe_ts"), now),
        )

    def _refresh_notice(self) -> None:
        settings = self._monitor.settings.get()
        messages = []
        if not settings.get("enabled", True):
            messages.append("El monitoreo automático está desactivado: los datos solo se actualizan con «Sondear ahora».")
        if not email_is_configured(settings.get("email", {})):
            messages.append("Las alertas por correo no están configuradas: los avisos solo se ven en este panel. "
                            "Use «Configuración».")
        if not self._monitor.owner:
            messages.append("Esta ventana no ejecuta el programador: los respaldos y alertas los gestiona otra "
                            "instancia (monitor en segundo plano) o están desactivados en este modo.")
        if messages:
            self._lbl_notice.config(text="\n".join(messages))
            self._notice.grid(row=2, column=0, sticky="ew", pady=(0, 6))
        else:
            self._notice.grid_remove()

    # ── Detail ───────────────────────────────────────────────────────────────

    def _selected(self) -> dict | None:
        selection = self._tree.selection()
        return self._summaries.get(selection[0]) if selection else None

    def _show_detail(self) -> None:
        summary = self._selected()
        self._txt.config(state="normal")
        self._txt.delete("1.0", "end")
        if summary:
            self._txt.insert("end", f"{summary['label']}", "title")
            self._txt.insert("end", f"   {summary.get('host') or ''}  ·  BD {summary.get('db_name') or '—'}"
                                    f" ({human_bytes(summary.get('db_bytes'))})\n", "muted")
            if not summary["findings"]:
                text = ("Regla deshabilitada: no se monitorea." if summary["level"] == LEVEL_OFF
                        else "Sin hallazgos: disco, servicios y respaldos dentro de los umbrales.")
                self._txt.insert("end", text + "\n", LEVEL_OK if summary["level"] == LEVEL_OK else "muted")
            for finding in summary["findings"]:
                self._txt.insert("end", f"● {finding['message']}\n", finding["level"])
            if summary.get("last_error") and summary.get("consecutive_failures"):
                self._txt.insert("end", "\nÚltimo error del respaldo:\n", "title")
                self._txt.insert("end", summary["last_error"].strip() + "\n", "muted")
        self._txt.config(state="disabled")
        self._load_chart_samples(summary)

    def _load_chart_samples(self, summary: dict | None) -> None:
        self._chart_points = []
        self._chart_mount = ""
        if summary and summary.get("profile"):
            since = time.time() - _CHART_DAYS * 86400
            # Small local file read; cheap enough for the GUI thread.
            samples = self._monitor.store.by_profile(since_ts=since).get(summary["profile"], [])
            mount = summary.get("disk_mount")
            for sample in samples:
                for entry in sample.get("mounts") or []:
                    capacity = entry["used"] + entry["avail"]
                    if capacity > 0 and (mount is None or entry["mount"] == mount):
                        self._chart_points.append((sample["ts"], 100.0 * entry["used"] / capacity))
                        self._chart_mount = entry["mount"]
                        break
        self._draw_chart()

    def _draw_chart(self) -> None:
        canvas = self._chart
        canvas.delete("all")
        width, height = canvas.winfo_width(), canvas.winfo_height()
        if width < 60 or height < 60:
            return
        left, right, top, bottom = 38, 12, 10, 22
        plot_w, plot_h = width - left - right, height - top - bottom
        points = self._chart_points

        def y_of(pct: float) -> float:
            return top + plot_h * (1 - pct / 100.0)

        for pct in (0, 50, 100):
            canvas.create_line(left, y_of(pct), width - right, y_of(pct), fill="#EEE9EC")
            canvas.create_text(left - 6, y_of(pct), text=f"{pct} %", anchor="e", font=theme.FONT_SMALL, fill=theme.C_MUTED)

        limits = {**DEFAULT_THRESHOLDS, **(self._monitor.settings.get().get("thresholds") or {})}
        for key, level in (("disk_warn_pct", LEVEL_WARN), ("disk_crit_pct", LEVEL_CRIT)):
            y = y_of(float(limits[key]))
            canvas.create_line(left, y, width - right, y, fill=theme.LEVEL_COLORS[level][0], dash=(4, 3))

        if not points:
            canvas.create_text(left + plot_w / 2, top + plot_h / 2, font=theme.FONT, fill=theme.C_MUTED,
                               text="Aún no hay sondeos de este servidor")
            return

        now = time.time()
        start = now - _CHART_DAYS * 86400

        def x_of(ts: float) -> float:
            return left + plot_w * (ts - start) / (now - start)

        coords: list[float] = []
        for ts, pct in points:
            coords += [x_of(ts), y_of(pct)]
        if len(points) > 1:
            canvas.create_line(*coords, fill=theme.C_PURPLE, width=2)
        last_x, last_y = coords[-2], coords[-1]
        canvas.create_oval(last_x - 3, last_y - 3, last_x + 3, last_y + 3, fill=theme.C_PURPLE, outline="")
        canvas.create_text(min(last_x, width - right) - 6, max(top + 8, last_y - 10), anchor="e",
                           text=f"{points[-1][1]:.0f} %  ({self._chart_mount})", font=theme.FONT_BOLD, fill=theme.C_PURPLE)

        canvas.create_text(left, height - 8, text=f"hace {_CHART_DAYS} días", anchor="w", font=theme.FONT_SMALL, fill=theme.C_MUTED)
        canvas.create_text(width - right, height - 8, text="hoy", anchor="e", font=theme.FONT_SMALL, fill=theme.C_MUTED)
        if len(points) == 1:
            canvas.create_text(left + plot_w / 2, top + 10, font=theme.FONT_SMALL, fill=theme.C_MUTED,
                               text="La tendencia aparece con varios días de sondeos")
