"""
PostgreSQL dump operations executed via SSH.

Connects as root and delegates to the postgres OS user with sudo,
which matches the standard Odoo server setup used in this workspace.
"""
from __future__ import annotations
from typing import Callable
from .ssh_client import SSHClient

# Databases that are always excluded from the selection list
_SYSTEM_DBS = {"template0", "template1", "postgres"}


class DBManager:
    """Creates and cleans up PostgreSQL dumps on a remote server."""

    def __init__(self, ssh: SSHClient) -> None:
        self._ssh = ssh

    # ── Database discovery ────────────────────────────────────────────────

    def list_databases(self) -> list[str]:
        """
        Return all non-system PostgreSQL database names on the remote server.

        Returns:
            Sorted list of database names.

        Raises:
            RuntimeError: If the psql command fails.
        """
        cmd = "sudo -u postgres psql -l -t -A --field-separator='|'"
        code, out, err = self._ssh.execute(cmd)

        if code != 0:
            raise RuntimeError(f"No se pudo listar las bases de datos:\n{err}")

        databases: list[str] = []
        for line in out.splitlines():
            parts = line.split("|")
            name = parts[0].strip() if parts else ""
            if name and name not in _SYSTEM_DBS:
                databases.append(name)

        return sorted(databases)

    # ── Dump generation ───────────────────────────────────────────────────

    def remote_file_exists(self, remote_path: str) -> bool:
        """Return True if the file already exists on the remote server."""
        code, _, _ = self._ssh.execute(f"test -f {remote_path}")
        return code == 0

    def default_dump_path(self, db_name: str, fmt: str) -> str:
        """Return the default /tmp/ path for a dump file."""
        ext = "dump" if fmt == "dump" else "sql"
        return f"/tmp/odoo_{db_name}.{ext}"

    def _check_tmp_space(
        self,
        db_name: str,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        """
        Verify /tmp has enough free space to hold the dump before starting.

        Uses the current database size as a conservative upper bound
        (pg custom format is typically 30-50% of raw size, but we check
        against 100% to account for plain SQL and index bloat).

        Raises:
            RuntimeError: If free space in /tmp is less than the database size.
        """
        # Get database size in bytes via PostgreSQL catalog
        code, size_out, _ = self._ssh.execute(
            f"sudo -u postgres psql -t -c "
            f"\"SELECT pg_database_size('{db_name}')\" 2>/dev/null"
        )
        if code != 0 or not size_out.strip().isdigit():
            # Cannot determine — proceed optimistically
            return

        db_bytes = int(size_out.strip())

        # Get available bytes in /tmp (column 4 of df output)
        code, avail_out, _ = self._ssh.execute(
            "df -B1 /tmp | tail -1 | tr -s ' ' | cut -d' ' -f4"
        )
        if code != 0 or not avail_out.strip().isdigit():
            return

        avail_bytes = int(avail_out.strip())
        db_mb    = db_bytes    / (1024 ** 2)
        avail_mb = avail_bytes / (1024 ** 2)

        if log_callback:
            log_callback(
                f"Espacio en /tmp: {avail_mb:,.0f} MB disponibles | "
                f"BD '{db_name}': {db_mb:,.0f} MB"
            )

        if avail_bytes < db_bytes:
            raise RuntimeError(
                f"Espacio insuficiente en /tmp para el dump de '{db_name}'.\n\n"
                f"  Disponible : {avail_mb:,.0f} MB\n"
                f"  Necesario  : {db_mb:,.0f} MB (tamanio de la BD)\n\n"
                "Libere espacio en /tmp o elija una ruta de destino diferente."
            )

    def create_dump(
        self,
        db_name: str,
        fmt: str = "dump",
        remote_path: str | None = None,
        log_callback: Callable[[str], None] | None = None,
        cancel_event=None,
    ) -> str:
        """
        Run pg_dump on the remote server and save the result in /tmp/.

        Args:
            db_name: Name of the database to dump.
            fmt: 'dump' for pg custom format (-Fc) or 'sql' for plain text (-Fp).
            remote_path: Override the output path (used when the caller resolved
                         a name conflict before calling this method).
            log_callback: Optional function receiving progress messages.

        Returns:
            Absolute path of the dump file on the remote server.

        Raises:
            RuntimeError: If pg_dump fails or the output file is missing.
        """
        fmt_flag = "c" if fmt == "dump" else "p"
        if remote_path is None:
            ext = "dump" if fmt == "dump" else "sql"
            remote_path = f"/tmp/odoo_{db_name}.{ext}"

        # Abort early if /tmp doesn't have enough room for the dump
        self._check_tmp_space(db_name, log_callback)

        # Remove stale file at the resolved path before generating
        self._ssh.execute(f"sudo rm -f {remote_path}")

        if log_callback:
            log_callback(f"Generando dump de '{db_name}' -> {remote_path} ...")

        # nice -n 19: lowest CPU priority so Odoo keeps responding normally.
        # --lock-wait-timeout: fail fast (30s) instead of blocking indefinitely
        # if another transaction holds locks on the DB being dumped.
        cmd = (
            f"nice -n 19 sudo -u postgres pg_dump -U postgres -d {db_name} "
            f"-F {fmt_flag} -b --lock-wait-timeout=30000 -f {remote_path}"
        )

        def _heartbeat(status: str) -> None:
            if log_callback:
                log_callback(f"  [pg_dump en curso] {status}")

        code, _, err = self._ssh.execute_long(
            cmd,
            watch_cmd=f"ls -lh {remote_path} 2>/dev/null || echo 'generando...'",
            heartbeat_callback=_heartbeat,
            timeout=3600,
            cancel_event=cancel_event,
        )

        if code != 0:
            raise RuntimeError(f"pg_dump fallo para '{db_name}':\n{err}")

        # Verify the output file exists and log its size
        v_code, v_out, _ = self._ssh.execute(f"ls -lh {remote_path}")
        if v_code != 0:
            raise RuntimeError(f"Archivo de dump no encontrado: {remote_path}")

        if log_callback:
            log_callback(f"Dump generado: {v_out}")

        return remote_path

    # ── Cleanup ───────────────────────────────────────────────────────────

    def cleanup_remote(self, remote_path: str) -> None:
        """Delete a temporary file on the remote server (best-effort)."""
        self._ssh.execute(f"sudo rm -f {remote_path}")
