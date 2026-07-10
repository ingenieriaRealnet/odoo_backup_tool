"""
Database and filestore restoration operations via SSH.

Restoration sequence:
  1. Upload dump and/or filestore zip to server (only if source is local)
  2. Create target PostgreSQL database
  3. Auto-detect dump format (pg custom vs plain SQL)
  4. Restore dump  →  pg_restore -j N  (custom)  or  psql  (plain SQL)
  5. Unzip filestore, rsync to destination, fix ownership
  6. Optional: neutralize the database (disable outgoing mail, crons, etc.)

All long-running steps use execute_long() so the tool survives if the
SSH connection drops mid-operation.
"""
from __future__ import annotations

import os
from typing import Callable

from .inventory_manager import InventoryManager, _human_size
from .ssh_client import SSHClient

# Temporary work directory on the remote server
_TMP_DIR = "/var/tmp"


class RestoreManager:
    """Orchestrates a full Odoo database + filestore restoration."""

    def __init__(self, ssh: SSHClient) -> None:
        self._ssh = ssh

    # ── File upload ───────────────────────────────────────────────────────

    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        progress_callback: Callable[[int, int], None] | None = None,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        """
        Upload a local file to the remote server via SFTP.

        Args:
            local_path: Absolute local path to the file.
            remote_path: Absolute destination path on the server.
            progress_callback: Called with (bytes_transferred, total_bytes).
            log_callback: Called with status messages.
        """
        filename = os.path.basename(local_path)
        size_mb = os.path.getsize(local_path) / (1024 * 1024)

        if log_callback:
            log_callback(f"Subiendo {filename} ({size_mb:.1f} MB) -> {remote_path} ...")

        sftp = self._ssh.open_sftp()
        try:
            def _on_progress(transferred: int, total: int) -> None:
                if progress_callback:
                    progress_callback(transferred, total)

            sftp.put(local_path, remote_path, callback=_on_progress)
        finally:
            sftp.close()

        if log_callback:
            log_callback(f"Subida completa: {remote_path}")

    # ── Database creation ─────────────────────────────────────────────────

    def create_database(
        self,
        db_name: str,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        """
        Create a new PostgreSQL database owned by the odoo OS user.

        Args:
            db_name: Name for the new database.

        Raises:
            RuntimeError: If the database already exists or creation fails.
        """
        # Abort early if the database already exists to avoid data loss
        code, out, _ = self._ssh.execute(
            f"sudo -u postgres psql -l -t -A --field-separator='|' "
            f"| cut -d'|' -f1 | grep -w '{db_name}'"
        )
        if code == 0 and out.strip() == db_name:
            raise RuntimeError(
                f"La base de datos '{db_name}' ya existe.\n"
                "Elimínela primero o elija otro nombre."
            )

        if log_callback:
            log_callback(f"Creando base de datos '{db_name}' ...")

        code, _, err = self._ssh.execute(
            f"sudo -u postgres createdb -O odoo {db_name}"
        )
        if code != 0:
            raise RuntimeError(f"Error creando '{db_name}':\n{err}")

        if log_callback:
            log_callback(f"Base de datos '{db_name}' creada.")

    # ── Dump format detection & version check ─────────────────────────────

    def detect_dump_format(self, remote_path: str) -> str:
        """
        Detect whether a dump file uses pg custom format or plain SQL.

        Reads the first 5 bytes: pg custom dumps start with 'PGDMP'.

        Returns:
            'custom' or 'sql'
        """
        code, out, _ = self._ssh.execute(
            f"dd if={remote_path} bs=1 count=5 2>/dev/null | cat"
        )
        if code == 0 and "PGDMP" in out:
            return "custom"
        return "sql"

    def check_version_compatibility(
        self,
        dump_path: str,
        log_callback: Callable[[str], None] | None = None,
    ) -> dict:
        """
        Compare the PostgreSQL version that created the dump against the
        version available on this server, and return a compatibility report.

        For custom-format dumps, pg_restore requires that the server version
        be >= the version that produced the dump. If it is older, the restore
        will fail with 'unsupported version in file header'.

        Returns:
            dict with keys:
              compatible (bool)
              dump_pg_version (str)   e.g. '16.14'
              server_pg_version (str) e.g. '14.12'
              recommendation (str)    actionable message
        """
        result = {
            "compatible": True,
            "dump_pg_version": "desconocida",
            "server_pg_version": "desconocida",
            "recommendation": "",
        }

        # Get the PostgreSQL version on the destination server
        code, srv_out, _ = self._ssh.execute(
            "sudo -u postgres psql -t -c 'SELECT version();' 2>/dev/null"
        )
        if code == 0 and srv_out:
            # "PostgreSQL 14.12 (Ubuntu ...)" → "14.12"
            parts = srv_out.strip().split()
            if len(parts) >= 2:
                result["server_pg_version"] = parts[1]

        # Get the pg_dump version from the dump header.
        # IMPORTANT: do NOT pipe to head or redirect stderr to /dev/null.
        # If the server pg_restore is older than the dump format, pg_restore --list
        # itself fails with "unsupported version (X.Y) in file header".
        # Piping masks the exit code and 2>/dev/null discards the error message,
        # causing the incompatibility to go undetected.
        code, hdr_out, hdr_err = self._ssh.execute(
            f"sudo -u postgres pg_restore --list {dump_path}"
        )

        if code == 0:
            # Normal path: parse the version comment from the archive listing
            for line in hdr_out.splitlines():
                # Example: "; Dumped from database version 16.14"
                lower = line.lower()
                if "dumped from database version" in lower:
                    result["dump_pg_version"] = line.split()[-1].strip()
                    break

        else:
            # pg_restore --list failed; check whether it's a format version error
            combined_err = (hdr_err + hdr_out).lower()
            if "unsupported version" in combined_err:
                # Extract the format version number from the error message:
                # "pg_restore: error: unsupported version (1.15) in file header"
                import re
                match = re.search(r"unsupported version \(([0-9.]+)\)", combined_err)
                fmt_ver = match.group(1) if match else "desconocida"

                result["compatible"] = False
                result["recommendation"] = (
                    f"Incompatibilidad de formato de dump detectada.\n\n"
                    f"El dump usa formato de archivo PostgreSQL v{fmt_ver}, "
                    f"pero el servidor destino tiene PostgreSQL "
                    f"{result['server_pg_version']} cuyo pg_restore no soporta "
                    f"ese formato.\n\n"
                    f"Opciones:\n"
                    f"  1. Generar el backup en formato SQL plano (.sql) desde el "
                    f"servidor origen — psql puede restaurarlo en versiones anteriores.\n"
                    f"  2. Actualizar PostgreSQL en el servidor destino a una version "
                    f"igual o superior a la del servidor origen.\n"
                    f"  3. Restaurar en un servidor con la misma version de PostgreSQL "
                    f"que el origen."
                )
                if log_callback:
                    log_callback(
                        f"INCOMPATIBILIDAD: formato dump={fmt_ver} | "
                        f"servidor={result['server_pg_version']}"
                    )
                return result

        # Normal major-version comparison (when pg_restore --list succeeded)
        try:
            srv_major  = int(result["server_pg_version"].split(".")[0])
            dump_major = int(result["dump_pg_version"].split(".")[0])

            if srv_major < dump_major:
                result["compatible"] = False
                result["recommendation"] = (
                    f"El servidor destino tiene PostgreSQL {result['server_pg_version']} "
                    f"pero el dump fue creado con PostgreSQL {result['dump_pg_version']}.\n\n"
                    f"Opciones:\n"
                    f"  1. Hacer el backup en formato SQL plano (.sql) — psql puede "
                    f"restaurarlo en versiones anteriores.\n"
                    f"  2. Actualizar PostgreSQL en el servidor destino a v{dump_major}+.\n"
                    f"  3. Restaurar en un servidor con PostgreSQL {dump_major} o superior."
                )
        except (ValueError, IndexError):
            # Could not parse versions — skip check and proceed
            pass

        if log_callback:
            log_callback(
                f"Versiones: dump={result['dump_pg_version']} | "
                f"servidor={result['server_pg_version']} | "
                f"compatible={'Si' if result['compatible'] else 'NO'}"
            )

        return result

    # ── Dump restoration ──────────────────────────────────────────────────

    def restore_dump(
        self,
        dump_path: str,
        db_name: str,
        fmt: str,
        jobs: int = 4,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        """
        Restore a dump file into an existing (empty) database.

        Args:
            dump_path: Absolute path of the dump file on the server.
            db_name: Target database name (must already exist).
            fmt: 'custom' → pg_restore -j N  |  'sql' → psql.
            jobs: Parallel workers for pg_restore (ignored for plain SQL).
            log_callback: Optional progress messages.

        Raises:
            RuntimeError: If the restore command fails.
        """
        if fmt == "custom":
            # cd /tmp first: postgres user cannot access /root (current dir when
            # running via sudo), which would cause a spurious 'permission denied'
            # warning before pg_restore even starts.
            # nice -n 19: keeps CPU priority low so the server stays responsive.
            # -j workers: parallel restore is fast but CPU-intensive; each worker
            # holds its own transaction, so on a production server with active
            # Odoo connections consider reducing workers to 1-2.
            cmd = (
                f"cd /tmp && nice -n 19 sudo -u postgres pg_restore -U postgres "
                f"-d {db_name} -j {jobs} --no-owner --role=odoo {dump_path}"
            )
            if log_callback:
                if jobs > 2:
                    log_callback(
                        f"[aviso] Restaurando con {jobs} workers paralelos. "
                        "En servidores en produccion se recomienda 1-2 workers "
                        "para no saturar la CPU."
                    )
                log_callback(
                    f"Restaurando dump custom en '{db_name}' con {jobs} workers ..."
                )
        else:
            # nice -n 19: plain SQL restore is single-threaded but can be slow
            # on large DBs; low priority keeps the server usable.
            cmd = (
                f"cd /tmp && nice -n 19 sudo -u postgres psql "
                f"-U postgres -d {db_name} -f {dump_path}"
            )
            if log_callback:
                log_callback(f"Restaurando dump SQL plano en '{db_name}' ...")

        def _heartbeat(status: str) -> None:
            if log_callback:
                log_callback(f"  [restauracion BD] en proceso: {status.strip()}")

        code, _, err = self._ssh.execute_long(
            cmd,
            # Count public tables as a proxy for restore progress
            watch_cmd=(
                f"sudo -u postgres psql -d {db_name} -t -c "
                f"\"SELECT count(*) FROM pg_tables "
                f"WHERE schemaname='public'\" 2>/dev/null"
            ),
            heartbeat_callback=_heartbeat,
            timeout=7200,
        )

        # pg_restore with -j may return non-zero for non-fatal warnings;
        # only treat it as failure if the DB has no tables afterwards.
        if code != 0:
            v_code, v_out, _ = self._ssh.execute(
                f"sudo -u postgres psql -d {db_name} -t -c "
                f"\"SELECT count(*) FROM pg_tables WHERE schemaname='public'\""
            )
            if v_code != 0 or int(v_out.strip() or "0") == 0:
                raise RuntimeError(f"Error restaurando dump:\n{err}")
            if log_callback:
                log_callback("Restauracion completada con advertencias no criticas.")
        else:
            if log_callback:
                log_callback(f"Dump restaurado correctamente en '{db_name}'.")

    # ── Filestore restoration ─────────────────────────────────────────────

    def restore_filestore(
        self,
        zip_path: str,
        db_name: str,
        filestore_root: str,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        """
        Extract a filestore zip and place it in the correct location.

        Strategy: extract directly into a staging directory on the SAME partition
        as the final destination (filestore_root), then do an atomic ``mv`` to the
        final path.  This avoids the old two-step (extract to /var/tmp + rsync) that
        required double the disk space and could fail silently when /var/tmp was full.

        Handles two zip structures automatically:
          - Files at the zip root           → staging dir is moved to dest_path
          - Full server path embedded in zip → db-named subfolder is moved to dest_path

        Rollback guarantee: if the unzip fails at any point, the staging directory is
        removed immediately and dest_path is never created/modified.

        Args:
            zip_path: Absolute path of the zip file on the server.
            db_name: Database name (used as destination subfolder name).
            filestore_root: Root filestore directory
                            (e.g. /opt/odoo/.local/share/Odoo/filestore).
            log_callback: Optional progress messages.

        Raises:
            RuntimeError: If unzip fails, staging cannot be created, or the final
                          move fails.
        """
        dest_path = f"{filestore_root}/{db_name}"
        # Staging sits on the same partition as dest so the final mv is instant.
        staging = f"{filestore_root}/{db_name}_obt_tmp"

        # ── Pre-flight: verify unzip is available ─────────────────────────
        # Only unzip is needed — rsync is no longer used because we move
        # files within the same partition instead of copying across partitions.
        code, _, _ = self._ssh.execute("which unzip >/dev/null 2>&1")
        if code != 0:
            if log_callback:
                log_callback("'unzip' no encontrado — intentando instalacion automatica ...")
            self._ssh.execute(
                "apt-get install -y unzip 2>/dev/null || "
                "yum install -y unzip 2>/dev/null || "
                "apk add --no-cache unzip 2>/dev/null"
            )
            code, _, _ = self._ssh.execute("which unzip >/dev/null 2>&1")
            if code != 0:
                raise RuntimeError(
                    "'unzip' no esta instalado en el servidor destino "
                    "y la instalacion automatica fallo.\n\n"
                    "Instale 'unzip' manualmente:\n"
                    "  Debian/Ubuntu : apt-get install unzip\n"
                    "  RHEL/CentOS   : yum install unzip\n"
                    "  Alpine        : apk add unzip"
                )
            if log_callback:
                log_callback("'unzip' instalado correctamente.")

        # ── Create staging directory (same partition as dest) ─────────────
        # Any leftover staging from a previous failed attempt is removed first
        # so re-running the restore after a failure starts from a clean state.
        self._ssh.execute(f"sudo rm -rf {staging}")
        code_mk, _, err_mk = self._ssh.execute(f"sudo mkdir -p {staging}")
        if code_mk != 0:
            raise RuntimeError(
                f"No se pudo crear el directorio de staging:\n"
                f"  {staging}\n\n{err_mk}\n\n"
                f"Verifique permisos en '{filestore_root}' del servidor de restauracion."
            )

        # ── Pre-flight: disk-space check on the filestore partition ───────
        # Query the zip's central directory to get the total uncompressed size
        # before extracting.  This produces a clear error before any disk I/O
        # rather than a silent mid-extraction failure.
        # The space check targets the filestore partition (where staging lives),
        # NOT /var/tmp — this is the key advantage over the old approach.
        _, list_out, _ = self._ssh.execute(
            f"unzip -l {zip_path} 2>/dev/null | tail -1"
        )
        # "unzip -l" summary last line: "  <total_bytes>  <n> files"
        list_parts = list_out.strip().split()
        if list_parts and list_parts[0].isdigit():
            try:
                needed_mb = int(list_parts[0]) // (1024 * 1024) + 200  # 200 MB safety buffer
                _, df_out, _ = self._ssh.execute(
                    f"df -m {filestore_root} 2>/dev/null | tail -1"
                )
                df_parts = df_out.split()
                # df -m columns: Filesystem 1M-blocks Used Available Use% Mounted
                if len(df_parts) >= 4:
                    available_mb = int(df_parts[3])
                    if available_mb < needed_mb:
                        # Cleanup staging before raising so a retry starts clean
                        self._ssh.execute(f"sudo rm -rf {staging}")
                        raise RuntimeError(
                            f"Espacio insuficiente en la particion del filestore "
                            f"para descomprimir.\n\n"
                            f"  Particion : {filestore_root}\n"
                            f"  Necesario : ~{needed_mb:,} MB\n"
                            f"  Disponible: {available_mb:,} MB\n\n"
                            f"Libere espacio en esa particion y vuelva a intentarlo."
                        )
                    if log_callback:
                        log_callback(
                            f"Espacio en particion filestore: {available_mb:,} MB "
                            f"disponibles | Filestore descomprimido: ~{needed_mb:,} MB"
                        )
            except ValueError:
                pass  # df output format unexpected — proceed and let unzip report the error

        # ── Extract zip directly into staging ─────────────────────────────
        if log_callback:
            log_callback(f"Descomprimiendo filestore en {staging} ...")

        def _heartbeat_unzip(status: str) -> None:
            if log_callback:
                log_callback(f"  [unzip en curso] {status}")

        # Omit -q so that unzip errors (e.g. "No space left on device") are
        # captured in the nohup log and displayed to the user.
        code, _, err = self._ssh.execute_long(
            f"nice -n 19 unzip {zip_path} -d {staging}",
            watch_cmd=f"du -sh {staging} 2>/dev/null",
            heartbeat_callback=_heartbeat_unzip,
            timeout=3600,
        )
        if code != 0:
            # Rollback: remove partial staging so the server stays clean
            self._ssh.execute(f"sudo rm -rf {staging}")
            raise RuntimeError(
                f"Error descomprimiendo filestore:\n"
                f"{err or '(sin detalle — revise espacio en disco en el servidor de restauracion)'}"
            )

        # ── Locate actual filestore content inside staging ────────────────
        # Two possible zip structures depending on how the zip was created:
        #
        #   Relative-path zip (current): staging/{original_db}/00/...
        #     → first direct child of staging IS the content directory
        #
        #   Absolute-path zip (old backups): staging/opt/.../filestore/{original_db}/00/...
        #     → there is an embedded 'filestore' directory deep in the tree
        #
        # Strategy:
        #   1. Look for an embedded 'filestore' directory (absolute-path zip).
        #      If found, take its first child as the source.
        #   2. Otherwise take the first direct child of staging (relative-path zip).
        #   3. Fall back to staging root if neither yields a result.
        source_dir = staging  # safe default

        _, fs_dir_out, _ = self._ssh.execute(
            f"find {staging} -maxdepth 12 -type d -name 'filestore' 2>/dev/null | head -1"
        )
        fs_dir = fs_dir_out.strip()

        if fs_dir:
            # Absolute-path zip: navigate into the embedded filestore dir
            _, child_out, _ = self._ssh.execute(
                f"ls -d {fs_dir}/*/ 2>/dev/null | head -1"
            )
            candidate = child_out.strip().rstrip("/")
            if candidate:
                source_dir = candidate
                if log_callback:
                    log_callback(f"  Contenido filestore detectado (ruta absoluta en ZIP): {source_dir}")
        else:
            # Relative-path zip: the DB dir is a direct child of staging
            _, child_out, _ = self._ssh.execute(
                f"ls -d {staging}/*/ 2>/dev/null | head -1"
            )
            candidate = child_out.strip().rstrip("/")
            if candidate:
                source_dir = candidate
                if log_callback:
                    log_callback(f"  Contenido filestore detectado (ruta relativa en ZIP): {source_dir}")

        # ── Rotate old destination if it exists ───────────────────────────
        # Rename the previous filestore to _obt_prev before the mv so there
        # is a recovery point if something goes wrong after the move.
        _, dest_exists, _ = self._ssh.execute(f"test -d {dest_path} && echo yes")
        if dest_exists.strip() == "yes":
            prev_path = f"{dest_path}_obt_prev"
            self._ssh.execute(f"sudo rm -rf {prev_path}")
            self._ssh.execute(f"sudo mv {dest_path} {prev_path}")
            if log_callback:
                log_callback(f"Filestore anterior renombrado a '{db_name}_obt_prev' como respaldo.")

        # ── Atomic move to final path ─────────────────────────────────────
        # mv within the same partition is instant (inode rename), regardless
        # of the filestore size.  No data is copied at this step.
        if log_callback:
            log_callback(f"Moviendo filestore -> {dest_path} ...")

        code_mv, _, err_mv = self._ssh.execute(f"sudo mv {source_dir} {dest_path}")
        if code_mv != 0:
            # mv failed: restore previous filestore from the _obt_prev backup
            if dest_exists.strip() == "yes":
                self._ssh.execute(f"sudo mv {dest_path}_obt_prev {dest_path} 2>/dev/null || true")
            self._ssh.execute(f"sudo rm -rf {staging}")
            raise RuntimeError(
                f"Error moviendo el filestore a su destino final:\n{err_mv}\n\n"
                f"El staging queda disponible en:\n  {staging}\n"
                f"Si la particion tiene espacio, puede intentar el mv manualmente."
            )

        # ── Fix ownership ─────────────────────────────────────────────────
        if log_callback:
            log_callback("Ajustando propietario del filestore (odoo:odoo) ...")
        self._ssh.execute(f"sudo chown -R odoo:odoo {dest_path}")

        # ── Cleanup ───────────────────────────────────────────────────────
        # Remove staging (may have leftover parent dirs when source_dir was a
        # deep subfolder).  Also remove the _obt_prev backup once everything
        # succeeded — keep it only if there was a failure above.
        self._ssh.execute(f"sudo rm -rf {staging}")
        if dest_exists.strip() == "yes":
            self._ssh.execute(f"sudo rm -rf {dest_path}_obt_prev")

        if log_callback:
            log_callback(f"Filestore restaurado en {dest_path}")

    # ── Neutralization ────────────────────────────────────────────────────

    def neutralize(
        self,
        db_name: str,
        odoo_conf: str = "/etc/odoo/odoo.conf",
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        """
        Neutralize a restored database to prevent production side-effects.

        Tries Odoo Enterprise's built-in 'neutralize' command first.
        Falls back to direct SQL patching if the command is unavailable
        (e.g. Community edition or older versions).

        What neutralization disables:
          - Outgoing mail servers
          - Scheduled actions (crons)
          - Fetchmail / incoming mail servers
          - Webhook / external connectors where possible

        Args:
            db_name: Name of the database to neutralize.
            odoo_conf: Path to the Odoo configuration file.
            log_callback: Optional progress messages.
        """
        if log_callback:
            log_callback(f"Neutralizando '{db_name}' ...")

        # --- Attempt 1: Odoo Enterprise built-in command (v14+) ---
        code, _, _ = self._ssh.execute(
            f"sudo -u odoo /usr/bin/odoo neutralize "
            f"-c {odoo_conf} -d {db_name} 2>&1",
            timeout=300,
        )
        if code == 0:
            if log_callback:
                log_callback("Neutralizacion via 'odoo neutralize' completada.")
            return

        # --- Attempt 2: venv-based Odoo install ---
        # Detect the Python binary used by the running Odoo service
        code2, venv_py, _ = self._ssh.execute(
            "ps aux | grep '/odoo' | grep -v grep "
            "| awk '{print $11}' | head -1"
        )
        if code2 == 0 and venv_py.strip():
            code, _, _ = self._ssh.execute(
                f"sudo -u odoo {venv_py.strip()} /usr/bin/odoo neutralize "
                f"-c {odoo_conf} -d {db_name} 2>&1",
                timeout=300,
            )
            if code == 0:
                if log_callback:
                    log_callback("Neutralizacion via venv Python completada.")
                return

        # --- Fallback: direct SQL neutralization ---
        if log_callback:
            log_callback(
                "Comando 'odoo neutralize' no disponible — aplicando SQL ..."
            )

        # Each tuple: (description, SQL statement)
        sql_patches: list[tuple[str, str]] = [
            (
                "Deshabilitar servidores de correo saliente",
                "UPDATE ir_mail_server SET active = false;",
            ),
            (
                "Deshabilitar acciones programadas (crons)",
                "UPDATE ir_cron SET active = false;",
            ),
            (
                "Deshabilitar servidores de correo entrante",
                "UPDATE fetchmail_server SET active = false "
                "WHERE active = true;",
            ),
            (
                "Desactivar integraciones de pago en produccion",
                "UPDATE payment_provider SET state = 'disabled' "
                "WHERE state = 'enabled';",
            ),
            (
                "Limpiar token de seguridad de reportes",
                "UPDATE ir_config_parameter "
                "SET value = 'neutralized' "
                "WHERE key = 'report.url';",
            ),
        ]

        for description, sql in sql_patches:
            if log_callback:
                log_callback(f"  SQL: {description} ...")
            c, _, e = self._ssh.execute(
                f"sudo -u postgres psql -d {db_name} -c \"{sql}\" 2>&1"
            )
            if c != 0 and log_callback:
                # Non-fatal — some tables may not exist in all Odoo versions
                log_callback(f"  [aviso] {e.splitlines()[0] if e else 'error menor ignorado'}")

        if log_callback:
            log_callback(f"Neutralizacion SQL de '{db_name}' completada.")

    # ── Odoo privileges ───────────────────────────────────────────────────

    def grant_odoo_privileges(
        self,
        db_name: str,
        odoo_user: str = "odoo",
        filestore_path: str | None = None,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        """
        Grant full ownership and privileges on a restored database to the
        Odoo OS/DB user so Odoo can connect and operate it.

        This is always required after pg_restore because the dump may have
        been created on a different server where object owners differ.

        Steps applied:
          1. Reassign all objects owned by any user to odoo (covers cases
             where the dump owner was 'postgres' or a different username).
          2. Set the database owner to odoo.
          3. Grant all privileges on the database, schema, tables, sequences.
          4. Set default privileges for future objects.
          5. If filestore_path is given: chown + chmod the directory tree.

        Args:
            db_name: Target database name.
            odoo_user: PostgreSQL role that Odoo uses (default: 'odoo').
            filestore_path: Absolute path of the DB filestore folder on the
                            server (e.g. /opt/odoo/.local/.../filestore/db_name).
                            When provided, ownership and permissions are fixed.
            log_callback: Optional progress messages.
        """
        if log_callback:
            log_callback(
                f"Asignando permisos de '{odoo_user}' sobre '{db_name}' ..."
            )

        def _sql(statement: str) -> None:
            """Run a SQL statement connected to db_name as postgres superuser."""
            code, _, err = self._ssh.execute(
                f"sudo -u postgres psql -d {db_name} -c \"{statement}\" 2>&1"
            )
            if code != 0 and log_callback:
                # Non-fatal: log the warning but keep going
                log_callback(f"  [aviso] {err.splitlines()[0] if err else statement}")

        # Ensure the odoo role exists before reassigning
        self._ssh.execute(
            f"sudo -u postgres psql -c "
            f"\"DO \\$\\$ BEGIN "
            f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='{odoo_user}') "
            f"THEN CREATE ROLE {odoo_user} LOGIN; END IF; END \\$\\$;\" 2>/dev/null"
        )

        # Reassign ownership of every object that belongs to any other role
        # This handles dumps where owner was 'postgres' or a different username
        _sql(f"REASSIGN OWNED BY postgres TO {odoo_user};")

        # Database-level ownership and privileges
        self._ssh.execute(
            f"sudo -u postgres psql -c "
            f"\"ALTER DATABASE {db_name} OWNER TO {odoo_user};\" 2>&1"
        )
        _sql(f"GRANT ALL PRIVILEGES ON DATABASE {db_name} TO {odoo_user};")

        # Schema-level privileges
        _sql(f"GRANT ALL PRIVILEGES ON SCHEMA public TO {odoo_user};")
        _sql(f"GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO {odoo_user};")
        _sql(f"GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO {odoo_user};")
        _sql(f"GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public TO {odoo_user};")

        # Default privileges for objects created in future migrations
        _sql(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT ALL ON TABLES TO {odoo_user};"
        )
        _sql(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT ALL ON SEQUENCES TO {odoo_user};"
        )
        _sql(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT ALL ON FUNCTIONS TO {odoo_user};"
        )

        # ── Filestore filesystem permissions ──────────────────────────────
        if filestore_path:
            if log_callback:
                log_callback(f"Ajustando permisos del filestore: {filestore_path} ...")

            # Ownership: odoo user and group recursively
            self._ssh.execute(
                f"sudo chown -R {odoo_user}:{odoo_user} {filestore_path}"
            )
            # Directories: rwxr-x--- (750) — odoo can traverse, others cannot.
            # Use '-exec {} +' (batch mode): groups multiple paths per chmod call
            # instead of spawning one process per file. On a filestore with
            # 100k files, '\;' would spawn 100k processes and spike server load.
            self._ssh.execute(
                f"sudo find {filestore_path} -type d -exec chmod 750 {{}} +"
            )
            # Files: rw-r----- (640) — odoo can read/write, others cannot
            self._ssh.execute(
                f"sudo find {filestore_path} -type f -exec chmod 640 {{}} +"
            )

            if log_callback:
                log_callback("Permisos del filestore ajustados.")

        if log_callback:
            log_callback(
                f"Permisos de '{odoo_user}' aplicados correctamente sobre '{db_name}'."
            )

    # ── Post-restore validation ───────────────────────────────────────────

    def post_restore_check(
        self,
        db_name: str,
        filestore_path: str | None = None,
        odoo_user: str = "odoo",
        inventory: dict | None = None,
        log_callback: Callable[[str], None] | None = None,
    ) -> dict:
        """
        Run post-restore sanity checks on the database and (optionally) the
        filestore before declaring the restoration complete.

        Checks performed
        ----------------
        Standalone checks (always run)
          - Public table count (minimum expected for a functional Odoo instance)
          - Admin user exists and is active (login='admin' with active=True)
          - Active user count (at least 1 usable account)
          - Installed module count
          - ir_attachment rows that reference filestore files (store_fname IS NOT NULL)
          - Odoo DB role can actually connect (sudo -u odoo psql)

        Filestore integrity  (only when filestore_path is provided)
          - Directory exists and is readable
          - Total file count and total size
          - Files with size 0 bytes (truncation / incomplete transfer indicator)
          - Ownership check (all files should be owned by odoo_user)
          - Coverage: filestore file count vs. ir_attachment count that needs files
            (ratio below 0.90 = 90% flags a warning)

        Inventory comparison  (only when inventory is provided)
          Compares every collected metric against the backup baseline using
          InventoryManager.compare().  Results are merged into errors/warnings.

        Returns
        -------
        dict with keys:
          ok        (bool)         True if no blocking errors were found
          warnings  (list[str])    Non-fatal issues the user should review
          errors    (list[str])    Blocking issues that explain why login may fail
          summary   (dict)         Raw counters for the log
        """
        result: dict = {
            "ok": True,
            "warnings": [],
            "errors": [],
            "summary": {},
        }

        def _warn(msg: str) -> None:
            result["warnings"].append(msg)
            if log_callback:
                log_callback(f"  [aviso] {msg}")

        def _err(msg: str) -> None:
            result["errors"].append(msg)
            result["ok"] = False
            if log_callback:
                log_callback(f"  [ERROR] {msg}")

        def _info(msg: str) -> None:
            if log_callback:
                log_callback(f"  {msg}")

        if log_callback:
            log_callback("Ejecutando verificaciones post-restauracion ...")

        # ── 1. DB role connectivity ───────────────────────────────────────
        # Verify the Odoo PostgreSQL role can connect to the restored DB,
        # not just the superuser. Odoo always connects as its own role.
        code, _, conn_err = self._ssh.execute(
            f"sudo -u {odoo_user} psql -d {db_name} -c \"SELECT 1;\" 2>&1"
        )
        if code != 0:
            _err(
                f"El rol '{odoo_user}' no puede conectarse a la base de datos.\n"
                f"  Detalle: {conn_err.splitlines()[0] if conn_err else 'sin detalle'}\n"
                f"  Causa probable: los permisos de base de datos no se aplicaron "
                f"correctamente o el rol no existe en PostgreSQL."
            )
        else:
            _info(f"Rol '{odoo_user}' puede conectarse a '{db_name}'. OK")

        # ── 2. Public table count ─────────────────────────────────────────
        code, tbl_out, _ = self._ssh.execute(
            f"sudo -u postgres psql -d {db_name} -t -c "
            f"\"SELECT count(*) FROM pg_tables WHERE schemaname='public'\" 2>/dev/null"
        )
        table_count = int(tbl_out.strip() or "0") if code == 0 else 0
        result["summary"]["table_count"] = table_count
        _info(f"Tablas en schema public: {table_count}")

        # A minimal functional Odoo DB has at least ~200 tables.
        # Below 50 almost certainly means the restore was incomplete.
        if table_count == 0:
            _err("La base de datos no tiene tablas en schema public — el dump no se restauro.")
        elif table_count < 50:
            _warn(
                f"Solo {table_count} tablas encontradas. Una instancia Odoo funcional "
                "necesita al menos 200 tablas. El dump puede estar incompleto."
            )

        # ── 3. Admin user ─────────────────────────────────────────────────
        # Check login='admin' first, then id=1, then any user in the Odoo
        # administrator group (base.group_system).  A missing 'admin' login
        # is NOT critical: many production instances rename or deactivate the
        # default admin account for security reasons.  As long as the DB has
        # active users (checked in step 4) the instance is usable.
        # We emit a WARNING rather than an ERROR so the restore is not marked
        # as failed when the admin account is intentionally absent.
        admin_ok = False
        code, admin_out, _ = self._ssh.execute(
            f"sudo -u postgres psql -d {db_name} -t -c "
            f"\"SELECT count(*) FROM res_users WHERE login='admin' AND active=true\" "
            f"2>/dev/null"
        )
        if code == 0 and int(admin_out.strip() or "0") > 0:
            admin_ok = True
        else:
            # Fallback 1: user with id=1 (Odoo Community default)
            code2, id1_out, _ = self._ssh.execute(
                f"sudo -u postgres psql -d {db_name} -t -c "
                f"\"SELECT count(*) FROM res_users WHERE id=1 AND active=true\" "
                f"2>/dev/null"
            )
            if code2 == 0 and int(id1_out.strip() or "0") > 0:
                admin_ok = True

            if not admin_ok:
                # Fallback 2: any user in the base.group_system (Administrator) group
                code3, grp_out, _ = self._ssh.execute(
                    f"sudo -u postgres psql -d {db_name} -t -c "
                    f"\"SELECT count(*) FROM res_users u "
                    f"JOIN res_groups_users_rel gr ON gr.uid=u.id "
                    f"JOIN res_groups g ON g.id=gr.gid "
                    f"JOIN ir_model_data d ON d.res_id=g.id AND d.model='res.groups' "
                    f"WHERE d.module='base' AND d.name='group_system' "
                    f"AND u.active=true\" 2>/dev/null"
                )
                if code3 == 0 and int(grp_out.strip() or "0") > 0:
                    admin_ok = True

        result["summary"]["admin_exists"] = admin_ok
        if admin_ok:
            _info("Usuario administrador activo encontrado. OK")
        else:
            # WARNING only — the admin account may be intentionally renamed.
            # The active-user count (step 4) is the definitive integrity check.
            _warn(
                "No se encontro un usuario con login='admin' ni usuario en el grupo "
                "Administrador activo.\n"
                "  Nota: en produccion es comun renombrar o desactivar el usuario 'admin' "
                "por seguridad.\n"
                "  Si hay usuarios activos (verificado en el paso siguiente), "
                "la restauracion es correcta."
            )

        # ── 4. Active user count ──────────────────────────────────────────
        code, usr_out, _ = self._ssh.execute(
            f"sudo -u postgres psql -d {db_name} -t -c "
            f"\"SELECT count(*) FROM res_users WHERE active=true\" 2>/dev/null"
        )
        active_users = int(usr_out.strip() or "0") if code == 0 else 0
        result["summary"]["active_users"] = active_users
        _info(f"Usuarios activos en la BD: {active_users}")

        if active_users == 0:
            _err("No hay usuarios activos en la base de datos. No sera posible iniciar sesion.")

        # ── 5. Installed modules ──────────────────────────────────────────
        code, mod_out, _ = self._ssh.execute(
            f"sudo -u postgres psql -d {db_name} -t -c "
            f"\"SELECT count(*) FROM ir_module_module WHERE state='installed'\" "
            f"2>/dev/null"
        )
        installed_mods = int(mod_out.strip() or "0") if code == 0 else 0
        result["summary"]["installed_modules"] = installed_mods
        _info(f"Modulos instalados: {installed_mods}")

        if installed_mods == 0:
            _warn(
                "No se detectaron modulos instalados en ir_module_module. "
                "Odoo puede no arrancar correctamente."
            )
        elif installed_mods < 5:
            _warn(
                f"Solo {installed_mods} modulo(s) instalado(s). "
                "Una instancia funcional normalmente tiene decenas de modulos."
            )

        # ── 6. ir_attachment: expected filestore files ────────────────────
        # IMPORTANT: use COUNT(DISTINCT store_fname) not COUNT(*).
        # Odoo deduplicates file content by SHA1 hash — multiple ir_attachment
        # rows can point to the same physical file.  COUNT(*) would massively
        # over-count the expected files and produce misleading coverage ratios.
        code, att_out, _ = self._ssh.execute(
            f"sudo -u postgres psql -d {db_name} -t -c "
            f"\"SELECT count(DISTINCT store_fname) FROM ir_attachment "
            f"WHERE store_fname IS NOT NULL AND store_fname != ''\" 2>/dev/null"
        )
        db_attachments = int(att_out.strip() or "0") if code == 0 else 0
        result["summary"]["db_attachments"] = db_attachments
        _info(f"Archivos unicos esperados en filestore (ir_attachment DISTINCT): {db_attachments}")

        # ── 7. Filestore checks ───────────────────────────────────────────
        if filestore_path:

            # 7a. Directory existence
            code, _, _ = self._ssh.execute(f"test -d {filestore_path}")
            if code != 0:
                _err(
                    f"El directorio del filestore no existe: {filestore_path}\n"
                    "  La extraccion del ZIP pudo haber fallado silenciosamente."
                )
                # Cannot continue filestore checks without the directory
                result["summary"]["filestore_files"] = 0
                result["summary"]["zero_byte_files"] = 0
                result["summary"]["owner_ok"] = False
            else:
                # 7b. Total file count in filestore
                code, fc_out, _ = self._ssh.execute(
                    f"find {filestore_path} -type f 2>/dev/null | wc -l"
                )
                fs_files = int(fc_out.strip() or "0") if code == 0 else 0
                result["summary"]["filestore_files"] = fs_files
                _info(f"Archivos reales en filestore: {fs_files}")

                # 7c. Files with 0 bytes
                # A small number of 0-byte files is NORMAL in Odoo: the filestore
                # can contain empty placeholder files for attachments with no binary
                # content (e.g. links, URL-type attachments stored without data).
                # Only flag as problematic when the count is significant relative
                # to the total — threshold: more than 0.5% of all files OR > 500.
                code, zb_out, _ = self._ssh.execute(
                    f"find {filestore_path} -type f -size 0 2>/dev/null | wc -l"
                )
                zero_byte = int(zb_out.strip() or "0") if code == 0 else 0
                result["summary"]["zero_byte_files"] = zero_byte
                if zero_byte == 0:
                    _info("Sin archivos de 0 bytes en filestore. OK")
                else:
                    zero_pct = (zero_byte / fs_files * 100) if fs_files > 0 else 0
                    if zero_byte > 500 or zero_pct > 0.5:
                        # Potentially indicative of a corrupted or incomplete transfer
                        _warn(
                            f"{zero_byte} archivo(s) de 0 bytes en el filestore "
                            f"({zero_pct:.1f}% del total).\n"
                            "  Puede indicar una transferencia incompleta o ZIP corrupto.\n"
                            "  Los adjuntos afectados no se mostraran correctamente en Odoo."
                        )
                    else:
                        # Small count — likely legitimate Odoo placeholder files
                        _info(
                            f"{zero_byte} archivo(s) de 0 bytes en filestore "
                            f"({zero_pct:.2f}%). Normal en Odoo — adjuntos sin contenido binario. OK"
                        )

                # 7d. Coverage: ratio of real files vs. expected from DB
                # Only meaningful when the DB has at least some attachments.
                if db_attachments > 0 and fs_files > 0:
                    coverage = fs_files / db_attachments
                    result["summary"]["coverage_ratio"] = round(coverage, 3)
                    _info(
                        f"Cobertura filestore: {fs_files}/{db_attachments} "
                        f"= {coverage * 100:.1f}%"
                    )
                    if coverage < 0.50:
                        _err(
                            f"Cobertura del filestore critica: {coverage * 100:.1f}% "
                            f"({fs_files} archivos presentes de {db_attachments} esperados).\n"
                            "  Mas de la mitad de los adjuntos estan ausentes. "
                            "El filestore puede estar incompleto o apuntar a una ruta incorrecta."
                        )
                    elif coverage < 0.90:
                        _warn(
                            f"Cobertura del filestore: {coverage * 100:.1f}% "
                            f"({fs_files} de {db_attachments} archivos esperados).\n"
                            "  Algunos adjuntos o imagenes pueden no mostrarse correctamente."
                        )
                    else:
                        _info(f"Cobertura del filestore aceptable ({coverage * 100:.1f}%). OK")
                elif db_attachments > 0 and fs_files == 0:
                    _err(
                        f"La BD referencia {db_attachments} adjuntos en filestore "
                        f"pero el directorio esta vacio.\n"
                        "  El filestore no se copio correctamente."
                    )

                # 7e. Ownership check
                # Checks that the odoo user owns the directory (not root or others).
                code, own_out, _ = self._ssh.execute(
                    f"stat -c '%U' {filestore_path} 2>/dev/null"
                )
                dir_owner = own_out.strip() if code == 0 else "desconocido"
                owner_ok = dir_owner == odoo_user
                result["summary"]["owner_ok"] = owner_ok
                if owner_ok:
                    _info(f"Propietario del filestore: {dir_owner}. OK")
                else:
                    _warn(
                        f"El directorio del filestore pertenece a '{dir_owner}' "
                        f"en lugar de '{odoo_user}'.\n"
                        "  Odoo puede no tener permisos de lectura/escritura."
                    )

                # 7f. Read access test: verify odoo user can actually open a file
                code, sample_out, _ = self._ssh.execute(
                    f"find {filestore_path} -type f 2>/dev/null | head -1"
                )
                sample_file = sample_out.strip()
                if sample_file:
                    code, _, read_err = self._ssh.execute(
                        f"sudo -u {odoo_user} cat {sample_file} > /dev/null 2>&1"
                    )
                    if code != 0:
                        _err(
                            f"El usuario '{odoo_user}' no puede leer archivos del filestore.\n"
                            f"  Archivo de prueba: {sample_file}\n"
                            "  Ejecute: sudo chown -R odoo:odoo {filestore_path}"
                        )
                    else:
                        _info(f"Acceso de lectura como '{odoo_user}' al filestore. OK")

        # ── 8. Inventory comparison ───────────────────────────────────────
        # When a backup inventory is available, compare every metric collected
        # during the standalone checks against the known-good baseline.
        # This produces exact "X restored vs Y in backup" messages instead of
        # generic threshold warnings.
        if inventory:
            if log_callback:
                log_callback("Comparando contra inventario del backup ...")

            # Build the current-state dict using values already computed above
            current_db: dict = {
                "table_count":           table_count,
                "active_users":          active_users,
                "admin_exists":          admin_ok,
                "installed_modules":     installed_mods,
                "filestore_attachments": db_attachments,
            }

            # DB size — quick query, only needed for comparison
            code, sz_out, _ = self._ssh.execute(
                f"sudo -u postgres psql -d {db_name} -t -c "
                f"\"SELECT pg_database_size('{db_name}')\" 2>/dev/null"
            )
            if code == 0 and sz_out.strip().isdigit():
                current_db["size_bytes"] = int(sz_out.strip())

            # Row counts for the tables present in the baseline
            baseline_rows = inventory.get("database", {}).get("row_counts", {})
            current_rows: dict[str, int] = {}
            for table in baseline_rows:
                code, rc_out, _ = self._ssh.execute(
                    f"sudo -u postgres psql -d {db_name} -t -c "
                    f"\"SELECT count(*) FROM {table}\" 2>/dev/null"
                )
                if code == 0 and rc_out.strip().isdigit():
                    current_rows[table] = int(rc_out.strip())
            current_db["row_counts"] = current_rows

            # Filestore current state (re-use values from check 7b if available)
            current_fs: dict = {}
            if filestore_path and result["summary"].get("filestore_files") is not None:
                current_fs["total_files"] = result["summary"].get("filestore_files", 0)
                # Also grab directory count and total size for comparison
                code, dc_out, _ = self._ssh.execute(
                    f"find {filestore_path} -type d 2>/dev/null | wc -l"
                )
                current_fs["total_dirs"] = (
                    int(dc_out.strip() or "0") if code == 0 else 0
                )
                code, sz_out, _ = self._ssh.execute(
                    f"du -sb {filestore_path} 2>/dev/null | cut -f1"
                )
                if code == 0 and sz_out.strip().isdigit():
                    current_fs["total_size_bytes"] = int(sz_out.strip())

            # Run comparison and merge results
            cmp = InventoryManager.compare(
                baseline=inventory,
                current={"database": current_db, "filestore": current_fs},
            )

            for msg in cmp["errors"]:
                result["errors"].append(f"[vs backup] {msg}")
                result["ok"] = False
            for msg in cmp["warnings"]:
                result["warnings"].append(f"[vs backup] {msg}")
            for msg in cmp["matches"]:
                if log_callback:
                    log_callback(f"  ✓ {msg}")

            # Show baseline metadata so the user can confirm which backup was used
            meta = inventory.get("meta", {})
            if log_callback and meta:
                log_callback(
                    f"Inventario: backup del {meta.get('timestamp', '?')} "
                    f"desde {meta.get('source_host', '?')}"
                )

        # ── Final summary ─────────────────────────────────────────────────
        if log_callback:
            n_err  = len(result["errors"])
            n_warn = len(result["warnings"])
            status = "OK" if result["ok"] else f"{n_err} error(es) critico(s)"
            log_callback(
                f"Verificacion completada — {status}, {n_warn} aviso(s)."
            )

        return result

    # ── Cleanup ───────────────────────────────────────────────────────────

    def drop_database(self, db_name: str) -> None:
        """Drop a database (used on rollback after failed restore)."""
        self._ssh.execute(f"sudo -u postgres dropdb --if-exists {db_name}")

    def cleanup_upload(self, remote_path: str) -> None:
        """Remove a file uploaded to the server (best-effort)."""
        self._ssh.execute(f"rm -f {remote_path}")
