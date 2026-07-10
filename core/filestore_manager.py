"""
Filestore discovery and compression via SSH.

Searches common Odoo filestore locations, allows directory browsing
on the remote server, and compresses a selected DB folder into /tmp/.
"""
from __future__ import annotations
from typing import Callable
from .ssh_client import SSHClient

# Ordered list of paths to probe before falling back to a full `find`
_CANDIDATE_PATHS: list[str] = [
    "/var/lib/odoo/.local/share/Odoo/filestore",
    "/opt/odoo/.local/share/Odoo/filestore",
    "/home/odoo/.local/share/Odoo/filestore",
    "/root/.local/share/Odoo/filestore",
    "/home/odoo18/.local/share/Odoo/filestore",
    "/home/odoo19/.local/share/Odoo/filestore",
]


class FilestoreManager:
    """Handles filestore discovery and compression on a remote server."""

    def __init__(self, ssh: SSHClient) -> None:
        self._ssh = ssh

    # ── Discovery ─────────────────────────────────────────────────────────

    def find_filestore_roots(self) -> list[str]:
        """
        Locate Odoo filestore root directories on the remote server.

        Checks known candidate paths first, then runs a broader `find`
        for non-standard installations.

        Returns:
            List of existing filestore root paths (may be empty).
        """
        found: list[str] = []

        for path in _CANDIDATE_PATHS:
            code, _, _ = self._ssh.execute(f"test -d {path}")
            if code == 0 and path not in found:
                found.append(path)

        # Broader search as a fallback (limited depth to stay fast).
        # nice -n 19 keeps disk I/O at lowest priority so Odoo is not affected.
        code, out, _ = self._ssh.execute(
            "nice -n 19 find /var /opt /home /root -maxdepth 7 -type d "
            r"-name 'filestore' 2>/dev/null",
            timeout=30,
        )
        if code == 0:
            for line in out.splitlines():
                line = line.strip()
                if line and line not in found:
                    found.append(line)

        return found

    def list_db_folders(self, filestore_root: str) -> list[str]:
        """
        Return the names of subdirectories inside a filestore root.

        Each subdirectory typically corresponds to one Odoo database.

        Args:
            filestore_root: Absolute path of the filestore root.

        Returns:
            Sorted list of folder names.

        Raises:
            RuntimeError: If the path cannot be listed.
        """
        code, out, err = self._ssh.execute(
            f"ls -1 --color=never {filestore_root}"
        )
        if code != 0:
            raise RuntimeError(f"No se puede listar {filestore_root}:\n{err}")

        return sorted(line.strip() for line in out.splitlines() if line.strip())

    def browse_directory(self, path: str) -> list[dict]:
        """
        Return a listing of a remote directory suitable for display in a tree.

        Args:
            path: Absolute path on the remote server.

        Returns:
            List of dicts with keys: name (str), type ('d' or 'f'), size (str).

        Raises:
            RuntimeError: If the directory cannot be listed.
        """
        code, out, err = self._ssh.execute(f"ls -lA --color=never {path}")
        if code != 0:
            raise RuntimeError(f"No se puede explorar {path}:\n{err}")

        entries: list[dict] = []
        for line in out.splitlines():
            # Skip total line and empty lines
            if not line or line.startswith("total"):
                continue
            parts = line.split(None, 8)
            if len(parts) < 9:
                continue
            name = parts[8].strip()
            if name in (".", ".."):
                continue
            entry_type = "d" if line.startswith("d") else "f"
            size = parts[4] if entry_type == "f" else ""
            entries.append({"name": name, "type": entry_type, "size": size})

        return entries

    # ── Compression ───────────────────────────────────────────────────────

    def remote_file_exists(self, remote_path: str) -> bool:
        """Return True if the file already exists on the remote server."""
        code, _, _ = self._ssh.execute(f"test -f {remote_path}")
        return code == 0

    def default_zip_path(self, db_name: str) -> str:
        """Return the default /tmp/ path for a filestore zip."""
        return f"/tmp/filestore_{db_name}.zip"

    def compress_filestore(
        self,
        filestore_root: str,
        db_name: str,
        remote_zip: str | None = None,
        log_callback: Callable[[str], None] | None = None,
        cancel_event=None,
    ) -> str:
        """
        Compress a database's filestore folder into a zip file in /tmp/.

        Args:
            filestore_root: Root filestore path on the server.
            db_name: Name of the DB subfolder to compress.
            remote_zip: Override the output zip path (used when the caller
                        resolved a name conflict before calling this method).
            log_callback: Optional function for progress messages.

        Returns:
            Absolute path of the zip file on the remote server.

        Raises:
            RuntimeError: If the source folder is missing or zip fails.
        """
        source_path = f"{filestore_root}/{db_name}"
        if remote_zip is None:
            remote_zip = f"/tmp/filestore_{db_name}.zip"

        # Verify source directory exists
        code, _, _ = self._ssh.execute(f"test -d {source_path}")
        if code != 0:
            raise RuntimeError(
                f"Carpeta de filestore no encontrada: {source_path}"
            )

        self._ssh.execute(f"sudo rm -f {remote_zip}")

        # Check /tmp has enough space before compressing.
        # Use the uncompressed filestore size as a conservative upper bound.
        code, size_out, _ = self._ssh.execute(
            f"du -sb {source_path} 2>/dev/null | cut -f1"
        )
        if code == 0 and size_out.strip().isdigit():
            fs_bytes = int(size_out.strip())
            code2, avail_out, _ = self._ssh.execute(
                "df -B1 /tmp | tail -1 | tr -s ' ' | cut -d' ' -f4"
            )
            if code2 == 0 and avail_out.strip().isdigit():
                avail_bytes = int(avail_out.strip())
                fs_mb    = fs_bytes    / (1024 ** 2)
                avail_mb = avail_bytes / (1024 ** 2)
                if log_callback:
                    log_callback(
                        f"Espacio en /tmp: {avail_mb:,.0f} MB disponibles | "
                        f"Filestore '{db_name}': {fs_mb:,.0f} MB sin comprimir"
                    )
                if avail_bytes < fs_bytes:
                    raise RuntimeError(
                        f"Espacio insuficiente en /tmp para comprimir el filestore "
                        f"'{db_name}'.\n\n"
                        f"  Disponible : {avail_mb:,.0f} MB\n"
                        f"  Necesario  : {fs_mb:,.0f} MB (tamanio sin comprimir)\n\n"
                        "Libere espacio en /tmp o elija una ruta personalizada."
                    )

        if log_callback:
            log_callback(f"Comprimiendo filestore '{db_name}' ...")

        # Ensure zip is available (Debian/Ubuntu and RHEL/CentOS paths)
        self._ssh.execute(
            "which zip >/dev/null 2>&1 || "
            "(apt-get install -y zip 2>/dev/null || yum install -y zip 2>/dev/null)"
        )

        def _heartbeat(status: str) -> None:
            if log_callback:
                log_callback(f"  [zip en curso] {status}")

        # nice -n 19: lowest CPU priority.
        # -1 compression level: fastest, minimal CPU — filestore files are
        # mostly already-compressed attachments (PDFs, images), so heavy
        # compression wastes CPU without significant size reduction.
        #
        # cd to filestore_root before zipping so the archive stores only the
        # relative path "{db_name}/00/..." instead of the full absolute path.
        # Without this, extraction reproduces the entire directory tree inside
        # the staging folder and the content ends up at the wrong nested path.
        code, _, err = self._ssh.execute_long(
            f"cd {filestore_root} && nice -n 19 zip -1 -r {remote_zip} {db_name}",
            watch_cmd=f"ls -lh {remote_zip} 2>/dev/null || echo 'comprimiendo...'",
            heartbeat_callback=_heartbeat,
            timeout=3600,
            cancel_event=cancel_event,
        )
        if code != 0:
            raise RuntimeError(f"zip fallo para '{db_name}':\n{err}")

        _, size_out, _ = self._ssh.execute(f"ls -lh {remote_zip}")
        if log_callback:
            log_callback(f"Filestore comprimido: {size_out}")

        return remote_zip

    # ── Cleanup ───────────────────────────────────────────────────────────

    def cleanup_remote(self, remote_path: str) -> None:
        """Delete a temporary file on the remote server (best-effort)."""
        self._ssh.execute(f"sudo rm -f {remote_path}")
