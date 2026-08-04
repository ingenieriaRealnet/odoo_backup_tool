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
import re
import shlex
import socket
import time
from typing import Callable

import paramiko

from .ssh_client import SSHClient


# Paths used on the DESTINATION server — always cleaned up after use
_REMOTE_KEY_PATH       = "/tmp/.obt_gh_key"
_REMOTE_SSH_WRAPPER    = "/tmp/.obt_git_ssh.sh"
_REMOTE_DECRYPTED_KEY_PATH = "/tmp/.obt_gh_key_unlocked"

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

    def server_key_needs_passphrase(self, server_key_path: str) -> bool:
        """
        Check whether the key at server_key_path — already ON the remote
        server — is passphrase-protected, without ever reading its bytes
        off the server (preserves "server mode"'s no-key-transfer promise).

        Runs `ssh-keygen -y` against it with an empty passphrase; OpenSSH
        reports "incorrect passphrase supplied" when the key is protected.

        Returns:
            True if the key needs a passphrase. False if it doesn't, OR if
            the check itself couldn't run (missing ssh-keygen, unreadable
            path, ...) — in that case the real git operation will surface
            whatever the actual problem is, same as before this feature.
        """
        code, out, err = self._ssh.execute(
            f"ssh-keygen -y -P '' -f {server_key_path} 2>&1 >/dev/null"
        )
        if code == 0:
            return False
        return "passphrase" in (out + err).lower()

    def unlock_server_key(
        self,
        server_key_path: str,
        passphrase: str,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        """
        Decrypt a passphrase-protected server key into a temporary,
        unprotected COPY on the server, and repoint the GIT_SSH wrapper at
        that copy for the rest of the sync (main clone/pull + every
        submodule fetch).

        Superseded an ssh-agent-based approach (start `ssh-agent -s` on the
        server, `ssh-add` the key into it via a one-shot SSH_ASKPASS script,
        then pass SSH_AUTH_SOCK=... as an env prefix to every git command).
        That depended on the backgrounded agent process surviving for the
        entire sync across many separate SSH channels — fragile on servers
        where sshd's PAM/systemd session teardown reaps a channel's
        background processes once that channel closes (`KillUserProcesses`).
        Confirmed 2026-07-27: a real-estate client sync got through ~26
        submodules authenticating fine and then started failing, consistent
        with the agent dying mid-sequence rather than a real auth problem.
        A file-based unlock has no such lifetime dependency — the decrypted
        copy simply exists on disk until cleanup_key() removes it, same as
        the "local" key-source mode already does.

        Args:
            server_key_path: Absolute path to the encrypted private key ON
                              THE SERVER (never read into this process).
            passphrase:      The key's passphrase, collected from the user.

        Raises:
            RuntimeError: If the passphrase is wrong or decryption fails.
        """
        if log_callback:
            log_callback("Descifrando llave del servidor (copia temporal sin passphrase)...")

        code, _, err = self._ssh.execute(
            f"cp {server_key_path} {_REMOTE_DECRYPTED_KEY_PATH} && "
            f"chmod 600 {_REMOTE_DECRYPTED_KEY_PATH}"
        )
        if code != 0:
            raise RuntimeError(f"No se pudo copiar la llave del servidor:\n{err}")

        code, out, err = self._ssh.execute(
            f"ssh-keygen -p -P {shlex.quote(passphrase)} -N '' "
            f"-f {_REMOTE_DECRYPTED_KEY_PATH} 2>&1"
        )
        if code != 0:
            self._ssh.execute(f"rm -f {_REMOTE_DECRYPTED_KEY_PATH}")
            raise RuntimeError(
                "No se pudo desbloquear la llave con la passphrase suministrada.\n"
                f"{out or err}\n\n"
                "Verifique que la passphrase es correcta."
            )

        # Repoint the wrapper at the decrypted copy — every subsequent git
        # command (main clone/pull + every submodule fetch) uses it directly,
        # no agent and no env var required.
        self.write_server_key_wrapper(_REMOTE_DECRYPTED_KEY_PATH)

        if log_callback:
            log_callback("Llave descifrada — se usara durante toda la sincronizacion.")

    def cleanup_key(self, uploaded: bool = True) -> None:
        """
        Remove temporary files from the server.

        Args:
            uploaded: If True (default), also remove the uploaded private key
                      from /tmp/.  Set to False when the key was already on the
                      server (server mode) — only the wrapper (and any
                      decrypted copy from unlock_server_key()) need cleanup.
        """
        paths = f"{_REMOTE_SSH_WRAPPER} {_REMOTE_DECRYPTED_KEY_PATH}"
        if uploaded:
            paths = f"{_REMOTE_KEY_PATH} {paths}"
        self._ssh.execute(f"rm -f {paths}")

    # ── Repository sync ───────────────────────────────────────────────────

    def sync(
        self,
        repo_url: str,
        branch: str,
        target_path: str,
        odoo_user: str = "odoo",
        use_submodules: bool = False,
        force_mirror: bool = False,
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
            force_mirror:   If True, production is forced to become an exact
                            mirror of the remote branch instead of being
                            merged with it — see _sync_submodules() docstring
                            for the full rationale. On an existing checkout
                            this replaces `git pull` (fetch+merge, which
                            refuses to run or produces a merge commit when
                            production has local drift) with
                            `git fetch && git reset --hard origin/<branch> &&
                            git clean -fd` — local changes and untracked
                            files are discarded, never merged. No effect on
                            a first clone (already a clean mirror by
                            definition). Destructive — the caller (GUI) must
                            confirm with the user before setting this.
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
                f"git -c safe.directory=* clone --branch {branch} --single-branch "
                f"{repo_url} {target_path}"
            )
        elif force_mirror:
            if log_callback:
                log_callback(
                    f"Actualizando addons en {target_path}  (rama: {branch}, "
                    "modo espejo forzado — descarta cambios locales) ..."
                )
            # fetch + reset --hard + clean -fd: the remote branch always
            # wins, never merges with whatever is on the server. `git pull`
            # (the non-mirror path below) either refuses to run or produces
            # a merge commit when production has local drift — neither is
            # "production is a mirror of the repo" (see docstring above).
            cmd = (
                f"cd {target_path} && "
                f"GIT_SSH={_REMOTE_SSH_WRAPPER} "
                f"git -c safe.directory=* fetch origin {branch} && "
                f"git -c safe.directory=* reset --hard origin/{branch} && "
                f"git -c safe.directory=* clean -fd"
            )
        else:
            if log_callback:
                log_callback(
                    f"Actualizando addons en {target_path}  (rama: {branch}) ..."
                )
            cmd = (
                f"cd {target_path} && "
                f"GIT_SSH={_REMOTE_SSH_WRAPPER} "
                f"git -c safe.directory=* pull origin {branch}"
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
        # Stale local submodule registrations (see detect_stale_submodules())
        # are handled by the caller BEFORE sync() runs — it needs to confirm
        # with the user before deiniting anything local. See gui/app.py's
        # _worker_addons for the confirm-then-deinit_submodules() sequence.
        if use_submodules:
            self._sync_submodules(
                target_path,
                is_first_clone=is_first_clone,
                force_mirror=force_mirror,
                log_callback=log_callback,
                cancel_event=cancel_event,
            )

        # Fix ownership so Odoo can read the addon files — excluding every
        # .git directory (top-level and each submodule's). A plain
        # `chown -R` used to hand those to odoo_user too; since git commands
        # in this class run as the SSH user (typically root), the NEXT sync
        # would then hit git's ownership-mismatch guard ("detected dubious
        # ownership in repository at ...", CVE-2022-24765 fix) and refuse to
        # run entirely. Confirmed 2026-07-28 on a Limatec server: every sync
        # after the first failed this way. `find ... -prune` skips descending
        # into any .git dir instead of chowning its contents.
        self._ssh.execute(
            f"sudo find {target_path} -name .git -prune -o "
            f"-exec chown {odoo_user}:{odoo_user} {{}} + 2>/dev/null || true"
        )

        if log_callback:
            action = "clonado" if is_first_clone else "actualizado"
            log_callback(f"Repositorio {action} correctamente en {target_path}.")

        return is_first_clone

    def detect_stale_submodules(self, target_path: str) -> list[str]:
        """
        Return names of submodules still registered in this checkout's local
        `.git/config` but no longer listed in the CURRENT `.gitmodules`.

        A `git pull` that merges in a commit removing a submodule updates the
        working tree/.gitmodules fine, but does NOT touch this EXISTING
        checkout's own `.git/config` ([submodule "name"] section) or
        `.git/modules/name/` — those only get cleaned up by an explicit
        `git submodule deinit`, which nothing in the normal pull flow runs.
        Left in place, `git submodule sync --recursive` / `update --remote
        --recursive --merge` (see _sync_submodules) can still try to operate
        on that no-longer-tracked submodule and fail — confirmed 2026-07-27:
        a submodule (realnet_real_estate_crm) was removed from the ecoerp
        repo's develop branch, and a fresh clone picked that up cleanly, but
        a server with a pre-existing checkout kept failing on it indefinitely
        because its local .git/config still had the submodule registered.

        Read-only — never modifies the checkout. The caller (gui/app.py's
        _worker_addons) confirms with the user before calling
        deinit_submodules() with whatever names this returns. Safe to call
        even before a first clone: if target_path doesn't exist yet, the `cd`
        fails and this simply returns an empty list.

        Returns:
            List of submodule names (possibly empty) registered locally but
            absent from the current .gitmodules.
        """
        _, out, _ = self._ssh.execute(
            f"cd {target_path} && "
            "for name in $(git -c safe.directory=* config -f .git/config --get-regexp "
            "'^submodule\\..*\\.url' 2>/dev/null | "
            "sed -E 's/^submodule\\.(.*)\\.url .*/\\1/'); do "
            "  git -c safe.directory=* config -f .gitmodules --get \"submodule.$name.url\" "
            "  >/dev/null 2>&1 || echo \"$name\"; "
            "done"
        )
        return [name.strip() for name in out.splitlines() if name.strip()]

    def deinit_submodules(
        self,
        target_path: str,
        names: list[str],
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        """
        Remove the local registration ([submodule "name"] in .git/config and
        .git/modules/name/) for each of the given submodule names.

        Only touches this LOCAL checkout — never contacts GitHub/GitLab and
        never modifies the remote repository. Intended to be called only
        after the caller has confirmed with the user (see
        detect_stale_submodules()'s docstring for why this cleanup is
        sometimes needed).
        """
        for name in names:
            if log_callback:
                log_callback(
                    f"  Limpiando registro local de submodulo obsoleto: {name}"
                )
            self._ssh.execute(
                f"cd {target_path} && git -c safe.directory=* submodule deinit -f {name} 2>&1"
            )

    def _sync_submodules(
        self,
        target_path: str,
        is_first_clone: bool,
        force_mirror: bool = False,
        log_callback: Callable[[str], None] | None = None,
        cancel_event=None,
    ) -> None:
        """
        Run the full git-submodule update sequence inside target_path.

        On first clone:
          git submodule update --init --recursive

        On subsequent pulls, default (tracking) mode:
          git submodule sync --recursive
          git submodule update --remote --recursive --merge
          git submodule update --init --recursive   (ensures any new submodules)

        `--remote` moves each submodule to the latest commit of ITS OWN
        tracked branch, independent of whatever commit the superproject's
        tree actually pins — and `--merge` merges that into whatever is
        locally checked out instead of discarding local drift. Together
        that means production ends up reflecting "whatever is newest per
        submodule", not "exactly what the parent repo records" — not a
        mirror, and any local submodule edit survives as a merge instead
        of being overwritten.

        On subsequent pulls, force_mirror=True:
          git submodule sync --recursive
          git submodule update --init --recursive --force   (NO --remote, NO --merge —
              pins each submodule to the EXACT commit the superproject's
              tree records, discarding local modifications during checkout)
          git submodule foreach --recursive "git reset --hard && git clean -fd"
              (belt-and-suspenders: --force above discards changes to
              tracked files during checkout, but does not by itself remove
              untracked files left inside a submodule — clean -fd does)

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
                f"git -c safe.directory=* submodule update --init --recursive"
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
        elif force_mirror:
            # 1. Sync URLs from .gitmodules (in case remotes changed)
            if log_callback:
                log_callback("  Sincronizando URLs de submódulos...")
            self._ssh.execute(f"cd {target_path} && git -c safe.directory=* submodule sync --recursive")

            # 2. Pin every submodule to the EXACT commit the superproject's
            # tree records — no --remote (which would move past that commit
            # to each submodule's own branch tip) and no --merge (which
            # would mix local drift into the result). --force discards
            # local modifications to tracked files during the checkout.
            if log_callback:
                log_callback(
                    "  Fijando submódulos al commit exacto del repositorio "
                    "(descarta cambios locales) ..."
                )
            cmd = (
                f"cd {target_path} && "
                f"GIT_SSH={_REMOTE_SSH_WRAPPER} "
                f"git -c safe.directory=* submodule update --init --recursive --force"
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
                    f"Error fijando submódulos al commit del repositorio:\n\n{err}\n\n"
                    "Verifique que la llave tiene acceso a todos los repositorios de submódulos."
                )

            # 3. Belt-and-suspenders: remove any untracked files left inside
            # submodules (--force above only handles tracked-file drift).
            if log_callback:
                log_callback("  Limpiando archivos no rastreados dentro de submódulos...")
            self._ssh.execute(
                f"cd {target_path} && "
                f"git -c safe.directory=* submodule foreach --recursive "
                f"'git -c safe.directory=* reset --hard && git -c safe.directory=* clean -fd'",
                timeout=120,
            )
        else:
            # 1. Sync URLs from .gitmodules (in case remotes changed)
            if log_callback:
                log_callback("  Sincronizando URLs de submódulos...")
            self._ssh.execute(f"cd {target_path} && git -c safe.directory=* submodule sync --recursive")

            # 2. Fetch latest commit of each submodule's tracked branch
            if log_callback:
                log_callback("  Bajando cambios remotos en submódulos...")
            cmd = (
                f"cd {target_path} && "
                f"GIT_SSH={_REMOTE_SSH_WRAPPER} "
                f"git -c safe.directory=* submodule update --remote --recursive --merge"
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
                f"git -c safe.directory=* submodule update --init --recursive"
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

    # ── Changed-module detection (for -u database updates) ────────────────

    def get_head_commit(self, target_path: str) -> str | None:
        """
        Return the current HEAD commit SHA of target_path's checkout, or
        None if it doesn't exist yet / isn't a git repo (e.g. before a
        first clone). Called by the caller (gui/app.py's _worker_addons)
        before and after sync() to snapshot state for diff_changed_modules().
        """
        code, out, _ = self._ssh.execute(
            f"cd {target_path} && git -c safe.directory=* rev-parse HEAD 2>/dev/null"
        )
        return out.strip() if code == 0 and out.strip() else None

    def get_submodule_status(self, target_path: str) -> dict[str, str]:
        """
        Return {submodule_name: commit_sha} for every submodule currently
        checked out under target_path. Empty dict if target_path doesn't
        exist yet or has no submodules.
        """
        code, out, _ = self._ssh.execute(
            f"cd {target_path} && git -c safe.directory=* submodule status 2>/dev/null"
        )
        if code != 0:
            return {}
        status: dict[str, str] = {}
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            # Format: "[+-U ]<sha> <path> (<describe>)" — a leading +/-/U
            # marks out-of-sync/uninitialized/conflicted, absent for clean.
            parts = line.lstrip("+-U ").split(" ", 1)
            if len(parts) < 2:
                continue
            sha = parts[0]
            path = parts[1].split(" ", 1)[0]
            status[path] = sha
        return status

    def _list_module_dirs(self, target_path: str) -> set[str]:
        """
        Top-level directory names under target_path that look like Odoo
        addons (contain __manifest__.py) — used to filter changed-file
        diffs down to real modules for the -u module list.
        """
        code, out, _ = self._ssh.execute(
            f"cd {target_path} && for d in */; do "
            f"[ -f \"$d/__manifest__.py\" ] && echo \"${{d%/}}\"; done 2>/dev/null"
        )
        return set(out.splitlines()) if code == 0 else set()

    def diff_changed_modules(
        self,
        target_path: str,
        before_head: str | None,
        after_head: str | None,
        before_submodules: dict[str, str],
        after_submodules: dict[str, str],
    ) -> list[str]:
        """
        Determine which addon modules actually changed between a before/after
        snapshot of the checkout (see get_head_commit()/get_submodule_status(),
        captured by the caller right before and after sync()).

        Combines two signals:
          - Submodules whose pinned commit moved (covers the common case
            where every addon is its own git submodule).
          - Top-level, non-submodule directories with file changes in the
            main repo's own history between before_head and after_head
            (covers addons tracked directly in the superproject).

        Returns only names that are real Odoo modules (have __manifest__.py)
        — this list feeds update_db_modules()'s `-u` argument. Empty list if
        before_head is None: on a first clone nothing was "updated", it's
        all brand new and not yet installed in any database (an -i install
        is a different operation, out of scope here).
        """
        if before_head is None or after_head is None:
            return []

        module_dirs = self._list_module_dirs(target_path)
        changed: set[str] = set()

        # Submodules whose commit moved
        for name, sha in after_submodules.items():
            if before_submodules.get(name) != sha:
                changed.add(name)

        # Directly-tracked (non-submodule) module dirs with file changes
        non_submodule_dirs = module_dirs - set(after_submodules.keys())
        if before_head != after_head and non_submodule_dirs:
            code, out, _ = self._ssh.execute(
                f"cd {target_path} && "
                f"git -c safe.directory=* diff --name-only {before_head} {after_head} -- "
                + " ".join(non_submodule_dirs)
            )
            if code == 0:
                for line in out.splitlines():
                    top = line.split("/", 1)[0]
                    if top in non_submodule_dirs:
                        changed.add(top)

        return sorted(changed & module_dirs)

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

    def resolve_service_launcher(self, service_name: str) -> tuple[str, str]:
        """
        Infer the Odoo binary path and config file used by a systemd
        service, by reading its ExecStart= directive.

        Used by update_db_modules() so the user only has to name the
        database — the binary and config path come from whatever the
        service is already configured to run, instead of a second field
        that could drift out of sync with the service's real launch command.

        Returns:
            (odoo_binary_path, conf_path)

        Raises:
            RuntimeError: If the service's ExecStart can't be read, or
                          doesn't use -c for its config file.
        """
        code, out, err = self._ssh.execute(
            f"systemctl show {service_name} --property=ExecStart --no-pager 2>&1"
        )
        if code != 0 or not out.strip():
            raise RuntimeError(
                f"No se pudo leer la configuracion del servicio '{service_name}':\n{err or out}"
            )

        m = re.search(r"argv\[\]=(.*?)\s*;", out)
        if not m:
            raise RuntimeError(
                f"No se pudo interpretar el ExecStart del servicio '{service_name}':\n{out}"
            )
        argv = m.group(1).split()

        # The launcher isn't always the odoo binary directly — e.g. a venv
        # service runs "<python> <odoo-script> -c ... -d ...". Take every
        # leading token that isn't a flag (interpreter + script, or just the
        # binary if it's invoked directly) so update_db_modules() reproduces
        # the exact same invocation the service itself uses.
        prefix: list[str] = []
        for tok in argv:
            if tok.startswith("-"):
                break
            prefix.append(tok)
        if not prefix:
            raise RuntimeError(
                f"No se pudo determinar el comando de arranque de '{service_name}' "
                f"a partir de su ExecStart:\n{out}"
            )

        conf_match = re.search(r"(?:-c|--config)[ =](\S+)", " ".join(argv))
        if not conf_match:
            raise RuntimeError(
                f"El servicio '{service_name}' no especifica un archivo de configuracion "
                f"(-c/--config) en su ExecStart:\n{out}\n\n"
                "Verifique manualmente la ruta del odoo.conf de este servidor."
            )
        return " ".join(prefix), conf_match.group(1)

    def update_db_modules(
        self,
        db_name: str,
        modules: list[str],
        conf_path: str,
        odoo_bin: str,
        odoo_user: str = "odoo",
        log_callback: Callable[[str], None] | None = None,
        cancel_event=None,
    ) -> None:
        """
        Apply pending module upgrades to db_name for the given modules — the
        step Odoo needs after new module code lands on disk (git sync) before
        it actually takes effect in the database (new fields, views, data,
        migrations). Runs as odoo_user via `sudo -u`, matching how the
        service itself runs, with --stop-after-init so it exits once done
        rather than staying up as a second live instance.

        Raises:
            RuntimeError: If the update command fails.
        """
        if not modules:
            return
        module_list = ",".join(modules)
        if log_callback:
            log_callback(
                f"Actualizando {len(modules)} modulo(s) en la base de datos "
                f"'{db_name}': {module_list} ..."
            )

        # Odoo's own --logfile, tailed for heartbeat — without this the
        # whole -u run was silent until it finished (or failed), which made
        # it look instantaneous/broken even when it was genuinely still
        # running for several minutes. Own temp file (not the service's
        # production log) so this ad-hoc run's output doesn't interleave
        # with — or contend for — the live service's own logfile.
        log_file = f"/tmp/.obt_update_{int(time.time())}.log"
        cmd = (
            f"sudo -u {odoo_user} {odoo_bin} -c {conf_path} -d {db_name} "
            f"-u {module_list} --without-demo=1 --stop-after-init --no-http "
            f"--logfile={log_file} 2>&1"
        )

        def _heartbeat(_status: str) -> None:
            if not log_callback:
                return
            _, out, _ = self._ssh.execute(f"tail -n 1 {log_file} 2>/dev/null")
            if out.strip():
                log_callback(f"  [actualizando BD] {out.strip()}")

        code, _, err = self._ssh.execute_long(
            cmd,
            watch_cmd=f"tail -n 1 {log_file} 2>/dev/null",
            heartbeat_callback=_heartbeat,
            heartbeat_interval=10,
            timeout=1800,
            cancel_event=cancel_event,
        )

        if code != 0:
            # A non-zero exit doesn't always mean the upgrade itself failed —
            # confirmed 2026-08-04 on limatec_prod: non-fatal RST/reportlab
            # warnings while rendering a module's description to PDF made
            # odoo exit non-zero, even though ir_module_module already showed
            # every module 'installed' with a write_date matching this exact
            # run (i.e. the actual upgrade had already committed). Verify
            # against the database itself before reporting a false failure.
            if self._verify_modules_installed(db_name, modules):
                if log_callback:
                    log_callback(
                        "Aviso: el proceso de actualizacion devolvio un codigo de "
                        "salida distinto de cero (probablemente advertencias no "
                        "fatales, p.ej. generando documentacion/PDF), pero se "
                        f"verifico en la base de datos que los modulos SI quedaron "
                        f"'installed' en '{db_name}'."
                    )
                self._ssh.execute(f"rm -f {log_file}")
                if log_callback:
                    log_callback(f"Base de datos '{db_name}' actualizada correctamente.")
                return
            raise RuntimeError(
                f"Error actualizando modulos en '{db_name}':\n\n{err}\n\n"
                f"Log completo conservado en el servidor: {log_file}"
            )

        self._ssh.execute(f"rm -f {log_file}")
        if log_callback:
            log_callback(f"Base de datos '{db_name}' actualizada correctamente.")

    def _verify_modules_installed(self, db_name: str, modules: list[str]) -> bool:
        """
        Best-effort check: query ir_module_module directly for whether every
        module in `modules` ended up 'installed', independent of the -u
        process's own exit code (see update_db_modules()'s docstring above
        for why that exit code alone isn't always trustworthy).

        Returns False — never claims success — if the query itself fails or
        if ANY module isn't 'installed'; this only ever turns a reported
        failure into a success, never the other way around.
        """
        placeholders = ",".join(f"'{m}'" for m in modules)
        code, out, _ = self._ssh.execute(
            f"sudo -u postgres psql -d {db_name} -t -A -c "
            f"\"SELECT name, state FROM ir_module_module WHERE name IN ({placeholders})\""
        )
        if code != 0 or not out.strip():
            return False
        states: dict[str, str] = {}
        for line in out.splitlines():
            if "|" in line:
                name, state = line.split("|", 1)
                states[name.strip()] = state.strip()
        return all(states.get(m) == "installed" for m in modules)
