"""
Single-owner lock for the backup scheduler.

Two processes can run the tool at the same time: the GUI, and the headless
monitor started by Windows at logon (main.py --headless). Both contain a
BackupScheduler. If both ran it, every rule would fire twice at the same
minute and the two jobs would race on the same /tmp paths on the client's
server — the collision run_rule_now() already guards against within one
process.

The lock is an OS-level byte lock on a file, held for the life of the
process. Unlike a PID file it cannot go stale: if the owner crashes or is
killed, the OS releases it.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_DATA_DIR = Path.home() / ".odoo_backup_tool"
_LOCK_FILE = _DATA_DIR / "scheduler.lock"


class SchedulerLock:
    """Non-blocking, process-lifetime lock. acquire() is safe to call once."""

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self._path = Path(path) if path else _LOCK_FILE
        # Who holds the lock, for display only. A separate file because on
        # Windows a locked byte range cannot be read by other processes.
        self._info_path = self._path.with_name(self._path.name + ".info")
        self._fh = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self, mode: str) -> bool:
        """
        Try to become the scheduler owner.

        Args:
            mode: "gui" or "headless" — recorded so the other instance can
                  tell the user who is running the schedule.

        Returns:
            True if this process now owns the scheduler.
        """
        if self._fh is not None:
            return True
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self._path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False

        self._fh = fh
        try:
            with open(self._info_path, "w", encoding="utf-8") as info:
                json.dump({"pid": os.getpid(), "mode": mode, "since": time.time()}, info)
        except OSError:
            pass  # informational only
        return True

    def release(self) -> None:
        """Give up ownership (also happens automatically on process exit)."""
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None

    def owner_info(self) -> dict:
        """Return {"pid", "mode", "since"} of the current owner, or {}."""
        try:
            with open(self._info_path, "r", encoding="utf-8") as info:
                return json.load(info)
        except (OSError, json.JSONDecodeError):
            return {}
