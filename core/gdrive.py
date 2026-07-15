"""
Google Drive uploader for Odoo Backup Tool.

Uses a Service Account JSON key file for server-to-server authentication —
no browser redirect, no OAuth flow, no user interaction required.

Required pip packages:
  google-api-python-client>=2.0.0
  google-auth>=2.0.0
  google-auth-httplib2>=0.1.0

Optimization summary (applied throughout this module):
  - Chunk size 16 MB (was 5 MB) — maximizes throughput, Google minimum = 256 KB.
  - Folder ID cached per instance — avoids redundant API round-trips.
  - Retry with exponential backoff on 429/5xx — survives transient network hiccups.
  - MD5 integrity check — verifies Drive-stored hash matches source after every upload.
  - Resumable checkpoint (upload_file) — cross-session resume after a crash or disconnect.
  - _HashingReader — computes MD5 in-flight during streaming without a second pass.
  - Speed + ETA reporting — log lines include MB/s and remaining time.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

# ── Optional dependency guard ─────────────────────────────────────────────────
try:
    from google.oauth2.service_account import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload
    _GOOGLE_LIBS_OK = True
except ImportError:
    _GOOGLE_LIBS_OK = False

_DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"

# 16 MB per chunk — 64 × 256 KB (Google minimum multiple), optimal for large files.
# Larger chunks reduce HTTP round-trips; smaller chunks waste more bandwidth on retry.
_CHUNK_SIZE = 16 * 1024 * 1024

# Directory for cross-session resumable-upload checkpoint files (upload_file only).
_CHECKPOINT_DIR = Path.home() / ".odoo_backup_tool" / "upload_checkpoints"

# HTTP status codes that indicate a transient error worth retrying.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


def _require_libs() -> None:
    """Raise ImportError with a helpful install hint if google libs are missing."""
    if not _GOOGLE_LIBS_OK:
        raise ImportError(
            "Las bibliotecas de Google Drive no estan instaladas.\n\n"
            "Ejecute en su entorno virtual:\n"
            "  pip install google-api-python-client google-auth google-auth-httplib2"
        )


def _with_retry(fn, *, max_attempts: int = 5, log_callback=None):
    """
    Execute fn(), retrying on transient Drive HTTP errors with exponential backoff.

    Retries on status codes in _RETRYABLE_STATUS (rate-limit, server errors).
    Delay: 2s, 4s, 8s, 16s, 32s — capped at 60s.
    Non-retryable errors (400, 403, 404, …) are re-raised immediately.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except HttpError as exc:
            if exc.resp.status not in _RETRYABLE_STATUS or attempt == max_attempts:
                raise
            delay = min(2 ** attempt, 60)
            if log_callback:
                log_callback(
                    f"  [Drive] Error {exc.resp.status} — "
                    f"reintento {attempt}/{max_attempts - 1} en {delay}s ..."
                )
            time.sleep(delay)


