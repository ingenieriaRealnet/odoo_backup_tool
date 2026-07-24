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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .bundle_manager import BundleManager
from .notifier       import notify_error, notify_success

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
            # Back-fill any missing keys added in later versions.
            # If last_run_ts is None (rule never ran or was reset), stamp it
            # with the current time so the rule does NOT fire immediately on
            # app open — it will first run at the next scheduled time.
            _now_iso = datetime.now().isoformat(timespec="seconds")
            self._rules = [
                {**_default_rule(), **r,
                 "last_run_ts": r.get("last_run_ts") or _now_iso}
                for r in data.get("rules", [])
            ]
        except (json.JSONDecodeError, OSError):
            self._rules = []

    def _save(self) -> None:
        """
        Persist rules to disk. Must be called with self._lock held.

        Writes to a temp file then os.replace()'s it into place, so a write
        that lands mid-way through a scheduler-thread read/tick can never
        leave schedules.json truncated — an interrupted plain write would be
        silently treated as corrupt by _load() and reset every rule to empty.
        """
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp_path = _SCHEDULE_FILE.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump({"rules": self._rules}, fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, _SCHEDULE_FILE)

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
        burst_stagger_secs: int = 120,
        temp_registry=None,
    ) -> None:
        self._sched   = schedule_mgr
        self._profiles = profile_mgr
        self._q       = notify_queue
        self._interval = poll_interval

        # Shared with the GUI's manual backup flow — a single instance so
        # both writers see each other's registrations (two independent
        # RemoteTempRegistry instances would each overwrite the on-disk
        # manifest with only their own half, silently losing the other's
        # entries). Falls back to a private instance for standalone/test use.
        from .temp_registry import RemoteTempRegistry
        self._temp_registry = temp_registry or RemoteTempRegistry()
        self._stop    = threading.Event()
        self._paused  = threading.Event()   # set = paused, clear = running
        self._active_jobs: dict[str, threading.Thread] = {}
        self._jobs_lock = threading.Lock()

        # Delay between the starts of rules that become due in the SAME poll
        # tick (a "catch-up burst" — e.g. the tool was closed past several
        # rules' scheduled times and they all fire together on the next
        # tick after reopening). Confirmed in the 2026-07-16 stress-test log:
        # bancasa/equiredes/novalens all fired within the same minute, piled
        # onto the Drive upload semaphore together, and one (novalens) hung
        # for ~35 min before failing with a raw SSL error. Staggering the
        # start of each subsequent rule in the same burst spreads out when
        # each one reaches Phase B instead of all racing for the semaphore
        # at once. Manual "Ejecutar ahora" (run_rule_now) is NOT staggered —
        # only the automatic _tick() burst.
        self._burst_stagger_secs = burst_stagger_secs

        # Cache of DriveUploader instances keyed by (creds_path, folder_id).
        # Reusing instances avoids rebuilding the OAuth service and re-resolving
        # the target folder on every scheduled rule execution.
        self._drive_cache:      dict[tuple, object] = {}
        self._drive_cache_lock: threading.Lock      = threading.Lock()

        # Bounds Drive uploads across concurrently running rules. Phase A
        # (dump + filestore generation on the source server) still runs fully
        # in parallel between rules; only Phase B (Drive upload) is capped.
        # Uploading N clients to Drive at once was the root cause of the
        # throttling seen in the 2026-07-15 log (mega degraded to 0.6 MB/s
        # while competing with equiredes/novalens/variedades uploads).
        # 2 permits (not 1): a single rule's dump + filestore now upload in
        # parallel to each other (no more bundling for Drive destinations —
        # see the "Bundle" section below), so 1 permit would have forced
        # them to queue behind each other and lost that benefit. 2 still
        # bounds total cross-client concurrency far below the unbounded
        # free-for-all that caused the 2026-07-15 throttling.
        self._drive_upload_semaphore = threading.Semaphore(2)

        # Tracks rule labels currently streaming a file to Drive (inside the
        # semaphore-guarded section of _upload_one_to_drive). Lets the GUI
        # warn the user before starting a competing manual local download —
        # a local SFTP download from the same source server competed with
        # an in-flight Drive upload in the 2026-07-15 log (ARANZAZU download
        # degraded mega's upload to 0.3 MB/s).
        # Counter (not a set) — a single rule can have several files uploading
        # in parallel (dump + filestore + inventory); the label must stay
        # "active" until the last of them finishes, not the first.
        self._active_uploads:      dict[str, int] = {}
        self._active_uploads_lock: threading.Lock = threading.Lock()

        self._thread = threading.Thread(
            target=self._loop,
            name="backup-scheduler",
            daemon=True,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the scheduler loop."""
        self._thread.start()

    def has_active_jobs(self) -> bool:
        """True if any scheduled rule (Phase A or B) is currently running."""
        with self._jobs_lock:
            return any(t.is_alive() for t in self._active_jobs.values())

    def has_active_uploads(self) -> bool:
        """True if any scheduled rule is currently streaming a file to Drive."""
        with self._active_uploads_lock:
            return bool(self._active_uploads)

    def active_upload_labels(self) -> list[str]:
        """Labels of rules currently streaming a file to Drive (for warnings)."""
        with self._active_uploads_lock:
            return sorted(self._active_uploads)

    def _mark_upload_active(self, label: str, delta: int) -> None:
        """Internal: increment/decrement the active-upload counter for label."""
        with self._active_uploads_lock:
            count = self._active_uploads.get(label, 0) + delta
            if count > 0:
                self._active_uploads[label] = count
            else:
                self._active_uploads.pop(label, None)

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

        # Rules still actively running (e.g. a large upload that hasn't
        # finished, so its last_run_ts hasn't been updated yet) must NOT
        # count toward the burst-stagger index below. Confirmed in the
        # 2026-07-17 log: bancasa's job was still running hours after it
        # started, so it kept reappearing in the "due" list on every later
        # tick — inflating burst_index for equiredes/novalens/variedades
        # even though each of them was the ONLY genuinely new arrival at
        # its own tick, giving them a pointless 120s "en cola" delay.
        with self._jobs_lock:
            active_ids = {rid for rid, t in self._active_jobs.items() if t.is_alive()}

        due_rules = [
            r for r in rules
            if r.get("enabled") and self._is_due(r) and r["id"] not in active_ids
        ]

        for burst_index, rule in enumerate(due_rules):
            rule_id = rule["id"]
            with self._jobs_lock:
                if rule_id in self._active_jobs and self._active_jobs[rule_id].is_alive():
                    # Job already running for this rule — skip
                    continue
                # burst_index 0 starts immediately; each subsequent rule due
                # in this SAME tick is delayed by another _burst_stagger_secs
                # so a catch-up burst doesn't pile every rule onto the Drive
                # upload semaphore at once (see __init__ docstring above).
                delay = burst_index * self._burst_stagger_secs
                t = threading.Thread(
                    target=self._run_rule_after_delay,
                    args=(rule, delay),
                    name=f"sched-job-{rule_id[:8]}",
                    daemon=True,
                )
                self._active_jobs[rule_id] = t
            t.start()

    def _run_rule_after_delay(self, rule: dict, delay: float) -> None:
        """Sleep `delay` seconds (burst stagger), then run the rule."""
        if delay > 0:
            label = rule.get("label") or rule.get("db_name", rule["id"][:8])
            self._log(
                rule["id"],
                f"[{label}] En cola — varias reglas vencieron a la vez, "
                f"inicio programado en {delay}s para no saturar la subida a Drive.",
            )
            if self._stop.wait(timeout=delay):
                return  # scheduler was stopped while this job was queued
        self._run_rule(rule)

    def run_rule_now(self, rule: dict) -> bool:
        """
        Force-run a rule immediately, guarded by the same _active_jobs lock
        used by _tick(). Used by the GUI's "Ejecutar ahora" button so a
        manual trigger can never collide with an automatic run of the same
        rule — both used to be able to fire _run_rule concurrently for the
        same rule_id, racing on the same /tmp remote file paths (same-minute
        timestamps) and on cleanup_remote deleting a file the other job was
        still reading/uploading.

        Returns:
            True if a new job thread was started, False if the rule was
            already running (caller should tell the user to wait).
        """
        rule_id = rule["id"]
        with self._jobs_lock:
            if rule_id in self._active_jobs and self._active_jobs[rule_id].is_alive():
                return False
            t = threading.Thread(
                target=self._run_rule,
                args=(rule,),
                name=f"sched-manual-{rule_id[:8]}",
                daemon=True,
            )
            self._active_jobs[rule_id] = t
        t.start()
        return True

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
            # Defensive: _load() and add() always stamp last_run_ts, so None
            # should not occur in normal use. Don't fire to be safe.
            return False

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

    def _get_uploader(self, creds_path: str, folder_id: str):
        """
        Return a cached DriveUploader for (creds_path, folder_id).

        Reusing the same instance avoids rebuilding the OAuth service connection
        and re-resolving the Drive folder on every rule execution.
        """
        from .gdrive import DriveUploader
        key = (creds_path, folder_id)
        with self._drive_cache_lock:
            if key not in self._drive_cache:
                self._drive_cache[key] = DriveUploader(creds_path, folder_id)
            return self._drive_cache[key]

    def _run_rule(self, rule: dict) -> None:
        """
        Execute one backup job headlessly, reusing the same core classes
        as the interactive wizard (Tabs 1-5).

        Phases match _worker_backup in gui/app.py:
          Phase A: create dump + filestore zip on source server (non-retryable)
          Phase B: transfer to destination (retryable in manual mode; not here)
        """
        from .ssh_client        import SSHClient
        from .db_manager        import DBManager
        from .filestore_manager import FilestoreManager
        from .transfer          import TransferManager

        rule_id = rule["id"]
        label   = rule.get("label") or rule.get("db_name", rule_id[:8])
        remote_tmp: list[str] = []

        def log(msg: str) -> None:
            # Force [label] prefix on every line so concurrent client logs stay
            # identifiable when multiple rules run simultaneously. Avoids doubling
            # the prefix for outer calls that already include it explicitly.
            stripped = msg.lstrip()
            if not stripped.startswith(f"[{label}]"):
                msg = f"[{label}] {stripped}"
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

        # Tracks remote /tmp files this run creates so they get cleaned up
        # even if the run is interrupted (crash, force-close, exception)
        # before reaching its own cleanup code — see core/temp_registry.py.
        _registered: dict[str, str] = {}

        def _register(path: str, kind: str) -> None:
            entry_id = self._temp_registry.register(
                profile["host"], profile["port"], profile["user"], profile["password"],
                path, kind, label,
            )
            _registered[path] = entry_id

        def _unregister(path: str) -> None:
            entry_id = _registered.pop(path, None)
            if entry_id:
                self._temp_registry.unregister(entry_id)

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
                _register(dump_path, "dump")
                log(f"[{label}] Dump listo: {dump_path}")

            if rule.get("include_filestore") and rule.get("filestore_root"):
                fs_db   = rule.get("filestore_db") or db_name
                zip_name = f"filestore_{db_name}_{ts}.tar"
                zip_path = f"/tmp/{zip_name}"
                log(f"[{label}] Empaquetando filestore: {zip_path}")
                zip_path = fs_mgr.compress_filestore(
                    rule["filestore_root"], fs_db, zip_path,
                    log_callback=log,
                )
                remote_tmp.append(zip_path)
                _register(zip_path, "filestore")
                log(f"[{label}] Filestore comprimido: {zip_path}")

            if not remote_tmp:
                msg = "No hay nada que transferir (dump e filestore desactivados)."
                log(f"[{label}] {msg}")
                self._sched._update_result(rule_id, "error", msg)
                return

            # ── Phase B destination (resolved early — the bundle decision
            # below depends on it) ─────────────────────────────────────────
            dest_type = rule.get("dest_type", "gdrive")

            # ── Bundle: wrap all artifacts into a single .tar ─────────────────
            # Matches the bundle step in gui/app.py _worker_backup.
            # On failure we fall back silently to individual file transfer.
            #
            # Skipped entirely for Google Drive destinations: bundling forces
            # one long sequential upload stream instead of letting dump and
            # filestore upload to Drive in parallel (optimization plan item 4).
            # local/remote destinations keep the bundle — it is still useful
            # there as a single associated deliverable and those transfers
            # aren't bottlenecked by Drive's per-stream throughput.
            bm = BundleManager(ssh)
            inventory = {
                "db_name":    db_name,
                "created_at": ts,
                "files":      {os.path.basename(p): p for p in remote_tmp},
            }
            bundled = False
            if dest_type == "gdrive":
                # Still write the inventory JSON so it travels to Drive
                # alongside dump + filestore, just not wrapped in a .tar.
                inv_json_name   = f"{db_name}_{ts}_inventory.json"
                inv_remote_path = f"/tmp/{inv_json_name}"
                try:
                    bm.write_json_to_server(inventory, inv_remote_path)
                    remote_tmp.append(inv_remote_path)
                    _register(inv_remote_path, "inventory")
                except Exception as inv_exc:  # noqa: BLE001
                    log(f"[{label}] ADVERTENCIA: no se pudo escribir el inventario. ({inv_exc})")
            else:
                try:
                    bundle_name = BundleManager.bundle_name_for(db_name, ts)
                    bundle_path = f"/tmp/{bundle_name}"

                    # Write inventory JSON to server before bundling
                    inv_json_name  = f"{db_name}_{ts}_inventory.json"
                    inv_remote_path = f"/tmp/{inv_json_name}"
                    bm.write_json_to_server(inventory, inv_remote_path)
                    _register(inv_remote_path, "inventory")

                    all_files = remote_tmp + [inv_remote_path]
                    bm.create(bundle_path, all_files, log_callback=log)
                    _register(bundle_path, "bundle")

                    # Remove the individual files — bundle is the single deliverable
                    for f in all_files:
                        try:
                            db_mgr.cleanup_remote(f)
                            _unregister(f)
                        except Exception:  # noqa: BLE001
                            pass

                    remote_tmp = [bundle_path]
                    bundled = True
                    log(f"[{label}] Bundle listo: {bundle_name}")
                except Exception as bundle_exc:  # noqa: BLE001
                    log(f"[{label}] ADVERTENCIA: no se pudo crear bundle, se transferiran archivos individuales. ({bundle_exc})")

            # Resolve Drive credentials once — used both by the upload helper
            # and by the post-upload retention cleanup.
            creds_path = (
                rule.get("dest_gdrive_creds")
                or profile.get("gdrive_creds_path", "")
            )
            folder_id = (
                rule.get("dest_gdrive_folder_id")
                or profile.get("gdrive_folder_id", "")
            )

            def _upload_one_to_drive(remote_path: str) -> None:
                """Upload a single remote file to Drive via streaming SFTP
                (relay: server -> this machine -> Drive)."""
                filename   = os.path.basename(remote_path)
                uploader   = self._get_uploader(creds_path, folder_id)
                total_size = tm.get_remote_file_size(remote_path)
                sftp_sess, sftp_file = tm.open_remote_file(remote_path)
                try:
                    with self._drive_upload_semaphore:
                        self._mark_upload_active(label, +1)
                        try:
                            log(f"[{label}] Enviando a Drive (streaming): {filename}")
                            uploader.upload_stream(
                                sftp_file,
                                filename,
                                total_size=total_size,
                                log_callback=log,
                            )
                        finally:
                            self._mark_upload_active(label, -1)
                finally:
                    sftp_file.close()
                    sftp_sess.close()

            # Direct mode (server -> Drive, bypassing this machine's uplink —
            # see docs/diseno_upload_directo_drive.md) is opt-in per rule via
            # "upload_mode": "direct", and only ever used if check_viable()
            # confirms the server itself can reach Google and has/gets rclone.
            # Any failure here falls back to the relay path above for this
            # run — direct mode is never forced. No semaphore/mark_upload_active
            # needed: direct uploads don't consume this machine's bandwidth,
            # so they don't compete with relay uploads from other rules.
            use_direct = False
            if rule.get("upload_mode") == "direct" and dest_type not in ("local", "remote"):
                from .remote_upload_manager import RemoteUploadManager
                rum = RemoteUploadManager(
                    ssh, profile["user"], profile["password"],
                    temp_registry=self._temp_registry,
                )
                viable, why = rum.check_viable()
                if viable:
                    use_direct = True
                    log(f"[{label}] Subida directa servidor->Drive habilitada (rclone).")
                else:
                    log(f"[{label}] Modo directo no viable ({why}) — usando modo rele (streaming).")

            def _upload_one_to_drive_direct(remote_path: str) -> None:
                rum.upload_file(remote_path, creds_path, folder_id, label=label, log_callback=log)

            upload_fn = _upload_one_to_drive_direct if use_direct else _upload_one_to_drive

            if dest_type == "local":
                local_dir = rule.get("dest_local_dir", "")
                if not local_dir:
                    raise RuntimeError("Directorio local de destino no configurado.")
                os.makedirs(local_dir, exist_ok=True)
                for remote_path in remote_tmp:
                    filename = os.path.basename(remote_path)
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
                for remote_path in remote_tmp:
                    filename = os.path.basename(remote_path)
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
                if not creds_path or not folder_id:
                    raise RuntimeError(
                        "Credenciales o carpeta de Google Drive no configuradas."
                    )

                if len(remote_tmp) > 1:
                    # Multiple files — the normal case for Drive destinations
                    # now that bundling is skipped (dump + filestore + inventory
                    # upload independently). Upload them in parallel, bounded by
                    # _drive_upload_semaphore, to cut total time roughly in half.
                    log(f"[{label}] Subiendo {len(remote_tmp)} archivos a Drive en paralelo ...")
                    with ThreadPoolExecutor(
                        max_workers=len(remote_tmp),
                        thread_name_prefix=f"drive-upload-{rule_id[:6]}",
                    ) as pool:
                        futures = {
                            pool.submit(upload_fn, rp): rp
                            for rp in remote_tmp
                        }
                        for fut in as_completed(futures):
                            fut.result()  # re-raises any upload exception
                else:
                    upload_fn(remote_tmp[0])

                # Retention cleanup — applied once per rule after all uploads succeed.
                # Bundle naming: "{db_name}_{ts}_obt.tar" → prefix "{db_name}_".
                # Unbundled naming (Drive default, see above) has three distinct
                # prefixes since dump/filestore/inventory don't share one:
                #   odoo_{db}_{ts}.dump|sql, filestore_{db}_{ts}.tar|zip,
                #   {db}_{ts}_inventory.json — a single "{db_name}_" prefix
                #   would only ever match the inventory file and silently
                #   leave the actual backup artifacts un-retained.
                retention = int(rule.get("retention_days", 90))
                if retention > 0:
                    uploader = self._get_uploader(creds_path, folder_id)
                    prefixes = (
                        [f"{db_name}_"]
                        if bundled
                        else [f"odoo_{db_name}_", f"filestore_{db_name}_", f"{db_name}_"]
                    )
                    deleted = 0
                    for prefix in prefixes:
                        deleted += uploader.cleanup_old_files(prefix, retention, log_callback=log)
                    if deleted:
                        log(f"[{label}] Retencion: {deleted} archivo(s) eliminados de Drive.")

            # ── Cleanup server tmp files ──────────────────────────────────────
            if rule.get("cleanup_server"):
                for p in remote_tmp:
                    try:
                        db_mgr.cleanup_remote(p)
                        _unregister(p)
                        log(f"[{label}] Limpiado del servidor: {p}")
                    except Exception:  # noqa: BLE001
                        pass
            else:
                # User opted out of cleanup for this rule — these files are
                # being kept on the server on purpose, so stop tracking them:
                # the orphan sweep must never delete something intentionally
                # left in place.
                for p in remote_tmp:
                    _unregister(p)

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
