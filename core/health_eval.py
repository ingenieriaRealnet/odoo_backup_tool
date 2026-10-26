"""
Turns raw health samples + backup results into a per-client status.

Everything here is a pure function of its inputs (no I/O, no clock unless
passed in), so the alerting rules can be unit-tested against the real cases
that motivated them:

  * Variedades, Sept 2026 — disk filling for weeks, 12 consecutive backup
    failures, nobody alerted.
  * Mega, Sept 2026 — server unreachable for two weeks, same silence.
"""
from __future__ import annotations

import time

# ── Levels ───────────────────────────────────────────────────────────────────
LEVEL_OK = "ok"
LEVEL_WARN = "warn"
LEVEL_CRIT = "crit"
LEVEL_UNKNOWN = "unknown"   # no fresh data: not a problem by itself, but not "fine" either
LEVEL_OFF = "off"           # rule disabled by the user

_SEVERITY = {LEVEL_OFF: 0, LEVEL_OK: 0, LEVEL_UNKNOWN: 1, LEVEL_WARN: 2, LEVEL_CRIT: 3}

LEVEL_LABELS = {
    LEVEL_OK: "OK",
    LEVEL_WARN: "Alerta",
    LEVEL_CRIT: "Crítico",
    LEVEL_UNKNOWN: "Sin datos",
    LEVEL_OFF: "Deshabilitada",
}

# ── Default thresholds (overridable in monitor_settings.json) ────────────────
DEFAULT_THRESHOLDS = {
    "disk_warn_pct": 80,
    "disk_crit_pct": 90,
    # Projected days until the disk is full, at the recent growth rate.
    "days_to_full_warn": 21,
    "days_to_full_crit": 7,
    # Scheduled backups failing in a row. One failure is often transient
    # (network blip) so it is shown but not alerted.
    "backup_fail_warn": 2,
    "backup_fail_crit": 3,
    # Days without a successful backup (covers "the tool was not open").
    "days_without_backup_warn": 3,
    "days_without_backup_crit": 5,
    # A probe older than this no longer describes the server.
    "probe_stale_hours": 30,
}

# Window used to project disk growth, and the minimum evidence required
# before trusting a projection (avoids alarming on two points an hour apart).
_TREND_WINDOW_DAYS = 14
_TREND_MIN_SAMPLES = 3
_TREND_MIN_SPAN_DAYS = 1.0

_GB = 1024 ** 3


def human_bytes(value: float | None) -> str:
    """Format a byte count for display (Spanish decimal comma)."""
    if value is None:
        return "—"
    for unit, size in (("TB", 1024 ** 4), ("GB", _GB), ("MB", 1024 ** 2)):
        if value >= size:
            return f"{value / size:.1f} {unit}".replace(".", ",")
    return f"{value / 1024:.0f} KB"


def worst_level(levels: list[str]) -> str:
    """Return the most severe level in the list (ok when empty)."""
    return max(levels, key=lambda lv: _SEVERITY.get(lv, 0), default=LEVEL_OK)


def fullest_mount(sample: dict) -> dict | None:
    """Return the mount with the highest usage percentage, annotated with pct."""
    best: dict | None = None
    for mount in sample.get("mounts") or []:
        capacity = mount["used"] + mount["avail"]
        if capacity <= 0:
            continue
        # used/(used+avail) matches what `df` prints: it excludes the blocks
        # reserved for root, which is the space applications actually have.
        pct = 100.0 * mount["used"] / capacity
        if best is None or pct > best["pct"]:
            best = {**mount, "pct": pct}
    return best


