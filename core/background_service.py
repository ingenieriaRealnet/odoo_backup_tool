"""
On/off switch for the headless monitor (main.py --headless), driven from the
GUI instead of a PowerShell script.

"Enabled" means: Windows starts `OdooBackupTool.exe --headless` at every
logon of this user, and it is started right away. The logon entry is a value
under HKCU\\...\\CurrentVersion\\Run rather than a Task Scheduler task because
it needs no administrator rights and no UAC prompt — the same place any
per-user tray application registers itself.

The running headless process is identified by the PID file it writes
(core/headless.py). The PID alone is not trusted: Windows reuses PIDs, so
the process image name is checked too before reporting it alive or stopping
it.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

_DATA_DIR = Path.home() / ".odoo_backup_tool"
PID_FILE = _DATA_DIR / "headless.pid"

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "OdooBackupToolMonitor"

# Windows process creation flags: no console window, not tied to the GUI
# that launched it (closing the window must not kill the monitor).
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def headless_command() -> list[str]:
    """
    Command line that starts the headless monitor for this installation.

    Frozen (.exe): the executable itself. From source (development):
    pythonw.exe + main.py, so no console window appears either.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--headless"]
    interpreter = Path(sys.executable)
    windowless = interpreter.with_name("pythonw.exe")
    main_py = Path(__file__).resolve().parent.parent / "main.py"
    return [str(windowless if windowless.exists() else interpreter), str(main_py), "--headless"]


def _quoted(command: list[str]) -> str:
    return subprocess.list2cmdline(command)


# ── Start at logon (HKCU Run) ────────────────────────────────────────────────

def is_enabled() -> bool:
    """True if the monitor is registered to start at logon."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
        return True
    except OSError:
        return False


def registered_command() -> str | None:
    """The command stored in the Run key, or None when not registered."""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, VALUE_NAME)[0]
    except OSError:
        return None


def _set_logon_entry(enabled: bool) -> None:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _quoted(headless_command()))
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass


# ── Running process ──────────────────────────────────────────────────────────

def _image_name(pid: int) -> str | None:
    """Executable path of a live process, or None if it is not running."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
            return None
        size = ctypes.c_ulong(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def running_pid() -> int | None:
    """PID of the running headless monitor, or None."""
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    image = _image_name(pid)
    if image is None:
        return None
    # A stale PID file whose number now belongs to an unrelated process.
    name = os.path.basename(image).lower()
    if image and not (name.startswith("odoobackuptool") or name.startswith("python")):
        return None
    return pid


def start_now() -> int | None:
    """Start the headless monitor unless it is already running. Returns its PID."""
    pid = running_pid()
    if pid:
        return pid
    process = subprocess.Popen(
        headless_command(),
        creationflags=_DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW,
        close_fds=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return process.pid


def stop_running() -> bool:
    """Stop the headless monitor if it is running. Returns True if one was stopped."""
    pid = running_pid()
    if not pid:
        return False
    # /T: the frozen .exe runs as a bootloader + child pair.
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True, creationflags=_CREATE_NO_WINDOW)
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    return True


# ── Switch ───────────────────────────────────────────────────────────────────

def enable() -> int | None:
    """Register at logon and start it now. Returns the PID of the monitor."""
    _set_logon_entry(True)
    return start_now()


def disable() -> bool:
    """Unregister from logon and stop it. Returns True if a running one was stopped."""
    _set_logon_entry(False)
    return stop_running()
