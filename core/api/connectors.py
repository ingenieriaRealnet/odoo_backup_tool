"""
API connectors: one class per kind of external system.

Every connector receives the connection dict (from ApiConnectionManager) and the
secret, and exposes:
    test() -> str     a cheap authenticated call; returns a human summary or raises ApiError
    call(...)         the system-specific request primitive

To support a new system, subclass ApiConnector and register it in CONNECTOR_TYPES;
the GUI page and the CLI pick it up from there. Only the standard library is used.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

# Transport signature, injectable for tests: (method, url, headers, body_bytes|None) -> (status, body_bytes)
Transport = Callable[[str, str, dict, "bytes | None"], "tuple[int, bytes]"]

_USER_AGENT = "OdooBackupTool-API/1.0"


class ApiError(RuntimeError):
    """An API call failed (HTTP error, unreachable host, bad credentials...)."""


def urllib_transport(timeout: float) -> Transport:
    def send(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, bytes]:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ApiError(f"No se pudo conectar con {url.split('/')[2] if '//' in url else url}: {exc}") from exc
    return send


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", "replace")


class ApiConnector:
    """Base class. Subclasses declare TYPE, LABEL and FIELDS (extra connection fields)."""

    TYPE = ""
    LABEL = ""
    # (key, label shown in the GUI, required)
    FIELDS: tuple[tuple[str, str, bool], ...] = ()

    def __init__(self, connection: dict, secret: str, transport: Transport | None = None) -> None:
        self.connection = connection
        self.base_url = connection["base_url"].rstrip("/")
        self._secret = secret
        self._transport = transport or urllib_transport(float(connection.get("timeout", 30)))

    def test(self) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    def _request(self, method: str, url: str, headers: dict, payload: Any = None) -> Any:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"User-Agent": _USER_AGENT, "Accept": "application/json", **headers}
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        status, raw = self._transport(method, url, headers, body)
        data = _decode(raw)
        if status >= 400:
            message = data.get("message") if isinstance(data, dict) else str(data)[:300]
            raise ApiError(f"HTTP {status}: {message}")
        return data


class OdooJson2Connector(ApiConnector):
    """
    Odoo 19+ external API: ``POST /json/2/<model>/<method>`` with an API key as bearer.

    XML-RPC / JSON-RPC are deprecated starting with Odoo 19; JSON-2 runs the public ORM
    method with the same access rights and record rules as the web client.
    """

    TYPE = "odoo_json2"
    LABEL = "Odoo 19+ (JSON-2)"
    FIELDS = (("database", "Base de datos", True),)

    def call(self, model: str, method: str, **params: Any) -> Any:
        try:
            return self._request(
                "POST", f"{self.base_url}/json/2/{model}/{method}",
                {"Authorization": f"bearer {self._secret}", "X-Odoo-Database": self.connection.get("database", "")},
                params,
            )
        except ApiError as exc:
            raise ApiError(f"{model}.{method} -> {exc}") from exc

    def test(self) -> str:
        user = self.call("res.users", "context_get")
        version = self._request("POST", f"{self.base_url}/web/webclient/version_info", {},
                                {"jsonrpc": "2.0", "method": "call", "params": {}})
        server = (version or {}).get("result", {}).get("server_version", "?") if isinstance(version, dict) else "?"
        return f"Conectado a Odoo {server} (idioma {user.get('lang')}, zona {user.get('tz')})."

    # ORM shortcuts
    def search_read(self, model, domain, fields, limit=None, order=None, context=None):
        params = {"domain": domain, "fields": fields}
        if limit:
            params["limit"] = limit
        if order:
            params["order"] = order
        if context:
            params["context"] = context
        return self.call(model, "search_read", **params)

    def read(self, model, ids, fields):
        return self.call(model, "read", ids=list(ids), fields=fields)

    def create(self, model, vals, context=None):
        params = {"vals_list": [vals]}
        if context:
            params["context"] = context
        return self.call(model, "create", **params)[0]

    def write(self, model, ids, vals):
        return self.call(model, "write", ids=list(ids), vals=vals)


class RestBearerConnector(ApiConnector):
    """Generic JSON REST API authenticated with ``Authorization: Bearer <token>``."""

    TYPE = "rest_bearer"
    LABEL = "REST genérico (Bearer token)"
    FIELDS = (("test_path", "Ruta de prueba (GET)", False),)

    def call(self, method: str, path: str, payload: Any = None, query: dict | None = None) -> Any:
        url = f"{self.base_url}/{path.lstrip('/')}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        return self._request(method.upper(), url, {"Authorization": f"Bearer {self._secret}"}, payload)

    def test(self) -> str:
        path = self.connection.get("test_path") or "/"
        self.call("GET", path)
        return f"Respuesta correcta de GET {path}."


CONNECTOR_TYPES: dict[str, type[ApiConnector]] = {
    cls.TYPE: cls for cls in (OdooJson2Connector, RestBearerConnector)
}
