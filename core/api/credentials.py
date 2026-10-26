"""
Secret storage for API connections: the Windows Credential Manager.

API keys are NEVER written to api_connections.json (unlike the SSH passwords in
servers.json): an API key usually grants full access to a production ERP, and the
Credential Manager keeps it encrypted under the Windows user's profile.

Implemented with ctypes over advapi32 (CredReadW / CredWriteW / CredDeleteW) to
avoid adding the ``keyring`` dependency to the executable.

Fallback for non-Windows / automation: environment variable
``OBT_API_KEY_<CONNECTION_NAME>`` (upper case, non-alphanumerics -> '_').
"""
from __future__ import annotations

import os
import re
import sys

_TARGET_PREFIX = "odoo_backup_tool/api/"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2


def target_for(connection_name: str) -> str:
    """Credential Manager target name of a connection (visible in 'Credenciales de Windows')."""
    return _TARGET_PREFIX + connection_name


def env_var_for(connection_name: str) -> str:
    return "OBT_API_KEY_" + re.sub(r"[^A-Za-z0-9]", "_", connection_name).upper()


class WindowsCredentialStore:
    """get/set/delete secrets of generic credentials. Injectable so tests never touch Windows."""

    def __init__(self) -> None:
        self._api = _load_advapi32() if sys.platform == "win32" else None

    def get(self, connection_name: str) -> str | None:
        secret = self._read(target_for(connection_name)) if self._api else None
        return secret or os.environ.get(env_var_for(connection_name), "").strip() or None

    def set(self, connection_name: str, user: str, secret: str) -> None:
        if not self._api:
            raise RuntimeError("El Administrador de credenciales solo existe en Windows; "
                               f"use la variable de entorno {env_var_for(connection_name)}.")
        import ctypes
        api, CREDENTIAL = self._api
        blob = secret.encode("utf-16-le")
        buffer = ctypes.create_string_buffer(blob, len(blob))  # kept alive until CredWriteW returns
        cred = CREDENTIAL()
        cred.Type = _CRED_TYPE_GENERIC
        cred.TargetName = target_for(connection_name)
        cred.UserName = user or ""
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))
        cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
        if not api.CredWriteW(ctypes.byref(cred), 0):
            raise OSError(ctypes.get_last_error(), "No se pudo guardar la clave en el Administrador de credenciales.")

    def delete(self, connection_name: str) -> None:
        if self._api:
            api, _ = self._api
            api.CredDeleteW(target_for(connection_name), _CRED_TYPE_GENERIC, 0)  # missing = no-op

    def has(self, connection_name: str) -> bool:
        return self.get(connection_name) is not None

    def _read(self, target: str) -> str | None:
        import ctypes
        api, CREDENTIAL = self._api
        cred_ptr = ctypes.POINTER(CREDENTIAL)()
        if not api.CredReadW(target, _CRED_TYPE_GENERIC, 0, ctypes.byref(cred_ptr)):
            return None
        try:
            cred = cred_ptr.contents
            blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
            try:
                return blob.decode("utf-16-le").strip() or None
            except UnicodeDecodeError:  # written by another tool as UTF-8
                return blob.decode("utf-8").strip() or None
        finally:
            api.CredFree(cred_ptr)


def _load_advapi32():
    """Bind the three advapi32 functions once; returns (dll, CREDENTIAL struct)."""
    import ctypes
    from ctypes import wintypes

    class CREDENTIAL(ctypes.Structure):
        _fields_ = [
            ("Flags", wintypes.DWORD),
            ("Type", wintypes.DWORD),
            ("TargetName", wintypes.LPWSTR),
            ("Comment", wintypes.LPWSTR),
            ("LastWritten", wintypes.FILETIME),
            ("CredentialBlobSize", wintypes.DWORD),
            ("CredentialBlob", ctypes.POINTER(ctypes.c_char)),
            ("Persist", wintypes.DWORD),
            ("AttributeCount", wintypes.DWORD),
            ("Attributes", ctypes.c_void_p),
            ("TargetAlias", wintypes.LPWSTR),
            ("UserName", wintypes.LPWSTR),
        ]

    api = ctypes.WinDLL("advapi32", use_last_error=True)
    api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
    api.CredReadW.restype = wintypes.BOOL
    api.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
    api.CredWriteW.restype = wintypes.BOOL
    api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
    api.CredDeleteW.restype = wintypes.BOOL
    api.CredFree.argtypes = [ctypes.c_void_p]
    return api, CREDENTIAL
