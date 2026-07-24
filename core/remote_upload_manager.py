"""
Direct server-to-Drive upload via rclone, orchestrated over SSH.

Replaces the relay path (server -> this machine -> Drive, see gdrive.py)
for servers that qualify: uses the origin server's own uplink instead of
this machine's, eliminating the shared-bandwidth bottleneck confirmed in
the 2026-07-21 incident (see docs/diseno_upload_directo_drive.md).

Design constraints validated in the manual pilot (novalens, 2026-07-21),
both learned the hard way during that pilot:
  - Cleanup (local /tmp AND Drive-side) must only run after confirming the
    exit code of the specific upload it belongs to — never unconditionally.
  - Any Drive-side cleanup must target a specific file id, never a
    folder/remote-scoped command (a folder-scoped `rclone delete` briefly
    wiped novalens's real backups during the pilot).
  - `rclone copy` takes exactly one source file per call — never batch
    multiple artifacts into a single invocation.
"""
from __future__ import annotations
import io
import time
import uuid
from typing import Callable
from .ssh_client import SSHClient
from .temp_registry import RemoteTempRegistry


class RemoteUploadManager:
    """Orchestrates a direct server->Drive upload of one file via rclone."""

    def __init__(
        self,
        ssh: SSHClient,
        user: str,
        password: str,
        temp_registry: RemoteTempRegistry | None = None,
    ) -> None:
        # SSHClient itself doesn't retain user/password (see ssh_client.py) —
        # needed here only to register the ephemeral credential file in
        # RemoteTempRegistry, which must be able to reconnect independently
        # during the orphan sweep if this process crashes mid-upload.
        self._ssh = ssh
        self._user = user
        self._password = password
        self._temp_registry = temp_registry or RemoteTempRegistry()

    # ── Viability ────────────────────────────────────────────────────────

    def check_viable(self) -> tuple[bool, str]:
        """
        Return (True, "") if this server can upload directly to Drive.

        Checks (in order, cheapest first): outbound HTTPS to Google, and
        rclone availability (installing it if missing). A server that
        fails either check should fall back to the relay path (gdrive.py)
        instead of being forced into direct mode.
        """
        code, out, _ = self._ssh.execute(
            "curl -sS --max-time 10 -o /dev/null -w '%{http_code}' "
            "https://www.googleapis.com 2>&1"
        )
        if code != 0 or not out.strip().isdigit():
            return False, f"Sin salida HTTPS a Google desde el servidor ({out or 'sin respuesta'})."

        ok, msg = self._ensure_rclone()
        if not ok:
            return False, msg

        return True, ""

    def _ensure_rclone(self) -> tuple[bool, str]:
        """Install rclone if missing. Same auto-install pattern already used
        for `zip` in filestore_manager.py.compress_filestore()."""
        code, _, _ = self._ssh.execute("which rclone")
        if code == 0:
            return True, ""

        code, _, err = self._ssh.execute(
            "curl https://rclone.org/install.sh 2>/dev/null | sudo bash 2>&1",
            timeout=120,
        )
        if code != 0:
            # No-sudo fallback: user-local install, no root required.
            code, _, err = self._ssh.execute(
                "curl https://rclone.org/install.sh 2>/dev/null | "
                "bash -s -- --update --beta 2>&1 || true; "
                "mkdir -p ~/bin && "
                "curl -sSL https://downloads.rclone.org/rclone-current-linux-amd64.zip "
                "-o /tmp/.rclone_install.zip 2>&1 && "
                "cd /tmp && unzip -o .rclone_install.zip >/dev/null && "
                "cp rclone-*-linux-amd64/rclone ~/bin/ && chmod +x ~/bin/rclone && "
                "export PATH=$HOME/bin:$PATH && which rclone",
                timeout=120,
            )
            if code != 0:
                return False, f"No se pudo instalar rclone (con o sin sudo): {err}"

        return True, ""

    # ── Credential lifecycle ────────────────────────────────────────────

    def _deploy_credential(self, local_sa_path: str, folder_id: str, label: str) -> tuple[str, str, str]:
        """
        Transfer the Service Account JSON + an isolated rclone config to the
        server. Returns (remote_sa_path, remote_conf_path, registry_entry_id).

        Ephemeral by design: the caller MUST call _cleanup_credential() in a
        finally block once the upload attempt (success or failure) is done.
        Registered in RemoteTempRegistry so a crash mid-upload doesn't leave
        the credential on the server indefinitely — the orphan sweep at app
        startup will remove it.
        """
        ts = time.strftime("%Y%m%d_%H%M%S")
        uid = uuid.uuid4().hex[:8]
        remote_sa_path = f"/tmp/.rclone_sa_{uid}_{ts}.json"
        remote_conf_path = f"/tmp/.rclone_cfg_{uid}_{ts}.conf"

        sftp = self._ssh.open_sftp()
        try:
            sftp.put(local_sa_path, remote_sa_path)
            sftp.chmod(remote_sa_path, 0o600)
            conf = (
                "[obt_drive]\n"
                "type = drive\n"
                f"service_account_file = {remote_sa_path}\n"
                f"root_folder_id = {folder_id}\n"
            )
            sftp.putfo(io.BytesIO(conf.encode()), remote_conf_path)
            sftp.chmod(remote_conf_path, 0o600)
        finally:
            sftp.close()

        entry_id = self._temp_registry.register(
            self._ssh.host, self._ssh.port, self._user, self._password,
            remote_sa_path, kind="drive_credential", label=label,
        )
        return remote_sa_path, remote_conf_path, entry_id

    def _cleanup_credential(self, remote_sa_path: str, remote_conf_path: str, entry_id: str) -> None:
        """Best-effort removal of the ephemeral credential — always runs,
        regardless of upload outcome. Local /tmp files only, never Drive."""
        try:
            self._ssh.execute(f"rm -f {remote_sa_path} {remote_conf_path}")
        finally:
            self._temp_registry.unregister(entry_id)

    # ── Upload ───────────────────────────────────────────────────────────

    # Floor throughput assumed when sizing the upload timeout — deliberately
    # conservative. Confirmed 2026-07-22: a fixed 3600s (1h) timeout killed
    # variedades' filestore upload (14.6 GB) at 72% after 72 real minutes of
    # healthy, uninterrupted progress at ~3.3-3.6 MiB/s — that server's own
    # uplink is just slower than bancasa/equiredes/mega's (20+ MiB/s), not
    # stalled. Same failure class as the _StallWatchdog incident: a rigid
    # timeout killing a slow-but-working transfer instead of a genuinely
    # dead one. Sized per-file instead of a flat constant.
    _MIN_ASSUMED_THROUGHPUT_BPS = 1 * 1024 * 1024  # 1 MiB/s
    _MIN_UPLOAD_TIMEOUT_SECS = 3600   # floor: small files still get 1h
    _MAX_UPLOAD_TIMEOUT_SECS = 21600  # ceiling: 6h, so a truly dead rclone
                                       # process doesn't hang unnoticed forever

    def _upload_timeout(self, remote_path: str) -> int:
        """
        Timeout for a single file's rclone upload, sized to its actual size
        instead of a flat constant — see _MIN_ASSUMED_THROUGHPUT_BPS above.
        Falls back to _MIN_UPLOAD_TIMEOUT_SECS if the size can't be read.
        """
        code, out, _ = self._ssh.execute(f"stat -c %s {remote_path} 2>/dev/null")
        if code != 0 or not out.strip().isdigit():
            return self._MIN_UPLOAD_TIMEOUT_SECS
        size_bytes = int(out.strip())
        # 25% margin over the floor-throughput estimate.
        estimated = int(size_bytes / self._MIN_ASSUMED_THROUGHPUT_BPS * 1.25)
        return max(self._MIN_UPLOAD_TIMEOUT_SECS, min(self._MAX_UPLOAD_TIMEOUT_SECS, estimated))

    def upload_file(
        self,
        remote_path: str,
        local_sa_path: str,
        folder_id: str,
        label: str = "",
        log_callback: Callable[[str], None] | None = None,
        cancel_event=None,
    ) -> None:
        """
        Upload a single file already sitting on this SSH server directly to
        Drive via rclone, using an ephemeral credential.

        Raises RuntimeError on failure (viability, transfer, or verification).
        The caller decides what "failure" means for cleanup of remote_path
        itself (dump/tar) — this method never deletes the source file, only
        its own ephemeral credential.
        """
        filename = remote_path.rsplit("/", 1)[-1]
        remote_sa_path, remote_conf_path, entry_id = self._deploy_credential(
            local_sa_path, folder_id, label
        )
        try:
            log_file = f"/tmp/.rclone_log_{uuid.uuid4().hex[:8]}.txt"
            cmd = (
                f"rclone --config {remote_conf_path} copy {remote_path} obt_drive: "
                f"--checksum --stats=15s --stats-one-line --log-file={log_file} "
                f"--log-level INFO"
            )

            def _heartbeat(_status: str) -> None:
                if not log_callback:
                    return
                code, out, _ = self._ssh.execute(f"tail -n 1 {log_file} 2>/dev/null")
                if out:
                    log_callback(f"  [rclone] {out}")

            if log_callback:
                log_callback(f"Enviando a Drive (directo desde el servidor): {filename} ...")

            code, _, err = self._ssh.execute_long(
                cmd,
                watch_cmd=f"tail -n 1 {log_file} 2>/dev/null",
                heartbeat_callback=_heartbeat,
                timeout=self._upload_timeout(remote_path),
                cancel_event=cancel_event,
            )
            if code != 0:
                raise RuntimeError(f"rclone fallo subiendo {filename}: {err}")

            # Verify by file-id-targeted check, not a folder-scoped command.
            # Retried a few times with a short delay: `rclone copy` reporting
            # success only means the upload API call returned, not that the
            # file is immediately visible via Drive's files.list — the same
            # search-index propagation lag documented for the relay path
            # (_fetch_uploaded_file_meta). Confirmed 2026-07-23: bancasa's
            # dump was verifiably in Drive (real createdTime) just 5s before
            # `rclone check` reported "file not in Google drive root" —  not
            # a real integrity failure, just checked too soon after upload.
            out = err = ""
            verified = False
            for attempt in range(1, 4):
                code, out, err = self._ssh.execute(
                    f"rclone --config {remote_conf_path} check {remote_path} obt_drive: "
                    f"--one-way 2>&1",
                    timeout=120,
                )
                if "0 differences found" in out or "0 differences found" in err:
                    verified = True
                    break
                if attempt < 3:
                    if log_callback:
                        log_callback(
                            f"  [Drive] Verificacion no encontro el archivo aun "
                            f"(posible latencia de indexacion) — reintento {attempt}/3 en 5s ..."
                        )
                    time.sleep(5)
            if not verified:
                raise RuntimeError(
                    f"Verificacion de integridad fallo para {filename}: {out or err}"
                )
            if log_callback:
                log_callback(f"Subida directa completa y verificada: {filename}")

            self._ssh.execute(f"rm -f {log_file}")
        finally:
            self._cleanup_credential(remote_sa_path, remote_conf_path, entry_id)
