"""
Monitoring settings, e-mail delivery and alert escalation.

The Variedades incident was not a detection failure: the tool recorded
"Espacio insuficiente" for 19 days in a tab nobody was looking at. What was
missing is what this module adds — the warning has to leave the application
and reach a person:

  * on every change of state (new problem, escalation, recovery),
  * again every `reminder_hours` while something stays critical,
  * and once a day as a digest, which doubles as a heartbeat: a day without
    the digest means the monitor itself is not running.
"""
from __future__ import annotations

import json
import os
import smtplib
import ssl
import threading
import time
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Callable

from .health_eval import (
    DEFAULT_THRESHOLDS, LEVEL_CRIT, LEVEL_LABELS, LEVEL_OFF, LEVEL_OK,
    LEVEL_UNKNOWN, LEVEL_WARN, human_bytes,
)

_DATA_DIR = Path.home() / ".odoo_backup_tool"
_SETTINGS_FILE = _DATA_DIR / "monitor_settings.json"

# Levels that warrant notifying someone. "unknown" is deliberately excluded:
# missing data is shown on the dashboard but would be noise as an e-mail.
_ALERTING_LEVELS = (LEVEL_WARN, LEVEL_CRIT)

_SMTP_TIMEOUT_SECS = 30


def default_settings() -> dict:
    """Factory defaults for monitor_settings.json."""
    return {
        "enabled": True,
        "probe_interval_hours": 6,
        "thresholds": dict(DEFAULT_THRESHOLDS),
        "digest_enabled": True,
        "digest_hour": 8,
        "reminder_hours": 24,
        "email": {
            "enabled": False,
            "host": "",
            "port": 587,
            "security": "starttls",   # "starttls" | "ssl" | "none"
            "user": "",
            "password": "",
            "sender": "",
            "recipients": [],
        },
    }


class MonitorSettings:
    """
    Persistent monitoring configuration (~/.odoo_backup_tool/monitor_settings.json).

    Kept in its own file rather than settings.json because the GUI rewrites
    settings.json wholesale with only window geometry on every close, which
    would silently drop anything else stored there.

    The SMTP password is stored in plain text, consistent with servers.json
    (see core/profiles.py): this tool is for local admin use only.
    """

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self._path = Path(path) if path else _SETTINGS_FILE
        self._lock = threading.Lock()
        self._mtime: float = 0.0
        self._data: dict = default_settings()
        self._load()

    def _load(self) -> None:
        data = default_settings()
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            self._mtime = self._path.stat().st_mtime
        except (OSError, json.JSONDecodeError):
            stored = {}
        # Merge one level deep so options added in later versions get their
        # default instead of a KeyError on an older file.
        for key, value in stored.items():
            if isinstance(value, dict) and isinstance(data.get(key), dict):
                data[key] = {**data[key], **value}
            else:
                data[key] = value
        self._data = data

    def get(self) -> dict:
        """Return a copy of the current settings, reloading if changed on disk."""
        with self._lock:
            try:
                if self._path.stat().st_mtime != self._mtime:
                    # Edited by another process (GUI vs. headless monitor).
                    self._load()
            except OSError:
                pass
            return json.loads(json.dumps(self._data))

    def save(self, data: dict) -> None:
        """Persist new settings atomically."""
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._path.with_suffix(".json.tmp")
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self._path)
            self._data = json.loads(json.dumps(data))
            self._mtime = self._path.stat().st_mtime


def email_is_configured(email_cfg: dict) -> bool:
    """True when e-mail delivery is enabled and has the minimum fields."""
    return bool(
        email_cfg.get("enabled")
        and email_cfg.get("host")
        and email_cfg.get("recipients")
        and (email_cfg.get("sender") or email_cfg.get("user"))
    )


def send_email(email_cfg: dict, subject: str, body: str) -> None:
    """
    Send a plain-text e-mail with the configured SMTP account.

    Raises:
        RuntimeError: If e-mail is not configured.
        smtplib.SMTPException / OSError: On delivery failure — the caller
            decides whether to retry; nothing is swallowed here so the
            "Probar correo" button can show the real cause.
    """
    if not email_is_configured(email_cfg):
        raise RuntimeError("El correo de alertas no está configurado.")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = email_cfg.get("sender") or email_cfg["user"]
    message["To"] = ", ".join(email_cfg["recipients"])
    message.set_content(body)

    host, port = email_cfg["host"], int(email_cfg.get("port") or 587)
    security = email_cfg.get("security", "starttls")
    context = ssl.create_default_context()

    if security == "ssl":
        server = smtplib.SMTP_SSL(host, port, timeout=_SMTP_TIMEOUT_SECS, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT_SECS)
    try:
        if security == "starttls":
            server.starttls(context=context)
        if email_cfg.get("user"):
            server.login(email_cfg["user"], email_cfg.get("password", ""))
        server.send_message(message)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 — the message is already sent or failed
            pass


