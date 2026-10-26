"""
A remote /tmp file must be in the temp manifest BEFORE it is created, so a
dump or filestore tar cut off by a dropped SSH session is still swept later.

Incident 2026-10-24 (backup_equiredes): the connection dropped while tar was
writing; the file was registered only after compress_filestore() returned,
so the 11.6 GB partial tar was never tracked and stayed in /tmp.

No network: SSH, DB and filestore managers are mocked.
"""
from __future__ import annotations

import queue
import unittest
from unittest import mock

from core import scheduler as sched


class _RecordingRegistry:
    """Stands in for RemoteTempRegistry and keeps entries in memory."""

    def __init__(self):
        self.entries: dict[str, dict] = {}

    def register(self, host, port, user, password, path, kind, label=""):
        entry_id = f"id-{len(self.entries)}"
        self.entries[entry_id] = {"host": host, "path": path, "kind": kind}
        return entry_id

    def unregister(self, entry_id):
        self.entries.pop(entry_id, None)

    def paths(self) -> set[str]:
        return {e["path"] for e in self.entries.values()}


_PROFILE = {"host": "10.0.0.1", "port": 22, "user": "root", "password": "x"}
_RULE = {
    "id": "rule-1", "label": "backup_test", "server_profile": "srv",
    "db_name": "testdb", "db_format": "sql",
    "include_db": True, "include_filestore": True,
    "filestore_root": "/var/lib/odoo/filestore", "dest_type": "gdrive",
}


class RegisterBeforeCreateTest(unittest.TestCase):
    def setUp(self):
        self.registry = _RecordingRegistry()
        profiles = mock.Mock()
        profiles.get.return_value = dict(_PROFILE)
        self.scheduler = sched.BackupScheduler(
            mock.Mock(), profiles, queue.Queue(),
            temp_registry=self.registry, history=mock.Mock(),
        )
        for target in ("core.ssh_client.SSHClient", "core.transfer.TransferManager"):
            patcher = mock.patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(sched, "notify_error")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run_with(self, dump_side_effect, tar_side_effect):
        with mock.patch("core.db_manager.DBManager") as db_cls, \
             mock.patch("core.filestore_manager.FilestoreManager") as fs_cls:
            db_cls.return_value.create_dump.side_effect = dump_side_effect
            fs_cls.return_value.compress_filestore.side_effect = tar_side_effect
            self.scheduler._run_rule(dict(_RULE))

    def test_filestore_cut_off_mid_tar_stays_registered(self):
        seen_at_tar_start: set[str] = set()

        def tar_drops_connection(root, fs_db, path, **_kw):
            seen_at_tar_start.update(self.registry.paths())
            raise RuntimeError("SSH session not active")

        self._run_with(lambda db, fmt, path, **_kw: path, tar_drops_connection)

        tar_paths = {p for p in self.registry.paths() if "/filestore_testdb_" in p}
        self.assertEqual(len(tar_paths), 1, "the partial tar must stay in the manifest")
        self.assertTrue(tar_paths <= seen_at_tar_start, "registered before tar started")
        self.assertTrue(any("/odoo_testdb_" in p for p in self.registry.paths()))

    def test_dump_cut_off_mid_pg_dump_stays_registered(self):
        # Asserting inside the side effect would be swallowed by _run_rule's
        # own except, so record what the registry held and check afterwards.
        registered_at_dump_start: list[bool] = []

        def dump_drops_connection(db, fmt, path, **_kw):
            registered_at_dump_start.append(path in self.registry.paths())
            raise RuntimeError("SSH session not active")

        self._run_with(dump_drops_connection, RuntimeError("tar must not run"))

        self.assertEqual(registered_at_dump_start, [True])
        self.assertEqual(
            [e["kind"] for e in self.registry.entries.values()], ["dump"],
        )


if __name__ == "__main__":
    unittest.main()
