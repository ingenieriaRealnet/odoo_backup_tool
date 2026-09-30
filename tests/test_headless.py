"""
Tests for core/headless.py start-up and shutdown wiring.

The scheduler, the monitor and every manager are replaced by mocks: these
tests must never start a real scheduler (it would run the user's rules) nor
touch the real ~/.odoo_backup_tool lock.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import headless


class FakeLock:
    def __init__(self, acquire_results, owner):
        self._results = list(acquire_results)
        self._owner = owner
        self.released = False

    def acquire(self, mode):
        self.mode = mode
        return self._results.pop(0)

    def owner_info(self):
        return self._owner

    def release(self):
        self.released = True


class HeadlessTest(unittest.TestCase):
    def setUp(self):
        self.patches = {}
        for name in ("ScheduleManager", "ProfileManager", "RemoteTempRegistry", "HistoryManager",
                     "HealthMonitor", "BackupScheduler", "sweep_orphaned_files", "_configure_logging"):
            patcher = mock.patch.object(headless, name)
            self.patches[name] = patcher.start()
            self.addCleanup(patcher.stop)
        # The PID file must never be the user's real one.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.pid_file = Path(tmp.name) / "headless.pid"
        pid_patch = mock.patch.object(headless, "_PID_FILE", self.pid_file)
        pid_patch.start()
        self.addCleanup(pid_patch.stop)

    def run_with(self, lock, sleep_effect):
        with mock.patch.object(headless, "SchedulerLock", return_value=lock), \
             mock.patch.object(headless.time, "sleep", side_effect=sleep_effect):
            return headless.run_headless()

    def test_exits_quietly_when_another_headless_instance_is_running(self):
        lock = FakeLock([False], {"mode": "headless", "pid": 4321})
        self.assertEqual(self.run_with(lock, AssertionError("must not wait")), 0)
        self.patches["BackupScheduler"].assert_not_called()
        # The switch in the GUI must keep seeing the monitor that IS running.
        self.assertEqual(self.pid_file.read_text(encoding="utf-8"), "4321")

    def test_waits_for_the_gui_then_takes_over(self):
        lock = FakeLock([False, True], {"mode": "gui", "pid": 99})
        # 1st sleep: waiting for the GUI to release; 2nd: the main loop, which
        # the test interrupts as Ctrl+C would.
        self.assertEqual(self.run_with(lock, [None, KeyboardInterrupt()]), 0)
        self.assertEqual(lock.mode, "headless")

        scheduler = self.patches["BackupScheduler"].return_value
        monitor = self.patches["HealthMonitor"].return_value
        scheduler.start.assert_called_once()
        scheduler.stop.assert_called_once()
        self.assertTrue(lock.released)
        self.assertFalse(self.pid_file.exists())   # removed on a clean stop
        # The monitor must be the owner and must hear about every backup result.
        self.assertTrue(self.patches["HealthMonitor"].call_args.kwargs["owner"])
        self.assertIs(self.patches["ScheduleManager"].return_value.on_result, monitor.record_backup_result)
        self.assertIs(self.patches["BackupScheduler"].call_args.kwargs["monitor"], monitor)


if __name__ == "__main__":
    unittest.main()
