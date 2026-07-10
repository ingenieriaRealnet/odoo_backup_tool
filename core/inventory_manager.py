"""
Backup inventory: collects server-side metrics at backup time and saves them
as a JSON companion file alongside the dump.

During restoration, post_restore_check() loads the inventory to validate the
restored system against the known-good baseline, turning generic threshold
checks into exact comparisons against what was actually backed up.

Inventory structure
-------------------
{
  "meta":     { tool_version, timestamp, source_host, db_name, filestore_path }
  "database": { pg_version, size_bytes, size_human, table_count, active_users,
                admin_exists, installed_modules, total_attachments,
                filestore_attachments, row_counts: {table: count, ...} }
  "filestore": { path, exists, total_files, total_dirs, total_size_bytes,
                 size_human, zero_byte_files, top_level_dirs: [...] }
}
"""
from __future__ import annotations

import datetime
import json
import os
from typing import Callable

from .ssh_client import SSHClient


# Key tables measured during backup and compared after restore.
# Missing tables (modules not installed) are silently skipped.
_ROW_COUNT_TABLES: list[tuple[str, str]] = [
    ("res_users",        "Usuarios"),
    ("res_company",      "Companias"),
    ("res_partner",      "Contactos"),
    ("ir_attachment",    "Adjuntos (total)"),
    ("ir_module_module", "Modulos"),
    ("account_move",     "Asientos contables"),
    ("sale_order",       "Pedidos de venta"),
    ("purchase_order",   "Pedidos de compra"),
    ("stock_picking",    "Transferencias"),
    ("hr_employee",      "Empleados"),
    ("product_template", "Productos"),
]

# Local directory where inventories are stored when the backup destination
# is remote (so the inventory always stays on this machine and can be found
# via auto-detect when restoring from the same workstation).
_LOCAL_INVENTORY_DIR = os.path.join(
    os.path.expanduser("~"), ".odoo_backup_tool", "inventories"
)


