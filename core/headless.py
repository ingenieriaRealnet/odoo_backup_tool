"""
Headless mode: scheduled backups + client health monitoring with no window.

Started with `OdooBackupTool.exe --headless` (normally by a Windows task at
logon — see scripts/install_monitor_task.ps1).

Why: the scheduler used to live only inside the GUI, so on any day nobody
opened the tool there were no backups and nothing watching the clients
(2026-09-17, 19-21, 26-27 in the run history). A monitor that only works
while someone is looking at it cannot warn anyone.

Coexistence with the GUI is arbitrated by core.instance_lock.SchedulerLock:
whichever process holds it runs the schedule; the other one only displays.
If the GUI already owns the lock when this starts, this process waits and
takes over as soon as the GUI is closed.
"""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .history_manager import HistoryManager
from .instance_lock import SchedulerLock
from .monitor import HealthMonitor
from .profiles import ProfileManager
from .scheduler import BackupScheduler, ScheduleManager
from .temp_registry import RemoteTempRegistry, sweep_orphaned_files

_DATA_DIR = Path.home() / ".odoo_backup_tool"
_LOG_FILE = _DATA_DIR / "headless.log"
_PID_FILE = _DATA_DIR / "headless.pid"   # read by core/background_service.py

# How often to retry taking the scheduler lock while the GUI owns it.
_LOCK_RETRY_SECS = 30

logger = logging.getLogger("obt.headless")


def _configure_logging() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(_LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def _drain_queue(events: queue.Queue, stop: threading.Event) -> None:
    """
    Write to the log file what the GUI would have shown in its log panel.

    The scheduler and the monitor publish on a queue.Queue designed for the
    Tk poll loop; here there is no GUI, so this thread is its only consumer
    (otherwise the queue would grow without bound).
    """
    while not stop.is_set():
        try:
            event, data = events.get(timeout=1)
        except queue.Empty:
            continue
        if event == "sched_log":
            logger.info(data[1])
        elif event == "log":
            logger.info(data)
        # sched_refresh / health_refresh are GUI repaint requests: nothing to do.


def run_headless() -> int:
    """
    Run the scheduler and the health monitor until the process is stopped.

    Returns:
        Process exit code (0 on a clean stop, 0 also when another headless
        instance is already running — that is not an error).
    """
    _configure_logging()
    lock = SchedulerLock()
    stop = threading.Event()

    # Lets the GUI's on/off switch find (and stop) this process — see
    # core/background_service.py. Written before waiting for the lock, so
    # a monitor that is only waiting for the GUI to close is visible too.
    _write_pid_file()

    while not lock.acquire("headless"):
        owner = lock.owner_info()
        if owner.get("mode") == "headless":
            logger.info("Ya hay un monitor en segundo plano (PID %s). Saliendo.", owner.get("pid"))
            # The PID file must keep pointing at the monitor that is running.
            _write_pid_file(owner.get("pid"))
            return 0
        logger.info("La aplicacion (PID %s) esta ejecutando el programador; en espera.", owner.get("pid"))
        time.sleep(_LOCK_RETRY_SECS)

    logger.info("Monitor en segundo plano iniciado: programador de respaldos y sondeo de salud activos.")
    events: queue.Queue = queue.Queue()
    threading.Thread(target=_drain_queue, args=(events, stop), name="headless-log", daemon=True).start()

    schedule_mgr = ScheduleManager()
    profile_mgr = ProfileManager()
    temp_registry = RemoteTempRegistry()
    history = HistoryManager()

    monitor = HealthMonitor(schedule_mgr, profile_mgr, notify_queue=events, history=history, owner=True)
    schedule_mgr.on_result = monitor.record_backup_result

    scheduler = BackupScheduler(
        schedule_mgr, profile_mgr, events,
        temp_registry=temp_registry, history=history, monitor=monitor,
    )
    scheduler.start()

    # Same startup sweep the GUI performs — see core/temp_registry.py.
    threading.Thread(
        target=lambda: sweep_orphaned_files(temp_registry, log_callback=logger.info),
        name="headless-sweep", daemon=True,
    ).start()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Detenido por el usuario.")
    finally:
        scheduler.stop()
        stop.set()
        lock.release()
        try:
            _PID_FILE.unlink()
        except OSError:
            pass
    return 0


def _write_pid_file(pid: int | None = None) -> None:
    try:
        _PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _PID_FILE.write_text(str(pid or os.getpid()), encoding="utf-8")
    except OSError:
        pass  # informational: the switch then reports "no en ejecucion"
