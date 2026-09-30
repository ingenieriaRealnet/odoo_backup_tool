"""
HealthMonitor — orchestrates probing, evaluation and alerting for every
client that has a scheduled-backup rule.

It rides on the scheduler that already exists: BackupScheduler calls
maybe_run_cycle() on each of its 60 s ticks, and ScheduleManager reports
every backup result through record_backup_result(). No extra daemon, no
extra credentials — the same saved profiles the backups use.

Two processes may have the tool open at once (the GUI and the headless
monitor, see main.py --headless). Only the one holding the scheduler lock is
the "owner": it writes monitor_state.json and sends alerts. A non-owner may
still probe on demand and read everything to draw the dashboard.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Callable

from .alerting import AlertManager, MonitorSettings
from .health_eval import evaluate_client
from .health_probe import HealthStore, probe_server
from .notifier import notify_action_required

_DATA_DIR = Path.home() / ".odoo_backup_tool"

# How often a tick actually does work. Probes have their own, much longer
# interval (settings["probe_interval_hours"]); this only bounds how late a
# digest or a staleness warning can be.
_CYCLE_MIN_INTERVAL_SECS = 600

# Samples fed to the evaluator: a little more than its 14-day trend window.
_EVAL_WINDOW_DAYS = 16

# Probing is I/O bound (SSH); a small pool keeps a slow or dead server from
# delaying the others without opening a burst of connections.
_MAX_PARALLEL_PROBES = 4


class HealthMonitor:
    """
    Args:
        schedule_mgr: ScheduleManager (source of the rules = monitored clients).
        profile_mgr:  ProfileManager (server credentials).
        notify_queue: Optional queue.Queue shared with the GUI; receives
                      ("health_refresh", summaries) and ("log", text).
        history:      Optional HistoryManager, used once to seed each rule's
                      backup track record from runs that predate the monitor.
        owner:        True if this process holds the scheduler lock.
        data_dir:     Override of ~/.odoo_backup_tool (tests).
        probe:        Override of probe_server (tests).
    """

    def __init__(
        self,
        schedule_mgr,
        profile_mgr,
        notify_queue: queue.Queue | None = None,
        history=None,
        owner: bool = True,
        data_dir: str | os.PathLike | None = None,
        probe: Callable[..., dict] = probe_server,
        alert_manager: AlertManager | None = None,
    ) -> None:
        base = Path(data_dir) if data_dir else _DATA_DIR
        self._sched = schedule_mgr
        self._profiles = profile_mgr
        self._q = notify_queue
        self.owner = owner
        self._probe = probe
        self._state_path = base / "monitor_state.json"
        self.store = HealthStore(base / "health_metrics.jsonl")
        self.settings = MonitorSettings(base / "monitor_settings.json")
        self._alerts = alert_manager or AlertManager(toast=notify_action_required, log=self._log)

        self._state_lock = threading.Lock()
        self._state_mtime = 0.0
        self._state: dict = {"rules": {}, "alerts": {}, "last_digest_date": None}
        self._load_state()

        # Serializes cycles: a manual "Sondear ahora", a scheduled cycle and
        # a just-finished backup must not evaluate/alert concurrently.
        self._cycle_lock = threading.Lock()
        self._last_cycle_started = 0.0

        # A viewer seeds its in-memory copy too (so its dashboard is right
        # even before any owner has ever run); _save_state() is a no-op for
        # it, so nothing is written.
        if history is not None:
            self._bootstrap_from_history(history)

    # ── State persistence ────────────────────────────────────────────────────

    def _load_state(self) -> None:
        try:
            with open(self._state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._state_mtime = self._state_path.stat().st_mtime
        except (OSError, json.JSONDecodeError):
            return
        for key in ("rules", "alerts"):
            data.setdefault(key, {})
        self._state = data

    def _save_state(self) -> None:
        """Atomic write; must be called with self._state_lock held."""
        if not self.owner:
            return  # a viewer never writes the owner's state
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._state_path.with_suffix(".json.tmp")
        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(self._state, fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, self._state_path)
        self._state_mtime = self._state_path.stat().st_mtime

    def become_owner(self, history=None) -> None:
        """
        Promote a viewer to owner — the GUI calls this when it acquires the
        scheduler lock after the headless monitor that held it has exited.
        """
        with self._state_lock:
            self._load_state()   # pick up whatever the previous owner wrote
            self.owner = True
        if history is not None:
            self._bootstrap_from_history(history)

    def _refresh_state_if_viewer(self) -> None:
        """A non-owner re-reads the state the owner keeps writing."""
        if self.owner:
            return
        try:
            if self._state_path.stat().st_mtime != self._state_mtime:
                self._load_state()
        except OSError:
            pass

    def _bootstrap_from_history(self, history) -> None:
        """
        Seed the backup track record of rules the monitor has never seen,
        from the existing run history.

        Without this, a rule that has been failing for weeks would start at
        "0 consecutive failures" the day the monitor is first deployed —
        exactly the situation of Variedades and Mega on 2026-09-30.
        """
        try:
            entries = [e for e in history.list_entries() if e.get("action_type") == "backup_scheduled"]
        except Exception:  # noqa: BLE001 — history is a convenience, never a blocker
            return
        entries.sort(key=lambda e: e.get("ended_at", 0))

        changed = False
        with self._state_lock:
            for rule in self._sched.list_rules():
                if rule["id"] in self._state["rules"]:
                    continue
                label = rule.get("label") or rule.get("db_name", "")
                runs = [e for e in entries if e.get("server_label") == label]
                if not runs:
                    continue
                record = {"consecutive_failures": 0, "last_ok_ts": None,
                          "last_run_ts": None, "last_status": None, "last_error": ""}
                for run in runs:
                    self._apply_result(record, run.get("status"), run.get("summary", ""), run.get("ended_at"))
                self._state["rules"][rule["id"]] = record
                changed = True
            if changed:
                self._save_state()

    # ── Backup results ───────────────────────────────────────────────────────

    @staticmethod
    def _apply_result(record: dict, status: str | None, message: str, when: float | None) -> None:
        record["last_run_ts"] = when
        record["last_status"] = status
        if status == "ok":
            record["consecutive_failures"] = 0
            record["last_ok_ts"] = when
            record["last_error"] = ""
        else:
            record["consecutive_failures"] = int(record.get("consecutive_failures") or 0) + 1
            record["last_error"] = (message or "")[:500]

    def _reconcile_with_schedule(self) -> None:
        """
        Apply run results the monitor did not witness.

        record_backup_result() only sees runs executed while this monitor is
        wired to the scheduler. A run recorded by anything else — a previous
        version of the tool, another instance — still updates the rule's
        last_run_ts/last_result in schedules.json. Found on 2026-09-30: the
        old .exe completed backup_variedades at 11:42, and without this the
        new version kept counting 13 consecutive failures and would have
        raised a false critical alert.
        """
        changed = False
        with self._state_lock:
            for rule in self._sched.list_rules():
                record = self._state["rules"].get(rule["id"])
                result = rule.get("last_result")
                if record is None or result not in ("ok", "error"):
                    continue  # never seen (bootstrap covers it) or never ran
                try:
                    when = datetime.fromisoformat(rule.get("last_run_ts") or "").timestamp()
                except ValueError:
                    continue
                # record_backup_result() stamps its own time AFTER the rule's
                # (second-precision) last_run_ts, so a run the monitor did see
                # never matches here and is not counted twice.
                if when > float(record.get("last_run_ts") or 0) + 1:
                    self._apply_result(record, result, rule.get("last_message", ""), when)
                    changed = True
            if changed:
                self._save_state()

    def record_backup_result(self, rule_id: str, result: str, message: str) -> None:
        """
        Hook for ScheduleManager.on_result: called after every scheduled run.

        Updates the rule's track record and re-evaluates right away, so a
        failure that crosses a threshold alerts now rather than at the next
        periodic cycle.
        """
        with self._state_lock:
            record = self._state["rules"].setdefault(rule_id, {
                "consecutive_failures": 0, "last_ok_ts": None,
                "last_run_ts": None, "last_status": None, "last_error": "",
            })
            self._apply_result(record, result, message, time.time())
            self._save_state()
        threading.Thread(
            target=self.run_cycle, kwargs={"probe": False},
            name="health-after-backup", daemon=True,
        ).start()

    # ── Evaluation ───────────────────────────────────────────────────────────

    def summaries(self) -> list[dict]:
        """Evaluate every rule from stored data (no network access)."""
        self._refresh_state_if_viewer()
        self._reconcile_with_schedule()
        now = time.time()
        settings = self.settings.get()
        samples = self.store.by_profile(since_ts=now - _EVAL_WINDOW_DAYS * 86400)
        with self._state_lock:
            rule_states = json.loads(json.dumps(self._state["rules"]))
        return [
            evaluate_client(
                rule, rule_states.get(rule["id"]),
                samples.get(rule.get("server_profile", ""), []),
                settings.get("thresholds"), now,
            )
            for rule in self._sched.list_rules()
        ]

    # ── Cycle ────────────────────────────────────────────────────────────────

    def maybe_run_cycle(self) -> None:
        """
        Cheap entry point for the scheduler tick (every 60 s): starts a
        cycle in the background at most every _CYCLE_MIN_INTERVAL_SECS.
        """
        if not self.owner:
            return
        now = time.monotonic()
        if now - self._last_cycle_started < _CYCLE_MIN_INTERVAL_SECS:
            return
        self._last_cycle_started = now
        threading.Thread(target=self.run_cycle, name="health-cycle", daemon=True).start()

    def run_cycle(self, probe: bool = True, force: bool = False) -> list[dict]:
        """
        Probe the servers that are due, evaluate, alert and publish.

        Args:
            probe: False to only re-evaluate stored data (after a backup).
            force: True to probe every server regardless of the interval
                   (the dashboard's "Sondear ahora").

        Returns:
            The fresh list of per-client summaries.
        """
        with self._cycle_lock:
            settings = self.settings.get()
            if not settings.get("enabled", True) and not force:
                return []
            try:
                if probe:
                    self._probe_due_servers(settings, force)
                summaries = self.summaries()
                if self.owner:
                    with self._state_lock:
                        self._alerts.process(summaries, settings, self._state)
                        self._alerts.maybe_send_digest(summaries, settings, self._state)
                        self._save_state()
            except Exception as exc:  # noqa: BLE001 — monitoring must never take the scheduler down
                self._log(f"[monitor] Error en el ciclo de monitoreo: {exc}")
                return []
            self._publish(summaries)
            return summaries

    def _probe_due_servers(self, settings: dict, force: bool) -> None:
        now = time.time()
        interval_secs = float(settings.get("probe_interval_hours", 6)) * 3600
        latest_ts = {
            profile: samples[-1]["ts"]
            for profile, samples in self.store.by_profile(since_ts=now - interval_secs * 2).items()
            if samples
        }

        # One probe per server even if several rules share it; the first
        # rule's database is the one sized.
        targets: dict[str, str] = {}
        for rule in self._sched.list_rules():
            profile_name = rule.get("server_profile", "")
            if not rule.get("enabled") or not profile_name or profile_name in targets:
                continue
            if force or now - latest_ts.get(profile_name, 0) >= interval_secs:
                targets[profile_name] = rule.get("db_name", "")
        if not targets:
            return

        def _one(item: tuple[str, str]) -> None:
            profile_name, db_name = item
            profile = self._profiles.get(profile_name)
            if not profile:
                return
            sample = self._probe(profile, db_name)
            sample["profile"] = profile_name
            self.store.append(sample)
            if not sample.get("reachable"):
                self._log(f"[monitor] {profile_name}: inalcanzable ({sample.get('error', '')[:100]})")

        self._log(f"[monitor] Sondeando {len(targets)} servidor(es): {', '.join(sorted(targets))}")
        with ThreadPoolExecutor(max_workers=_MAX_PARALLEL_PROBES, thread_name_prefix="health-probe") as pool:
            list(pool.map(_one, targets.items()))

    # ── Output ───────────────────────────────────────────────────────────────

    def _publish(self, summaries: list[dict]) -> None:
        if self._q is None:
            return
        try:
            self._q.put_nowait(("health_refresh", summaries))
        except Exception:  # noqa: BLE001
            pass

    def _log(self, message: str) -> None:
        if self._q is None:
            return
        try:
            self._q.put_nowait(("log", message))
        except Exception:  # noqa: BLE001
            pass
