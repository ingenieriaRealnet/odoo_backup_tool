"""
Server connection profile manager.

Saves and loads SSH connection profiles to/from a JSON file in the
user's home directory (~/.odoo_backup_tool/servers.json).

Passwords are stored in plain text — this tool is intended for local
admin use only. Do not deploy where the profile file could be exposed.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


# Storage location: ~/.odoo_backup_tool/servers.json
_PROFILE_DIR  = Path.home() / ".odoo_backup_tool"
_PROFILE_FILE = _PROFILE_DIR / "servers.json"


class ProfileManager:
    """
    Manages a persistent list of SSH server connection profiles.

    Profile structure (per entry):
        name (str)     — human-readable label shown in the UI dropdown
        host (str)     — IP or hostname
        port (int)     — SSH port (default 22)
        user (str)     — SSH username
        password (str) — SSH password (plain text)
    """

    def __init__(self) -> None:
        self._profiles: list[dict] = []
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load profiles from disk. Silently starts empty if file missing."""
        if _PROFILE_FILE.exists():
            try:
                with open(_PROFILE_FILE, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                    self._profiles = data.get("servers", [])
            except (json.JSONDecodeError, OSError):
                # Corrupt or unreadable file — start fresh
                self._profiles = []

    def _save(self) -> None:
        """Persist the current profile list to disk."""
        _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        with open(_PROFILE_FILE, "w", encoding="utf-8") as fh:
            json.dump({"servers": self._profiles}, fh, indent=2, ensure_ascii=False)

    # ── Public API ────────────────────────────────────────────────────────

    def names(self) -> list[str]:
        """Return the list of profile names in storage order."""
        return [p["name"] for p in self._profiles]

    def get(self, name: str) -> Optional[dict]:
        """
        Return the profile dict for the given name, or None if not found.

        Returns:
            dict with keys: name, host, port, user, password
        """
        for p in self._profiles:
            if p["name"] == name:
                return dict(p)
        return None

    def save(
        self,
        name: str,
        host: str,
        port: int,
        user: str,
        password: str,
    ) -> None:
        """
        Save (create or update) a profile with the given name.

        If a profile with the same name already exists it is overwritten.

        Args:
            name: Human-readable label (shown in dropdowns).
            host: IP or hostname.
            port: SSH port.
            user: SSH username.
            password: SSH password.

        Raises:
            ValueError: If name or host is empty.
        """
        name = name.strip()
        host = host.strip()
        if not name:
            raise ValueError("El nombre del perfil no puede estar vacio.")
        if not host:
            raise ValueError("El host no puede estar vacio.")

        entry = {
            "name": name,
            "host": host,
            "port": int(port),
            "user": user.strip(),
            "password": password,
        }

        # Replace existing entry with same name, or append
        for i, p in enumerate(self._profiles):
            if p["name"] == name:
                self._profiles[i] = entry
                self._save()
                return

        self._profiles.append(entry)
        self._save()

    def delete(self, name: str) -> bool:
        """
        Remove the profile with the given name.

        Returns:
            True if a profile was removed, False if it did not exist.
        """
        before = len(self._profiles)
        self._profiles = [p for p in self._profiles if p["name"] != name]
        if len(self._profiles) < before:
            self._save()
            return True
        return False
