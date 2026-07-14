"""
Google Drive uploader for Odoo Backup Tool.

Uses a Service Account JSON key file for server-to-server authentication —
no browser redirect, no OAuth flow, no user interaction required.

Required pip packages:
  google-api-python-client>=2.0.0
  google-auth>=2.0.0
  google-auth-httplib2>=0.1.0
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Callable

# ── Optional dependency guard ─────────────────────────────────────────────────
# Importing at module level so the error is clear if the libs are not installed.
try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload
    _GOOGLE_LIBS_OK = True
except ImportError:
    _GOOGLE_LIBS_OK = False

# Full Drive scope — required for list + create + delete operations.
_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"

# Chunk size for resumable uploads (5 MB).  Google recommends multiples of 256 KB.
_CHUNK_SIZE = 5 * 1024 * 1024


def _require_libs() -> None:
    """Raise ImportError with a helpful install hint if google libs are missing."""
    if not _GOOGLE_LIBS_OK:
        raise ImportError(
            "Las bibliotecas de Google Drive no estan instaladas.\n\n"
            "Ejecute en su entorno virtual:\n"
            "  pip install google-api-python-client google-auth google-auth-httplib2"
        )


class DriveUploader:
    """
    Manages Google Drive uploads using a Service Account JSON key.

    The Service Account must have access to the target folder:
      - For a folder in 'My Drive': share the folder with the SA's client_email.
      - For a Shared Drive: add the SA as a member with 'Contributor' or higher.

    Args:
        creds_path: Absolute path to the Service Account JSON key file.
        folder_id:  ID of the Drive folder (or Shared Drive ID) where backups
                    will be stored.  Copy it from the folder's URL:
                    drive.google.com/drive/folders/<FOLDER_ID>
    """

    def __init__(self, creds_path: str, folder_id: str) -> None:
        _require_libs()
        self._creds_path = creds_path
        self._folder_id  = folder_id
        self._service    = None   # built lazily on first use

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _load_creds(self) -> "Credentials":
        """Load and validate the Service Account JSON, return Credentials object."""
        if not os.path.isfile(self._creds_path):
            raise FileNotFoundError(
                f"Archivo de credenciales no encontrado: {self._creds_path}"
            )
        with open(self._creds_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)

        required = ("type", "client_email", "private_key")
        missing = [k for k in required if not data.get(k)]
        if missing:
            raise ValueError(
                f"El JSON de Service Account no tiene los campos requeridos: {missing}\n"
                "Verifique que descargo el archivo correcto desde Google Cloud Console."
            )
        if data.get("type") != "service_account":
            raise ValueError(
                f"El JSON no es de tipo 'service_account' (tipo encontrado: {data.get('type')!r}).\n"
                "Debe usar una clave de Service Account, no OAuth2 de usuario."
            )
        return Credentials.from_service_account_info(data, scopes=[_DRIVE_SCOPE])

    def _get_service(self):
        """Return the cached Drive v3 service, building it on first call."""
        if self._service is None:
            creds = self._load_creds()
            self._service = build("drive", "v3", credentials=creds, cache_discovery=False)
        return self._service

    def _resolve_target_folder(self) -> str:
        """
        Resolve self._folder_id to a writable folder ID.

        Tries in order:
          1. Use it directly if it is a folder on 'My Drive' or Shared Drive.
          2. Treat it as a Shared Drive ID and look for (or create) an
             'Odoo Backups' folder inside it.

        Returns the resolved folder ID.
        Raises RuntimeError if the ID cannot be resolved.
        """
        svc = self._get_service()
        try:
            meta = svc.files().get(
                fileId=self._folder_id,
                fields="id,name,mimeType",
                supportsAllDrives=True,
            ).execute()
            # Already a folder — use directly
            if meta.get("mimeType") == "application/vnd.google-apps.folder":
                return meta["id"]
            raise RuntimeError(
                f"El ID '{self._folder_id}' existe en Drive pero NO es una carpeta "
                f"(tipo: {meta.get('mimeType')}).\n"
                "Copie el ID de una carpeta, no de un archivo."
            )
        except HttpError as exc:
            if exc.resp.status != 404:
                raise RuntimeError(f"Error al verificar la carpeta en Drive: {exc}") from exc

        # 404 → try treating it as a Shared Drive ID
        try:
            svc.drives().get(driveId=self._folder_id).execute()
        except HttpError as exc:
            raise RuntimeError(
                f"El ID '{self._folder_id}' no se encontro ni como carpeta ni como "
                f"Shared Drive.\n\nVerifique que:\n"
                f"  • El ID es correcto (copie desde la URL de Drive)\n"
                f"  • La Service Account tiene acceso a esa carpeta\n"
                f"Error original: {exc}"
            ) from exc

        # Found as Shared Drive — find or create 'Odoo Backups' subfolder
        result = svc.files().list(
            q=(
                f"'{self._folder_id}' in parents "
                "and mimeType='application/vnd.google-apps.folder' "
                "and name='Odoo Backups' "
                "and trashed=false"
            ),
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            fields="files(id)",
        ).execute()
        folders = result.get("files", [])
        if folders:
            return folders[0]["id"]

        # Create 'Odoo Backups' folder in the Shared Drive
        new_folder = svc.files().create(
            body={
                "name": "Odoo Backups",
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [self._folder_id],
            },
            supportsAllDrives=True,
            fields="id",
        ).execute()
        return new_folder["id"]

    # ── Public API ────────────────────────────────────────────────────────────

    def verify_connection(self) -> tuple[bool, str]:
        """
        Test credentials and folder access without uploading anything.

        Returns:
            (True, folder_name)  on success.
            (False, error_msg)   on failure.
        """
        try:
            _require_libs()
            svc = self._get_service()
            folder_id = self._resolve_target_folder()
            meta = svc.files().get(
                fileId=folder_id,
                fields="name",
                supportsAllDrives=True,
            ).execute()
            return True, meta.get("name", folder_id)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    def file_exists(self, filename: str) -> bool:
        """Return True if `filename` already exists in the target folder."""
        try:
            folder_id = self._resolve_target_folder()
            svc = self._get_service()
            result = svc.files().list(
                q=(
                    f"'{folder_id}' in parents "
                    f"and name='{filename}' "
                    "and trashed=false"
                ),
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                fields="files(id)",
            ).execute()
            return len(result.get("files", [])) > 0
        except Exception:  # noqa: BLE001
            return False

    def upload_file(
        self,
        local_path: str,
        dest_filename: str | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
        log_callback: Callable[[str], None] | None = None,
    ) -> str:
        """
        Upload a local file to the configured Drive folder using resumable upload.

        Args:
            local_path:        Absolute path of the local file to upload.
            dest_filename:     Name to use in Drive (defaults to basename of local_path).
            progress_callback: Called with (bytes_uploaded, total_bytes).
            log_callback:      Called with human-readable status strings.

        Returns:
            The Drive file ID of the uploaded file.

        Raises:
            FileNotFoundError: If local_path does not exist.
            RuntimeError:      On upload errors.
        """
        if not os.path.isfile(local_path):
            raise FileNotFoundError(f"Archivo local no encontrado: {local_path}")

        filename   = dest_filename or os.path.basename(local_path)
        total_size = os.path.getsize(local_path)
        folder_id  = self._resolve_target_folder()
        svc        = self._get_service()

        if log_callback:
            mb = total_size / (1024 * 1024)
            log_callback(f"Subiendo a Google Drive: {filename} ({mb:.1f} MB) ...")

        # Determine MIME type — Drive stores everything as application/octet-stream
        # unless we have a more specific type.
        if filename.endswith(".zip"):
            mime = "application/zip"
        elif filename.endswith(".dump") or filename.endswith(".sql"):
            mime = "application/octet-stream"
        elif filename.endswith(".json"):
            mime = "application/json"
        else:
            mime = "application/octet-stream"

        media = MediaFileUpload(local_path, mimetype=mime, resumable=True, chunksize=_CHUNK_SIZE)

        request = svc.files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media,
            fields="id,name",
            supportsAllDrives=True,
        )

        # Drive resumable upload loop
        response = None
        last_pct = -1
        while response is None:
            status, response = request.next_chunk()
            if status:
                uploaded = int(status.resumable_progress)
                if progress_callback:
                    progress_callback(uploaded, total_size)
                pct = int(uploaded / total_size * 100) if total_size else 0
                if pct != last_pct:
                    last_pct = pct
                    if log_callback:
                        log_callback(f"  Subiendo a Drive ... {pct}%")

        file_id = response.get("id", "")
        if log_callback:
            log_callback(f"Subida completa a Drive: {filename} (id={file_id})")
        return file_id

    def cleanup_old_files(
        self,
        name_prefix: str,
        retention_days: int,
        log_callback: Callable[[str], None] | None = None,
    ) -> int:
        """
        Delete Drive files whose name starts with name_prefix and whose
        createdTime is older than retention_days days.

        Only files directly inside the configured folder are considered —
        subfolders are not recursed.

        Args:
            name_prefix:    Filename prefix to filter by (e.g. "odoo_bancasa_prod_").
            retention_days: Files older than this many days are deleted.
            log_callback:   Optional status logger.

        Returns:
            Number of files deleted.
        """
        if retention_days <= 0:
            return 0

        folder_id  = self._resolve_target_folder()
        svc        = self._get_service()
        cutoff     = datetime.now(tz=timezone.utc) - timedelta(days=retention_days)
        deleted    = 0

        # Escape single quotes in prefix for the query string
        safe_prefix = name_prefix.replace("'", "\\'")

        # List all non-trashed files in the folder whose name starts with the prefix.
        # Drive's `contains` operator is a substring match, so we filter by startswith
        # in Python after fetching.
        page_token = None
        while True:
            params: dict = {
                "q": (
                    f"'{folder_id}' in parents "
                    f"and name contains '{safe_prefix}' "
                    "and trashed=false "
                    "and mimeType != 'application/vnd.google-apps.folder'"
                ),
                "includeItemsFromAllDrives": True,
                "supportsAllDrives":         True,
                "fields":                    "nextPageToken, files(id, name, createdTime)",
                "pageSize":                  100,
            }
            if page_token:
                params["pageToken"] = page_token

            result    = svc.files().list(**params).execute()
            files     = result.get("files", [])
            page_token = result.get("nextPageToken")

            for f in files:
                if not f.get("name", "").startswith(name_prefix):
                    continue
                created_str = f.get("createdTime", "")
                try:
                    created = datetime.fromisoformat(
                        created_str.replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    continue

                if created < cutoff:
                    try:
                        svc.files().delete(
                            fileId=f["id"],
                            supportsAllDrives=True,
                        ).execute()
                        deleted += 1
                        if log_callback:
                            log_callback(
                                f"  Retencion Drive: eliminado {f['name']} "
                                f"(creado {created.date()})"
                            )
                    except HttpError as exc:
                        if log_callback:
                            log_callback(
                                f"  [aviso] No se pudo eliminar {f['name']}: {exc}"
                            )

            if not page_token:
                break

        return deleted

    def upload_stream(
        self,
        file_obj,
        filename: str,
        total_size: int = 0,
        progress_callback: Callable[[int, int], None] | None = None,
        log_callback: Callable[[str], None] | None = None,
    ) -> str:
        """
        Upload a file-like object directly to Drive without a local temp copy.

        Data flows: SFTP file → Paramiko read() → MediaIoBaseUpload chunks → Drive API.
        No local disk is used for the transfer — only a 5 MB in-memory chunk at a time.

        Args:
            file_obj:          File-like object (e.g. Paramiko SFTPFile opened in 'rb').
            filename:          Name to give the file in Drive.
            total_size:        Total byte size for accurate progress reporting (0 = unknown).
            progress_callback: Called with (bytes_uploaded, total_bytes).
            log_callback:      Called with human-readable status strings.

        Returns:
            The Drive file ID of the uploaded file.

        Raises:
            RuntimeError: On upload errors.
        """
        folder_id = self._resolve_target_folder()
        svc       = self._get_service()

        if log_callback:
            mb = total_size / (1024 * 1024) if total_size else 0
            size_str = f" ({mb:.1f} MB)" if total_size else ""
            log_callback(
                f"Enviando a Google Drive (streaming){size_str}: {filename} ..."
            )

        if filename.endswith(".zip"):
            mime = "application/zip"
        elif filename.endswith((".dump", ".sql")):
            mime = "application/octet-stream"
        elif filename.endswith(".json"):
            mime = "application/json"
        else:
            mime = "application/octet-stream"

        # MediaIoBaseUpload streams from any file-like supporting read() + seek().
        # Paramiko SFTPFile supports both, making server→Drive streaming possible.
        media = MediaIoBaseUpload(
            file_obj, mimetype=mime, chunksize=_CHUNK_SIZE, resumable=True
        )

        request = svc.files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media,
            fields="id,name",
            supportsAllDrives=True,
        )

        response  = None
        last_pct  = -1
        while response is None:
            status, response = request.next_chunk()
            if status and total_size:
                uploaded = int(status.resumable_progress)
                if progress_callback:
                    progress_callback(uploaded, total_size)
                pct = int(uploaded / total_size * 100)
                if pct != last_pct:
                    last_pct = pct
                    if log_callback:
                        log_callback(f"  Drive streaming ... {pct}%")

        file_id = response.get("id", "")
        if log_callback:
            log_callback(f"Subida completa a Drive: {filename} (id={file_id})")
        return file_id