class _HashingReader:
    """
    File-like wrapper that accumulates an MD5 hash of all bytes read through it.

    Designed for use with MediaIoBaseUpload so the hash is computed during the
    upload loop without a second pass over the source data.

    Handles seek-and-re-read safely: bytes that have already been hashed
    (position < confirmed_pos) are silently skipped during re-reads triggered
    by chunk retries, so the final digest equals MD5(complete_file).
    """

    def __init__(self, wrapped) -> None:
        self._wrapped       = wrapped
        self._md5           = hashlib.md5()
        self._confirmed_pos = 0   # bytes hashed so far (sequential, no re-hashing)

    def read(self, size: int = -1) -> bytes:
        current_pos = self._wrapped.tell()
        data = self._wrapped.read(size)
        if data:
            # Skip bytes already included in the hash (chunk retry after seek-back).
            hash_start = max(0, self._confirmed_pos - current_pos)
            if hash_start < len(data):
                self._md5.update(data[hash_start:])
                self._confirmed_pos = current_pos + len(data)
        return data

    def seek(self, *args):
        return self._wrapped.seek(*args)

    def tell(self) -> int:
        return self._wrapped.tell()

    @property
    def hexdigest(self) -> str:
        return self._md5.hexdigest()


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
        self._creds_path          = creds_path
        self._folder_id           = folder_id
        self._service             = None              # lazily built on first use
        self._resolved_folder_id: str | None = None  # cached after first API call

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
        missing  = [k for k in required if not data.get(k)]
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

        Result is cached within this instance so subsequent calls (upload,
        file_exists, cleanup) never incur extra API round-trips.

        Tries in order:
          1. Use directly if it is a folder on 'My Drive' or Shared Drive.
          2. Treat as a Shared Drive ID and look for (or create) an
             'Odoo Backups' subfolder inside it.
        """
        if self._resolved_folder_id:
            return self._resolved_folder_id

        svc = self._get_service()
        try:
            meta = svc.files().get(
                fileId=self._folder_id,
                fields="id,name,mimeType",
                supportsAllDrives=True,
            ).execute()
            if meta.get("mimeType") == "application/vnd.google-apps.folder":
                self._resolved_folder_id = meta["id"]
                return self._resolved_folder_id
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
            self._resolved_folder_id = folders[0]["id"]
            return self._resolved_folder_id

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
        self._resolved_folder_id = new_folder["id"]
        return self._resolved_folder_id

    # ── Checkpoint helpers (upload_file only) ─────────────────────────────────

    def _checkpoint_path(self, filename: str) -> Path:
        """Return the checkpoint file path for a given upload destination filename."""
        safe = filename.replace("/", "_").replace("\\", "_").replace(":", "_")
        return _CHECKPOINT_DIR / f"{safe}.json"

    def _save_checkpoint(
        self,
        filename: str,
        resumable_uri: str,
        offset: int,
        total_size: int,
    ) -> None:
        """Persist upload progress so the session can be resumed after a crash."""
        _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
        cp = self._checkpoint_path(filename)
        cp.write_text(
            json.dumps(
                {
                    "filename":      filename,
                    "resumable_uri": resumable_uri,
                    "offset":        offset,
                    "total_size":    total_size,
                    "saved_at":      datetime.now().isoformat(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _load_checkpoint(self, filename: str) -> dict | None:
        """Return saved checkpoint dict for filename, or None if not found/corrupt."""
        cp = self._checkpoint_path(filename)
        if not cp.exists():
            return None
        try:
            return json.loads(cp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def _clear_checkpoint(self, filename: str) -> None:
        """Delete the checkpoint file (upload finished or failed permanently)."""
        try:
            self._checkpoint_path(filename).unlink(missing_ok=True)
        except OSError:
            pass

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
            svc       = self._get_service()
            folder_id = self._resolve_target_folder()
            meta      = svc.files().get(
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
            svc       = self._get_service()
            result    = svc.files().list(
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

        Supports cross-session checkpoint resume: if a previous upload of the same
        filename was interrupted (network drop, process crash), the next call will
        attempt to continue from the last confirmed byte instead of restarting.

        Verifies MD5 integrity after upload by comparing Drive's stored checksum
        against a second local file read.

        Args:
            local_path:        Absolute path of the local file to upload.
            dest_filename:     Name to use in Drive (defaults to basename of local_path).
            progress_callback: Called with (bytes_uploaded, total_bytes).
            log_callback:      Called with human-readable status strings.

        Returns:
            The Drive file ID of the uploaded file.

        Raises:
            FileNotFoundError: If local_path does not exist.
            RuntimeError:      On upload errors or MD5 mismatch.
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

        mime = _mime_for(filename)

        def _make_request():
            media_ = MediaFileUpload(
                local_path, mimetype=mime, resumable=True, chunksize=_CHUNK_SIZE
            )
            req_ = svc.files().create(
                body={"name": filename, "parents": [folder_id]},
                media_body=media_,
                fields="id,name,md5Checksum",
                supportsAllDrives=True,
            )
            return media_, req_

        media, request = _make_request()

        # ── Checkpoint resume ─────────────────────────────────────────────────
        # If a prior upload of this filename was interrupted, try to resume it
        # rather than restarting from byte 0.
        checkpoint = self._load_checkpoint(filename)
        if (
            checkpoint
            and checkpoint.get("resumable_uri")
            and checkpoint.get("total_size") == total_size
        ):
            try:
                pct = int(checkpoint["offset"] / total_size * 100) if total_size else 0
                if log_callback:
                    log_callback(
                        f"  Retomando upload interrumpido desde {pct}% "
                        f"({checkpoint['offset'] // (1024 * 1024)} MB / "
                        f"{total_size // (1024 * 1024)} MB)"
                    )
                # Restore resumable session state (private attributes of googleapiclient)
                request._resumable_uri      = checkpoint["resumable_uri"]
                request._resumable_progress = checkpoint["offset"]
                media._fd.seek(checkpoint["offset"])
            except Exception:
                # Restoration failed (expired URI, API change) — start fresh.
                if log_callback:
                    log_callback("  No se pudo retomar — iniciando upload desde el inicio.")
                self._clear_checkpoint(filename)
                media, request = _make_request()

        # ── Upload loop ───────────────────────────────────────────────────────
        response   = None
        last_pct   = -1
        t_start    = time.monotonic()
        t_last     = t_start
        bytes_last = checkpoint["offset"] if checkpoint else 0

        while response is None:
            status, response = _with_retry(
                request.next_chunk,
                max_attempts=5,
                log_callback=log_callback,
            )

            if status:
                uploaded    = int(status.resumable_progress)
                now         = time.monotonic()
                chunk_secs  = now - t_last
                chunk_bytes = uploaded - bytes_last
                speed_mbps  = (chunk_bytes / chunk_secs / (1024 * 1024)) if chunk_secs > 0 else 0.0
                elapsed     = now - t_start
                t_last      = now
                bytes_last  = uploaded

                if progress_callback:
                    progress_callback(uploaded, total_size)

                pct = int(uploaded / total_size * 100) if total_size else 0
                if pct != last_pct:
                    last_pct = pct
                    eta_str  = _eta(total_size, uploaded, elapsed)
                    if log_callback:
                        log_callback(
                            f"  Subiendo a Drive ... {pct}%  "
                            f"({speed_mbps:.1f} MB/s — ETA {eta_str})"
                        )

                # Persist checkpoint so a crash can resume from here.
                uri = getattr(request, "_resumable_uri", None)
                if uri:
                    self._save_checkpoint(filename, uri, uploaded, total_size)

        # ── MD5 integrity check ───────────────────────────────────────────────
        file_id   = response.get("id", "")
        drive_md5 = response.get("md5Checksum", "")
        if drive_md5:
            local_md5 = hashlib.md5()
            with open(local_path, "rb") as fh:
                while chunk := fh.read(_CHUNK_SIZE):
                    local_md5.update(chunk)
            if local_md5.hexdigest() != drive_md5:
                raise RuntimeError(
                    f"Verificacion MD5 fallida para {filename}:\n"
                    f"  Local : {local_md5.hexdigest()}\n"
                    f"  Drive : {drive_md5}\n"
                    "El archivo puede estar corrupto en Drive. Repita el upload."
                )
            if log_callback:
                log_callback(f"  MD5 verificado: {drive_md5}")

        self._clear_checkpoint(filename)

        if log_callback:
            elapsed  = time.monotonic() - t_start
            avg_mbps = (total_size / elapsed / (1024 * 1024)) if elapsed > 0 else 0.0
            log_callback(
                f"Subida completa a Drive: {filename} (id={file_id}) "
                f"— {avg_mbps:.1f} MB/s promedio"
            )
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

        Only files directly inside the configured folder are considered.
        Delete calls are retried with backoff on transient errors.

        Args:
            name_prefix:    Filename prefix to filter by (e.g. "bancasa_prod_").
            retention_days: Files older than this many days are deleted.
            log_callback:   Optional status logger.

        Returns:
            Number of files deleted.
        """
        if retention_days <= 0:
            return 0

        folder_id   = self._resolve_target_folder()
        svc         = self._get_service()
        cutoff      = datetime.now(tz=timezone.utc) - timedelta(days=retention_days)
        deleted     = 0
        safe_prefix = name_prefix.replace("'", "\\'")

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

            result     = svc.files().list(**params).execute()
            files      = result.get("files", [])
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

                if created >= cutoff:
                    continue

                try:
                    _with_retry(
                        lambda fid=f["id"]: svc.files().delete(
                            fileId=fid, supportsAllDrives=True
                        ).execute(),
                        max_attempts=3,
                        log_callback=log_callback,
                    )
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

        Data flows: SFTP file → _HashingReader → MediaIoBaseUpload → Drive API.
        Only _CHUNK_SIZE (16 MB) bytes reside in memory at a time.

        MD5 is computed in-flight by _HashingReader and verified against the
        Drive-returned checksum after the upload completes.

        Args:
            file_obj:          File-like object (e.g. Paramiko SFTPFile in 'rb').
            filename:          Name to give the file in Drive.
            total_size:        Total byte size for progress/ETA (0 = unknown).
            progress_callback: Called with (bytes_uploaded, total_bytes).
            log_callback:      Called with human-readable status strings.

        Returns:
            The Drive file ID of the uploaded file.

        Raises:
            RuntimeError: On upload errors or MD5 mismatch.
        """
        folder_id = self._resolve_target_folder()
        svc       = self._get_service()

        if log_callback:
            mb       = total_size / (1024 * 1024) if total_size else 0
            size_str = f" ({mb:.1f} MB)" if total_size else ""
            log_callback(
                f"Enviando a Google Drive (streaming){size_str}: {filename} ..."
            )

        # Wrap source stream to compute MD5 during the upload loop.
        hashing_reader = _HashingReader(file_obj)

        media = MediaIoBaseUpload(
            hashing_reader,
            mimetype=_mime_for(filename),
            chunksize=_CHUNK_SIZE,
            resumable=True,
        )
        request = svc.files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media,
            fields="id,name,md5Checksum",
            supportsAllDrives=True,
        )

        # ── Upload loop ───────────────────────────────────────────────────────
        response   = None
        last_pct   = -1
        t_start    = time.monotonic()
        t_last     = t_start
        bytes_last = 0

        while response is None:
            status, response = _with_retry(
                request.next_chunk,
                max_attempts=5,
                log_callback=log_callback,
            )

            if status and total_size:
                uploaded    = int(status.resumable_progress)
                now         = time.monotonic()
                chunk_secs  = now - t_last
                chunk_bytes = uploaded - bytes_last
                speed_mbps  = (chunk_bytes / chunk_secs / (1024 * 1024)) if chunk_secs > 0 else 0.0
                elapsed     = now - t_start
                t_last      = now
                bytes_last  = uploaded

                if progress_callback:
                    progress_callback(uploaded, total_size)

                pct = int(uploaded / total_size * 100)
                if pct != last_pct:
                    last_pct = pct
                    eta_str  = _eta(total_size, uploaded, elapsed)
                    if log_callback:
                        log_callback(
                            f"  Drive streaming ... {pct}%  "
                            f"({speed_mbps:.1f} MB/s — ETA {eta_str})"
                        )

        # ── MD5 integrity check ───────────────────────────────────────────────
        file_id   = response.get("id", "")
        drive_md5 = response.get("md5Checksum", "")
        if drive_md5:
            local_md5 = hashing_reader.hexdigest
            if local_md5 != drive_md5:
                raise RuntimeError(
                    f"Verificacion MD5 fallida para {filename}:\n"
                    f"  Enviado : {local_md5}\n"
                    f"  Drive   : {drive_md5}\n"
                    "El archivo puede estar corrupto en Drive. Repita el upload."
                )
            if log_callback:
                log_callback(f"  MD5 verificado: {drive_md5}")

        if log_callback:
            elapsed   = time.monotonic() - t_start
            avg_mbps  = (
                total_size / elapsed / (1024 * 1024)
                if elapsed > 0 and total_size else 0.0
            )
            speed_str = f" — {avg_mbps:.1f} MB/s promedio" if avg_mbps > 0 else ""
            log_callback(
                f"Subida completa a Drive: {filename} (id={file_id}){speed_str}"
            )
        return file_id


# ── Module-level helpers ──────────────────────────────────────────────────────

def _mime_for(filename: str) -> str:
    """Return a reasonable MIME type based on the file extension."""
    lower = filename.lower()
    if lower.endswith(".zip"):
        return "application/zip"
    if lower.endswith((".dump", ".sql")):
        return "application/octet-stream"
    if lower.endswith(".json"):
        return "application/json"
    if lower.endswith(".tar"):
        return "application/x-tar"
    return "application/octet-stream"


def _eta(total: int, uploaded: int, elapsed_secs: float) -> str:
    """Return a human-readable ETA string (MM:SS) or '--:--' if not computable."""
    if elapsed_secs <= 0 or uploaded <= 0:
        return "--:--"
    remaining = total - uploaded
    secs = int(remaining / (uploaded / elapsed_secs))
    return f"{secs // 60:02d}:{secs % 60:02d}"
