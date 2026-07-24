"""
SSH connection manager.

Wraps paramiko to provide a clean interface for executing remote commands
and opening SFTP sessions. All state lives in one instance so callers
don't need to track the underlying paramiko client.
"""
import socket
import threading
import time
import uuid
from typing import Callable
import paramiko

# Inactivity timeout applied to the underlying socket after connecting: if a
# channel/SFTP read or write goes this long without any data, it raises
# socket.timeout instead of hanging forever. Mirrors the same fix applied to
# the Google Drive HTTP client (gdrive.py) — without it, a stalled transfer
# can block the worker thread indefinitely, and since gui/app.py._on_close()
# calls SSHClient.close() synchronously on the Tk main thread, that hang used
# to freeze the entire GUI ("No responde" in Task Manager) instead of the app
# actually closing.
_STALL_TIMEOUT_SECS = 180

# Upper bound on how long SSHClient.close() itself will wait for the
# underlying paramiko close to finish. See close() docstring below.
_CLOSE_WAIT_SECS = 5


class SSHClient:
    """Single SSH connection to a remote server."""

    def __init__(self):
        self._client: paramiko.SSHClient | None = None
        self.connected: bool = False
        self.host: str | None = None
        self.port: int | None = None

    # ── Connection lifecycle ─────────────────────────────────────────────

    def connect(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        timeout: int = 15,
    ) -> None:
        """
        Open an SSH connection.

        Args:
            host: IP or hostname of the remote server.
            port: SSH port.
            username: SSH login username.
            password: SSH login password.
            timeout: Socket timeout in seconds.

        Raises:
            ConnectionError: On authentication failure, SSH error, or network issue.
        """
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            client.connect(
                hostname=host,
                port=port,
                username=username,
                password=password,
                timeout=timeout,
                allow_agent=False,
                look_for_keys=False,
            )
            # Close any previous connection before replacing it
            if self._client:
                self._client.close()

            self._client = client
            self.connected = True
            self.host = host
            self.port = port

            transport = client.get_transport()
            if transport is not None:
                # SSH-level keepalive: detects a silently-dead connection
                # (peer stopped responding without a clean disconnect) and
                # closes the transport instead of leaving it looking alive.
                transport.set_keepalive(30)
                # Bounds any subsequent blocking socket read/write to
                # _STALL_TIMEOUT_SECS of inactivity — see module docstring.
                sock = transport.sock
                if sock is not None:
                    sock.settimeout(_STALL_TIMEOUT_SECS)

        except paramiko.AuthenticationException:
            raise ConnectionError("Autenticacion fallida: usuario o contrasena incorrectos.")
        except paramiko.SSHException as exc:
            raise ConnectionError(f"Error SSH: {exc}")
        except socket.timeout:
            raise ConnectionError(f"Tiempo de conexion agotado ({timeout}s).")
        except OSError as exc:
            raise ConnectionError(f"Error de red: {exc}")

    def close(self) -> None:
        """
        Close the SSH connection if open.

        Runs paramiko's own close() in a daemon thread and waits at most
        _CLOSE_WAIT_SECS for it — never blocks the caller indefinitely.
        paramiko's close() can itself wait on internal transport locks that
        a still-busy channel in another thread is holding; since this method
        is called synchronously from the Tk main thread on app shutdown
        (gui/app.py._on_close), a stuck close() used to freeze the entire
        GUI ("No responde" in Task Manager) instead of the window actually
        closing. This instance is considered closed to callers immediately
        regardless of whether the background close finished — the daemon
        thread is abandoned and reclaimed by the OS when the process exits.
        """
        client = self._client
        self._client = None
        self.connected = False
        self.host = None
        self.port = None
        if client is None:
            return
        t = threading.Thread(target=client.close, daemon=True)
        t.start()
        t.join(timeout=_CLOSE_WAIT_SECS)

    # ── Remote execution ─────────────────────────────────────────────────

    def execute(
        self, command: str, timeout: int = 300
    ) -> tuple[int, str, str]:
        """
        Run a command on the remote server.

        Args:
            command: Shell command string.
            timeout: Seconds to wait for the command to finish.

        Returns:
            (exit_code, stdout, stderr)

        Raises:
            RuntimeError: If not connected.
        """
        if not self.connected or self._client is None:
            raise RuntimeError("No hay conexion SSH activa.")

        _, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        try:
            exit_code = stdout.channel.recv_exit_status()
            out = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
            return exit_code, out, err
        finally:
            # paramiko's Transport keeps every exec_command channel registered
            # internally until this is called explicitly — it is NOT released
            # by Python garbage collection. execute_long() below calls execute()
            # multiple times per second (heartbeat + sentinel checks) for the
            # whole duration of a dump/filestore/upload job; without this close()
            # each of those calls permanently leaked one channel, so a long job
            # could accumulate past the remote sshd's MaxSessions limit (10 by
            # default) and the NEXT exec_command/exec_command-based call (e.g.
            # BundleManager.create()'s tar) would fail with paramiko's exact
            # "Unable to open channel" — and, on some servers, enough leaked
            # channels destabilize the transport enough to drop the whole
            # connection outright.
            stdout.channel.close()

    def execute_long(
        self,
        command: str,
        watch_cmd: str | None = None,
        heartbeat_callback: Callable[[str], None] | None = None,
        heartbeat_interval: int = 15,
        timeout: int = 7200,
        cancel_event=None,          # threading.Event | None
    ) -> tuple[int, str, str]:
        """
        Run a long-running command detached from the SSH session (nohup),
        then poll its completion every `heartbeat_interval` seconds.

        The command is wrapped in ``nohup ... &`` so it survives if the
        SSH connection drops or the tool is closed. Completion is detected
        by sentinel files. If ``cancel_event`` is set the remote process
        is killed and a CancelledError is raised.

        Args:
            command: Main shell command. Do NOT add nohup/& yourself.
            watch_cmd: Quick status command polled for heartbeat messages.
            heartbeat_callback: Receives the watch_cmd output as a string.
            heartbeat_interval: Seconds between heartbeat polls.
            timeout: Hard timeout in seconds for the whole operation.
            cancel_event: threading.Event; when set, the remote process is
                          killed and execution is aborted.

        Returns:
            (exit_code, stdout, stderr) of the wrapped command.

        Raises:
            RuntimeError: If not connected, timeout exceeded, or cancelled.
        """
        if not self.connected or self._client is None:
            raise RuntimeError("No hay conexion SSH activa.")

        # Unique per call: RemoteUploadManager (core/remote_upload_manager.py)
        # runs several execute_long() calls concurrently on the SAME SSHClient
        # (dump/filestore/inventory uploading in parallel via ThreadPoolExecutor
        # — see scheduler.py._run_rule). Fixed sentinel paths used to collide
        # across those concurrent calls: one call's "rm -f" at the start could
        # wipe another's in-flight sentinel, or a call could pick up the
        # sentinel left by a DIFFERENT command and report done/failed
        # prematurely. Confirmed 2026-07-22: the filestore upload was marked
        # complete before it actually finished (its rclone check then failed
        # with "file not in Google drive root") because a concurrent dump/
        # inventory upload's sentinel was mistaken for its own.
        call_id      = uuid.uuid4().hex[:8]
        sentinel_ok  = f"/tmp/.obt_done_ok_{call_id}"
        sentinel_err = f"/tmp/.obt_done_err_{call_id}"
        pid_file     = f"/tmp/.obt_pid_{call_id}"
        nohup_log    = f"/tmp/.obt_nohup_{call_id}.log"

        # Launch command detached; save its PID so we can kill it if needed
        wrapped = (
            f"nohup bash -c '{command} "
            f"&& touch {sentinel_ok} || touch {sentinel_err}' "
            f"> {nohup_log} 2>&1 & echo $! > {pid_file}"
        )
        self.execute(wrapped)

        # Poll until a sentinel appears, a timeout fires, or the user cancels
        elapsed = 0
        while True:
            time.sleep(1)
            elapsed += 1

            # ── User-requested cancellation ───────────────────────────────
            if cancel_event is not None and cancel_event.is_set():
                _, pid_out, _ = self.execute(f"cat {pid_file} 2>/dev/null")
                pid = pid_out.strip()
                if pid.isdigit():
                    # Kill the entire process group so child processes
                    # (pg_dump, zip, pg_restore, etc.) are also terminated.
                    #
                    # Strategy: use PGID-based kill (POSIX, works on all Linux
                    # distros including Alpine/BusyBox that lack pkill/procps).
                    # Sending a negative PID to kill() targets the whole group.
                    # pkill -P is used as an additional safety net on systems
                    # that have it, but the group kill is the primary mechanism.
                    #
                    # Sequence:
                    #   1. Get PGID of the bash wrapper via ps
                    #   2. SIGTERM  → whole process group (wrapper + children)
                    #   3. 2s wait  → allow clean shutdown
                    #   4. SIGKILL  → whole process group (force if still alive)
                    self.execute(
                        # Get the process group ID (PGID) — POSIX, available everywhere
                        f"_PGID=$(ps -o pgid= -p {pid} 2>/dev/null | tr -d ' '); "
                        # Kill entire group with SIGTERM (negative PGID = group signal)
                        f"[ -n \"$_PGID\" ] && [ \"$_PGID\" != '0' ] && "
                        f"  kill -TERM -- -$_PGID 2>/dev/null || true; "
                        # Also try pkill -P as safety net (where available)
                        f"pkill -TERM -P {pid} 2>/dev/null || true; "
                        f"kill -TERM {pid} 2>/dev/null || true; "
                        f"sleep 2; "
                        # Force kill anything still alive
                        f"[ -n \"$_PGID\" ] && [ \"$_PGID\" != '0' ] && "
                        f"  kill -KILL -- -$_PGID 2>/dev/null || true; "
                        f"pkill -KILL -P {pid} 2>/dev/null || true; "
                        f"kill -KILL {pid} 2>/dev/null || true"
                    )
                self.execute(f"rm -f {sentinel_ok} {sentinel_err} {pid_file} {nohup_log}")
                raise RuntimeError("__CANCELLED__")

            # ── Heartbeat ─────────────────────────────────────────────────
            if elapsed % heartbeat_interval == 0 and watch_cmd and heartbeat_callback:
                try:
                    _, w_out, _ = self._client.exec_command(watch_cmd)
                    try:
                        msg = w_out.read().decode("utf-8", errors="replace").strip()
                        heartbeat_callback(msg if msg else "... en proceso ...")
                    finally:
                        # See execute()'s finally block: exec_command channels
                        # are never released by paramiko unless closed here.
                        w_out.channel.close()
                except Exception:
                    heartbeat_callback("... en proceso ...")

            # ── Timeout guard ─────────────────────────────────────────────
            if elapsed >= timeout:
                # Only clears our own tracking files (tiny, harmless if
                # left behind) — deliberately does NOT kill the remote
                # process: like pg_dump/tar, it's running detached via
                # nohup and may legitimately still finish on its own.
                self.execute(f"rm -f {sentinel_ok} {sentinel_err} {pid_file} {nohup_log}")
                raise RuntimeError(
                    f"Timeout: el comando supero {timeout}s sin terminar."
                )

            # ── Completion check ──────────────────────────────────────────
            ok_code,  _, _ = self.execute(f"test -f {sentinel_ok}")
            err_code, _, _ = self.execute(f"test -f {sentinel_err}")

            if ok_code == 0:
                self.execute(f"rm -f {sentinel_ok} {sentinel_err} {pid_file} {nohup_log}")
                return 0, "", ""

            if err_code == 0:
                _, log_content, _ = self.execute(f"cat {nohup_log} 2>/dev/null")
                self.execute(f"rm -f {sentinel_ok} {sentinel_err} {pid_file} {nohup_log}")
                return 1, "", log_content

    # ── SFTP ─────────────────────────────────────────────────────────────

    def open_sftp(self) -> paramiko.SFTPClient:
        """
        Open an SFTP session on the current connection.

        Returns:
            An active paramiko SFTPClient.

        Raises:
            RuntimeError: If not connected.
        """
        if not self.connected or self._client is None:
            raise RuntimeError("No hay conexion SSH activa.")
        return self._client.open_sftp()

    def __del__(self):
        self.close()
