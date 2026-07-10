"""
Remote filesystem browser via SFTP + SSH.

Provides directory listing with metadata, directory-size calculation,
mkdir, rmdir, and file removal on a remote server through an existing
SSHClient connection.

Usage:
    browser = RemoteBrowser(ssh_client)
    entries = browser.list_dir("/home/odoo")
    for e in entries:
        print(e["name"], e["size_human"], e["modified"])

Notes:
    - list_dir() uses SFTP listdir_attr() which is fast but gives 0 for
      directory sizes.  Call get_dir_size() separately (it runs du -sh and
      can be slow for large trees).
    - All public methods are safe to call from a background thread.  UI
      updates must still go through the main-thread queue.
"""
from __future__ import annotations

import datetime
import posixpath
import stat
import time
from typing import Callable

from .ssh_client import SSHClient


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def _human_size(size_bytes: int) -> str:
    """Convert a byte count to a readable string (e.g. 2.3 GB)."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


# ---------------------------------------------------------------------------
# RemoteBrowser
# ---------------------------------------------------------------------------

class RemoteBrowser:
    """
    Browse, measure, and manage the filesystem of a remote server via SSH.

    All methods are thread-safe: they only communicate with the server via
    the underlying SSHClient and do not touch any tkinter state.
    """

    def __init__(self, ssh: SSHClient) -> None:
        self._ssh = ssh

    # ── Navigation ────────────────────────────────────────────────────────

    def home(self) -> str:
        """
        Return the home directory of the connected SSH user.

        Returns "/" if the shell does not set $HOME.
        """
        _, out, _ = self._ssh.execute("echo $HOME")
        return out.strip() or "/"

    def resolve_path(self, path: str) -> str:
        """
        Expand ~ and resolve relative segments in path on the remote server.

        Falls back to the original path string if realpath is not available.
        """
        code, out, _ = self._ssh.execute(
            f"realpath {path} 2>/dev/null || echo {path}"
        )
        return out.strip() or path

    def list_dir(self, path: str) -> list[dict]:
        """
        List the contents of path on the remote server.

        Returns a list of entry dicts sorted directories-first then
        alphabetically.  Each dict has these keys:

            name        (str)  : filename only (no path prefix)
            full_path   (str)  : absolute path of the entry
            is_dir      (bool) : True for directories and dir-symlinks
            is_link     (bool) : True for any symlink
            size_bytes  (int)  : size in bytes; 0 for directories
            size_human  (str)  : human-readable size (empty for directories)
            modified    (str)  : last-modified as "YYYY-MM-DD HH:MM"
            permissions (str)  : rwx string from stat.filemode()

        Raises:
            FileNotFoundError : path does not exist on the remote server.
            PermissionError   : path exists but cannot be listed.
        """
        sftp = self._ssh.open_sftp()
        try:
            entries = sftp.listdir_attr(path)
        except IOError as exc:
            msg = str(exc).lower()
            if "no such file" in msg or "does not exist" in msg:
                raise FileNotFoundError(f"No existe: {path}")
            raise PermissionError(f"Sin permisos para listar: {path}")
        finally:
            sftp.close()

        result: list[dict] = []
        for entry in entries:
            mode    = entry.st_mode or 0
            is_dir  = stat.S_ISDIR(mode)
            is_link = stat.S_ISLNK(mode)

            size_b  = entry.st_size or 0
            size_h  = _human_size(size_b) if not is_dir else ""

            modified = ""
            if entry.st_mtime:
                dt = datetime.datetime.fromtimestamp(entry.st_mtime)
                modified = dt.strftime("%Y-%m-%d %H:%M")

            full_path = f"{path.rstrip('/')}/{entry.filename}"

            result.append({
                "name":        entry.filename,
                "full_path":   full_path,
                "is_dir":      is_dir or is_link,
                "is_link":     is_link,
                "size_bytes":  size_b,
                "size_human":  size_h,
                "modified":    modified,
                "permissions": stat.filemode(mode) if mode else "----------",
            })

        # Directories first, then files — each group case-insensitive alpha
        result.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
        return result

    # ── Size calculation ──────────────────────────────────────────────────

    def get_dir_size(
        self,
        path: str,
        log_callback: Callable[[str], None] | None = None,
    ) -> str:
        """
        Calculate the total disk usage of path using du -sh.

        This can be slow for large directory trees — always call from a
        background thread.

        Returns a human-readable string (e.g. "2.3G") or "?" on error.
        """
        if log_callback:
            log_callback(f"Calculando tamano de {path} ...")
        code, out, _ = self._ssh.execute(
            f"du -sh {path} 2>/dev/null | cut -f1"
        )
        return out.strip() or "?"

    def get_dir_sizes_bulk(
        self,
        paths: list[str],
        log_callback: Callable[[str], None] | None = None,
    ) -> dict[str, str]:
        """
        Calculate disk usage for multiple paths in a single du call.

        More efficient than calling get_dir_size() for each path separately.

        Returns:
            dict mapping path → human-readable size string.
        """
        if not paths:
            return {}
        path_list = " ".join(f'"{p}"' for p in paths)
        code, out, _ = self._ssh.execute(
            f"du -sh {path_list} 2>/dev/null"
        )
        result: dict[str, str] = {}
        for line in out.strip().splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                size, path = parts
                # du may prepend "./" — normalize
                path = path.strip().lstrip("./")
                result[path.strip()] = size.strip()
        return result

    # ── Filesystem operations ─────────────────────────────────────────────

    def mkdir(self, path: str) -> None:
        """
        Create a directory (and all missing parents) on the remote server.

        Raises:
            RuntimeError: If the mkdir command fails.
        """
        code, _, err = self._ssh.execute(f"mkdir -p {path}")
        if code != 0:
            raise RuntimeError(f"Error creando directorio '{path}':\n{err}")

    def rmdir(self, path: str, recursive: bool = False) -> None:
        """
        Remove a directory on the remote server.

        Args:
            path:      Absolute path of the directory to remove.
            recursive: If True, removes the directory and ALL its contents
                       (equivalent to rm -rf).  If False, only succeeds if
                       the directory is empty (rmdir).

        Raises:
            RuntimeError: If the removal fails.
        """
        cmd = f"rm -rf {path}" if recursive else f"rmdir {path}"
        code, _, err = self._ssh.execute(cmd)
        if code != 0:
            raise RuntimeError(f"Error eliminando '{path}':\n{err}")

    def rm_file(self, path: str) -> None:
        """
        Remove a single file on the remote server.

        Raises:
            RuntimeError: If the removal fails.
        """
        code, _, err = self._ssh.execute(f"rm -f {path}")
        if code != 0:
            raise RuntimeError(f"Error eliminando archivo '{path}':\n{err}")

    def rename(self, old_path: str, new_path: str) -> None:
        """
        Rename or move a file/directory on the remote server.

        Args:
            old_path: Current absolute path.
            new_path: New absolute path (can be in a different directory = move).

        Raises:
            RuntimeError: If the mv command fails.
        """
        code, _, err = self._ssh.execute(f"mv {old_path} {new_path}")
        if code != 0:
            raise RuntimeError(f"Error renombrando '{old_path}' -> '{new_path}':\n{err}")

    def copy(self, src_path: str, dst_path: str) -> None:
        """
        Copy a file or directory recursively on the remote server (server-side).

        Args:
            src_path: Source absolute path.
            dst_path: Destination absolute path.

        Raises:
            RuntimeError: If the cp command fails.
        """
        code, _, err = self._ssh.execute(f"cp -r {src_path} {dst_path}")
        if code != 0:
            raise RuntimeError(f"Error copiando '{src_path}' -> '{dst_path}':\n{err}")

    def chmod(self, path: str, mode: str) -> None:
        """
        Change permissions of a file or directory on the remote server.

        Args:
            path: Absolute path to the target.
            mode: Permission string (e.g. '755', '644', 'u+x').

        Raises:
            RuntimeError: If chmod fails.
        """
        code, _, err = self._ssh.execute(f"chmod {mode} {path}")
        if code != 0:
            raise RuntimeError(f"Error aplicando permisos '{mode}' a '{path}':\n{err}")

    def get_file_info(self, path: str) -> dict:
        """
        Return detailed metadata for a single file or directory.

        Returns a dict with: name, full_path, is_dir, size_bytes, size_human,
        modified, permissions, owner, group, symlink_target.
        """
        code, out, _ = self._ssh.execute(
            f"stat -c '%F|%s|%Y|%a|%U|%G|%N' {path} 2>/dev/null"
        )
        info: dict = {
            "full_path":      path,
            "name":           path.rstrip("/").rsplit("/", 1)[-1],
            "is_dir":         False,
            "size_bytes":     0,
            "size_human":     "",
            "modified":       "",
            "permissions":    "",
            "owner":          "",
            "group":          "",
            "symlink_target": "",
        }
        if code == 0 and out.strip():
            parts = out.strip().split("|", 6)
            if len(parts) >= 6:
                ftype         = parts[0]
                info["is_dir"]      = "directory" in ftype
                info["size_bytes"]  = int(parts[1]) if parts[1].isdigit() else 0
                info["size_human"]  = _human_size(info["size_bytes"]) if not info["is_dir"] else ""
                import datetime
                try:
                    dt = datetime.datetime.fromtimestamp(int(parts[2]))
                    info["modified"] = dt.strftime("%Y-%m-%d %H:%M")
                except (ValueError, OSError):
                    pass
                info["permissions"]    = parts[3]
                info["owner"]          = parts[4]
                info["group"]          = parts[5]
                if len(parts) > 6 and "->" in parts[6]:
                    info["symlink_target"] = parts[6].split("->")[-1].strip().strip("'")
        return info

    def download_file(
        self,
        remote_path: str,
        local_path: str,
        progress_callback=None,
    ) -> None:
        """Download a single file from the remote server via SFTP."""
        sftp = self._ssh.open_sftp()
        try:
            sftp.get(remote_path, local_path, callback=progress_callback)
        finally:
            sftp.close()

    def upload_file(
        self,
        local_path: str,
        remote_path: str,
        progress_callback=None,
    ) -> None:
        """Upload a single file to the remote server via SFTP."""
        sftp = self._ssh.open_sftp()
        try:
            sftp.put(local_path, remote_path, callback=progress_callback)
        finally:
            sftp.close()

    def download_dir_as_zip(
        self,
        remote_path: str,
        local_zip_path: str,
        progress_callback=None,
    ) -> None:
        """
        Compress a remote directory into a .zip and download it locally.

        Strategy:
          1. On the server: cd to the parent and run `zip -r <tmp>.zip <dirname>`.
          2. Download the .zip via SFTP to local_zip_path.
          3. Remove the temporary .zip from the server (best-effort).

        Args:
            remote_path:    Absolute path of the remote directory to compress.
            local_zip_path: Local filesystem path where the .zip will be saved.
            progress_callback: Optional SFTP progress callback(bytes_done, total).

        Raises:
            RuntimeError: If `zip` fails on the server or the download fails.
        """
        # Unique temp path to avoid collisions between concurrent downloads
        basename   = posixpath.basename(remote_path.rstrip("/"))
        parent     = posixpath.dirname(remote_path.rstrip("/")) or "/"
        ts         = int(time.time())
        remote_zip = f"/tmp/obt_dl_{basename}_{ts}.zip"

        # Compress on the remote server; cd to parent so the archive keeps
        # the folder name as the root entry inside the zip.
        code, _, err = self._ssh.execute(
            f"cd {parent} && zip -r {remote_zip} {basename} 2>&1"
        )
        if code != 0:
            raise RuntimeError(
                f"Error al comprimir '{remote_path}' en el servidor:\n{err}"
            )

        try:
            self.download_file(remote_zip, local_zip_path, progress_callback)
        finally:
            # Always clean up the temporary zip from the server
            self._ssh.execute(f"rm -f {remote_zip}")
