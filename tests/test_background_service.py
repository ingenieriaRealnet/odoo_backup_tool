"""
Tests for the background-monitor switch (core/background_service.py).

The registry, process creation and taskkill are all mocked: these tests
never register anything at logon nor start/stop a real process.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import background_service as bs


class FakeRegistry:
    """Just enough of winreg for the Run key."""
    HKEY_CURRENT_USER = object()
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values: dict[str, str] = {}

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def OpenKey(self, *_args):
        return self._Key()

    def QueryValueEx(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, _key, name, _reserved, _kind, value):
        self.values[name] = value

    def DeleteValue(self, _key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


class BackgroundServiceTest(unittest.TestCase):
    def setUp(self):
        self.registry = FakeRegistry()
        patcher = mock.patch.dict(sys.modules, {"winreg": self.registry})
        patcher.start()
        self.addCleanup(patcher.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        pid_patch = mock.patch.object(bs, "PID_FILE", Path(tmp.name) / "headless.pid")
        pid_patch.start()
        self.addCleanup(pid_patch.stop)

    def test_command_from_source_runs_main_py_without_console(self):
        with mock.patch.object(sys, "frozen", False, create=True):
            command = bs.headless_command()
        self.assertTrue(command[1].endswith("main.py"))
        self.assertEqual(command[-1], "--headless")

    def test_command_when_frozen_is_the_executable_itself(self):
        with mock.patch.object(sys, "frozen", True, create=True), \
             mock.patch.object(sys, "executable", r"C:\Tools\OdooBackupTool.exe"):
            self.assertEqual(bs.headless_command(), [r"C:\Tools\OdooBackupTool.exe", "--headless"])

    def test_enable_registers_at_logon_and_starts_once(self):
        with mock.patch.object(bs.subprocess, "Popen") as popen, \
             mock.patch.object(bs, "running_pid", side_effect=[None, 4321]):
            popen.return_value.pid = 4321
            self.assertEqual(bs.enable(), 4321)
            self.assertTrue(bs.is_enabled())
            self.assertIn("--headless", bs.registered_command())
            bs.start_now()                        # already running -> no second process
        popen.assert_called_once()

    def test_disable_unregisters_and_stops_the_running_monitor(self):
        self.registry.values[bs.VALUE_NAME] = "x"
        bs.PID_FILE.write_text("4321", encoding="utf-8")
        with mock.patch.object(bs, "_image_name", return_value=r"C:\Tools\OdooBackupTool.exe"), \
             mock.patch.object(bs.subprocess, "run") as run:
            self.assertTrue(bs.disable())
        self.assertFalse(bs.is_enabled())
        self.assertEqual(run.call_args.args[0][:3], ["taskkill", "/PID", "4321"])
        self.assertFalse(bs.PID_FILE.exists())

    def test_disable_when_already_off_is_harmless(self):
        with mock.patch.object(bs.subprocess, "run") as run:
            self.assertFalse(bs.disable())
        run.assert_not_called()

    def test_stale_pid_reused_by_another_program_is_not_ours(self):
        bs.PID_FILE.write_text("4321", encoding="utf-8")
        with mock.patch.object(bs, "_image_name", return_value=r"C:\Windows\explorer.exe"):
            self.assertIsNone(bs.running_pid())
        with mock.patch.object(bs, "_image_name", return_value=None):
            self.assertIsNone(bs.running_pid())

    def test_image_name_of_this_process_is_python(self):
        # Real Win32 call, read-only, on the test process itself.
        self.assertTrue(os.path.basename(bs._image_name(os.getpid())).lower().startswith("python"))


if __name__ == "__main__":
    unittest.main()
