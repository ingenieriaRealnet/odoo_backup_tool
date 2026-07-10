"""
File transfer operations.

Supports two destinations:
  - Local: download from remote server to the machine running this tool.
  - Remote: server-to-server copy using sshpass + scp on the source server.
"""
from __future__ import annotations
import os
from typing import Callable
from .ssh_client import SSHClient


class TransferManager:
    """Transfers files from the source server to a local or remote destination."""

    def __init__(self, ssh: SSHClient) -> None:
        self._ssh = ssh

    # ── Helpers ───────────────────────────────────────────────────────────

    def get_remote_file_size(self, remote_path: str) -> int:
        """
        Return the byte size of a file on the remote server.

        Returns 0 when the file is not found or stat is unavailable,
        so callers can still show an indeterminate progress indicator.
        """
        code, out, _ = self._ssh.execute(
            f"stat -c %s {remote_path} 2>/dev/null"
        )
        return int(out.strip()) if code == 0 and out.strip().isdigit() else 0

    # ── Local download ────────────────────────────────────────────────────

    def local_file_exists(self, local_dir: str, filename: str) -> bool:
        """Return True if `filename` already exists inside `local_dir`."""
        return os.path.exists(os.path.join(local_dir, filename))

    def remote_file_exists(
        self,
        dest_host: str,
        dest_port: int,
        dest_user: str,
        dest_pass: str,
        dest_dir: str,
        filename: str,
    ) -> bool:
        """
        Return True if `filename` already exists in `dest_dir` on the
        destination server. Uses sshpass+ssh from the source server.
        """
        check_cmd = (
            f"sshpass -p '{dest_pass}' ssh -F /dev/null -p {dest_port} "
            f"-o StrictHostKeyChecking=no "
            f"{dest_user}@{dest_host} 'test -f {dest_dir}/{filename}'"
        )
        code, _, _ = self._ssh.execute(check_cmd, timeout=15)
        return code == 0

    def download_to_local(
        self,
        remote_path: str,
        local_dir: str,
        dest_filename: str | None = None,
        progress_callback: Callable[[int, int], None] | None = None,
        log_callback: Callable[[str], None] | None = None,
    ) -> str:
        """
        Download a single file from the remote server to a local directory.

        Args:
            remote_path: Absolute path on the remote server.
            local_dir: Local directory where the file will be saved.
            dest_filename: Override the saved filename (for rename on conflict).
            progress_callback: Called with (bytes_transferred, total_bytes).
            log_callback: Called with status messages.

        Returns:
            Absolute local path of the downloaded file.

        Raises:
            RuntimeError: On SFTP or OS errors.
        """
        filename = dest_filename or os.path.basename(remote_path)
        local_path = os.path.join(local_dir, filename)
        total_size = self.get_remote_file_size(remote_path)

        if log_callback:
            mb = total_size / (1024 * 1024) if total_size else 0
            log_callback(f"Descargando {filename} ({mb:.1f} MB) -> {local_path} ...")

        sftp = self._ssh.open_sftp()
        try:
            def _on_progress(transferred: int, total: int) -> None:
                if progress_callback:
                    progress_callback(transferred, total or total_size or 1)

            sftp.get(remote_path, local_path, callback=_on_progress)
        finally:
            sftp.close()

        if log_callback:
            log_callback(f"Descarga completa: {local_path}")

        return local_path

    # ── Server-to-server transfer ─────────────────────────────────────────

    def transfer_to_server(
        self,
        remote_path: str,
        dest_host: str,
        dest_port: int,
        dest_user: str,
        dest_pass: str,
        dest_dir: str,
        dest_filename: str | None = None,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        """
        Push a file from the source server to another server using scp.

        sshpass is used on the source server to provide the password
        non-interactively. If sshpass is not installed, the method
        attempts to install it automatically.

        Args:
            remote_path: File path on the source server.
            dest_host: Destination server hostname or IP.
            dest_port: Destination SSH port.
            dest_user: Destination SSH username.
            dest_pass: Destination SSH password.
            dest_dir: Directory on the destination server.
            log_callback: Called with status messages.

        Raises:
            RuntimeError: If the scp command fails.
        """
        filename = os.path.basename(remote_path)

        if log_callback:
            log_callback(f"Transfiriendo {filename} -> {dest_host}:{dest_dir} ...")

        # Ensure sshpass is available on the source server
        code, _, _ = self._ssh.execute("which sshpass")
        if code != 0:
            self._ssh.execute(
                "apt-get install -y sshpass 2>/dev/null || "
                "yum install -y sshpass 2>/dev/null"
            )

        # ── 1. Create destination directory ───────────────────────────────
        # scp cannot create missing parent directories and will fail with
        # "dest open: Failure" if the path does not exist.
        mkdir_cmd = (
            f"sshpass -p '{dest_pass}' ssh -F /dev/null -p {dest_port} "
            f"-o StrictHostKeyChecking=no "
            f"{dest_user}@{dest_host} 'mkdir -p {dest_dir}'"
        )
        code, _, err = self._ssh.execute(mkdir_cmd, timeout=30)
        if code != 0:
            raise RuntimeError(
                f"No se pudo crear el directorio {dest_dir} en {dest_host}:\n{err}"
            )

        # ── 2. Verify write permission ─────────────────────────────────────
        # The directory may exist but belong to a different user/group.
        perm_cmd = (
            f"sshpass -p '{dest_pass}' ssh -F /dev/null -p {dest_port} "
            f"-o StrictHostKeyChecking=no "
            f"{dest_user}@{dest_host} 'test -w {dest_dir} && echo writable'"
        )
        code, perm_out, _ = self._ssh.execute(perm_cmd, timeout=15)
        if code != 0 or "writable" not in perm_out:
            raise RuntimeError(
                f"Sin permiso de escritura en {dest_host}:{dest_dir} "
                f"para el usuario '{dest_user}'.\n\n"
                f"Posibles causas:\n"
                f"  • El directorio pertenece a otro usuario\n"
                f"  • Permisos del directorio no permiten escritura\n"
                f"Sugerencia: ejecute 'chmod 755 {dest_dir}' o "
                f"'chown {dest_user} {dest_dir}' en el servidor destino."
            )

        # ── 3. Check available disk space ─────────────────────────────────
        # Get source file size, then compare against free space on destination.
        # Prevents the cryptic "scp: write ... Failure" caused by a full disk.
        file_size = self.get_remote_file_size(remote_path)
        if file_size > 0:
            # df -B1 gives bytes; tail -1 skips the header; tr+cut extract column 4 (Avail)
            space_cmd = (
                f"sshpass -p '{dest_pass}' ssh -F /dev/null -p {dest_port} "
                f"-o StrictHostKeyChecking=no "
                f"{dest_user}@{dest_host} "
                f"'df -B1 {dest_dir} | tail -1 | tr -s \" \" | cut -d\" \" -f4'"
            )
            code, space_out, _ = self._ssh.execute(space_cmd, timeout=15)
            if code == 0 and space_out.strip().isdigit():
                avail_bytes = int(space_out.strip())
                need_mb  = file_size   / (1024 * 1024)
                avail_mb = avail_bytes / (1024 * 1024)

                if log_callback:
                    log_callback(
                        f"Espacio en destino: {avail_mb:,.1f} MB disponibles | "
                        f"Archivo: {need_mb:,.1f} MB"
                    )

                if avail_bytes < file_size:
                    raise RuntimeError(
                        f"Espacio insuficiente en {dest_host}:{dest_dir}\n\n"
                        f"  Disponible : {avail_mb:,.1f} MB\n"
                        f"  Necesario  : {need_mb:,.1f} MB\n\n"
                        "Libere espacio en el servidor destino e intente de nuevo."
                    )
            elif log_callback:
                log_callback(
                    f"[aviso] No se pudo verificar el espacio disponible en "
                    f"{dest_host}:{dest_dir} — procediendo de todas formas."
                )
        else:
            if log_callback:
                log_callback(f"Directorio {dest_dir} verificado en {dest_host}.")

        # ── 4. Transfer file via SCP ───────────────────────────────────────
        # NOTE: the password is passed via sshpass, never echoed to the log.
        dest_name = dest_filename or filename
        scp_cmd = (
            f"sshpass -p '{dest_pass}' scp -F /dev/null -P {dest_port} "
            f"-o StrictHostKeyChecking=no "
            f"{remote_path} {dest_user}@{dest_host}:{dest_dir}/{dest_name}"
        )
        code, _, err = self._ssh.execute(scp_cmd, timeout=3600)
        if code != 0:
            raise RuntimeError(
                f"Transferencia a {dest_host} fallo:\n{err}"
            )

        if log_callback:
            log_callback(
                f"Transferencia completa: {dest_user}@{dest_host}:{dest_dir}/{filename}"
            )
