"""
PostgreSQL dump operations executed via SSH.

Connects as root and delegates to the postgres OS user with sudo,
which matches the standard Odoo server setup used in this workspace.

Also supports PostgreSQL running inside a Docker container (target.container
set) via core.pg_exec — see DockerManager for container discovery.
"""
from __future__ import annotations
from typing import Callable
from .ssh_client import SSHClient
from .pg_exec import PgTarget, build_psql, build_pg_dump, wrap_exec

# Databases that are always excluded from the selection list
_SYSTEM_DBS = {"template0", "template1", "postgres"}


class DBManager:
    """Creates and cleans up PostgreSQL dumps on a remote server."""

    def __init__(self, ssh: SSHClient, target: PgTarget | None = None) -> None:
        self._ssh = ssh
        self._target = target or PgTarget()

    # ── Database discovery ────────────────────────────────────────────────

    def list_databases(self) -> list[str]:
        """
        Return all non-system PostgreSQL database names on the remote server.

        Returns:
            Sorted list of database names.

        Raises:
            RuntimeError: If the psql command fails.
        """
        # Consulta directa a pg_database en vez de 'psql -l': -l imprime la
        # columna de privilegios de acceso (ACL, un array), y cuando esa ACL
        # tiene mas de una entrada (comun en template0/template1 de Postgres
        # 15+), psql -A la imprime en varias lineas — 'splitlines()' entonces
        # confunde cada linea extra de la ACL con un nombre de BD adicional
        # inexistente (confirmado en contenedores Postgres 15 de Pruebas_19,
        # ej. 'odoo=CTc/odoo'). La consulta a pg_database no trae esa columna.
        # -d postgres: sin -d, psql intenta conectar a una BD con el mismo
        # nombre que el usuario (-U), que no existe en Postgres dockerizado
        # (confirmado: "database 'odoo' does not exist" contra Pruebas_19).
        # 'postgres' siempre existe (no es un template, nunca se filtra).
        sql = "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname"
        cmd = build_psql(self._target, f"-d postgres -t -A -c \"{sql}\"")
        code, out, err = self._ssh.execute(cmd)

        if code != 0:
            raise RuntimeError(f"No se pudo listar las bases de datos:\n{err}")

        databases: list[str] = []
        for line in out.splitlines():
            name = line.strip()
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
        # Get database size in bytes via PostgreSQL catalog.
        # -d postgres: sin -d, psql en modo contenedor intenta conectar a
        # una BD con el mismo nombre que el usuario -U, que no existe.
        code, size_out, _ = self._ssh.execute(
            build_psql(self._target,
                f"-d postgres -t -c \"SELECT pg_database_size('{db_name}')\" 2>/dev/null")
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
        # En modo contenedor, pg_dump escribe DENTRO del contenedor en la
        # misma ruta /tmp/... ; se extrae al host con 'docker cp' al terminar
        # (el resto del flujo, ej. SFTP, ya asume el archivo en el host).
        dump_target_path = remote_path
        cmd = build_pg_dump(
            self._target,
            f"-d {db_name} -F {fmt_flag} -b --lock-wait-timeout=30000 "
            f"-f {dump_target_path}",
        )
        if not self._target.container:
            cmd = f"nice -n 19 {cmd}"

        def _heartbeat(status: str) -> None:
            if log_callback:
                log_callback(f"  [pg_dump en curso] {status}")

        watch_cmd = f"ls -lh {remote_path} 2>/dev/null || echo 'generando...'"
        if self._target.container:
            watch_cmd = wrap_exec(self._target, watch_cmd)

        code, _, err = self._ssh.execute_long(
            cmd,
            watch_cmd=watch_cmd,
            heartbeat_callback=_heartbeat,
            timeout=3600,
            cancel_event=cancel_event,
        )

        if code != 0:
            raise RuntimeError(f"pg_dump fallo para '{db_name}':\n{err}")

        if self._target.container:
            if log_callback:
                log_callback(f"Extrayendo dump del contenedor '{self._target.container}' al host ...")
            cp_code, _, cp_err = self._ssh.execute(
                f"docker cp {self._target.container}:{remote_path} {remote_path}",
                timeout=600,
            )
            if cp_code != 0:
                raise RuntimeError(
                    f"No se pudo extraer el dump del contenedor:\n{cp_err}"
                )

        # Verify the output file exists (on the host) and log its size
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
        if self._target.container:
            self._ssh.execute(wrap_exec(self._target, f"rm -f {remote_path}"))
