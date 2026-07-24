"""
Registry of remote temp files created by this tool on Odoo servers.

Every dump / filestore archive / bundle / inventory this tool writes to
/tmp on a remote server is registered here BEFORE the SSH connection that
created it is allowed to close. The entry is removed only after that same
file has actually been deleted from the server.

Why this exists: cleanup of /tmp on the remote server used to happen only
on the "everything succeeded" path. If a job failed partway, was cancelled,
or the whole app crashed / was force-closed, whatever had already been
created stayed on the server forever with nothing tracking it. Because this
manifest lives on local disk (not in memory), it survives all of those —
next time the app starts, sweep_orphaned_files() reads it back and finishes
the cleanup the interrupted run never got to do.

The manifest intentionally stores raw host/port/user/password per entry
(not just a profile name) because manual backups (Tab 1) can connect with
ad-hoc credentials that were never saved as a profile — the registry must
be able to reconnect on its own regardless of where the connection came
from. This matches the plaintext-password precedent already established in
profiles.py (core/profiles.py) for the same reason (local admin tool only).
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

_REGISTRY_DIR  = Path.home() / ".odoo_backup_tool"
_REGISTRY_FILE = _REGISTRY_DIR / "remote_tmp_manifest.json"

# Entries newer than this are left alone during a sweep even if their file
# still exists — avoids racing a job that is still legitimately running
# (e.g. a sweep triggered manually while a backup is mid-flight).
_MIN_AGE_FOR_SWEEP_SECS = 600


class RemoteTempRegistry:
    """
    Thread-safe, disk-persisted list of "files this tool created on a
    remote server and has not yet confirmed deleting".
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: list[dict] = []
        self._load()

    # ── Persistence (same atomic-write pattern as ProfileManager) ─────────

    def _load(self) -> None:
        if _REGISTRY_FILE.exists():
            try:
                with open(_REGISTRY_FILE, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    self._entries = data.get("entries", [])
            except (json.JSONDecodeError, OSError):
                self._entries = []

    def _save(self) -> None:
        """Must be called with self._lock held."""
        _REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = _REGISTRY_FILE.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump({"entries": self._entries}, fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, _REGISTRY_FILE)

    # ── Public API ──────────────────────────────────────────────────────

    def register(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        path: str,
        kind: str,
        label: str = "",
    ) -> str:
        """
        Record a remote temp file as "created, not yet confirmed deleted".

        Args:
            host, port, user, password: how to reconnect to delete it later.
            path: absolute path of the file on the remote server.
            kind: "dump" | "filestore" | "bundle" | "inventory" — informational.
            label: rule label or db name, for readable sweep logs.

        Returns:
            The entry id (pass to unregister() once the file is deleted).
        """
        entry_id = str(uuid.uuid4())
        entry = {
            "id":         entry_id,
            "host":       host,
            "port":       int(port),
            "user":       user,
            "password":   password,
            "path":       path,
            "kind":       kind,
            "label":      label,
            "created_at": datetime.now(tz=timezone.utc).isoformat(),
        }
        with self._lock:
            self._entries.append(entry)
            self._save()
        return entry_id

    def unregister(self, entry_id: str) -> None:
        """Remove an entry once its file has been confirmed deleted."""
        with self._lock:
            before = len(self._entries)
            self._entries = [e for e in self._entries if e["id"] != entry_id]
            if len(self._entries) != before:
                self._save()

    def unregister_path(self, host: str, path: str) -> None:
        """
        Remove any entry for (host, path) without needing its id.

        Convenience for callers that didn't capture the id returned by
        register() — e.g. a retry flow running in a separate thread/closure
        from the one that originally created the file, or the manual
        "delete without transferring" cleanup action.
        """
        with self._lock:
            before = len(self._entries)
            self._entries = [
                e for e in self._entries
                if not (e["host"] == host and e["path"] == path)
            ]
            if len(self._entries) != before:
                self._save()

    def list_all(self) -> list[dict]:
        with self._lock:
            return [dict(e) for e in self._entries]


def sweep_orphaned_files(
    registry: RemoteTempRegistry,
    log_callback: Callable[[str], None] | None = None,
    min_age_secs: int = _MIN_AGE_FOR_SWEEP_SECS,
) -> int:
    """
    Connect to every server referenced in the registry and delete any
    registered file that is still there and old enough to be safely
    considered orphaned (not from a job that might still be running).

    Entries are grouped by (host, port, user) so a server with several
    orphaned files only needs one SSH connection. Connection failures for
    a given server are logged and that server's entries are left in the
    registry to retry on the next sweep — a temporarily unreachable server
    must not make the tool "forget" about its orphaned files.

    Returns:
        Number of remote files actually deleted.
    """
    from .ssh_client import SSHClient  # local import: avoids a hard
    # dependency on paramiko for callers that only need the registry itself.

    entries = registry.list_all()
    if not entries:
        return 0

    now = datetime.now(tz=timezone.utc)
    by_server: dict[tuple, list[dict]] = {}
    for e in entries:
        try:
            age_secs = (now - datetime.fromisoformat(e["created_at"])).total_seconds()
        except (ValueError, KeyError):
            age_secs = min_age_secs + 1  # malformed timestamp — sweep it anyway
        if age_secs < min_age_secs:
            continue
        key = (e["host"], e["port"], e["user"], e["password"])
        by_server.setdefault(key, []).append(e)

    if not by_server:
        return 0

    def _log(msg: str) -> None:
        if log_callback:
            log_callback(f"[limpieza-huerfanos] {msg}")

    deleted = 0
    for (host, port, user, password), server_entries in by_server.items():
        ssh = SSHClient()
        try:
            ssh.connect(host, port, user, password, timeout=10)
        except Exception as exc:  # noqa: BLE001
            _log(
                f"No se pudo conectar a {host}:{port} para limpiar "
                f"{len(server_entries)} archivo(s) huerfano(s) — se reintentara "
                f"en el proximo inicio. ({exc})"
            )
            continue

        try:
            for e in server_entries:
                path = e["path"]
                label = e.get("label") or "?"
                try:
                    code, _, _ = ssh.execute(f"test -f {path}")
                    if code == 0:
                        ssh.execute(f"sudo rm -f {path}")
                        deleted += 1
                        _log(f"[{label}] Eliminado huerfano en {host}: {path}")
                    # File already gone (cleaned up normally, or manually) —
                    # unregister either way, nothing left to track.
                    registry.unregister(e["id"])
                except Exception as exc:  # noqa: BLE001
                    _log(f"[{label}] No se pudo eliminar {path} en {host}: {exc}")
        finally:
            ssh.close()

    return deleted
