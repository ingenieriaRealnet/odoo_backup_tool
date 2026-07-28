"""
Persistent history of server connections/actions performed by the tool.

Independent of the in-memory GUI log panel, which is cleared on every app
close by design (see gui/app.py._append_log). This gives a durable record —
across app restarts — of every manual backup, restore, and addons sync run,
plus every scheduled backup rule execution, so a client's activity can be
audited without needing the app open at the time it happened.

Storage: JSON Lines (append-only) at
~/.odoo_backup_tool/connection_history.jsonl — one line per entry, so adding
a record never needs to rewrite the whole file (unlike servers.json /
schedules.json, which are small and rewritten wholesale on every save).
Pruned back down to _MAX_ENTRIES once it grows past _PRUNE_THRESHOLD, so a
long-running install doesn't grow this file without bound.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid


def _history_path() -> str:
    base = os.path.join(os.path.expanduser("~"), ".odoo_backup_tool")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "connection_history.jsonl")


class HistoryManager:
    """Append-only, thread-safe log of server connections/actions."""

    _MAX_ENTRIES = 500
    _PRUNE_THRESHOLD = 600  # only prune once growth clearly exceeds the cap,
                            # so a normal session doesn't rewrite the file
                            # on every single entry

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path = _history_path()

    def record(
        self,
        action_type: str,
        server_label: str,
        host: str,
        status: str,
        summary: str,
        log_text: str = "",
        started_at: float | None = None,
    ) -> None:
        """
        Append one history entry.

        Args:
            action_type:  "backup_manual" | "backup_scheduled" | "restore" |
                          "addons_sync" | "terminal"
            server_label: Profile/rule name shown to the user (falls back to
                          host when no profile is selected).
            host:         Server host[:port], for quick identification.
            status:       "ok" | "error" | "cancelled" | "warning"
            summary:      One-line human summary (shown in the history table).
            log_text:     Full captured log text for this operation, shown on
                          demand in the "Ver detalle" panel.
            started_at:   time.time() when the operation began; defaults to now.
        """
        entry = {
            "id": uuid.uuid4().hex[:12],
            "started_at": started_at if started_at is not None else time.time(),
            "ended_at": time.time(),
            "action_type": action_type,
            "server_label": server_label,
            "host": host,
            "status": status,
            "summary": summary,
            "log": log_text,
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._prune_if_needed()

    def list_entries(self, limit: int | None = None) -> list[dict]:
        """Return entries newest-first. `limit` caps how many are returned."""
        entries = self._read_all()
        entries.sort(key=lambda e: e.get("ended_at", 0), reverse=True)
        if limit:
            entries = entries[:limit]
        return entries

    def _read_all(self) -> list[dict]:
        if not os.path.isfile(self._path):
            return []
        entries: list[dict] = []
        with self._lock:
            try:
                with open(self._path, encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue  # skip a corrupted line, don't lose the rest
            except OSError:
                return []
        return entries

    def _prune_if_needed(self) -> None:
        """Keep only the newest _MAX_ENTRIES once the file grows past
        _PRUNE_THRESHOLD. Caller already holds self._lock."""
        try:
            with open(self._path, encoding="utf-8") as fh:
                lines = [ln for ln in (l.strip() for l in fh) if ln]
        except OSError:
            return
        if len(lines) <= self._PRUNE_THRESHOLD:
            return
        keep = lines[-self._MAX_ENTRIES:]
        tmp_path = self._path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(keep) + "\n")
        os.replace(tmp_path, self._path)
