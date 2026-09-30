"""
Read-only health probe of a client server, plus the persistent store of the
samples it produces.

Why this exists: on 2026-09-30 the Variedades server went down because its
disk filled up gradually over months. The only place that showed it coming
was the scheduled backup itself failing with "Espacio insuficiente en /tmp"
for 19 days. This module measures the server directly (disk, PostgreSQL,
Odoo) on every monitoring cycle so the trend is visible *before* a backup
starts failing, and keeps each measurement so a projection can be drawn.

Nothing here modifies the remote server: every command is an inspection
(df, /proc, psql SELECT, ss, curl to localhost).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .pg_exec import PgTarget, build_psql
from .ssh_client import SSHClient

_DATA_DIR = Path.home() / ".odoo_backup_tool"
_METRICS_FILE = _DATA_DIR / "health_metrics.jsonl"

# Samples older than this are dropped on prune: long enough to draw a
# multi-month trend, short enough to keep the file small (a few MB).
_RETENTION_DAYS = 180

# Prune only every N appends so a normal append never rewrites the file.
_PRUNE_EVERY_APPENDS = 200

# Pseudo filesystems that never hold client data — excluded from disk checks.
_PSEUDO_FILESYSTEMS = {
    "tmpfs", "devtmpfs", "udev", "overlay", "shm", "efivarfs", "none",
    "squashfs", "ramfs", "proc", "sysfs", "cgroup", "cgroup2",
}

# Partitions smaller than this (e.g. /boot/efi) are irrelevant for capacity.
_MIN_MOUNT_BYTES = 2 * 1024 ** 3

# Section markers printed by the probe script; parse_probe_output() splits on them.
_MARK = "@@OBT_"

# Odoo's default HTTP port. The health endpoint is only queried when
# something is actually listening there, so servers that publish Odoo on a
# different port report "none" (unknown) instead of a false alarm.
_ODOO_PORT = 8069


def build_probe_command(target: PgTarget, db_name: str = "") -> str:
    """
    Build the single read-only shell command that gathers every metric.

    One command (instead of one SSH round-trip per metric) keeps the probe
    fast and leaves a single exec channel open on the client's sshd.

    Args:
        target:  Where PostgreSQL runs (bare-metal or Docker container).
        db_name: Production database whose size should be measured; empty
                 skips the size query.

    Returns:
        A POSIX shell command string safe to pass to SSHClient.execute().
    """
    pg_state = build_psql(target, "-d postgres -Atc \"SELECT pg_is_in_recovery()\"")
    parts = [
        f"echo '{_MARK}DF'; df -B1 -P 2>/dev/null",
        f"echo '{_MARK}TMP'; df -B1 -P /tmp 2>/dev/null | tail -n 1",
        f"echo '{_MARK}LOAD'; cat /proc/loadavg 2>/dev/null; nproc 2>/dev/null",
        f"echo '{_MARK}MEM'; grep -E '^(MemTotal|MemAvailable):' /proc/meminfo 2>/dev/null",
        f"echo '{_MARK}UP'; cut -d' ' -f1 /proc/uptime 2>/dev/null",
        # psql prints directory warnings on stderr when run from /root via
        # sudo; only the last line (the actual value or the FATAL) matters.
        f"echo '{_MARK}PG'; _o=$({pg_state} 2>&1); echo \"rc=$?\"; echo \"$_o\" | tail -n 1",
    ]
    if db_name:
        # db_name comes from a saved rule; quote-doubling keeps a stray
        # apostrophe from breaking out of the SQL literal.
        safe_db = db_name.replace("'", "''")
        pg_size = build_psql(
            target, f"-d postgres -Atc \"SELECT pg_database_size('{safe_db}')\""
        )
        parts.append(f"echo '{_MARK}DB'; {pg_size} 2>/dev/null | tail -n 1")
    parts.append(
        f"echo '{_MARK}ODOO'; "
        f"if (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -q ':{_ODOO_PORT} '; then "
        f"curl -s -o /dev/null -m 8 -w '%{{http_code}}' http://127.0.0.1:{_ODOO_PORT}/web/health 2>/dev/null "
        f"|| echo err; else echo none; fi"
    )
    return "; ".join(parts)


def _parse_df(lines: list[str]) -> list[dict]:
    """Parse `df -B1 -P` rows into real, data-bearing mounts."""
    mounts: list[dict] = []
    for line in lines:
        cols = line.split()
        # Filesystem 1-blocks Used Available Capacity Mounted-on
        if len(cols) < 6 or not cols[1].isdigit():
            continue
        filesystem, total, used, avail = cols[0], int(cols[1]), int(cols[2]), int(cols[3])
        mount = " ".join(cols[5:])
        if filesystem in _PSEUDO_FILESYSTEMS or filesystem.startswith("/dev/loop"):
            continue
        if mount.startswith(("/snap", "/boot", "/run", "/sys", "/dev", "/var/lib/docker/")):
            continue
        if total < _MIN_MOUNT_BYTES:
            continue
        mounts.append({"mount": mount, "total": total, "used": used, "avail": avail})
    return mounts


def parse_probe_output(text: str) -> dict:
    """
    Turn the raw output of build_probe_command() into a metrics dict.

    Pure function (no I/O) so it can be unit-tested with captured output.
    Any section that is missing or unparseable yields None for its fields
    rather than raising — a partial probe is still useful.
    """
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith(_MARK):
            current = line[len(_MARK):]
            sections[current] = []
        elif current is not None and line:
            sections[current].append(line)

    metrics: dict = {
        "mounts": _parse_df(sections.get("DF", [])),
        "tmp_avail": None,
        "load1": None, "cpus": None,
        "mem_total": None, "mem_avail": None,
        "uptime_secs": None,
        "pg_ok": None, "pg_in_recovery": None, "pg_msg": "",
        "db_bytes": None,
        "odoo_http": None,
    }

    tmp_rows = _parse_df_any(sections.get("TMP", []))
    if tmp_rows:
        metrics["tmp_avail"] = tmp_rows[0]["avail"]

    load = sections.get("LOAD", [])
    try:
        metrics["load1"] = float(load[0].split()[0])
        metrics["cpus"] = int(load[1])
    except (IndexError, ValueError):
        pass

    for line in sections.get("MEM", []):
        key, _, rest = line.partition(":")
        try:
            kib = int(rest.split()[0])
        except (IndexError, ValueError):
            continue
        if key == "MemTotal":
            metrics["mem_total"] = kib * 1024
        elif key == "MemAvailable":
            metrics["mem_avail"] = kib * 1024

    try:
        metrics["uptime_secs"] = float(sections.get("UP", [""])[0])
    except ValueError:
        pass

    pg = sections.get("PG", [])
    if pg and pg[0].startswith("rc="):
        value = pg[1] if len(pg) > 1 else ""
        if pg[0] == "rc=0" and value in ("t", "f"):
            metrics["pg_ok"] = True
            metrics["pg_in_recovery"] = value == "t"
        else:
            metrics["pg_ok"] = False
            metrics["pg_msg"] = value[:300]

    db = sections.get("DB", [])
    if db and db[-1].isdigit():
        metrics["db_bytes"] = int(db[-1])

    odoo = sections.get("ODOO", [])
    if odoo:
        metrics["odoo_http"] = odoo[-1]

    return metrics


def _parse_df_any(lines: list[str]) -> list[dict]:
    """Like _parse_df but without filtering — used for the single /tmp row."""
    rows: list[dict] = []
    for line in lines:
        cols = line.split()
        if len(cols) >= 6 and cols[1].isdigit():
            rows.append({"mount": cols[5], "total": int(cols[1]),
                         "used": int(cols[2]), "avail": int(cols[3])})
    return rows


def probe_server(profile: dict, db_name: str = "", connect_timeout: int = 15) -> dict:
    """
    Connect to a server with its saved profile and take one health sample.

    Never raises: a server that cannot be reached is itself the finding,
    so the failure is returned as a sample with reachable=False.

    Args:
        profile: Entry from ProfileManager.get() (host/port/user/password...).
        db_name: Production database to size (optional).
        connect_timeout: SSH connect timeout in seconds.

    Returns:
        Sample dict ready for HealthStore.append().
    """
    sample: dict = {
        "ts": time.time(),
        "profile": profile.get("name", ""),
        "host": profile.get("host", ""),
        "db_name": db_name,
        "reachable": False,
        "error": "",
    }
    ssh = SSHClient()
    try:
        ssh.connect(
            profile["host"], int(profile["port"]),
            profile["user"], profile["password"],
            timeout=connect_timeout,
        )
    except Exception as exc:  # noqa: BLE001 — unreachable is a result, not a crash
        sample["error"] = str(exc)[:300]
        return sample

    try:
        target = PgTarget(
            container=profile.get("docker_container", ""),
            docker_exec_user=profile.get("docker_exec_user", ""),
        )
        _code, out, err = ssh.execute(build_probe_command(target, db_name), timeout=90)
        sample.update(parse_probe_output(out))
        sample["reachable"] = True
        if not sample["mounts"]:
            sample["error"] = (err or "df no devolvio particiones")[:300]
    except Exception as exc:  # noqa: BLE001
        # Connected but the probe itself failed (timeout, dropped session).
        sample["reachable"] = True
        sample["error"] = f"Sondeo incompleto: {exc}"[:300]
        sample.setdefault("mounts", [])
    finally:
        ssh.close()
    return sample


class HealthStore:
    """
    Append-only JSON Lines store of health samples
    (~/.odoo_backup_tool/health_metrics.jsonl).

    Append-only for the same reason as HistoryManager: recording a sample
    never rewrites the file, so the GUI and the headless monitor can both
    append without corrupting each other.
    """

    def __init__(self, path: str | os.PathLike | None = None) -> None:
        self._path = Path(path) if path else _METRICS_FILE
        self._lock = threading.Lock()
        self._appends_since_prune = 0

    def append(self, sample: dict) -> None:
        """Persist one sample."""
        line = json.dumps(sample, ensure_ascii=False)
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            self._appends_since_prune += 1
            if self._appends_since_prune >= _PRUNE_EVERY_APPENDS:
                self._appends_since_prune = 0
                self._prune_locked()

    def read(self, since_ts: float | None = None) -> list[dict]:
        """Return samples in chronological order, optionally newer than since_ts."""
        with self._lock:
            return self._read_locked(since_ts)

    def by_profile(self, since_ts: float | None = None) -> dict[str, list[dict]]:
        """Group samples by profile name, each list in chronological order."""
        grouped: dict[str, list[dict]] = {}
        for sample in self.read(since_ts):
            grouped.setdefault(sample.get("profile", ""), []).append(sample)
        return grouped

    def _read_locked(self, since_ts: float | None) -> list[dict]:
        if not self._path.exists():
            return []
        samples: list[dict] = []
        with open(self._path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    sample = json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn last line must not hide the rest
                if since_ts is None or sample.get("ts", 0) >= since_ts:
                    samples.append(sample)
        samples.sort(key=lambda s: s.get("ts", 0))
        return samples

    def _prune_locked(self) -> None:
        """Drop samples older than the retention window (atomic rewrite)."""
        cutoff = time.time() - _RETENTION_DAYS * 86400
        kept = self._read_locked(cutoff)
        tmp_path = self._path.with_suffix(".jsonl.tmp")
        with open(tmp_path, "w", encoding="utf-8") as fh:
            for sample in kept:
                fh.write(json.dumps(sample, ensure_ascii=False) + "\n")
        os.replace(tmp_path, self._path)
