"""
Tests for what lets the GUI and the headless monitor share the schedule:
reloading schedules.json when the other process changes it, the on_result
observer, and the "run this rule now" request drop-box.

Everything is redirected to a temporary directory; the real
~/.odoo_backup_tool is never touched.
"""
from __future__ import annotations

import os
import queue
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from core import scheduler as sched


class SharedScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        for name, value in (("_DATA_DIR", base), ("_SCHEDULE_FILE", base / "schedules.json"),
                            ("_RUN_REQUEST_DIR", base / "run_requests")):
            patcher = mock.patch.object(sched, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _touch_later(path: Path) -> None:
        """Make sure the file's mtime differs from the previous write."""
        stamp = time.time() + 2
        os.utime(path, (stamp, stamp))

    def test_edit_in_one_instance_is_seen_by_the_other(self):
        gui, headless = sched.ScheduleManager(), sched.ScheduleManager()
        rule = gui.add({"label": "backup_x", "schedule_hour": 9})
        self._touch_later(sched._SCHEDULE_FILE)
        self.assertEqual([r["label"] for r in headless.list_rules()], ["backup_x"])

        gui.update(rule["id"], {"schedule_hour": 3})
        self._touch_later(sched._SCHEDULE_FILE)
        self.assertEqual(headless.get(rule["id"])["schedule_hour"], 3)

    def test_recording_a_result_does_not_overwrite_the_other_instances_edit(self):
        gui, headless = sched.ScheduleManager(), sched.ScheduleManager()
        rule = gui.add({"label": "backup_x", "retention_days": 90})
        self._touch_later(sched._SCHEDULE_FILE)
        headless.list_rules()

        gui.update(rule["id"], {"retention_days": 30})          # user edits in the GUI...
        self._touch_later(sched._SCHEDULE_FILE)
        headless._update_result(rule["id"], "ok", "listo")       # ...headless finishes a run
        self._touch_later(sched._SCHEDULE_FILE)

        final = gui.get(rule["id"])
        self.assertEqual(final["retention_days"], 30)
        self.assertEqual(final["last_result"], "ok")

    def test_on_result_observer_is_called_and_cannot_break_the_run(self):
        manager = sched.ScheduleManager()
        rule = manager.add({"label": "backup_x"})
        seen = []
        manager.on_result = lambda rule_id, result, message: seen.append((rule_id, result, message))
        manager._update_result(rule["id"], "error", "sin espacio")
        self.assertEqual(seen, [(rule["id"], "error", "sin espacio")])

        def broken(*_args):
            raise RuntimeError("observer bug")
        manager.on_result = broken
        with self.assertLogs("core.scheduler", level="ERROR"):
            manager._update_result(rule["id"], "ok", "listo")   # must not raise
        self.assertEqual(manager.get(rule["id"])["last_result"], "ok")

    def test_run_request_is_consumed_once_by_the_owner(self):
        manager = sched.ScheduleManager()
        rule = manager.add({"label": "backup_x"})
        scheduler = sched.BackupScheduler(manager, profile_mgr=None, notify_queue=queue.Queue(),
                                          temp_registry=object(), history=object())
        started = []
        scheduler.run_rule_now = lambda r: started.append(r["id"]) or True

        sched.request_rule_run(rule["id"])
        sched.request_rule_run("rule-that-no-longer-exists")
        scheduler._consume_run_requests()
        scheduler._consume_run_requests()            # nothing left the second time
        self.assertEqual(started, [rule["id"]])
        self.assertEqual(list(sched._RUN_REQUEST_DIR.glob("*.req")), [])

    def test_monitor_gets_its_turn_even_while_backups_are_paused(self):
        calls = []
        monitor = mock.Mock()
        monitor.maybe_run_cycle.side_effect = lambda: calls.append("cycle")
        scheduler = sched.BackupScheduler(sched.ScheduleManager(), profile_mgr=None, notify_queue=queue.Queue(),
                                          temp_registry=object(), history=object(), monitor=monitor)
        scheduler.pause()
        scheduler._tick_monitor()
        self.assertEqual(calls, ["cycle"])
        monitor.maybe_run_cycle.side_effect = RuntimeError("probe bug")
        with self.assertLogs("core.scheduler", level="ERROR"):
            scheduler._tick_monitor()                 # a monitor bug must not stop the scheduler


if __name__ == "__main__":
    unittest.main()
