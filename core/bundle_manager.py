"""
Bundle manager for Odoo Backup Tool.

Wraps the three backup artifacts (.dump/.sql, filestore .zip, inventory .json)
into a single uncompressed .tar archive on the remote server so they travel
as one unit and remain associated.

Format choice: .tar (no re-compression)
  - The dump uses pg_dump -Fc internal compression.
  - The filestore uses zip -1 (fast, slight compression).
  - Re-compressing already-compressed data wastes CPU with no size benefit.
  - 'tar' is available on all Linux servers without extra packages.

Naming convention:  {db}_{YYYY-MM-DD_HH-MM}_obt.tar
"""
from __future__ import annotations

import json
import os
import tarfile
from typing import Callable

from .ssh_client import SSHClient


class BundleManager:
    """
    Creates and extracts OBT backup bundles (.tar) on remote servers.

    Args:
        ssh: Connected SSHClient pointing at the server where the bundle
             will be created or extracted.
    """

    def __init__(self, ssh: SSHClient) -> None:
        self._ssh = ssh

    # ── Remote operations ─────────────────────────────────────────────────

    def write_json_to_server(self, data: dict, remote_path: str) -> None:
        """Write a Python dict as a UTF-8 JSON file on the remote server via SFTP."""
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        sftp = self._ssh.open_sftp()
        try:
            with sftp.open(remote_path, "wb") as fh:
                fh.write(payload)
        finally:
            sftp.close()

    def create(
        self,
        bundle_path: str,
        file_paths: list[str],
        log_callback: Callable[[str], None] | None = None,
    ) -> str:
        """
        Create a .tar bundle on the remote server containing the given files.

        All files must reside in /tmp (or the same directory). Inside the tar
        they are stored with their basenames only (no directory prefix), so
        extraction puts them flat in the target directory.

        Args:
            bundle_path: Absolute path for the output .tar on the server.
            file_paths:  List of absolute paths on the server to include.
            log_callback: Optional status logger.

        Returns:
            bundle_path (same as input, for chaining).

        Raises:
            RuntimeError: If tar command fails.
        """
        if not file_paths:
            raise ValueError("No hay archivos para empaquetar.")

        # All files must be in the same directory; use that dir as -C
        work_dir = os.path.dirname(file_paths[0])
        basenames = " ".join(os.path.basename(f) for f in file_paths)

        if log_callback:
            log_callback(
                f"Creando bundle: {os.path.basename(bundle_path)} "
                f"({len(file_paths)} archivo(s))"
            )

        cmd = f"tar -cf {bundle_path} -C {work_dir} {basenames}"
        code, _, err = self._ssh.execute(cmd, timeout=120)
        if code != 0:
            raise RuntimeError(f"Error al crear el bundle tar: {err}")

        if log_callback:
            # Verify size for user feedback
            size_code, size_out, _ = self._ssh.execute(
                f"stat -c %s {bundle_path} 2>/dev/null"
            )
            if size_code == 0 and size_out.strip().isdigit():
                mb = int(size_out.strip()) / (1024 * 1024)
                log_callback(f"Bundle listo: {os.path.basename(bundle_path)} ({mb:.1f} MB)")
            else:
                log_callback(f"Bundle listo: {bundle_path}")

        return bundle_path

    def extract_on_server(
        self,
        bundle_path: str,
        extract_dir: str,
        log_callback: Callable[[str], None] | None = None,
    ) -> dict:
        """
        Extract a .tar bundle on the remote server to extract_dir.

        Returns a dict with detected artifact paths:
            {
              "dump":      "/tmp/.../odoo_bancasa_....dump"  or None,
              "filestore": "/tmp/.../filestore_....zip"      or None,
              "inventory": "/tmp/.../..._inventory.json"     or None,
            }

        Raises:
            RuntimeError: If tar extraction fails or bundle is not found.
        """
        if log_callback:
            log_callback(f"Extrayendo bundle: {os.path.basename(bundle_path)} -> {extract_dir}")

        # Ensure extract directory exists
        code, _, err = self._ssh.execute(f"mkdir -p {extract_dir}")
        if code != 0:
            raise RuntimeError(f"No se pudo crear el directorio de extraccion: {err}")

        code, _, err = self._ssh.execute(
            f"tar -xf {bundle_path} -C {extract_dir}", timeout=120
        )
        if code != 0:
            raise RuntimeError(f"Error al extraer el bundle: {err}")

        # List extracted files
        code, ls_out, _ = self._ssh.execute(f"ls {extract_dir}")
        extracted: dict[str, str | None] = {"dump": None, "filestore": None, "inventory": None}
        for name in (ls_out or "").strip().splitlines():
            name = name.strip()
            path = f"{extract_dir}/{name}"
            lower = name.lower()
            if lower.endswith(".dump") or lower.endswith(".sql"):
                extracted["dump"] = path
            elif lower.endswith(".zip"):
                extracted["filestore"] = path
            elif lower.endswith(".json"):
                extracted["inventory"] = path

        if log_callback:
            found = [k for k, v in extracted.items() if v]
            log_callback(f"Extraccion completa — encontrado: {', '.join(found) or 'ninguno'}")

        return extracted

    def read_inventory_from_server(self, remote_json_path: str) -> dict:
        """
        Read and parse the inventory JSON file from the remote server.

        Returns the parsed dict, or {} on any error.
        """
        sftp = self._ssh.open_sftp()
        try:
            with sftp.open(remote_json_path, "rb") as fh:
                raw = fh.read()
            return json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}
        finally:
            sftp.close()

    def cleanup_extract_dir(self, extract_dir: str) -> None:
        """Remove the temporary extraction directory from the server (best-effort)."""
        self._ssh.execute(f"rm -rf {extract_dir}")

    # ── Local operations ──────────────────────────────────────────────────

    @staticmethod
    def extract_local(
        bundle_path: str,
        extract_dir: str,
        log_callback: Callable[[str], None] | None = None,
    ) -> dict:
        """
        Extract a .tar bundle on the local machine using Python's tarfile module.

        Returns the same dict as extract_on_server:
            {"dump": path|None, "filestore": path|None, "inventory": path|None}

        Raises:
            FileNotFoundError: If bundle_path does not exist.
            RuntimeError: On extraction errors.
        """
        if not os.path.isfile(bundle_path):
            raise FileNotFoundError(f"Bundle no encontrado: {bundle_path}")

        os.makedirs(extract_dir, exist_ok=True)

        if log_callback:
            log_callback(f"Extrayendo bundle local: {os.path.basename(bundle_path)}")

        try:
            with tarfile.open(bundle_path, "r") as tf:
                tf.extractall(extract_dir)
        except tarfile.TarError as exc:
            raise RuntimeError(f"Error al extraer el bundle: {exc}") from exc

        extracted: dict[str, str | None] = {"dump": None, "filestore": None, "inventory": None}
        for name in os.listdir(extract_dir):
            path = os.path.join(extract_dir, name)
            lower = name.lower()
            if lower.endswith(".dump") or lower.endswith(".sql"):
                extracted["dump"] = path
            elif lower.endswith(".zip"):
                extracted["filestore"] = path
            elif lower.endswith(".json"):
                extracted["inventory"] = path

        if log_callback:
            found = [k for k, v in extracted.items() if v]
            log_callback(f"Extraccion local completa — encontrado: {', '.join(found) or 'ninguno'}")

        return extracted

    @staticmethod
    def bundle_name_for(db_name: str, timestamp: str) -> str:
        """
        Return the canonical bundle filename for a given DB and timestamp.

        Example: bundle_name_for("bancasa_prod", "2026-07-14_02-00")
                 -> "bancasa_prod_2026-07-14_02-00_obt.tar"
        """
        return f"{db_name}_{timestamp}_obt.tar"