def days_to_full(samples: list[dict], mount_point: str, now: float | None = None) -> float | None:
    """
    Project how many days remain until `mount_point` runs out of space.

    Least-squares slope of used bytes over the last _TREND_WINDOW_DAYS.
    Returns None when there is not enough evidence or usage is not growing.

    Args:
        samples:     Chronological samples of ONE server.
        mount_point: Mount to project (e.g. "/").
        now:         Reference time (defaults to time.time()).
    """
    now = now if now is not None else time.time()
    cutoff = now - _TREND_WINDOW_DAYS * 86400
    points: list[tuple[float, float]] = []
    latest_avail: float | None = None
    for sample in samples:
        if sample.get("ts", 0) < cutoff:
            continue
        for mount in sample.get("mounts") or []:
            if mount["mount"] == mount_point:
                points.append((sample["ts"] / 86400.0, float(mount["used"])))
                latest_avail = float(mount["avail"])

    if len(points) < _TREND_MIN_SAMPLES or latest_avail is None:
        return None
    if points[-1][0] - points[0][0] < _TREND_MIN_SPAN_DAYS:
        return None

    n = len(points)
    mean_t = sum(p[0] for p in points) / n
    mean_u = sum(p[1] for p in points) / n
    variance = sum((p[0] - mean_t) ** 2 for p in points)
    if variance == 0:
        return None
    slope = sum((p[0] - mean_t) * (p[1] - mean_u) for p in points) / variance  # bytes/day
    if slope <= 0:
        return None
    return latest_avail / slope


def _finding(code: str, level: str, message: str) -> dict:
    return {"code": code, "level": level, "message": message}


def _graded(value: float, warn: float, crit: float, higher_is_worse: bool = True) -> str | None:
    """Map a value to warn/crit against two thresholds (None = within limits)."""
    if higher_is_worse:
        if value >= crit:
            return LEVEL_CRIT
        if value >= warn:
            return LEVEL_WARN
    else:
        if value <= crit:
            return LEVEL_CRIT
        if value <= warn:
            return LEVEL_WARN
    return None


