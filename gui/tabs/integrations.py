"""
"Conexiones API" page (gui/api_panel.py).

Part of BackupApp (gui/app.py). Its page is appended after the monitoring
pages, see _TAB_API in gui/constants.py.
"""
from __future__ import annotations

from core.api.connections import ApiConnectionManager
from gui.api_panel import ApiPanel


class IntegrationsMixin:
    """Mixin of gui.app.BackupApp: `self` is the application object."""

    def _tab_api_connections(self) -> None:
        """Page "Conexiones API": external systems reachable by API."""
        frame = self._scrollable_tab("  API  ")
        self._api_manager = ApiConnectionManager()
        self._api_panel = ApiPanel(frame, self._api_manager, self._q)
        self._api_panel.pack(fill="x")
