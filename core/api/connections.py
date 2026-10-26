"""
API connection profiles: ~/.odoo_backup_tool/api_connections.json

Each connection describes ONE external system the tool can talk to:
    name (str)        unique label, also the key of its secret in the Credential Manager
    type (str)        a key of core.api.connectors.CONNECTOR_TYPES ("odoo_json2", "rest_bearer")
    base_url (str)    e.g. https://realnet.com.co
    user (str)        login the API key belongs to (informative, shown in the Credential Manager)
    <type fields>     e.g. "database" for odoo_json2
    settings (dict)   free parameters for the services that use the connection
                      (e.g. project/stages of the Realnet task board)
    notes (str)

The API key itself is NOT in this file: see core/api/credentials.py.
Persistence follows core/profiles.py: lock, atomic replace, reload when another
process (the headless monitor, the CLI) changed the file.
"""
from __future__ import annotations

import copy
import json
import os
import re
import threading
from pathlib import Path

from core.api.connectors import CONNECTOR_TYPES, ApiConnector, ApiError
from core.api.credentials import WindowsCredentialStore

_DEFAULT_PATH = Path.home() / ".odoo_backup_tool" / "api_connections.json"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,48}$")


class ApiConnectionManager:
    def __init__(self, path: Path | None = None, secret_store=None) -> None:
        self._path = Path(path) if path else _DEFAULT_PATH
        self._secrets = secret_store or WindowsCredentialStore()
        self._lock = threading.Lock()
        self._connections: list[dict] = []
        self._mtime = 0.0
        self._load()

    # ── Persistence ───────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            self._mtime = self._path.stat().st_mtime
            with open(self._path, encoding="utf-8") as fh:
                self._connections = json.load(fh).get("connections", [])
        except FileNotFoundError:
            self._connections = []
        except (json.JSONDecodeError, OSError):
            # Unreadable mid-write by another process: keep what we had.
            pass

    def _reload_if_changed(self) -> None:
        try:
            if self._path.stat().st_mtime != self._mtime:
                self._load()
        except OSError:
            pass

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"connections": self._connections}, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self._path)
        self._mtime = self._path.stat().st_mtime

    # ── Queries ───────────────────────────────────────────────────────────

    def list(self) -> list[dict]:
        with self._lock:
            self._reload_if_changed()
            return copy.deepcopy(self._connections)

    def get(self, name: str) -> dict | None:
        with self._lock:
            self._reload_if_changed()
            found = next((c for c in self._connections if c["name"] == name), None)
            return copy.deepcopy(found)

    def has_secret(self, name: str) -> bool:
        return self._secrets.has(name)

    # ── Changes ───────────────────────────────────────────────────────────

    def validate(self, connection: dict) -> None:
        """Raise ValueError with a user-facing (Spanish) message if the connection is invalid."""
        name = (connection.get("name") or "").strip()
        if not _NAME_RE.match(name):
            raise ValueError("Nombre inválido: use letras, números, '.', '_' o '-' (máx. 49), sin espacios.")
        connector_cls = CONNECTOR_TYPES.get(connection.get("type"))
        if not connector_cls:
            raise ValueError(f"Tipo de conexión desconocido: {connection.get('type')!r}.")
        url = (connection.get("base_url") or "").strip()
        if not url.startswith("https://") and not url.startswith("http://localhost") \
                and not url.startswith("http://127.0.0.1"):
            raise ValueError("La URL debe empezar por https:// (http solo se permite para localhost).")
        for key, label, required in connector_cls.FIELDS:
            if required and not str(connection.get(key) or "").strip():
                raise ValueError(f"El campo '{label}' es obligatorio para {connector_cls.LABEL}.")
        if not isinstance(connection.get("settings", {}), dict):
            raise ValueError("Los parámetros deben ser un objeto JSON.")

    def save(self, connection: dict, original_name: str | None = None) -> None:
        """Create or update. ``original_name`` allows renaming (moves the secret too)."""
        connection = {**connection, "name": connection["name"].strip(), "base_url": connection["base_url"].strip()}
        connection.setdefault("settings", {})
        self.validate(connection)
        with self._lock:
            self._reload_if_changed()
            key = original_name or connection["name"]
            clash = next((c for c in self._connections if c["name"] == connection["name"]), None)
            if clash and connection["name"] != key:
                raise ValueError(f"Ya existe una conexión llamada '{connection['name']}'.")
            index = next((i for i, c in enumerate(self._connections) if c["name"] == key), None)
            if index is None:
                self._connections.append(connection)
            else:
                self._connections[index] = connection
            self._save()
        if original_name and original_name != connection["name"]:
            secret = self._secrets.get(original_name)
            if secret:
                self._secrets.set(connection["name"], connection.get("user", ""), secret)
                self._secrets.delete(original_name)

    def delete(self, name: str) -> None:
        with self._lock:
            self._reload_if_changed()
            self._connections = [c for c in self._connections if c["name"] != name]
            self._save()
        self._secrets.delete(name)

    def set_secret(self, name: str, secret: str) -> None:
        connection = self.get(name)
        if not connection:
            raise ValueError(f"No existe la conexión '{name}'.")
        if not secret.strip():
            raise ValueError("La clave está vacía.")
        self._secrets.set(name, connection.get("user", ""), secret.strip())

    # ── Runtime ───────────────────────────────────────────────────────────

    def connector(self, name: str, transport=None) -> ApiConnector:
        """Build the connector of a saved connection, with its secret resolved."""
        connection = self.get(name)
        if not connection:
            raise ApiError(f"No existe la conexión '{name}'. Créela en 'Conexiones API' o con api_cli.py conn add.")
        secret = self._secrets.get(name)
        if not secret:
            raise ApiError(f"La conexión '{name}' no tiene clave guardada. Use 'Guardar clave' o api_cli.py conn set-key {name}.")
        return CONNECTOR_TYPES[connection["type"]](connection, secret, transport=transport)
