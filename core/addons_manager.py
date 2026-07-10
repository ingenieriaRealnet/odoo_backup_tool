"""
Custom addons synchronization from GitHub / GitLab via SSH key.

Three key-source modes
----------------------
server    The remote Odoo server already has a deploy key registered with
          GitHub/GitLab (e.g. /home/odoo/.ssh/id_ed25519).  The tool only
          writes a thin GIT_SSH wrapper script on the server and deletes it
          afterwards.  No key material is transferred.  This is the preferred
          and most common production setup.

local     A private key on THIS machine is registered with GitHub/GitLab.
          The tool loads it with paramiko (decrypting any passphrase in
          memory), exports a passphrase-free copy, uploads it to
          /tmp/.obt_gh_key on the server, runs git, then deletes it.

generate  A new Ed25519 key pair is generated on THIS machine and saved to
          ~/.ssh/.  The public key is shown for the user to register in
          GitHub/GitLab.  After registration the key becomes available as a
          "local" key for subsequent syncs.

Security notes
--------------
- Passphrases are only used locally by paramiko.  They are never written
  to disk or sent to the server.
- Keys uploaded in "local" mode are deleted in a try/finally block —
  even on failure or cancellation.
- git always runs with  -o BatchMode=yes  so it never prompts interactively.
"""
from __future__ import annotations

import io
import os
import socket
from typing import Callable

import paramiko

from .ssh_client import SSHClient


# Paths used on the DESTINATION server — always cleaned up after use
_REMOTE_KEY_PATH    = "/tmp/.obt_gh_key"
_REMOTE_SSH_WRAPPER = "/tmp/.obt_git_ssh.sh"

# Known private-key PEM headers used to identify files
_KEY_HEADERS = (
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
    "-----BEGIN DSA PRIVATE KEY-----",
)
_SKIP_NAMES = {"known_hosts", "known_hosts.old", "config", "authorized_keys"}


# ---------------------------------------------------------------------------
# Local key discovery and passphrase detection
# ---------------------------------------------------------------------------

def generate_local_key(name: str = "odoo_github_key") -> tuple[str, str]:
    """
    Generate a new Ed25519 SSH key pair on the local machine.

    The private key is saved to ~/.ssh/<name> (mode 0600).
    The public  key is saved to ~/.ssh/<name>.pub.

    Args:
        name: Filename for the key (no path, no extension).

    Returns:
        (private_key_path, public_key_string)
        The public_key_string is suitable for pasting into GitHub / GitLab
        as a deploy key or user SSH key.

    Raises:
        FileExistsError: A key with this name already exists.
    """
    ssh_dir = os.path.expanduser("~/.ssh")
    os.makedirs(ssh_dir, exist_ok=True)

    private_path = os.path.join(ssh_dir, name)
    public_path  = private_path + ".pub"

    if os.path.exists(private_path):
        raise FileExistsError(
            f"Ya existe una llave con ese nombre: {private_path}\n"
            "Elija otro nombre o elimine la existente."
        )

    key = paramiko.Ed25519Key.generate()

    # Write private key (no passphrase — user manages their local keys)
    buf = io.StringIO()
    key.write_private_key(buf)
    with open(private_path, "w", encoding="utf-8") as fh:
        fh.write(buf.getvalue())
    try:
        os.chmod(private_path, 0o600)
    except OSError:
        pass  # Windows — permissions handled differently

    # Build public key string in OpenSSH authorized_keys format
    hostname = socket.gethostname()
    pub_str = f"ssh-ed25519 {key.get_base64()} odoo-backup-tool@{hostname}"
    with open(public_path, "w", encoding="utf-8") as fh:
        fh.write(pub_str + "\n")

    return private_path, pub_str


def scan_ssh_directory() -> list[str]:
    """
    Scan ~/.ssh/ and return paths of SSH private key files found there.

    A file is considered a private key if it:
      - Does not end in .pub
      - Is not a known non-key file (known_hosts, config, authorized_keys)
      - Starts with a recognized PEM or OpenSSH private-key header

    Returns a list of absolute paths, sorted alphabetically.
    """
    ssh_dir = os.path.expanduser("~/.ssh")
    if not os.path.isdir(ssh_dir):
        return []

    _SKIP_NAMES = {"known_hosts", "known_hosts.old", "config", "authorized_keys"}
    _KEY_HEADERS = (
        b"-----BEGIN OPENSSH PRIVATE KEY-----",
        b"-----BEGIN RSA PRIVATE KEY-----",
        b"-----BEGIN EC PRIVATE KEY-----",
        b"-----BEGIN DSA PRIVATE KEY-----",
    )

    found: list[str] = []
    for name in sorted(os.listdir(ssh_dir)):
        # Skip obvious non-key files
        if name.endswith(".pub") or name in _SKIP_NAMES:
            continue
        path = os.path.join(ssh_dir, name)
        if not os.path.isfile(path):
            continue
        # Peek at the first line to confirm it's a private key
        try:
            with open(path, "rb") as fh:
                header = fh.read(50)
            if any(header.startswith(h) for h in _KEY_HEADERS):
                found.append(path)
        except OSError:
            continue

    return found