# ── Message formatting ───────────────────────────────────────────────────────

def _fmt_ts(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "—"


def format_client_block(summary: dict) -> str:
    """Multi-line plain-text description of one client's status."""
    lines = [f"{summary['label']}  [{LEVEL_LABELS.get(summary['level'], summary['level'])}]"]
    for finding in summary.get("findings", []):
        lines.append(f"   - {finding['message']}")
    if summary.get("disk_pct") is not None:
        extra = ""
        if summary.get("days_to_full") is not None:
            extra = f", se llena en ~{summary['days_to_full']:.0f} días"
        lines.append(
            f"   Disco {summary['disk_mount']}: {summary['disk_pct']:.0f} % usado, "
            f"{human_bytes(summary['disk_free'])} libres{extra}"
        )
    lines.append(
        f"   Último respaldo correcto: {_fmt_ts(summary.get('last_ok_ts'))}"
        f" | Fallos seguidos: {summary.get('consecutive_failures', 0)}"
        f" | Último sondeo: {_fmt_ts(summary.get('probe_ts'))}"
    )
    return "\n".join(lines)


def format_digest(summaries: list[dict], now: float) -> tuple[str, str]:
    """Build (subject, body) of the daily digest."""
    active = [s for s in summaries if s["level"] != LEVEL_OFF]
    counts = {lv: sum(1 for s in active if s["level"] == lv)
              for lv in (LEVEL_CRIT, LEVEL_WARN, LEVEL_UNKNOWN, LEVEL_OK)}
    subject = (
        f"[Respaldos Odoo] Resumen diario: {counts[LEVEL_OK]} OK, "
        f"{counts[LEVEL_WARN]} en alerta, {counts[LEVEL_CRIT]} críticos"
        + (f", {counts[LEVEL_UNKNOWN]} sin datos" if counts[LEVEL_UNKNOWN] else "")
    )
    order = {LEVEL_CRIT: 0, LEVEL_WARN: 1, LEVEL_UNKNOWN: 2, LEVEL_OK: 3}
    blocks = [format_client_block(s) for s in sorted(active, key=lambda s: order.get(s["level"], 9))]
    body = (
        f"Estado de los clientes monitoreados al {_fmt_ts(now)}.\n\n"
        + "\n\n".join(blocks)
        + "\n\n—\nOdoo Backup Tool. Si este resumen deja de llegar, el monitor no está en ejecución."
    )
    return subject, body


class AlertManager:
    """
    Decides when a status deserves a notification and sends it.

    State (what was last notified per rule, date of the last digest) is kept
    by the caller in a dict that it persists — see HealthMonitor — so that
    restarting the app does not re-send every open alert.
    """

    def __init__(
        self,
        send: Callable[[dict, str, str], None] = send_email,
        toast: Callable[[str, str], None] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._send = send
        self._toast = toast
        self._log = log or (lambda _msg: None)

    def process(self, summaries: list[dict], settings: dict, state: dict, now: float | None = None) -> list[dict]:
        """
        Compare current statuses with what was last notified and notify the
        differences.

        Args:
            summaries: Output of health_eval.evaluate_client() for each rule.
            settings:  MonitorSettings.get().
            state:     Mutable dict; this method updates state["alerts"].
            now:       Reference time.

        Returns:
            The list of events raised this cycle:
            {"kind": "new"|"escalated"|"reminder"|"resolved", "summary": {...}}.
        """
        now = now if now is not None else time.time()
        alerts: dict = state.setdefault("alerts", {})
        reminder_secs = float(settings.get("reminder_hours", 24)) * 3600
        events: list[dict] = []

        for summary in summaries:
            rule_id, level = summary["rule_id"], summary["level"]
            previous = alerts.get(rule_id)
            prev_level = previous["level"] if previous else LEVEL_OK

            if level in _ALERTING_LEVELS:
                if prev_level not in _ALERTING_LEVELS:
                    events.append({"kind": "new", "summary": summary})
                elif level == LEVEL_CRIT and prev_level == LEVEL_WARN:
                    events.append({"kind": "escalated", "summary": summary})
                elif level == LEVEL_CRIT and now - previous.get("last_notified_ts", 0) >= reminder_secs:
                    events.append({"kind": "reminder", "summary": summary})
            elif prev_level in _ALERTING_LEVELS and level == LEVEL_OK:
                # Only a confirmed OK closes an alert. "unknown" (stale probe,
                # app not running) must not be reported as a recovery.
                events.append({"kind": "resolved", "summary": summary})

        if not events:
            self._sync_levels(alerts, summaries, now, notified_ids=set())
            return events

        delivered = self._deliver(events, settings, now)
        # When delivery fails the stored levels stay untouched, so the same
        # transitions are detected — and retried — on the next cycle.
        if delivered:
            self._sync_levels(alerts, summaries, now,
                              notified_ids={e["summary"]["rule_id"] for e in events})
        return events

    @staticmethod
    def _sync_levels(alerts: dict, summaries: list[dict], now: float, notified_ids: set) -> None:
        for summary in summaries:
            rule_id, level = summary["rule_id"], summary["level"]
            if level in _ALERTING_LEVELS:
                entry = alerts.setdefault(rule_id, {"since_ts": now, "last_notified_ts": 0})
                entry["level"] = level
                entry["label"] = summary["label"]
                if rule_id in notified_ids:
                    entry["last_notified_ts"] = now
            elif level == LEVEL_OK:
                alerts.pop(rule_id, None)
            # unknown/off: keep whatever was there.

    def _deliver(self, events: list[dict], settings: dict, now: float) -> bool:
        """Send one e-mail covering every event of this cycle (plus toasts)."""
        titles = {"new": "NUEVA", "escalated": "AGRAVADA", "reminder": "SIGUE ACTIVA", "resolved": "RESUELTA"}
        blocks = []
        for event in events:
            summary = event["summary"]
            blocks.append(f"[{titles[event['kind']]}] " + format_client_block(summary))
            if self._toast and event["kind"] in ("new", "escalated"):
                headline = summary["findings"][0]["message"] if summary.get("findings") else ""
                self._toast(f"{summary['label']}: {LEVEL_LABELS[summary['level']]}", headline[:200])

        worst = LEVEL_CRIT if any(
            e["summary"]["level"] == LEVEL_CRIT for e in events if e["kind"] != "resolved"
        ) else LEVEL_WARN
        only_resolved = all(e["kind"] == "resolved" for e in events)
        names = ", ".join(sorted({e["summary"]["label"] for e in events}))
        prefix = "Resuelto" if only_resolved else LEVEL_LABELS[worst].upper()
        subject = f"[Respaldos Odoo] {prefix}: {names}"
        body = (
            f"Cambios detectados el {_fmt_ts(now)}.\n\n" + "\n\n".join(blocks)
            + "\n\n—\nOdoo Backup Tool, monitoreo de clientes."
        )

        email_cfg = settings.get("email", {})
        if not email_is_configured(email_cfg):
            # Nothing to retry: without a mail account the toast and the
            # dashboard are the only channels, so consider it delivered.
            self._log("[monitor] Alertas generadas, pero el correo no está configurado: " + names)
            return True
        try:
            self._send(email_cfg, subject, body)
            self._log(f"[monitor] Alerta enviada por correo: {subject}")
            return True
        except Exception as exc:  # noqa: BLE001 — retried next cycle
            self._log(f"[monitor] No se pudo enviar el correo de alerta: {exc}")
            return False

    def maybe_send_digest(self, summaries: list[dict], settings: dict, state: dict, now: float | None = None) -> bool:
        """
        Send the daily digest once per day after `digest_hour`.

        Returns True if a digest was sent in this call.
        """
        now = now if now is not None else time.time()
        if not settings.get("digest_enabled", True):
            return False
        email_cfg = settings.get("email", {})
        if not email_is_configured(email_cfg):
            return False
        moment = datetime.fromtimestamp(now)
        today = moment.strftime("%Y-%m-%d")
        if state.get("last_digest_date") == today or moment.hour < int(settings.get("digest_hour", 8)):
            return False
        if not any(s["level"] != LEVEL_OFF for s in summaries):
            return False
        subject, body = format_digest(summaries, now)
        try:
            self._send(email_cfg, subject, body)
        except Exception as exc:  # noqa: BLE001 — retried next cycle (date not stamped)
            self._log(f"[monitor] No se pudo enviar el resumen diario: {exc}")
            return False
        state["last_digest_date"] = today
        self._log(f"[monitor] Resumen diario enviado: {subject}")
        return True