class InventoryManager:
    """Collects and stores an Odoo system snapshot at backup time."""

    TOOL_VERSION = "1.0"

    def __init__(self, ssh: SSHClient) -> None:
        self._ssh = ssh

    # ── Collection ────────────────────────────────────────────────────────

    def collect(
        self,
        db_name: str,
        filestore_path: str | None = None,
        source_host: str = "",
        log_callback: Callable[[str], None] | None = None,
    ) -> dict:
        """
        Collect a full inventory snapshot from the source server.

        The method is read-only and runs in a few seconds. It is safe to
        call while Odoo is still running against the database.

        Args:
            db_name:        PostgreSQL database name to inventory.
            filestore_path: Absolute path of the DB filestore directory on the
                            source server.  None to skip filestore metrics.
            source_host:    Source server hostname (stored in metadata only).
            log_callback:   Optional function for progress messages.

        Returns:
            Inventory dict — pass to save() or save_local() to persist.
        """
        if log_callback:
            log_callback(f"Recopilando inventario de '{db_name}' ...")

        inventory = {
            "meta": {
                "tool_version":   self.TOOL_VERSION,
                "timestamp":      datetime.datetime.now().isoformat(timespec="seconds"),
                "source_host":    source_host,
                "db_name":        db_name,
                "filestore_path": filestore_path or "",
            },
            "database":  self._collect_db(db_name, log_callback),
            "filestore": (
                self._collect_filestore(filestore_path, log_callback)
                if filestore_path else {}
            ),
        }

        if log_callback:
            db = inventory["database"]
            fs = inventory["filestore"]
            log_callback(
                f"Inventario recopilado — "
                f"BD: {db.get('table_count', '?')} tablas, "
                f"{db.get('size_human', '?')}  |  "
                f"Filestore: {fs.get('total_files', '?')} archivos, "
                f"{fs.get('size_human', '?')}"
            )

        return inventory

    def _collect_db(
        self,
        db_name: str,
        log_callback: Callable[[str], None] | None = None,
    ) -> dict:
        """Run DB-side inventory queries. Each query is caught individually."""

        def _q(sql: str) -> str | None:
            """Run SQL, return stripped stdout or None on error."""
            code, out, _ = self._ssh.execute(
                f"sudo -u postgres psql -d {db_name} -t -c \"{sql}\" 2>/dev/null"
            )
            return out.strip() if code == 0 else None

        info: dict = {}

        # PostgreSQL server version
        code, pg_out, _ = self._ssh.execute(
            "sudo -u postgres psql -t -c \"SELECT version();\" 2>/dev/null"
        )
        info["pg_version"] = (
            " ".join(pg_out.strip().split()[:2]) if code == 0 and pg_out.strip()
            else "desconocida"
        )

        # Database size
        sz_raw = _q(f"SELECT pg_database_size('{db_name}')")
        if sz_raw and sz_raw.isdigit():
            sz = int(sz_raw)
            info["size_bytes"] = sz
            info["size_human"] = _human_size(sz)
        else:
            info["size_bytes"] = 0
            info["size_human"] = "desconocido"

        # Public table count
        tbl = _q("SELECT count(*) FROM pg_tables WHERE schemaname='public'")
        info["table_count"] = int(tbl) if tbl and tbl.isdigit() else 0

        # Active users
        usr = _q("SELECT count(*) FROM res_users WHERE active=true")
        info["active_users"] = int(usr) if usr and usr.isdigit() else 0

        # Admin user exists and is active
        adm = _q("SELECT count(*) FROM res_users WHERE login='admin' AND active=true")
        info["admin_exists"] = bool(adm and int(adm) > 0)

        # Installed modules
        mod = _q("SELECT count(*) FROM ir_module_module WHERE state='installed'")
        info["installed_modules"] = int(mod) if mod and mod.isdigit() else 0

        # ir_attachment: total and filestore-backed
        att_all = _q("SELECT count(*) FROM ir_attachment")
        info["total_attachments"] = int(att_all) if att_all and att_all.isdigit() else 0

        att_fs = _q(
            "SELECT count(*) FROM ir_attachment "
            "WHERE store_fname IS NOT NULL AND store_fname != ''"
        )
        info["filestore_attachments"] = int(att_fs) if att_fs and att_fs.isdigit() else 0

        # Per-table row counts (best-effort; missing tables silently omitted)
        row_counts: dict[str, int] = {}
        for table, _label in _ROW_COUNT_TABLES:
            raw = _q(f"SELECT count(*) FROM {table}")
            if raw is not None and raw.isdigit():
                row_counts[table] = int(raw)
        info["row_counts"] = row_counts

        return info

    def _collect_filestore(
        self,
        filestore_path: str,
        log_callback: Callable[[str], None] | None = None,
    ) -> dict:
        """Run filestore-side inventory commands. All errors are caught."""

        fs: dict = {"path": filestore_path}

        code, _, _ = self._ssh.execute(f"test -d {filestore_path}")
        if code != 0:
            fs["exists"] = False
            return fs

        fs["exists"] = True

        # File count
        code, fc, _ = self._ssh.execute(
            f"find {filestore_path} -type f 2>/dev/null | wc -l"
        )
        fs["total_files"] = int(fc.strip() or "0") if code == 0 else 0

        # Directory count (Odoo uses 2-char hash bucket dirs)
        code, dc, _ = self._ssh.execute(
            f"find {filestore_path} -type d 2>/dev/null | wc -l"
        )
        fs["total_dirs"] = int(dc.strip() or "0") if code == 0 else 0

        # Total size (du -sb is portable and fast across all distros)
        code, sz, _ = self._ssh.execute(
            f"du -sb {filestore_path} 2>/dev/null | cut -f1"
        )
        if code == 0 and sz.strip().isdigit():
            size_bytes = int(sz.strip())
            fs["total_size_bytes"] = size_bytes
            fs["size_human"] = _human_size(size_bytes)
        else:
            fs["total_size_bytes"] = 0
            fs["size_human"] = "desconocido"

        # Zero-byte files: indicator of truncated transfers or corrupt ZIP
        code, zb, _ = self._ssh.execute(
            f"find {filestore_path} -type f -size 0 2>/dev/null | wc -l"
        )
        fs["zero_byte_files"] = int(zb.strip() or "0") if code == 0 else 0

        # Top-level subdirectory names (informational — helps detect structural issues)
        code, ls, _ = self._ssh.execute(
            f"ls -1 {filestore_path} 2>/dev/null | head -60"
        )
        fs["top_level_dirs"] = (
            [d.strip() for d in ls.splitlines() if d.strip()] if code == 0 else []
        )

        return fs

    # ── Persistence ───────────────────────────────────────────────────────

    @staticmethod
    def save(inventory: dict, local_path: str) -> None:
        """
        Write inventory to a JSON file on the local machine.

        Args:
            inventory:  Dict returned by collect().
            local_path: Absolute path for the output file.
        """
        parent = os.path.dirname(local_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(local_path, "w", encoding="utf-8") as fh:
            json.dump(inventory, fh, indent=2, ensure_ascii=False)

    @staticmethod
    def load(local_path: str) -> dict:
        """
        Load an inventory JSON file from disk.

        Raises:
            FileNotFoundError: File does not exist.
            ValueError:        File is not valid JSON.
        """
        with open(local_path, encoding="utf-8") as fh:
            return json.load(fh)

    @staticmethod
    def companion_path(dump_local_path: str) -> str:
        """
        Derive the inventory filename for a given dump file.

        Convention:  odoo_bancasa.dump  →  odoo_bancasa_inventory.json

        Args:
            dump_local_path: Local path of the dump file (after download).

        Returns:
            Absolute path of the expected companion inventory file.
        """
        base = os.path.splitext(dump_local_path)[0]
        return base + "_inventory.json"

    @staticmethod
    def local_inventory_dir() -> str:
        """Return the local directory used to store inventories for remote backups."""
        return _LOCAL_INVENTORY_DIR

    # ── Comparison ────────────────────────────────────────────────────────

    @staticmethod
    def compare(baseline: dict, current: dict) -> dict:
        """
        Compare a current state snapshot against the backup baseline.

        Both dicts follow the same sub-structure as the inventory:
          { "database": {...}, "filestore": {...} }

        Thresholds
        ----------
        Tables           error < 80 %    warning < 95 %
        Active users     error = 0 when baseline > 0
        Installed mods   error < 80 %    warning < 95 %
        DB size          error < 50 %    warning < 70 %
        Row counts       warning < 80 %  (informational per table)
        Filestore files  error < 50 %    warning < 90 %
        Filestore size   error < 60 %    warning < 80 %

        Returns:
            dict with keys: errors (list), warnings (list), matches (list)
        """
        errors:   list[str] = []
        warnings: list[str] = []
        matches:  list[str] = []

        b = baseline.get("database", {})
        c = current.get("database", {})
        bf = baseline.get("filestore", {})
        cf = current.get("filestore", {})

        def _pct(cur: int | float, base: int | float) -> float:
            return (cur / base * 100) if base > 0 else 100.0

        # ── Table count ───────────────────────────────────────────────────
        b_tbl, c_tbl = b.get("table_count", 0), c.get("table_count", 0)
        if b_tbl > 0:
            p = _pct(c_tbl, b_tbl)
            if p < 80:
                errors.append(
                    f"Tablas publicas: {c_tbl} restauradas vs {b_tbl} en backup "
                    f"({p:.1f}%) — perdida significativa de estructura."
                )
            elif p < 95:
                warnings.append(
                    f"Tablas publicas: {c_tbl} vs {b_tbl} en backup ({p:.1f}%)."
                )
            else:
                matches.append(f"Tablas: {c_tbl}/{b_tbl} ({p:.1f}%). OK")

        # ── Active users ──────────────────────────────────────────────────
        b_usr, c_usr = b.get("active_users", 0), c.get("active_users", 0)
        if b_usr > 0:
            if c_usr == 0:
                errors.append(
                    f"Usuarios activos: 0 restaurados vs {b_usr} en backup."
                )
            elif c_usr < b_usr:
                warnings.append(
                    f"Usuarios activos: {c_usr} vs {b_usr} en backup."
                )
            else:
                matches.append(f"Usuarios activos: {c_usr}/{b_usr}. OK")

        # ── Admin user ────────────────────────────────────────────────────
        if b.get("admin_exists") and not c.get("admin_exists"):
            errors.append(
                "Usuario 'admin' existia en el backup pero no aparece tras la restauracion."
            )
        elif b.get("admin_exists") and c.get("admin_exists"):
            matches.append("Usuario admin: presente. OK")

        # ── Installed modules ─────────────────────────────────────────────
        b_mod, c_mod = b.get("installed_modules", 0), c.get("installed_modules", 0)
        if b_mod > 0:
            p = _pct(c_mod, b_mod)
            if p < 80:
                errors.append(
                    f"Modulos instalados: {c_mod} vs {b_mod} en backup ({p:.1f}%)."
                )
            elif p < 95:
                warnings.append(
                    f"Modulos instalados: {c_mod} vs {b_mod} en backup ({p:.1f}%)."
                )
            else:
                matches.append(f"Modulos: {c_mod}/{b_mod} ({p:.1f}%). OK")

        # ── Database size ─────────────────────────────────────────────────
        b_sz, c_sz = b.get("size_bytes", 0), c.get("size_bytes", 0)
        if b_sz > 0 and c_sz > 0:
            p = _pct(c_sz, b_sz)
            b_h, c_h = _human_size(b_sz), _human_size(c_sz)
            if p < 50:
                errors.append(
                    f"Tamano BD: {c_h} restaurado vs {b_h} en backup ({p:.1f}%) "
                    "— posible perdida de datos significativa."
                )
            elif p < 70:
                warnings.append(
                    f"Tamano BD: {c_h} vs {b_h} en backup ({p:.1f}%)."
                )
            else:
                matches.append(f"Tamano BD: {c_h}/{b_h} ({p:.1f}%). OK")

        # ── Per-table row counts ──────────────────────────────────────────
        b_rows = b.get("row_counts", {})
        c_rows = c.get("row_counts", {})
        for table, label in _ROW_COUNT_TABLES:
            b_r = b_rows.get(table)
            c_r = c_rows.get(table)
            if b_r is None or c_r is None or b_r == 0:
                continue
            p = _pct(c_r, b_r)
            if p < 80:
                warnings.append(
                    f"{label} ({table}): {c_r:,} registros vs {b_r:,} en backup "
                    f"({p:.1f}%)."
                )
            else:
                matches.append(f"{label}: {c_r:,}/{b_r:,} ({p:.1f}%). OK")

        # ── Filestore file count ──────────────────────────────────────────
        b_fc = bf.get("total_files", 0)
        c_fc = cf.get("total_files", 0) if cf else 0
        if b_fc > 0:
            p = _pct(c_fc, b_fc)
            if p < 50:
                errors.append(
                    f"Archivos filestore: {c_fc:,} vs {b_fc:,} en backup ({p:.1f}%) "
                    "— mas de la mitad de los archivos estan ausentes."
                )
            elif p < 90:
                warnings.append(
                    f"Archivos filestore: {c_fc:,} vs {b_fc:,} en backup ({p:.1f}%)."
                )
            else:
                matches.append(f"Archivos filestore: {c_fc:,}/{b_fc:,} ({p:.1f}%). OK")

        # ── Filestore directory count ─────────────────────────────────────
        b_dc = bf.get("total_dirs", 0)
        c_dc = cf.get("total_dirs", 0) if cf else 0
        if b_dc > 0 and c_dc > 0:
            p = _pct(c_dc, b_dc)
            if p < 70:
                warnings.append(
                    f"Directorios filestore: {c_dc} vs {b_dc} en backup ({p:.1f}%)."
                )
            else:
                matches.append(f"Directorios filestore: {c_dc}/{b_dc} ({p:.1f}%). OK")

        # ── Filestore size ────────────────────────────────────────────────
        b_fsz = bf.get("total_size_bytes", 0)
        c_fsz = cf.get("total_size_bytes", 0) if cf else 0
        if b_fsz > 0 and c_fsz > 0:
            p = _pct(c_fsz, b_fsz)
            b_h, c_h = _human_size(b_fsz), _human_size(c_fsz)
            if p < 60:
                errors.append(
                    f"Tamano filestore: {c_h} vs {b_h} en backup ({p:.1f}%)."
                )
            elif p < 80:
                warnings.append(
                    f"Tamano filestore: {c_h} vs {b_h} en backup ({p:.1f}%)."
                )
            else:
                matches.append(f"Tamano filestore: {c_h}/{b_h} ({p:.1f}%). OK")

        return {"errors": errors, "warnings": warnings, "matches": matches}


# ── Utilities ─────────────────────────────────────────────────────────────────

def _human_size(size_bytes: int | float) -> str:
    """Convert bytes to a human-readable string (B / KB / MB / GB / TB)."""
    val = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if val < 1024 or unit == "TB":
            return f"{val:.1f} {unit}"
        val /= 1024
    return f"{val:.1f} TB"