def key_needs_passphrase(key_path: str) -> bool:
    """
    Return True if the private key at key_path requires a passphrase.

    Tries loading with an empty passphrase; if paramiko raises
    PasswordRequiredException the key is protected.
    """
    try:
        _load_private_key(key_path, passphrase=None)
        return False
    except paramiko.ssh_exception.PasswordRequiredException:
        return True
    except Exception:
        # Unknown format or corrupt file — treat as unprotected and let
        # the real load attempt surface a proper error
        return False


# ---------------------------------------------------------------------------
# Internal key loader
# ---------------------------------------------------------------------------

def _load_private_key(
    key_path: str,
    passphrase: str | None,
) -> paramiko.PKey:
    """
    Load a private key from disk using the appropriate paramiko class.

    Detects key type from the PEM header, tries each relevant class in
    order, and returns the loaded key object.

    Args:
        key_path:   Absolute path to the private key file.
        passphrase: Passphrase string, or None for unprotected keys.

    Returns:
        A loaded paramiko PKey subclass instance.

    Raises:
        PasswordRequiredException: Key is encrypted and no passphrase given.
        AuthenticationException:   Wrong passphrase.
        ValueError:                Unknown or unsupported key format.
    """
    pw = passphrase.encode() if isinstance(passphrase, str) else passphrase

    # Read header to pick the right class first (avoids redundant attempts)
    with open(key_path, "rb") as fh:
        header = fh.read(60).decode("ascii", errors="ignore")

    # Try classes in order of likelihood
    loaders: list[type[paramiko.PKey]] = []
    if "OPENSSH" in header or "ED25519" in header.upper():
        loaders = [
            paramiko.Ed25519Key,
            paramiko.RSAKey,
            paramiko.ECDSAKey,
            paramiko.DSSKey,
        ]
    elif "RSA" in header:
        loaders = [paramiko.RSAKey, paramiko.ECDSAKey, paramiko.Ed25519Key]
    elif "EC" in header:
        loaders = [paramiko.ECDSAKey, paramiko.RSAKey, paramiko.Ed25519Key]
    elif "DSA" in header or "DSS" in header:
        loaders = [paramiko.DSSKey, paramiko.RSAKey]
    else:
        loaders = [
            paramiko.Ed25519Key,
            paramiko.RSAKey,
            paramiko.ECDSAKey,
            paramiko.DSSKey,
        ]

    last_exc: Exception = ValueError("Formato de llave SSH no reconocido.")
    for cls in loaders:
        try:
            return cls.from_private_key_file(key_path, password=pw)
        except paramiko.ssh_exception.PasswordRequiredException:
            raise   # passphrase required — bubble up immediately
        except paramiko.ssh_exception.SSHException:
            last_exc = ValueError(
                "Passphrase incorrecta o formato de llave no soportado."
            )
        except Exception as exc:
            last_exc = exc

    raise last_exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class AddonsManager:
    """Synchronizes custom Odoo addons from a git repository on a remote server."""

    def __init__(self, ssh: SSHClient) -> None:
        self._ssh = ssh

    # ── Pre-flight ────────────────────────────────────────────────────────

    def ensure_git(
        self, log_callback: Callable[[str], None] | None = None
    ) -> None:
        """
        Verify git is installed on the destination server; auto-install if not.

        Raises:
            RuntimeError: If git is unavailable after the install attempt.
        """
        code, _, _ = self._ssh.execute("which git >/dev/null 2>&1")
        if code == 0:
            return

        if log_callback:
            log_callback("'git' no encontrado — instalando automaticamente...")

        self._ssh.execute(
            "apt-get install -y git 2>/dev/null || "
            "yum install -y git 2>/dev/null || "
            "apk add --no-cache git 2>/dev/null"
        )

        code, _, _ = self._ssh.execute("which git >/dev/null 2>&1")
        if code != 0:
            raise RuntimeError(
                "No se pudo instalar 'git' en el servidor destino.\n\n"
                "Instale manualmente:\n"
                "  Debian/Ubuntu : apt-get install git\n"
                "  RHEL/CentOS   : yum install git\n"
                "  Alpine        : apk add git"
            )
        if log_callback:
            log_callback("'git' instalado correctamente.")

    # ── SSH key upload ────────────────────────────────────────────────────

    def upload_key(
        self,
        local_key_path: str,
        passphrase: str | None = None,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        """
        Load a private key (decrypting with passphrase if needed), export it
        WITHOUT passphrase to an in-memory buffer, and upload that buffer to
        the destination server.

        The server receives an unprotected copy so git can use it in
        BatchMode=yes without any interactive prompt.  The file is removed
        by cleanup_key() immediately after the git operation.

        Args:
            local_key_path: Absolute path to the SSH private key on THIS machine.
            passphrase:     Key passphrase, or None for unprotected keys.

        Raises:
            PasswordRequiredException: Key is encrypted but no passphrase given.
            ValueError:               Wrong passphrase or unsupported key format.
        """
        if log_callback:
            log_callback(
                f"Cargando llave SSH: {os.path.basename(local_key_path)} ..."
            )

        # Load and decrypt in memory — passphrase never leaves this process
        key = _load_private_key(local_key_path, passphrase)

        # Export without passphrase to a string buffer
        buf = io.StringIO()
        key.write_private_key(buf)
        key_bytes = buf.getvalue().encode("utf-8")

        if log_callback:
            log_callback("Subiendo llave SSH temporal al servidor...")

        sftp = self._ssh.open_sftp()
        try:
            # Upload the passphrase-free key content
            with sftp.open(_REMOTE_KEY_PATH, "wb") as fh:
                fh.write(key_bytes)

            # Write the git-ssh wrapper that points to the temp key.
            # Using a wrapper avoids embedding options inside GIT_SSH_COMMAND
            # which would require quoting incompatible with execute_long.
            wrapper = (
                "#!/bin/sh\n"
                f"exec ssh -i {_REMOTE_KEY_PATH} "
                "-o StrictHostKeyChecking=no "
                "-o UserKnownHostsFile=/dev/null "
                "-o BatchMode=yes "
                '"$@"\n'
            ).encode("utf-8")
            with sftp.open(_REMOTE_SSH_WRAPPER, "wb") as fh:
                fh.write(wrapper)
        finally:
            sftp.close()

        # SSH refuses keys with overly-permissive permissions
        self._ssh.execute(
            f"chmod 600 {_REMOTE_KEY_PATH} && chmod +x {_REMOTE_SSH_WRAPPER}"
        )

    # ── Server-side key discovery ─────────────────────────────────────────

    def scan_server_ssh_keys(self) -> list[str]:
        """
        List private SSH key files in ~/.ssh/ of the DESTINATION server.

        Reads the first 60 bytes of each candidate file via SFTP and checks
        for a recognised PEM private-key header.  Public keys (.pub), config
        and known_hosts files are skipped.

        Returns:
            Sorted list of absolute paths on the remote server, or [] on error.
        """
        try:
            sftp = self._ssh.open_sftp()
        except Exception:
            return []

        try:
            # Expand ~ relative to the SFTP root (home directory)
            home = sftp.normalize(".")
            ssh_remote = f"{home}/.ssh"

            try:
                entries = sftp.listdir_attr(ssh_remote)
            except FileNotFoundError:
                return []

            found: list[str] = []
            for entry in sorted(entries, key=lambda e: e.filename):
                fname = entry.filename
                if fname.endswith(".pub") or fname in _SKIP_NAMES:
                    continue
                full_path = f"{ssh_remote}/{fname}"
                try:
                    with sftp.open(full_path, "rb") as fh:
                        header = fh.read(60).decode("ascii", errors="ignore")
                    if any(h in header for h in _KEY_HEADERS):
                        found.append(full_path)
                except OSError:
                    continue

            return found
        finally:
            sftp.close()

    def write_server_key_wrapper(
        self,
        server_key_path: str,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        """
        Write a GIT_SSH wrapper script on the server that points to an
        existing key already present on that server.

        No key material is transferred — only the tiny wrapper script.

        Args:
            server_key_path: Absolute path to the private key ON THE SERVER
                             (e.g. /home/odoo/.ssh/id_ed25519).
        """
        if log_callback:
            log_callback(
                f"Usando llave del servidor: {server_key_path}"
            )

        wrapper = (
            "#!/bin/sh\n"
            f"exec ssh -i {server_key_path} "
            "-o StrictHostKeyChecking=no "
            "-o UserKnownHostsFile=/dev/null "
            "-o BatchMode=yes "
            '"$@"\n'
        ).encode("utf-8")

        sftp = self._ssh.open_sftp()
        try:
            with sftp.open(_REMOTE_SSH_WRAPPER, "wb") as fh:
                fh.write(wrapper)
        finally:
            sftp.close()

        self._ssh.execute(f"chmod +x {_REMOTE_SSH_WRAPPER}")

    def cleanup_key(self, uploaded: bool = True) -> None:
        """
        Remove temporary files from the server.

        Args:
            uploaded: If True (default), also remove the uploaded private key
                      from /tmp/.  Set to False when the key was already on the
                      server (server mode) — only the wrapper needs cleanup.
        """
        if uploaded:
            self._ssh.execute(f"rm -f {_REMOTE_KEY_PATH} {_REMOTE_SSH_WRAPPER}")
        else:
            self._ssh.execute(f"rm -f {_REMOTE_SSH_WRAPPER}")

    # ── Repository sync ───────────────────────────────────────────────────

    def sync(
        self,
        repo_url: str,
        branch: str,
        target_path: str,
        odoo_user: str = "odoo",
        use_submodules: bool = False,
        log_callback: Callable[[str], None] | None = None,
        cancel_event=None,
    ) -> bool:
        """
        Clone the repo to target_path (first time) or pull latest (subsequent).

        Requires upload_key() or write_server_key_wrapper() to have been called
        first so that /tmp/.obt_git_ssh.sh exists on the server.

        Args:
            repo_url:       Git SSH URL (git@github.com:org/repo.git).
            branch:         Branch to clone / pull.
            target_path:    Absolute path on the server for the addons.
            odoo_user:      OS user that should own the directory after sync.
            use_submodules: If True, run the full git-submodule update sequence
                            after the main clone / pull.  Use this when the repo
                            links other repositories via .gitmodules.
            log_callback:   Optional progress messages.
            cancel_event:   threading.Event; triggers cancellation when set.

        Returns:
            True if a fresh clone was performed, False if an existing repo
            was updated.

        Raises:
            RuntimeError: On path conflict, git failure, or cancellation.
        """
        # Determine clone vs pull
        code_git, _, _ = self._ssh.execute(f"test -d {target_path}/.git")
        is_first_clone = code_git != 0

        # Path exists but is not a git repo → fail early, no damage done
        code_exists, _, _ = self._ssh.execute(f"test -e {target_path}")
        if code_exists == 0 and is_first_clone:
            raise RuntimeError(
                f"La ruta '{target_path}' ya existe pero no es un repositorio git.\n\n"
                "Opciones:\n"
                f"  • Elija una ruta diferente\n"
                f"  • Elimine '{target_path}' manualmente y vuelva a intentar"
            )

        def _heartbeat(status: str) -> None:
            if log_callback:
                log_callback(f"  [git en curso] {status}")

        # ── Main clone / pull ─────────────────────────────────────────────
        if is_first_clone:
            parent = "/".join(target_path.rstrip("/").split("/")[:-1])
            if parent:
                self._ssh.execute(f"sudo mkdir -p {parent}")

            if log_callback:
                log_callback(
                    f"Clonando {repo_url}  ->  {target_path}  (rama: {branch}) ..."
                )
            cmd = (
                f"GIT_SSH={_REMOTE_SSH_WRAPPER} "
                f"git clone --branch {branch} --single-branch "
                f"{repo_url} {target_path}"
            )
        else:
            if log_callback:
                log_callback(
                    f"Actualizando addons en {target_path}  (rama: {branch}) ..."
                )
            cmd = (
                f"cd {target_path} && "
                f"GIT_SSH={_REMOTE_SSH_WRAPPER} "
                f"git pull origin {branch}"
            )

        code, _, err = self._ssh.execute_long(
            cmd,
            watch_cmd=f"ls -1 {target_path} 2>/dev/null | wc -l",
            heartbeat_callback=_heartbeat,
            timeout=300,
            cancel_event=cancel_event,
        )

        if code != 0:
            action = "clonar" if is_first_clone else "actualizar"
            raise RuntimeError(
                f"Error al {action} el repositorio:\n\n{err}\n\n"
                "Verifique:\n"
                "  • La llave publica esta registrada en GitHub / GitLab\n"
                f"  • La URL es correcta: {repo_url}\n"
                f"  • La rama existe: {branch}"
            )

        # ── Git submodules (optional) ─────────────────────────────────────
        if use_submodules:
            self._sync_submodules(
                target_path,
                is_first_clone=is_first_clone,
                log_callback=log_callback,
                cancel_event=cancel_event,
            )

        # Fix ownership so Odoo can read the addon files
        self._ssh.execute(
            f"sudo chown -R {odoo_user}:{odoo_user} {target_path} 2>/dev/null || true"
        )

        if log_callback:
            action = "clonado" if is_first_clone else "actualizado"
            log_callback(f"Repositorio {action} correctamente en {target_path}.")

        return is_first_clone

    def _sync_submodules(
        self,
        target_path: str,
        is_first_clone: bool,
        log_callback: Callable[[str], None] | None = None,
        cancel_event=None,
    ) -> None:
        """
        Run the full git-submodule update sequence inside target_path.

        On first clone:
          git submodule update --init --recursive

        On subsequent pulls:
          git submodule sync --recursive
          git submodule update --remote --recursive --merge
          git submodule update --init --recursive   (ensures any new submodules)

        The GIT_SSH wrapper must already be present on the server so that
        submodule fetches authenticate against GitHub / GitLab the same way
        as the parent repo.
        """
        if log_callback:
            log_callback("Sincronizando submódulos git...")

        def _heartbeat(status: str) -> None:
            if log_callback:
                log_callback(f"  [submodulos] {status}")

        if is_first_clone:
            # Initialize and check out all submodules after a fresh clone
            cmd = (
                f"cd {target_path} && "
                f"GIT_SSH={_REMOTE_SSH_WRAPPER} "
                f"git submodule update --init --recursive"
            )
            code, _, err = self._ssh.execute_long(
                cmd,
                watch_cmd=f"find {target_path} -name '.git' -mindepth 2 2>/dev/null | wc -l",
                heartbeat_callback=_heartbeat,
                timeout=300,
                cancel_event=cancel_event,
            )
            if code != 0:
                raise RuntimeError(
                    f"Error inicializando submódulos:\n\n{err}\n\n"
                    "Verifique que la llave tiene acceso a todos los repositorios de submódulos."
                )
        else:
            # 1. Sync URLs from .gitmodules (in case remotes changed)
            if log_callback:
                log_callback("  Sincronizando URLs de submódulos...")
            self._ssh.execute(f"cd {target_path} && git submodule sync --recursive")

            # 2. Fetch latest commit of each submodule's tracked branch
            if log_callback:
                log_callback("  Bajando cambios remotos en submódulos...")
            cmd = (
                f"cd {target_path} && "
                f"GIT_SSH={_REMOTE_SSH_WRAPPER} "
                f"git submodule update --remote --recursive --merge"
            )
            code, _, err = self._ssh.execute_long(
                cmd,
                watch_cmd=f"find {target_path} -name 'FETCH_HEAD' -mindepth 2 2>/dev/null | wc -l",
                heartbeat_callback=_heartbeat,
                timeout=300,
                cancel_event=cancel_event,
            )
            if code != 0:
                raise RuntimeError(
                    f"Error actualizando submódulos remotos:\n\n{err}\n\n"
                    "Verifique que la llave tiene acceso a todos los repositorios de submódulos."
                )

            # 3. Ensure any newly added submodules are also initialized
            if log_callback:
                log_callback("  Inicializando submódulos nuevos (si los hay)...")
            cmd2 = (
                f"cd {target_path} && "
                f"GIT_SSH={_REMOTE_SSH_WRAPPER} "
                f"git submodule update --init --recursive"
            )
            self._ssh.execute_long(
                cmd2,
                watch_cmd=f"find {target_path} -name '.git' -mindepth 2 2>/dev/null | wc -l",
                heartbeat_callback=_heartbeat,
                timeout=300,
                cancel_event=cancel_event,
            )

        if log_callback:
            log_callback("  Submódulos sincronizados correctamente.")

    # ── Odoo service management ───────────────────────────────────────────

    def detect_odoo_service(self) -> str | None:
        """
        Find the systemd service name for the running Odoo instance.

        Returns the service name string, or None if not found.
        """
        for name in ("odoo", "odoo18", "odoo17", "odoo16", "odoo-server", "odoo-bin"):
            code, _, _ = self._ssh.execute(
                f"systemctl is-active {name} >/dev/null 2>&1"
            )
            if code == 0:
                return name
        return None

    def restart_odoo(
        self,
        service_name: str,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        """
        Restart an Odoo systemd service.

        Raises:
            RuntimeError: If the restart command fails.
        """
        if log_callback:
            log_callback(f"Reiniciando servicio '{service_name}' ...")

        code, _, err = self._ssh.execute(
            f"sudo systemctl restart {service_name}", timeout=60
        )
        if code != 0:
            raise RuntimeError(
                f"No se pudo reiniciar '{service_name}':\n{err}\n\n"
                f"Intente manualmente: sudo systemctl restart {service_name}"
            )

        if log_callback:
            log_callback(f"Servicio '{service_name}' reiniciado correctamente.")