def evaluate_client(
    rule: dict,
    rule_state: dict | None,
    samples: list[dict],
    thresholds: dict | None = None,
    now: float | None = None,
) -> dict:
    """
    Build the status summary of one client (one scheduled-backup rule).

    Args:
        rule:       Rule dict from ScheduleManager (label, server_profile...).
        rule_state: Backup track record kept by HealthMonitor for this rule:
                    consecutive_failures, last_ok_ts, last_status, last_error.
        samples:    Chronological health samples of the rule's server.
        thresholds: Overrides for DEFAULT_THRESHOLDS.
        now:        Reference time (defaults to time.time()).

    Returns:
        JSON-serializable dict consumed by the dashboard and the alert
        manager: level, findings[], and the headline metrics.
    """
    now = now if now is not None else time.time()
    limits = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    state = rule_state or {}
    latest = samples[-1] if samples else None
    findings: list[dict] = []

    summary: dict = {
        "rule_id": rule.get("id", ""),
        "label": rule.get("label") or rule.get("db_name", ""),
        "profile": rule.get("server_profile", ""),
        "db_name": rule.get("db_name", ""),
        "host": (latest or {}).get("host", ""),
        "probe_ts": (latest or {}).get("ts"),
        "reachable": (latest or {}).get("reachable"),
        "disk_mount": None, "disk_pct": None, "disk_free": None, "disk_total": None,
        "days_to_full": None,
        "db_bytes": (latest or {}).get("db_bytes"),
        "pg_ok": (latest or {}).get("pg_ok"),
        "odoo_http": (latest or {}).get("odoo_http"),
        "consecutive_failures": int(state.get("consecutive_failures") or 0),
        "last_ok_ts": state.get("last_ok_ts"),
        "last_run_ts": state.get("last_run_ts"),
        "last_status": state.get("last_status"),
        "last_error": state.get("last_error", ""),
        "days_since_ok": None,
    }

    if not rule.get("enabled", True):
        summary.update(level=LEVEL_OFF, findings=[])
        return summary

    # ── Backup track record (needs no SSH — works even if the server is down) ─
    fails = summary["consecutive_failures"]
    level = _graded(fails, limits["backup_fail_warn"], limits["backup_fail_crit"])
    if level:
        # First line only: backup errors are multi-line ("Disponible: ...").
        detail = (summary["last_error"] or "").strip().split("\n", 1)[0][:140]
        findings.append(_finding(
            "backup_failing", level,
            f"{fails} respaldos fallidos seguidos" + (f": {detail}" if detail else ""),
        ))

    if summary["last_ok_ts"]:
        days = (now - float(summary["last_ok_ts"])) / 86400.0
        summary["days_since_ok"] = days
        level = _graded(days, limits["days_without_backup_warn"], limits["days_without_backup_crit"])
        if level:
            findings.append(_finding(
                "backup_stale", level, f"Último respaldo correcto hace {days:.0f} días",
            ))
    elif state:
        findings.append(_finding(
            "backup_never", LEVEL_WARN, "No hay ningún respaldo correcto registrado",
        ))

    # ── Server probe ─────────────────────────────────────────────────────────
    if latest is None:
        findings.append(_finding("no_probe", LEVEL_UNKNOWN, "Servidor aún sin sondear"))
    else:
        age_hours = (now - latest["ts"]) / 3600.0
        if age_hours > limits["probe_stale_hours"]:
            findings.append(_finding(
                "probe_stale", LEVEL_UNKNOWN,
                f"Último sondeo hace {age_hours / 24:.1f} días: los datos del servidor están desactualizados",
            ))

        if not latest.get("reachable"):
            # Count how many probes in a row failed to connect: one miss can
            # be a network blip, two or more is an outage.
            streak = 0
            for sample in reversed(samples):
                if sample.get("reachable"):
                    break
                streak += 1
            findings.append(_finding(
                "unreachable", LEVEL_CRIT if streak >= 2 else LEVEL_WARN,
                f"Servidor inalcanzable por SSH ({streak} sondeo(s) seguidos): "
                f"{latest.get('error', '')[:120]}",
            ))
        else:
            disk = fullest_mount(latest)
            if disk:
                summary.update(
                    disk_mount=disk["mount"], disk_pct=disk["pct"],
                    disk_free=disk["avail"], disk_total=disk["total"],
                )
                level = _graded(disk["pct"], limits["disk_warn_pct"], limits["disk_crit_pct"])
                if level:
                    findings.append(_finding(
                        "disk_usage", level,
                        f"Disco {disk['mount']} al {disk['pct']:.0f} % "
                        f"({human_bytes(disk['avail'])} libres)",
                    ))

                remaining = days_to_full(samples, disk["mount"], now)
                summary["days_to_full"] = remaining
                if remaining is not None:
                    level = _graded(
                        remaining, limits["days_to_full_warn"], limits["days_to_full_crit"],
                        higher_is_worse=False,
                    )
                    if level:
                        findings.append(_finding(
                            "disk_trend", level,
                            f"Al ritmo actual el disco {disk['mount']} se llena en ~{remaining:.0f} días",
                        ))

            # The scheduled backup writes its dump to /tmp first: if the
            # database no longer fits there, tonight's backup WILL fail.
            tmp_avail, db_bytes = latest.get("tmp_avail"), latest.get("db_bytes")
            if tmp_avail is not None and db_bytes and tmp_avail < db_bytes:
                findings.append(_finding(
                    "tmp_too_small", LEVEL_WARN,
                    f"El próximo respaldo no cabe en /tmp: {human_bytes(tmp_avail)} libres, "
                    f"la base de datos ocupa {human_bytes(db_bytes)}",
                ))

            if latest.get("pg_ok") is False:
                findings.append(_finding(
                    "postgres_down", LEVEL_CRIT,
                    f"PostgreSQL no acepta conexiones: {latest.get('pg_msg', '')[:140]}",
                ))
            elif latest.get("pg_in_recovery"):
                findings.append(_finding(
                    "postgres_recovery", LEVEL_WARN, "PostgreSQL está en modo recuperación",
                ))

            odoo = latest.get("odoo_http")
            if odoo not in (None, "none", "200"):
                findings.append(_finding(
                    "odoo_unhealthy", LEVEL_WARN,
                    f"Odoo no responde correctamente en el puerto 8069 (HTTP {odoo})",
                ))

            if latest.get("error"):
                findings.append(_finding("probe_partial", LEVEL_UNKNOWN, latest["error"][:160]))

    # Most severe first, so the first finding is the headline.
    findings.sort(key=lambda f: _SEVERITY.get(f["level"], 0), reverse=True)
    summary["findings"] = findings
    summary["level"] = worst_level([f["level"] for f in findings])
    return summary
