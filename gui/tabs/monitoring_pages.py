"""
Sidebar sync and the monitoring pages (Panel de clientes, Alertas).

Part of BackupApp (gui/app.py), moved here unchanged.
"""
from __future__ import annotations
from tkinter import ttk
from core.health_eval import LEVEL_CRIT, LEVEL_WARN
from gui import theme
from gui.health_panel import HealthPanel
from gui.alerts_panel import AlertsPanel
from gui.constants import _TAB_PANEL, _TAB_ALERTS, _SIDEBAR_SYNC_MS


class MonitoringPagesMixin:
    """
    Sidebar sync and the monitoring pages (Panel de clientes, Alertas).

    Mixin of gui.app.BackupApp: every method runs with `self` being the
    application object, so widgets and state created by other mixins are
    reachable exactly as before the split.
    """

    # ── Sidebar / monitoring pages ────────────────────────────────────────

    def _sync_sidebar_loop(self) -> None:
        """
        Keep the sidebar in step with the notebook.

        Pages are enabled/disabled from many places (self.nb.tab(i,
        state=...) as the wizard advances or the SSH session drops) and ttk
        raises no event for that, so the sidebar re-reads the states on a
        short timer instead of every call site having to notify it.
        """
        self._sidebar.sync()
        self.root.after(_SIDEBAR_SYNC_MS, self._sync_sidebar_loop)

    def _tab_health_panel(self) -> None:
        """Page "Panel de clientes": the monitoring dashboard (gui/health_panel.py)."""
        outer = ttk.Frame(self.nb)
        self.nb.add(outer, text="  Panel  ")
        self._health_panel = HealthPanel(
            outer, self._monitor, self._q,
            on_open_settings=lambda: self.nb.select(_TAB_ALERTS),
        )
        self._health_panel.pack(fill="both", expand=True)

    def _tab_alert_settings(self) -> None:
        """Page "Alertas": thresholds and e-mail account (gui/alerts_panel.py)."""
        frame = self._scrollable_tab("  Alertas  ")
        self._alerts_panel = AlertsPanel(
            frame, self._monitor, self._q,
            on_saved=lambda: self._health_panel.request_refresh(),
        )
        self._alerts_panel.pack(fill="x")

    def _on_health_refresh(self, summaries: list) -> None:
        """Repaint everything that shows client health (GUI thread)."""
        if self._health_panel is not None:
            self._health_panel.update_summaries(summaries)

        critical = sum(1 for s in summaries if s["level"] == LEVEL_CRIT)
        warning = sum(1 for s in summaries if s["level"] == LEVEL_WARN)
        if critical:
            self._sidebar.set_badge(_TAB_PANEL, str(critical), theme.LEVEL_COLORS[LEVEL_CRIT][0])
            text, color = f"Monitor: {critical} crítico(s)", theme.LEVEL_COLORS[LEVEL_CRIT][0]
        elif warning:
            self._sidebar.set_badge(_TAB_PANEL, str(warning), theme.LEVEL_COLORS[LEVEL_WARN][0])
            text, color = f"Monitor: {warning} en alerta", theme.LEVEL_COLORS[LEVEL_WARN][0]
        else:
            self._sidebar.set_badge(_TAB_PANEL, "", None)
            text, color = "Monitor: sin alertas", "#2E8B57"
        self._lbl_status_monitor.config(text=f"● {text}  ", fg=color)
