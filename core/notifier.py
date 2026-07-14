"""
Windows toast notifications for the Odoo Backup Tool scheduler.

Uses plyer for cross-platform desktop notifications (Windows 10/11 toast).
Fails silently if plyer is not installed — notifications are informational only
and must never block or crash the backup scheduler.
"""
from __future__ import annotations

try:
    from plyer import notification as _plyer_notification
    _PLYER_OK = True
except Exception:  # noqa: BLE001
    _PLYER_OK = False

# Application name shown in the Windows notification center
_APP_NAME = "Odoo Backup Tool"

# Icon path is optional; plyer will use a default icon if not found
_ICON_PATH: str | None = None

try:
    import os
    import sys
    _base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    _candidate = os.path.join(_base, "..", "icon.ico")
    if os.path.isfile(_candidate):
        _ICON_PATH = os.path.abspath(_candidate)
except Exception:  # noqa: BLE001
    pass


def _send(title: str, message: str, timeout: int) -> None:
    """Internal — dispatch a desktop notification. Silently ignores all errors."""
    if not _PLYER_OK:
        return
    try:
        kwargs: dict = {
            "app_name": _APP_NAME,
            "title":    title,
            "message":  message,
            "timeout":  timeout,
        }
        if _ICON_PATH:
            kwargs["app_icon"] = _ICON_PATH
        _plyer_notification.notify(**kwargs)
    except Exception:  # noqa: BLE001
        pass


def notify_success(title: str, message: str) -> None:
    """Show a success toast (8 s). Used when a scheduled backup completes."""
    _send(f"✓ {title}", message, timeout=8)


def notify_error(title: str, message: str) -> None:
    """Show an error toast (15 s). Used when a scheduled backup fails."""
    _send(f"✗ {title}", message, timeout=15)


def notify_action_required(title: str, message: str) -> None:
    """Show an action-required toast (20 s). Used for manual intervention alerts."""
    _send(f"⚠ {title}", message, timeout=20)
