"""
Background update checker — queries GitHub Releases API.

Usage:
    from core.updater import check_for_update
    check_for_update(GITHUB_REPO, APP_VERSION, callback)

The callback is invoked from the background thread.
Use root.after(0, ...) in the callback to safely update the Tkinter UI.

Callback signature:
    callback(new_version: str | None, download_url: str | None)
    - new_version is None when up to date, or on any network/parse error.
    - download_url points to the .exe asset when present, or the release page.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request


def _version_tuple(v: str) -> tuple[int, ...]:
    """
    Parse a version string like 'v1.2.3' or '1.2.3' into a comparable tuple.

    Returns (0,) on any parse failure so it never raises.
    """
    v = v.lstrip("v").strip()
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def check_for_update(
    repo: str,
    current_version: str,
    callback,
    timeout: int = 8,
) -> None:
    """
    Spawn a daemon thread that checks GitHub Releases and calls callback.

    The check is non-blocking and silent on all errors (network failures,
    rate limits, private repos, malformed responses).

    Args:
        repo:            GitHub repo in 'owner/name' format.
        current_version: Installed version string (e.g. '1.0.0').
        callback:        Called with (new_version, download_url) or (None, None).
        timeout:         HTTP request timeout in seconds.
    """

    def _worker() -> None:
        try:
            url = f"https://api.github.com/repos/{repo}/releases/latest"
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "odoo-backup-tool-updater/1.0",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            tag: str = data.get("tag_name", "")
            assets: list = data.get("assets", [])

            # Prefer a .exe asset download; fall back to the release HTML page.
            download_url: str = data.get("html_url", "")
            for asset in assets:
                if asset.get("name", "").lower().endswith(".exe"):
                    download_url = asset.get("browser_download_url", download_url)
                    break

            if _version_tuple(tag) > _version_tuple(current_version):
                callback(tag.lstrip("v"), download_url)
            else:
                callback(None, None)

        except Exception:
            # Fail silently — update check must never crash the app.
            callback(None, None)

    thread = threading.Thread(target=_worker, daemon=True, name="update-checker")
    thread.start()
