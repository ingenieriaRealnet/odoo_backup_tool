"""
Backup scheduler for the Odoo Backup Tool.

Two components:
  - ScheduleManager  : CRUD for scheduled-backup rules; persists to schedules.json.
  - BackupScheduler  : Daemon thread that polls rules every 60 s and fires backups.

The BackupScheduler reuses the same core classes (SSHClient, DBManager,
FilestoreManager, TransferManager, DriveUploader) as the interactive wizard,
but runs entirely headless — no Tkinter interaction required.

Callers pass a notify_queue (queue.Queue) so the GUI can refresh the automation
tab without polling the disk on every tick. Events placed on the queue follow the
same pattern used throughout gui/app.py:
    ("sched_refresh", list_of_rules)
    ("sched_log",     (rule_id, message))
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .notifier import notify_error, notify_success

logger = logging.getLogger(__name__)

# ── Storage paths ─────────────────────────────────────────────────────────────
_DATA_DIR      = Path.home() / ".odoo_backup_tool"
_SCHEDULE_FILE = _DATA_DIR / "schedules.json"

# ── Backup naming helpers ─────────────────────────────────────────────────────
_TIMESTAMP_FMT = "%Y-%m-%d_%H-%M"


def _ts_now() -> str:
    return datetime.now().strftime(_TIMESTAMP_FMT)


# ── Default rule template ─────────────────────────────────────────────────────

def _default_rule() -> dict:
    return {
        "id":                  str(uuid.uuid4()),
        "enabled":             True,
        "label":               "",
        "server_profile":      "",
        "db_name":             "",
        "db_format":           "dump",
        "include_db":          True,
        "include_filestore":   True,
        "filestore_root":      "",
        "filestore_db":        "",
        "dest_type":           "gdrive",
        "dest_local_dir":      "",
        "dest_remote_profile": "",
        "dest_remote_dir":     "",
        "dest_gdrive_creds":   "",
        "dest_gdrive_folder_id": "",
        "schedule_hour":       2,
        "schedule_minute":     0,
        "retention_days":      90,
        "cleanup_server":      True,
        "last_run_ts":         None,
        "last_result":         None,
        "last_message":        "",
    }


# =============================================================================
# ScheduleManager
# =============================================================================

class ScheduleManager:
    """
    CRUD for scheduled-backup rules.

    Thread-safe: all mutations are protected by a single lock so the GUI
    and the BackupScheduler can read/write concurrently without corruption.
    """

    def __init__(self) -> None:
        self._lock    = threading.Lock()
        self._rules:  list[dict] = []
        self._load()

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load(self) -> None:
        """Load rules from disk. Silently starts empty on any error."""
        if not _SCHEDULE_FILE.exists():
            return
        try:
            with open(_SCHEDULE_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            # Back-fill any missing keys added in later versions
            self._rules = [
                {**_default_rule(), **r} for r in data.get("rules", [])
            ]
        except (json.JSONDecodeError, OSError):
            self._rules = []

    def _save(self) -> None:
        """Persist rules to disk. Must be called with self._lock held."""
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(_SCHEDULE_FILE, "w", encoding="utf-8") as fh:
            json.dump({"rules": self._rules}, fh, indent=2, ensure_ascii=False)

    # ── Public API ────────────────────────────────────────────────────────────

    def list_rules(self) -> list[dict]:
        """Return a shallow copy of all rules (safe for GUI iteration)."""
        with self._lock:
            return [dict(r) for r in self._rules]

    def get(self, rule_id: str) -> dict | None:
        """Return a copy of the rule with the given id, or None."""
        with self._lock:
            for r in self._rules:
                if r["id"] == rule_id:
                    return dict(r)
        return None

    def add(self, partial: dict) -> dict:
        """
        Create a new rule by merging partial into the default template.

        Returns the complete rule dict (with generated id).
        """
        rule = {**_default_rule(), **partial}
        rule["id"] = str(uuid.uuid4())   # always generate a fresh id
        with self._lock:
            self._rules.append(rule)
            self._save()
        return dict(rule)

    def update(self, rule_id: str, partial: dict) -> bool:
        """
        Update an existing rule in place.

        Returns True if the rule was found and updated.
        """
        with self._lock:
            for i, r in enumerate(self._rules):
                if r["id"] == rule_id:
                    self._rules[i] = {**r, **partial}
                    self._save()
                    return True
        return False

    def delete(self, rule_id: str) -> bool:
        """
        Remove the rule with the given id.

        Returns True if removed.
        """
        with self._lock:
            before = len(self._rules)
            self._rules = [r for r in self._rules if r["id"] != rule_id]
            if len(self._rules) < before:
                self._save()
                return True
        return False

    def _update_result(
        self,
        rule_id: str,
        result:  str,
        message: str,
    ) -> None:
        """Internal: record last_run_ts / last_result / last_message."""
        self.update(rule_id, {
            "last_run_ts":  datetime.now().isoformat(timespec="seconds"),
            "last_result":  result,
            "last_message": message,
        })


# =============================================================================
# BackupScheduler
# =============================================================================

class BackupScheduler:
    """
    Daemon thread that checks schedule rules every 60 seconds and fires
    headless backup jobs for any rule that is due.

    Args:
        schedule_mgr:  Shared ScheduleManager instance.
        profile_mgr:   ProfileManager instance (to resolve server credentials).
        notify_queue:  queue.Queue shared with the GUI for status push.
        poll_interval: Seconds between schedule checks (default 60).
    """

    def __init__(
        self,
        schedule_mgr,
        profile_mgr,
        notify_queue: queue.Queue,
        poll_interval: int = 60,
    ) -> None:
        self._sched   = schedule_mgr
        self._profiles = profile_mgr
        self._q       = notify_queue
        self._interval = poll_interval
        self._stop    = threading.Event()
        self._paused  = threading.Event()   # set = paused, clear = running
        self._active_jobs: dict[str, threading.Thread] = {}
        self._jobs_lock = threading.Lock()

        self._thread = threading.Thread(
            target=self._loop,
            name="backup-scheduler",
            daemon=True,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the scheduler loop."""
        self._thread.start()

    def stop(self) -> None:
        """Signal the scheduler to stop and wait briefly for it."""
        self._stop.set()

    def pause(self) -> None:
        """Pause scheduling (in-progress jobs continue until they finish)."""
        self._paused.set()

    def resume(self) -> None:
        """Resume scheduling after a pause."""
        self._paused.clear()

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    # ── Scheduler loop ────────────────────────────────────────────────────────

    def _loop(self) -> None:
        """Main loop: check rules every poll_interval seconds."""
        while not self._stop.wait(timeout=self._interval):
            if self._paused.is_set():
                continue
            self._tick()

    def _tick(self) -> None:
        """Evaluate all rules; fire any that are due and not already running."""
        rules = self._sched.list_rules()
        for rule in rules:
            if not rule.get("enabled"):
                continue
            if not self._is_due(rule):
                continue
            rule_id = rule["id"]
            with self._jobs_lock:
                if rule_id in self._active_jobs and self._active_jobs[rule_id].is_alive():
                    # Job already running for this rule — skip
                    continue
                t = threading.Thread(
                    target=self._run_rule,
                    args=(rule,),
                    name=f"sched-job-{rule_id[:8]}",
                    daemon=True,
                )
                self._active_jobs[rule_id] = t
            t.start()

    # ── Due-date logic ────────────────────────────────────────────────────────

    @staticmethod
    def _is_due(rule: dict) -> bool:
        """
        Return True when the rule should fire now.

        A daily rule is due if:
          - It has never run (last_run_ts is None), OR
          - The last run was on a previous calendar day
        AND the current time is >= the configured schedule_hour:schedule_minute.
        """
        now = datetime.now()
        due_time = now.replace(
            hour=int(rule.get("schedule_hour", 2)),
            minute=int(rule.get("schedule_minute", 0)),
            second=0,
            microsecond=0,
        )
        if now < due_time:
            return False

        last_ts = rule.get("last_run_ts")
        if last_ts is None:
            return True

        try:
            last_run = datetime.fromisoformat(last_ts)
        except (ValueError, TypeError):
            return True

        # Due if last run was before today's date
        return last_run.date() < now.date()

    # ── Headless backup execution ─────────────────────────────────────────────

    def _log(self, rule_id: str, message: str) -> None:
        """Push a log line to the GUI queue (non-blocking)."""
        try:
            self._q.put_nowait(("sched_log", (rule_id, message)))
        except Exception:  # noqa: BLE001
            pass

    def _refresh(self) -> None:
        """Push a full rule refresh to the GUI queue."""
        try:
            self._q.put_nowait(("sched_refresh", self._sched.list_rules()))
        except Exception:  # noqa: BLE001
            pass

    def _run_rule(self, rule: dict) -> None:
        """
        Execute one backup job headlessly, reusing the same core classes
        as the interactive wizard (Tabs 1-5).

        Phases match _worker_backup in gui/app.py:
          Phase A: create dump + filestore zip on source server (non-retryable)
          Phase B: transfer to destination (retryable in manual mode; not here)
        """
        from .ssh_client          import SSHClient
        from .db_manager          import DBManager
        from .filestore_manager   import FilestoreManager
        from .transfer            import TransferManager
        from .gdrive              import DriveUploader

        rule_id = rule["id"]
        label   = rule.get("label") or rule.get("db_name", rule_id[:8])
        remote_tmp: list[str] = []

        def log(msg: str) -> None:
            self._log(rule_id, msg)

        log(f"[{label}] Iniciando backup programado — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

        # ── Resolve server credentials from profile ───────────────────────────
        profile_name = rule.get("server_profile", "")
        profile = self._profiles.get(profile_name) if profile_name else None
        if not profile:
            msg = f"Perfil '{profile_name}' no encontrado. Cancelando."
            log(f"[{label}] ERROR: {msg}")
            self._sched._update_result(rule_id, "error", msg)
            notify_error(label, msg)
            self._refresh()
            return

        ssh = SSHClient()
        try:
            ssh.connect(
                profile["host"],
                profile["port"],
                profile["user"],
                profile["password"],
            )
        except Exception as exc:
            msg = f"No se pudo conectar a {profile['host']}: {exc}"
            log(f"[{label}] ERROR: {msg}")
            self._sched._update_result(rule_id, "error", msg)
            notify_error(label, msg)
            self._refresh()
            return

        db_mgr = DBManager(ssh)
        fs_mgr = FilestoreManager(ssh)
        tm     = TransferManager(ssh)

        try:
            # ── Phase A: create files on server ──────────────────────────────
            db_name = rule["db_name"]
            ts      = _ts_now()

            if rule.get("include_db"):
                fmt       = rule.get("db_format", "dump")
                ext       = "dump" if fmt == "dump" else "sql"
                dump_name = f"odoo_{db_name}_{ts}.{ext}"
                dump_path = f"/tmp/{dump_name}"
                log(f"[{label}] Generando dump: {dump_path}")
                dump_path = db_mgr.create_dump(
                    db_name, fmt, dump_path,
                    log_callback=log,
                )
                remote_tmp.append(dump_path)
                log(f"[{label}] Dump listo: {dump_path}")

            if rule.get("include_filestore") and rule.get("filestore_root"):
                fs_db   = rule.get("filestore_db") or db_name
                zip_name = f"filestore_{db_name}_{ts}.zip"
                zip_path = f"/tmp/{zip_name}"
                log(f"[{label}] Comprimiendo filestore: {zip_path}")
                zip_path = fs_mgr.compress_filestore(
                    rule["filestore_root"], fs_db, zip_path,
                    log_callback=log,
                )
                remote_tmp.append(zip_path)
                log(f"[{label}] Filestore comprimido: {zip_path}")

            if not remote_tmp:
                msg = "No hay nada que transferir (dump e filestore desactivados)."
                log(f"[{label}] {msg}")
                self._sched._update_result(rule_id, "error", msg)
                return

            # ── Phase B: transfer to destination ─────────────────────────────
            dest_type = rule.get("dest_type", "gdrive")

            for remote_path in remote_tmp:
                filename = os.path.basename(remote_path)

                if dest_type == "local":
                    local_dir = rule.get("dest_local_dir", "")
                    if not local_dir:
                        raise RuntimeError("Directorio local de destino no configurado.")
                    os.makedirs(local_dir, exist_ok=True)
                    log(f"[{label}] Descargando {filename} -> {local_dir}")
                    tm.download_to_local(remote_path, local_dir, log_callback=log)

                elif dest_type == "remote":
                    dest_profile_name = rule.get("dest_remote_profile", "")
                    dest_prof = self._profiles.get(dest_profile_name) if dest_profile_name else None
                    if not dest_prof:
                        raise RuntimeError(
                            f"Perfil de servidor destino '{dest_profile_name}' no encontrado."
                        )
                    dest_dir = rule.get("dest_remote_dir", "/tmp")
                    log(f"[{label}] Transfiriendo {filename} -> {dest_prof['host']}:{dest_dir}")
                    tm.transfer_to_server(
                        remote_path,
                        dest_prof["host"],
                        dest_prof["port"],
                        dest_prof["user"],
                        dest_prof["password"],
                        dest_dir,
                        log_callback=log,
                    )

                else:
                    # Default: Google Drive streaming
                    creds_path = (
                        rule.get("dest_gdrive_creds")
                        or profile.get("gdrive_creds_path", "")
                    )
                    folder_id = (
                        rule.get("dest_gdrive_folder_id")
                        or profile.get("gdrive_folder_id", "")
                    )
                    if not creds_path or not folder_id:
                        raise RuntimeError(
                            "Credenciales o carpeta de Google Drive no configuradas."
                        )

                    uploader = DriveUploader(creds_path, folder_id)
                    total_size = tm.get_remote_file_size(remote_path)
                    sftp_sess, sftp_file = tm.open_remote_file(remote_path)
                    try:
                        log(f"[{label}] Enviando a Drive (streaming): {filename}")
                        uploader.upload_stream(
                            sftp_file,
                            filename,
                            total_size=total_size,
                            log_callback=log,
                        )
                    finally:
                        sftp_file.close()
                        sftp_sess.close()

                    # Retention cleanup
                    retention = int(rule.get("retention_days", 90))
                    if retention > 0:
                        prefix = filename.split("_")[0] + "_" + db_name + "_"
                        deleted = uploader.cleanup_old_files(
                            prefix, retention, log_callback=log
                        )
                        if deleted:
                            log(f"[{label}] Retencion: {deleted} archivo(s) eliminados de Drive.")

            # ── Cleanup server tmp files ──────────────────────────────────────
            if rule.get("cleanup_server"):
                for p in remote_tmp:
                    try:
                        db_mgr.cleanup_remote(p)
                        log(f"[{label}] Limpiado del servidor: {p}")
                    except Exception:  # noqa: BLE001
                        pass

            msg = f"Backup completado exitosamente — {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            log(f"[{label}] {msg}")
            self._sched._update_result(rule_id, "ok", msg)
            notify_success(label, f"Backup diario completado\n{db_name}")

        except Exception as exc:
            msg = str(exc)
            log(f"[{label}] ERROR: {msg}")
            self._sched._update_result(rule_id, "error", msg)
            notify_error(label, f"Error en backup de {db_name}:\n{msg[:120]}")

        finally:
            try:
                ssh.close()
            except Exception:  # noqa: BLE001
                pass
            self._refresh()
