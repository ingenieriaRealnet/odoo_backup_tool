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

    def open_remote_file(self, remote_path: str):
        """
        Open a remote file via SFTP for streaming reads without a local copy.

        Returns (sftp_session, sftp_file). The caller must close both when done.
        prefetch() is called automatically to enable Paramiko read-ahead pipelining
        for better streaming throughput over high-latency connections.

        Typical usage:
            sftp_session, sftp_file = transfer.open_remote_file(remote_path)
            try:
                ... consume sftp_file.read() ...
            finally:
                sftp_file.close()
                sftp_session.close()
        """
        sftp = self._ssh.open_sftp()
        sftp_file = sftp.open(remote_path, "rb")
        # Enable read-ahead pipelining for better streaming throughput.
        # Called without arguments for compatibility with all Paramiko versions
        # (older builds don't accept the size kwarg).
        sftp_file.prefetch()
        return sftp, sftp_file

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
        Push a file from the source server to another server.

        Uses rsync when available (preferred): supports partial-file resume
        (--partial) so an interrupted transfer can continue from where it left
        off instead of restarting from byte 0.  Falls back to scp transparently
        if rsync is not installed on the source server.

        Security: the password is passed via the SSHPASS environment variable
        (sshpass -e) rather than as a command-line argument (-p), so it does
        not appear in `ps aux` output on the source server.

        Args:
            remote_path: File path on the source server.
            dest_host: Destination server hostname or IP.
            dest_port: Destination SSH port.
            dest_user: Destination SSH username.
            dest_pass: Destination SSH password.
            dest_dir: Directory on the destination server.
            dest_filename: Override the saved filename (for rename on conflict).
            log_callback: Called with status messages.

        Raises:
            RuntimeError: If the transfer command fails.
        """
        filename  = os.path.basename(remote_path)
        dest_name = dest_filename or filename

        if log_callback:
            log_callback(f"Transfiriendo {filename} -> {dest_host}:{dest_dir} ...")

        # ── 1. Ensure sshpass is available ────────────────────────────────
        code, _, _ = self._ssh.execute("which sshpass")
        if code != 0:
            self._ssh.execute(
                "apt-get install -y sshpass 2>/dev/null || "
                "yum install -y sshpass 2>/dev/null"
            )

        # Common SSH options reused by both rsync and scp
        ssh_opts = f"-F /dev/null -p {dest_port} -o StrictHostKeyChecking=no"

        # ── 2. Create destination directory ───────────────────────────────
        mkdir_cmd = (
            f"SSHPASS='{dest_pass}' sshpass -e "
            f"ssh {ssh_opts} {dest_user}@{dest_host} 'mkdir -p {dest_dir}'"
        )
        code, _, err = self._ssh.execute(mkdir_cmd, timeout=30)
        if code != 0:
            raise RuntimeError(
                f"No se pudo crear el directorio {dest_dir} en {dest_host}:\n{err}"
            )

        # ── 3. Verify write permission ─────────────────────────────────────
        perm_cmd = (
            f"SSHPASS='{dest_pass}' sshpass -e "
            f"ssh {ssh_opts} {dest_user}@{dest_host} "
            f"'test -w {dest_dir} && echo writable'"
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

        # ── 4. Check available disk space ─────────────────────────────────
        file_size = self.get_remote_file_size(remote_path)
        if file_size > 0:
            space_cmd = (
                f"SSHPASS='{dest_pass}' sshpass -e "
                f"ssh {ssh_opts} {dest_user}@{dest_host} "
                f"'df -B1 {dest_dir} | tail -1 | tr -s \" \" | cut -d\" \" -f4'"
            )
            code, space_out, _ = self._ssh.execute(space_cmd, timeout=15)
            if code == 0 and space_out.strip().isdigit():
                avail_bytes = int(space_out.strip())
                need_mb     = file_size   / (1024 * 1024)
                avail_mb    = avail_bytes / (1024 * 1024)
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
                    f"[aviso] No se pudo verificar espacio disponible en "
                    f"{dest_host}:{dest_dir} — procediendo de todas formas."
                )
        elif log_callback:
            log_callback(f"Directorio {dest_dir} verificado en {dest_host}.")

        # ── 5. Transfer — rsync preferred, scp fallback ───────────────────
        # rsync --partial keeps the incomplete file on the destination so a
        # retry can resume from the last byte received, avoiding full re-sends.
        # -a: archive mode (preserve timestamps/permissions).
        # NOTE: SSHPASS env var keeps the password out of `ps aux` argv.
        use_rsync = self._ssh.execute("which rsync")[0] == 0

        if use_rsync:
            if log_callback:
                log_callback(f"  Usando rsync (modo: reanudable) ...")
            transfer_cmd = (
                f"SSHPASS='{dest_pass}' sshpass -e "
                f"rsync -a --partial --timeout=300 "
                f"-e 'ssh {ssh_opts}' "
                f"{remote_path} {dest_user}@{dest_host}:{dest_dir}/{dest_name}"
            )
        else:
            if log_callback:
                log_callback(f"  rsync no disponible — usando scp ...")
            transfer_cmd = (
                f"SSHPASS='{dest_pass}' sshpass -e "
                f"scp {ssh_opts.replace('-p ', '-P ')} "
                f"{remote_path} {dest_user}@{dest_host}:{dest_dir}/{dest_name}"
            )

        code, _, err = self._ssh.execute(transfer_cmd, timeout=3600)
        if code != 0:
            raise RuntimeError(
                f"Transferencia a {dest_host} fallo:\n{err}"
            )

        if log_callback:
            method = "rsync" if use_rsync else "scp"
            log_callback(
                f"Transferencia completa ({method}): "
                f"{dest_user}@{dest_host}:{dest_dir}/{dest_name}"
            )
