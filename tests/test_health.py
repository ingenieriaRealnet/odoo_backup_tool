"""
Unit tests for the monitoring core: probe parsing, evaluation rules, alert
escalation and the monitor cycle. No network access, no real user data —
everything runs against temporary directories and fake probes.

Run from the project root:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from core import health_eval as ev
from core.alerting import AlertManager, MonitorSettings, default_settings, format_digest
from core.health_probe import HealthStore, build_probe_command, parse_probe_output
from core.instance_lock import SchedulerLock
from core.monitor import HealthMonitor
from core.pg_exec import PgTarget

GB = 1024 ** 3
DAY = 86400
NOW = 1_790_000_000.0   # fixed reference clock for deterministic tests

# Output captured from the Variedades server on 2026-09-30 while its disk was
# full and PostgreSQL was refusing connections (trimmed to the relevant rows).
PROBE_DISK_FULL = """\
@@OBT_DF
S.ficheros     1B-bloques       Usados Disponibles Capacidad Montado en
tmpfs          1635778560      2539520  1633239040        1% /run
/dev/nvme0n1p2 501809635328 476447854592          0      100% /
tmpfs          8178876416        28672  8178847744        1% /dev/shm
/dev/nvme0n1p1 1124999168      6488064  1118511104        1% /boot/efi
@@OBT_TMP
/dev/nvme0n1p2 501809635328 476447854592          0      100% /
@@OBT_LOAD
8.06 7.93 7.85 3/1042 2270071
12
@@OBT_MEM
MemTotal:       15973376 kB
MemAvailable:   10447872 kB
@@OBT_UP
17786460.11
@@OBT_PG
rc=2
psql: error: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed: FATAL:  the database system is in recovery mode
@@OBT_DB
@@OBT_ODOO
200
"""

PROBE_HEALTHY = """\
@@OBT_DF
Filesystem     1-blocks         Used   Available Capacity Mounted on
/dev/nvme0n1p2 501809635328 185000000000 291000000000      39% /
@@OBT_TMP
/dev/nvme0n1p2 501809635328 185000000000 291000000000      39% /
@@OBT_LOAD
0.42 0.50 0.61 2/900 123
12
@@OBT_MEM
MemTotal:       15973376 kB
MemAvailable:   10447872 kB
@@OBT_UP
1000.5
@@OBT_PG
rc=0
f
@@OBT_DB
10271850496
@@OBT_ODOO
200
"""


def sample(ts, used_gb, avail_gb, profile="Variedades", reachable=True, **extra):
    """Build a minimal health sample for one root mount."""
    base = {
        "ts": ts, "profile": profile, "host": "10.0.0.1", "reachable": reachable, "error": "",
        "mounts": [{"mount": "/", "total": int((used_gb + avail_gb) * GB),
                    "used": int(used_gb * GB), "avail": int(avail_gb * GB)}] if reachable else [],
        "pg_ok": True if reachable else None, "pg_in_recovery": False, "pg_msg": "",
        "db_bytes": 10 * GB, "tmp_avail": int(avail_gb * GB), "odoo_http": "200",
    }
    base.update(extra)
    return base


RULE = {"id": "r1", "enabled": True, "label": "backup_variedades",
        "server_profile": "Variedades", "db_name": "aranzazudb"}


class ProbeParsingTest(unittest.TestCase):
    def test_disk_full_and_postgres_down_are_captured(self):
        metrics = parse_probe_output(PROBE_DISK_FULL)
        # Pseudo filesystems and the tiny EFI partition are ignored.
        self.assertEqual([m["mount"] for m in metrics["mounts"]], ["/"])
        self.assertEqual(metrics["mounts"][0]["avail"], 0)
        self.assertEqual(metrics["tmp_avail"], 0)
        self.assertIs(metrics["pg_ok"], False)
        self.assertIn("recovery mode", metrics["pg_msg"])
        self.assertIsNone(metrics["db_bytes"])
        self.assertEqual(metrics["cpus"], 12)
        self.assertAlmostEqual(metrics["load1"], 8.06)

    def test_healthy_server(self):
        metrics = parse_probe_output(PROBE_HEALTHY)
        self.assertIs(metrics["pg_ok"], True)
        self.assertIs(metrics["pg_in_recovery"], False)
        self.assertEqual(metrics["db_bytes"], 10271850496)
        self.assertEqual(metrics["odoo_http"], "200")
        self.assertEqual(metrics["mem_total"], 15973376 * 1024)

    def test_garbage_output_does_not_raise(self):
        metrics = parse_probe_output("bash: df: command not found\n@@OBT_PG\nnonsense")
        self.assertEqual(metrics["mounts"], [])
        self.assertIsNone(metrics["pg_ok"])

    def test_command_targets_docker_when_profile_says_so(self):
        bare = build_probe_command(PgTarget(), "db")
        docker = build_probe_command(PgTarget(container="pg15"), "db")
        self.assertIn("sudo -u postgres psql", bare)
        self.assertIn("docker exec -i pg15 psql", docker)
        # A quote in the database name must not break out of the SQL literal.
        self.assertIn("pg_database_size('o''brien')", build_probe_command(PgTarget(), "o'brien"))


class EvaluationTest(unittest.TestCase):
    def test_healthy_client_is_ok(self):
        state = {"consecutive_failures": 0, "last_ok_ts": NOW - 3600, "last_status": "ok"}
        result = ev.evaluate_client(RULE, state, [sample(NOW - 600, 180, 270)], now=NOW)
        self.assertEqual(result["level"], ev.LEVEL_OK)
        self.assertEqual(result["findings"], [])
        self.assertAlmostEqual(result["disk_pct"], 40.0)

    def test_variedades_would_have_alerted_before_the_outage(self):
        # 2026-09-12: second consecutive "Espacio insuficiente" failure, disk
        # at ~97 % and growing ~1 GB/day.
        samples = [sample(NOW - (5 - i) * DAY, 450 + i, 14 - i) for i in range(6)]
        state = {"consecutive_failures": 2, "last_ok_ts": NOW - 3 * DAY, "last_status": "error",
                 "last_error": "Espacio insuficiente en /tmp para comprimir el filestore"}
        result = ev.evaluate_client(RULE, state, samples, now=NOW)
        codes = {f["code"]: f["level"] for f in result["findings"]}
        self.assertEqual(result["level"], ev.LEVEL_CRIT)
        self.assertEqual(codes["disk_usage"], ev.LEVEL_CRIT)
        self.assertEqual(codes["backup_failing"], ev.LEVEL_WARN)
        # 9 GB free at 1 GB/day -> about 9 days.
        self.assertAlmostEqual(result["days_to_full"], 9.0, delta=0.5)
        self.assertEqual(codes["disk_trend"], ev.LEVEL_WARN)

    def test_mega_unreachable_twice_is_critical(self):
        samples = [
            sample(NOW - 7200, 100, 300, profile="Mega"),
            sample(NOW - 3600, 0, 0, profile="Mega", reachable=False, error="Tiempo de conexion agotado (15s)."),
            sample(NOW - 60, 0, 0, profile="Mega", reachable=False, error="Tiempo de conexion agotado (15s)."),
        ]
        state = {"consecutive_failures": 9, "last_ok_ts": NOW - 15 * DAY, "last_status": "error"}
        result = ev.evaluate_client({**RULE, "server_profile": "Mega"}, state, samples, now=NOW)
        codes = {f["code"]: f["level"] for f in result["findings"]}
        self.assertEqual(codes["unreachable"], ev.LEVEL_CRIT)
        self.assertEqual(codes["backup_failing"], ev.LEVEL_CRIT)
        self.assertEqual(codes["backup_stale"], ev.LEVEL_CRIT)

    def test_single_unreachable_probe_is_only_a_warning(self):
        samples = [sample(NOW - 3600, 100, 300),
                   sample(NOW - 60, 0, 0, reachable=False, error="timeout")]
        result = ev.evaluate_client(RULE, {"last_ok_ts": NOW - 3600}, samples, now=NOW)
        self.assertEqual(result["level"], ev.LEVEL_WARN)

    def test_one_failed_backup_is_shown_but_not_alerted(self):
        state = {"consecutive_failures": 1, "last_ok_ts": NOW - DAY, "last_status": "error"}
        result = ev.evaluate_client(RULE, state, [sample(NOW - 600, 100, 300)], now=NOW)
        self.assertEqual(result["level"], ev.LEVEL_OK)
        self.assertEqual(result["consecutive_failures"], 1)

    def test_postgres_down_is_critical(self):
        down = sample(NOW - 60, 100, 300, pg_ok=False, pg_msg="FATAL: the database system is in recovery mode")
        result = ev.evaluate_client(RULE, {"last_ok_ts": NOW - 3600}, [down], now=NOW)
        self.assertEqual(result["findings"][0]["code"], "postgres_down")
        self.assertEqual(result["level"], ev.LEVEL_CRIT)

    def test_backup_that_no_longer_fits_in_tmp(self):
        tight = sample(NOW - 60, 100, 300, tmp_avail=3 * GB, db_bytes=10 * GB)
        result = ev.evaluate_client(RULE, {"last_ok_ts": NOW - 3600}, [tight], now=NOW)
        self.assertIn("tmp_too_small", [f["code"] for f in result["findings"]])

    def test_stale_probe_is_unknown_not_ok(self):
        old = sample(NOW - 3 * DAY, 100, 300)
        result = ev.evaluate_client(RULE, {"last_ok_ts": NOW - 3600}, [old], now=NOW)
        self.assertEqual(result["level"], ev.LEVEL_UNKNOWN)

    def test_never_probed_is_unknown(self):
        result = ev.evaluate_client(RULE, None, [], now=NOW)
        self.assertEqual(result["level"], ev.LEVEL_UNKNOWN)

    def test_disabled_rule_is_off(self):
        result = ev.evaluate_client({**RULE, "enabled": False}, {"consecutive_failures": 9}, [], now=NOW)
        self.assertEqual(result["level"], ev.LEVEL_OFF)

    def test_projection_needs_a_day_of_evidence_and_growth(self):
        burst = [sample(NOW - 3600 + i * 600, 400 + i, 60 - i) for i in range(5)]
        self.assertIsNone(ev.days_to_full(burst, "/", NOW))      # under a day apart
        shrinking = [sample(NOW - (4 - i) * DAY, 400 - i, 60 + i) for i in range(5)]
        self.assertIsNone(ev.days_to_full(shrinking, "/", NOW))  # not growing

    def test_custom_thresholds_override_defaults(self):
        result = ev.evaluate_client(
            RULE, {"last_ok_ts": NOW - 3600}, [sample(NOW - 60, 60, 40)],
            thresholds={"disk_warn_pct": 50}, now=NOW,
        )
        self.assertEqual(result["level"], ev.LEVEL_WARN)


def summary(level, rule_id="r1", label="backup_variedades"):
    findings = [] if level == ev.LEVEL_OK else [{"code": "disk_usage", "level": level, "message": "Disco / al 95 %"}]
    return {"rule_id": rule_id, "label": label, "level": level, "findings": findings,
            "disk_pct": 95.0, "disk_mount": "/", "disk_free": 5 * GB, "days_to_full": None,
            "last_ok_ts": NOW - DAY, "consecutive_failures": 0, "probe_ts": NOW}


class AlertingTest(unittest.TestCase):
    def setUp(self):
        self.sent: list[tuple[str, str]] = []
        self.settings = default_settings()
        self.settings["email"].update(enabled=True, host="smtp.test", sender="a@test", recipients=["b@test"])
        self.manager = AlertManager(send=lambda cfg, subject, body: self.sent.append((subject, body)))

    def test_notifies_once_then_stays_quiet_then_reports_recovery(self):
        state: dict = {}
        self.assertEqual(self.manager.process([summary(ev.LEVEL_WARN)], self.settings, state, NOW)[0]["kind"], "new")
        self.assertEqual(self.manager.process([summary(ev.LEVEL_WARN)], self.settings, state, NOW + 600), [])
        events = self.manager.process([summary(ev.LEVEL_OK)], self.settings, state, NOW + 1200)
        self.assertEqual(events[0]["kind"], "resolved")
        self.assertEqual(len(self.sent), 2)
        self.assertTrue(self.sent[1][0].startswith("[Respaldos Odoo] Resuelto"))
        self.assertEqual(state["alerts"], {})

    def test_escalation_and_daily_reminder_while_critical(self):
        state: dict = {}
        self.manager.process([summary(ev.LEVEL_WARN)], self.settings, state, NOW)
        self.assertEqual(self.manager.process([summary(ev.LEVEL_CRIT)], self.settings, state, NOW + 60)[0]["kind"], "escalated")
        self.assertEqual(self.manager.process([summary(ev.LEVEL_CRIT)], self.settings, state, NOW + 3600), [])
        self.assertEqual(self.manager.process([summary(ev.LEVEL_CRIT)], self.settings, state, NOW + 25 * 3600)[0]["kind"], "reminder")

    def test_unknown_does_not_close_an_open_alert(self):
        state: dict = {}
        self.manager.process([summary(ev.LEVEL_CRIT)], self.settings, state, NOW)
        self.assertEqual(self.manager.process([summary(ev.LEVEL_UNKNOWN)], self.settings, state, NOW + 60), [])
        self.assertIn("r1", state["alerts"])

    def test_failed_delivery_is_retried_next_cycle(self):
        def boom(cfg, subject, body):
            raise OSError("smtp down")
        flaky = AlertManager(send=boom)
        state: dict = {}
        flaky.process([summary(ev.LEVEL_CRIT)], self.settings, state, NOW)
        self.assertEqual(state.get("alerts"), {})   # not recorded as notified
        self.assertEqual(self.manager.process([summary(ev.LEVEL_CRIT)], self.settings, state, NOW + 600)[0]["kind"], "new")

    def test_digest_is_sent_once_per_day_after_the_configured_hour(self):
        state: dict = {}
        day = time.mktime((2026, 10, 1, 0, 0, 0, 0, 0, -1))
        items = [summary(ev.LEVEL_OK), summary(ev.LEVEL_CRIT, "r2", "backup_mega")]
        self.assertFalse(self.manager.maybe_send_digest(items, self.settings, state, day + 7 * 3600))
        self.assertTrue(self.manager.maybe_send_digest(items, self.settings, state, day + 8 * 3600 + 60))
        self.assertFalse(self.manager.maybe_send_digest(items, self.settings, state, day + 12 * 3600))
        self.assertTrue(self.manager.maybe_send_digest(items, self.settings, state, day + DAY + 9 * 3600))
        self.assertIn("1 OK, 0 en alerta, 1 críticos", self.sent[0][0])

    def test_digest_lists_critical_clients_first(self):
        _subject, body = format_digest([summary(ev.LEVEL_OK), summary(ev.LEVEL_CRIT, "r2", "backup_mega")], NOW)
        self.assertLess(body.index("backup_mega"), body.index("backup_variedades"))

    def test_settings_roundtrip_and_forward_compatible_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "monitor_settings.json"
            path.write_text(json.dumps({"email": {"host": "smtp.x"}, "thresholds": {"disk_warn_pct": 70}}), encoding="utf-8")
            data = MonitorSettings(path).get()
            self.assertEqual(data["email"]["host"], "smtp.x")
            self.assertEqual(data["email"]["port"], 587)                 # default filled in
            self.assertEqual(data["thresholds"]["disk_warn_pct"], 70)
            self.assertEqual(data["thresholds"]["disk_crit_pct"], 90)    # default filled in


class FakeRules:
    def __init__(self, rules):
        self.rules = rules
        self.on_result = None

    def list_rules(self):
        return [dict(r) for r in self.rules]


class FakeProfiles:
    def get(self, name):
        return {"name": name, "host": "10.0.0.1", "port": 22, "user": "root", "password": "x"}


class FakeHistory:
    def __init__(self, entries):
        self.entries = entries

    def list_entries(self, limit=None):
        return list(self.entries)


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.probed: list[str] = []
        self.sent: list[str] = []

    def make(self, probe_result=None, history=None, owner=True):
        def fake_probe(profile, db_name=""):
            self.probed.append(profile["name"])
            return probe_result or sample(time.time(), 100, 300, profile=profile["name"])
        manager = AlertManager(send=lambda cfg, subject, body: self.sent.append(subject))
        monitor = HealthMonitor(FakeRules([RULE]), FakeProfiles(), history=history, owner=owner,
                                data_dir=self.tmp.name, probe=fake_probe, alert_manager=manager)
        cfg = monitor.settings.get()
        cfg["email"].update(enabled=True, host="smtp.test", sender="a@test", recipients=["b@test"])
        cfg["digest_enabled"] = False
        monitor.settings.save(cfg)
        return monitor

    def test_cycle_probes_once_per_interval(self):
        monitor = self.make()
        monitor.run_cycle()
        monitor.run_cycle()
        self.assertEqual(self.probed, ["Variedades"])          # second cycle: not due yet
        monitor.run_cycle(force=True)
        self.assertEqual(len(self.probed), 2)

    def test_failures_are_counted_and_reset_by_a_success(self):
        monitor = self.make()
        for _ in range(3):
            monitor._apply_result(monitor._state["rules"].setdefault("r1", {}), "error", "sin espacio", time.time())
        self.assertEqual(monitor.summaries()[0]["consecutive_failures"], 3)
        monitor._apply_result(monitor._state["rules"]["r1"], "ok", "", time.time())
        self.assertEqual(monitor.summaries()[0]["consecutive_failures"], 0)

    def test_track_record_is_seeded_from_existing_history(self):
        runs = [{"action_type": "backup_scheduled", "server_label": "backup_variedades",
                 "status": status, "summary": "Espacio insuficiente", "ended_at": NOW + i}
                for i, status in enumerate(["ok", "error", "error", "error"])]
        monitor = self.make(history=FakeHistory(runs))
        record = monitor._state["rules"]["r1"]
        self.assertEqual(record["consecutive_failures"], 3)
        self.assertEqual(record["last_ok_ts"], NOW)

    def test_results_recorded_by_another_version_are_reconciled_once(self):
        # Track record says 13 failures; then the OLD .exe (no monitor hook)
        # completes the backup and only writes it to the rule.
        runs = [{"action_type": "backup_scheduled", "server_label": "backup_variedades",
                 "status": "error", "summary": "Espacio insuficiente", "ended_at": NOW + i} for i in range(13)]
        rules = FakeRules([{**RULE, "last_run_ts": "2026-09-30T11:42:43", "last_result": "ok",
                            "last_message": "Backup completado exitosamente"}])
        monitor = HealthMonitor(rules, FakeProfiles(), history=FakeHistory(runs), owner=True,
                                data_dir=self.tmp.name, probe=lambda *a: {}, alert_manager=AlertManager(send=lambda *a: None))
        self.assertEqual(monitor.summaries()[0]["consecutive_failures"], 0)
        # A run the monitor itself recorded is not applied a second time.
        monitor.record_backup_result("r1", "error", "fallo real")
        self.assertEqual(monitor.summaries()[0]["consecutive_failures"], 1)

    def test_critical_cycle_sends_one_alert_and_persists_state(self):
        full = sample(time.time(), 470, 1, profile="Variedades")
        monitor = self.make(probe_result=full)
        monitor.run_cycle()
        monitor.run_cycle(probe=False)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("CRÍTICO", self.sent[0])
        saved = json.loads((Path(self.tmp.name) / "monitor_state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["alerts"]["r1"]["level"], ev.LEVEL_CRIT)

    def test_viewer_instance_never_alerts_nor_writes_state(self):
        full = sample(time.time(), 470, 1, profile="Variedades")
        monitor = self.make(probe_result=full, owner=False)
        result = monitor.run_cycle(force=True)
        self.assertEqual(result[0]["level"], ev.LEVEL_CRIT)     # still shown on the dashboard
        self.assertEqual(self.sent, [])
        self.assertFalse((Path(self.tmp.name) / "monitor_state.json").exists())

    def test_store_survives_a_torn_last_line(self):
        store = HealthStore(Path(self.tmp.name) / "m.jsonl")
        store.append(sample(NOW, 1, 1))
        with open(Path(self.tmp.name) / "m.jsonl", "a", encoding="utf-8") as fh:
            fh.write('{"ts": 1, "profile": "x", "mou')
        self.assertEqual(len(store.read()), 1)


class SchedulerLockTest(unittest.TestCase):
    def test_second_process_cannot_take_a_held_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "scheduler.lock")
            lock = SchedulerLock(path)
            self.assertTrue(lock.acquire("gui"))
            code = (
                "import sys; from core.instance_lock import SchedulerLock; "
                f"lock = SchedulerLock({path!r}); "
                "print(lock.acquire('headless'), lock.owner_info().get('mode'))"
            )
            root = str(Path(__file__).resolve().parent.parent)
            out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True)
            self.assertEqual(out.stdout.split(), ["False", "gui"], out.stderr)
            lock.release()
            out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True)
            self.assertEqual(out.stdout.split()[0], "True", out.stderr)


if __name__ == "__main__":
    unittest.main()
