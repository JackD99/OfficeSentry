"""The support package: one zip to attach to a bug report, with the secrets and the clients taken out.

An admin makes it on *Settings → Support*; ``python -m officesentry support-package`` makes the same one
from the shell when the portal won't start. ``build()`` gathers each part from stored state and the log
files only (nothing is asked of Microsoft), ``as_zip()`` packs it in memory, and nothing is written to the
server's disk unless the command is told to.

What goes in: versions and the environment, the effective configuration, the health checks, the database's
shape (tables, row counts, schema version, migrations), each client tenant as a number with its state and
last runs, failed runs and tasks of the last ``FAILURE_DAYS`` with their error text, the worker's queue, the
app connections' certificate dates, the installed packages, the recent activity log actions, and the tail of
the portal's and the worker's logs. A README in the zip says the same.

What never goes in: the keys and secrets (nothing is read from ``secrets.json``, the ``*_enc`` columns or
the encrypted settings), certificates and private keys, session tokens and cookies, passwords, e-mail
addresses, IP addresses, and report content of any kind. Client tenants appear as ``tenant <id>`` with
counts; their names, Microsoft tenant IDs and domains, and the portal's user names, are replaced in every
piece of free text by ``Redactor`` unless the admin asks for them (``include_identifiers``), which the
README of the zip then says in its first line. Free text (error messages, tracebacks, log lines) is also
swept for anything that looks like a token, a GUID, an address or a key, since Microsoft's error text and a
traceback can carry any of them.
"""

from __future__ import annotations

import importlib.metadata
import io
import json
import logging
import os
import platform
import re
import sqlite3
import sys
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__, logs, system_health
from .config import ENV_PREFIX, Config
from .db import latest_version, migration_files, schema_version, stored_app_version

log = logging.getLogger(__name__)

FAILURE_DAYS = 14
MAX_FAILED_RUNS = 100
MAX_FAILED_TASKS = 50
MAX_EVENTS_PER_RUN = 8
MAX_ACTIVITY = 150
LOG_TAIL_BYTES = 512 * 1024
ISO = "%Y-%m-%dT%H:%M:%SZ"

# Environment variables whose value is a secret: the package says whether they're set, never what to.
SECRET_ENV = re.compile(r"SECRET|KEY|TOKEN|PASSWORD|PASS\b|CODE", re.I)
# Settings whose value is plain configuration worth seeing. Everything else is listed by name only.
PLAIN_SETTINGS = (
    "app.version",
    "require_mfa",
    "currency",
    "mail_transport",
    "smtp_port",
    "smtp_security",
    "alerts.enabled",
    "alerts.format",
    "alerts.system",
    "alerts.min_severity",
    "alerts.on_failures",
    "alerts.on_recovery",
    "alerts.on_reports",
    "public_demo",
    "worker_seen_at",
)
PLAIN_SETTING_PREFIXES = ("retention.", "state_history.", "system_alert.")


# -- Redaction -----------------------------------------------------------------------------------------------


def _quoted_removed(m: re.Match) -> str:
    """``password: "two words"`` keeps its quotes, and loses the whole value, not just its first word."""
    value = m.group(3)
    quote = value[0] if value[0] in "'\"" else ""
    return f"{m.group(1)}{m.group(2)}{quote}[removed]{quote}"


# (what it matches, what replaces it). Order matters: PEM blocks and tokens before the long-value sweep.
PATTERNS: tuple[tuple[re.Pattern, str | Callable[[re.Match], str]], ...] = (
    (re.compile(r"-----BEGIN [A-Z ]+-----.*?-----END [A-Z ]+-----", re.S), "[certificate or key removed]"),
    # A JWT, and the portal's own session cookie (whose middle part, a timestamp, is shorter).
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"), "[token removed]"),
    (re.compile(r"\bgAAAAA[A-Za-z0-9_=-]{20,}"), "[encrypted value removed]"),
    # A header line, or a header in a printed dict: {'Authorization': 'Bearer …'}.
    (
        re.compile(r"(?i)\b(authorization['\"]?\s*[:=]\s*['\"]?)(?:(?:bearer|basic|negotiate)\s+)?[^\s'\",;]+"),
        r"\1[removed]",
    ),
    (re.compile(r"(?i)\b(bearer\s+)(?=[\w.~+/=-]*\d)[\w.~+/=-]{12,}"), r"\1[removed]"),
    (re.compile(r"(?i)\b((?:set-)?cookie\s*[:=]\s*)[^\r\n]+"), r"\1[removed]"),
    (
        re.compile(
            r"(?i)\b([\w-]*(?:password|passwd|pwd|secret|api[_-]?key|private_key|encryption_keys?|token|"
            r"device_code|sid))\b(['\"]?\s*[:=]\s*)(\"[^\"\r\n]*\"|'[^'\r\n]*'|['\"]?[^\s'\"&,;]+)"
        ),
        _quoted_removed,
    ),
    (
        re.compile(r"(?i)([?&](?:token|code|state|sig|signature|key|secret|password|access_token)=)[^&\s]+"),
        r"\1[removed]",
    ),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[email]"),
    (
        re.compile(
            r"(?i)\b[a-z0-9-]+\.(?:onmicrosoft\.(?:com|us|cn)|sharepoint\.(?:com|us|cn)|mail\.onmicrosoft\.com)\b"
        ),
        "[tenant domain]",
    ),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[ip]"),
    # IPv6, in full or shortened with "::", and with a prefix length (2001:db8::/32).
    (
        re.compile(
            r"(?i)(?<![\w:])(?:[0-9a-f]{1,4}:){4,7}[0-9a-f]{1,4}(?:/\d{1,3})?(?![\w:])"
            r"|(?<![\w:])(?=[:0-9a-f]*[0-9a-f])(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,6})?::"
            r"(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4}){0,6})?(?:/\d{1,3})?(?![\w:])"
        ),
        "[ip]",
    ),
    (re.compile(r"(?i)(C:\\Users\\|/home/|/Users/)[^\\/\s]+"), r"\1[user]"),
    (re.compile(r"[A-Za-z0-9_\-+/=]{40,}"), "[long value removed]"),
)
GUID = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


class Redactor:
    """Replaces secrets, addresses and (unless told to keep them) the names that identify a client or a
    person in free text. Tenant names and references become ``[tenant <id>]``, so the same client can still
    be followed from one file to the next; the portal's user names become ``[user]``."""

    def __init__(self, names: dict[str, str] | None = None, keep_identifiers: bool = False) -> None:
        self.keep_identifiers = keep_identifiers
        # Longest first, so "Contoso Ltd" goes before "Contoso".
        self.names = sorted(((k, v) for k, v in (names or {}).items() if len(k) >= 3), key=lambda kv: -len(kv[0]))
        self._name_re = re.compile("|".join(re.escape(k) for k, _ in self.names), re.I) if self.names else None
        self._labels = {k.lower(): v for k, v in self.names}

    def __call__(self, text) -> str:
        if text is None:
            return ""
        text = str(text)
        for pattern, replacement in PATTERNS:
            text = pattern.sub(replacement, text)
        if not self.keep_identifiers:
            text = GUID.sub("[guid]", text)
            if self._name_re is not None:
                text = self._name_re.sub(lambda m: self._labels.get(m.group(0).lower(), "[name]"), text)
        return text


def names_to_hide(conn, cfg: Config | None = None) -> dict[str, str]:
    """What identifies a client or a person in this install, and the label that stands in for it. The
    portal's own address names the MSP too (OFFICESENTRY_DOMAIN is the bundled Caddy's, from the same .env)."""
    names: dict[str, str] = {}
    hosts = {urlsplit(cfg.base_url).hostname if cfg else None, os.environ.get(ENV_PREFIX + "DOMAIN", "").strip()}
    for host in hosts - {None, "", "localhost"}:
        names[host] = "[portal]"
    if conn is None:
        return names
    for row in conn.execute("SELECT id, name, tenant_ref FROM tenants").fetchall():
        names[row["name"]] = f"[client {row['id']}]"
        names[row["tenant_ref"]] = f"[client {row['id']}]"
    for (username,) in conn.execute("SELECT username FROM users").fetchall():
        names[username] = "[user]"
    for key in ("brand_name", "brand_contact", "brand_support_name", "brand_support_email", "brand_support_phone"):
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row and row[0]:
            names[row[0]] = "[your company]"
    return names


# -- The package ---------------------------------------------------------------------------------------------


@dataclass
class Part:
    """One file in the package, as the Support page lists it before the download."""

    name: str  # the file's name in the zip
    title: str
    holds: str  # what it holds, in a line
    ok: bool = True
    note: str = ""  # a count ("3 failed runs"), or why it couldn't be made


@dataclass
class Package:
    made_at: datetime
    include_identifiers: bool
    files: dict[str, bytes] = field(default_factory=dict)
    parts: list[Part] = field(default_factory=list)

    @property
    def folder(self) -> str:
        return f"officesentry-support-{self.made_at.strftime('%Y%m%d-%H%M%S')}"

    @property
    def filename(self) -> str:
        return self.folder + ".zip"

    @property
    def problems(self) -> list[Part]:
        return [p for p in self.parts if not p.ok]


def _json(data) -> bytes:
    return json.dumps(data, indent=2, sort_keys=True, default=str).encode("utf-8")


def _iso(value) -> str | None:
    return value.strftime(ISO) if isinstance(value, datetime) else value


def _tolerant(conn, sql: str, params=()) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def build(
    conn: sqlite3.Connection | None,
    cfg: Config | None,
    *,
    include_identifiers: bool = False,
    now: datetime | None = None,
    config_error: str | None = None,
    data_dir: Path | None = None,
) -> Package:
    """Gather every part. ``conn`` may be None (the database can't be opened) and ``cfg`` may be None (the
    configuration doesn't load, ``config_error`` says why): the parts that can still be made are, and the
    README names the ones that couldn't."""
    now = now or datetime.now(UTC)
    pkg = Package(now, include_identifiers)
    data_dir = Path(data_dir or (cfg.data_dir if cfg else os.environ.get(ENV_PREFIX + "DATA_DIR", "data")))
    try:
        names = names_to_hide(conn, cfg)
    except sqlite3.Error:
        names = names_to_hide(None, cfg)
    redact = Redactor(names, keep_identifiers=include_identifiers)

    def part(name: str, title: str, holds: str, make, note=lambda data: ""):
        try:
            data = make()
            pkg.files[name] = data if isinstance(data, bytes) else _json(data)
            pkg.parts.append(Part(name, title, holds, True, note(data)))
        except Exception as exc:  # one part failing must not lose the rest
            log.warning("Support package: %s couldn't be made", name, exc_info=True)
            pkg.parts.append(Part(name, title, holds, False, f"Couldn't be made: {redact(exc)}"))

    part(
        "summary.json",
        "Summary",
        "Office Sentry's version, Python, operating system, the database's version and size, disk space",
        lambda: _summary(conn, cfg, data_dir, now, config_error),
    )
    part(
        "environment.json",
        "Configuration",
        "Every OFFICESENTRY_* setting (secrets shown as set or not set) and the configuration in force",
        lambda: _environment(cfg, redact),
    )
    part(
        "health.json",
        "Health checks",
        "The System status checks with their result and the suggested fix",
        lambda: _health(conn, cfg, now, redact),
        lambda d: f"{d['status']}" if d else "",
    )
    part(
        "database.json",
        "Database",
        "Tables and row counts, applied migrations, settings by name (values only for plain ones)",
        lambda: _database(conn, cfg, redact),
        lambda d: f"{len(d['tables'])} tables",
    )
    part(
        "clients.json",
        "Clients",
        "Each client as a number: cloud, state, consent, and the last run of each data source",
        lambda: _clients(conn, redact, include_identifiers),
        lambda d: f"{len(d)} clients",
    )
    part(
        "failures.json",
        "Recent failures",
        f"Failed collection runs and background tasks of the last {FAILURE_DAYS} days, with their error text",
        lambda: _failures(conn, redact, now),
        lambda d: f"{len(d['runs'])} failed runs, {len(d['tasks'])} failed tasks",
    )
    part(
        "worker.json",
        "Worker and queue",
        "The worker's last heartbeat and what's queued and running",
        lambda: _worker(conn, now),
    )
    part(
        "connections.json",
        "App connections",
        "Each app connection's cloud and certificate dates (never the certificate or its key)",
        lambda: _connections(conn, include_identifiers),
    )
    part(
        "activity.json",
        "Recent activity",
        f"The last {MAX_ACTIVITY} actions in the activity log, without who or from where",
        lambda: _activity(conn, redact, include_identifiers),
        lambda d: f"{len(d)} actions",
    )
    part("packages.txt", "Installed packages", "Every Python package and its version", _packages)
    for process in logs.PROCESSES:
        part(
            f"logs/{process}.log",
            f"{'Portal' if process == 'web' else 'Worker'} log",
            f"The last {LOG_TAIL_BYTES // 1024} KB of the {process} log, swept for addresses, names and tokens",
            lambda process=process: redact(logs.tail(data_dir, process, LOG_TAIL_BYTES)).encode("utf-8"),
            lambda d: f"{len(d) // 1024} KB" if d else "empty",
        )
    pkg.files["README.txt"] = _readme(pkg).encode("utf-8")
    return pkg


def as_zip(pkg: Package) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in ["README.txt", *sorted(n for n in pkg.files if n != "README.txt")]:
            info = zipfile.ZipInfo(f"{pkg.folder}/{name}", date_time=pkg.made_at.timetuple()[:6])
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, pkg.files[name])
    return buf.getvalue()


def write(pkg: Package, out: Path | None) -> Path | None:
    """Write the zip to ``out`` (``None``: standard output). Returns the path written."""
    data = as_zip(pkg)
    if out is None:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return None
    out.write_bytes(data)
    return out


# -- The parts -----------------------------------------------------------------------------------------------


def _summary(conn, cfg, data_dir: Path, now: datetime, config_error: str | None) -> dict:
    db_path = cfg.db_path if cfg else data_dir / "officesentry.db"
    summary = {
        "made_at": now.strftime(ISO),
        "version": __version__,
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "docker": Path("/.dockerenv").exists(),
        "data_dir": str(data_dir),
        "database_file_bytes": db_path.stat().st_size if db_path.exists() else None,
        "database_wal_bytes": wal.stat().st_size if (wal := db_path.with_name(db_path.name + "-wal")).exists() else 0,
        "events_folder_bytes": _folder_bytes(data_dir / "events"),
        "backups_in_data_folder": len(list((data_dir / "backups").glob("*.db*")))
        if (data_dir / "backups").is_dir()
        else 0,
        "log_bytes": logs.sizes(data_dir),
        "errors_last_24h": logs.recent_errors(data_dir, 24, now),
        "config_error": config_error,
    }
    try:
        used, free, total = system_health.disk_usage(data_dir)
        summary["disk"] = {"used_percent": used, "free_bytes": free, "total_bytes": total}
    except OSError as exc:
        summary["disk"] = {"error": str(exc)}
    if conn is not None:
        try:
            summary["schema_version"] = schema_version(conn)
            summary["schema_expected"] = latest_version()
            summary["upgraded_by_release"] = stored_app_version(conn)
        except sqlite3.Error as exc:
            summary["database_error"] = f"{type(exc).__name__}: {exc}"
    else:
        summary["database_error"] = "the database couldn't be opened"
    return summary


def _folder_bytes(folder: Path) -> int:
    if not folder.is_dir():
        return 0
    return sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())


def _environment(cfg, redact: Redactor) -> dict:
    env = {}
    for name, value in sorted(os.environ.items()):
        if not name.startswith(ENV_PREFIX):
            continue
        env[name] = "[set]" if SECRET_ENV.search(name) else redact(value)
    effective = None
    if cfg is not None:
        effective = {
            "base_url": redact(cfg.base_url),
            "timezone": cfg.timezone,
            "collect_cron": cfg.collect_cron,
            "max_age_hours": cfg.max_age_hours,
            "keep_daily": cfg.keep_daily,
            "keep_monthly": cfg.keep_monthly,
            "worker_concurrency": cfg.worker_concurrency,
            "secure_cookies": cfg.secure_cookies,
            "require_mfa": cfg.require_mfa,
            "require_mfa_explicit": cfg.require_mfa_explicit,
            "trusted_proxies": cfg.trusted_proxies,
            "session_idle_minutes": cfg.session_idle_minutes,
            "session_hours": cfg.session_hours,
            "public_demo": cfg.public_demo,
            "encryption_keys": len(cfg.encryption_keys),
            "secrets_file": str(cfg.secrets_file) if cfg.secrets_file else "(in the data folder)",
        }
    return {"variables": env, "effective": effective}


def _health(conn, cfg, now, redact: Redactor) -> dict | None:
    """The checks as the System status page shows them. Their messages and fixes quote errors (a run's,
    a backup's), so they're swept like any other free text."""
    if conn is None or cfg is None:
        return None
    health = system_health.assess(conn, cfg, now)
    data = health.as_json()
    for check in health.checks:
        data["checks"][check.key]["fix"] = check.fix
    return _redacted(data, redact)


def _redacted(value, redact: Redactor):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redacted(v, redact) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redacted(v, redact) for v in value]
    return value


def _database(conn, cfg, redact: Redactor) -> dict:
    if conn is None:
        raise RuntimeError("the database couldn't be opened")
    tables = {}
    for (table,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall():
        if table.startswith("sqlite_"):
            continue
        count = "SUM(items)" if table == "objects" else "COUNT(*)"
        tables[table] = conn.execute(f"SELECT COALESCE({count}, 0) FROM {table}").fetchone()[0]  # noqa: S608 (names from sqlite_master)
    version = schema_version(conn)
    settings = {}
    for key, value in _tolerant(conn, "SELECT key, value FROM settings ORDER BY key"):
        if key.startswith(PLAIN_SETTING_PREFIXES) or key in PLAIN_SETTINGS:
            settings[key] = redact(value)
        else:
            settings[key] = f"[set, {len(value or '')} characters]" if value else "[empty]"
    pragma = {}
    for name in ("journal_mode", "page_size", "page_count", "freelist_count", "user_version", "foreign_keys"):
        try:
            pragma[name] = conn.execute(f"PRAGMA {name}").fetchone()[0]
        except sqlite3.Error as exc:
            pragma[name] = f"error: {exc}"
    return {
        "schema_version": version,
        "schema_expected": latest_version(),
        "migrations_applied": [p.name for n, p in migration_files() if n <= version],
        "migrations_pending": [p.name for n, p in migration_files() if n > version],
        "pragma": pragma,
        "tables": tables,
        "settings": settings,
    }


def _clients(conn, redact: Redactor, include_identifiers: bool) -> list[dict]:
    if conn is None:
        raise RuntimeError("the database couldn't be opened")
    out = []
    for t in conn.execute("SELECT * FROM tenants ORDER BY id").fetchall():
        latest = {}
        for r in _tolerant(
            conn,
            "SELECT collector, status, finished_at, created_at, error FROM runs WHERE tenant_id = ? AND id IN"
            " (SELECT MAX(id) FROM runs WHERE tenant_id = ? GROUP BY collector) ORDER BY collector",
            (t["id"], t["id"]),
        ):
            latest[r["collector"]] = {
                "status": r["status"],
                "at": r["finished_at"] or r["created_at"],
                "error": redact(r["error"]) if r["error"] else None,
            }
        keys = t.keys()
        entry = {
            "id": t["id"],
            "cloud": t["cloud"] if "cloud" in keys else "commercial",
            "enabled": bool(t["enabled"]),
            "archived": bool(t["archived_at"]) if "archived_at" in keys else False,
            "consented": bool(t["consented_at"]),
            "connection_id": t["connection_id"],
            "created_at": t["created_at"],
            "capabilities": _capabilities(t),
            "latest_runs": latest,
            "open_findings": _count(
                conn, "SELECT COUNT(*) FROM findings WHERE tenant_id = ? AND resolved_at IS NULL", (t["id"],)
            ),
            "users_with_access": _count(conn, "SELECT COUNT(*) FROM user_tenants WHERE tenant_id = ?", (t["id"],)),
        }
        if include_identifiers:
            entry["name"] = t["name"]
            entry["tenant_ref"] = t["tenant_ref"]
        out.append(entry)
    return out


def _capabilities(tenant) -> dict | None:
    try:
        caps = json.loads(tenant["capabilities"] or "{}")
    except (ValueError, TypeError, KeyError):
        return None
    return {k: bool(v) for k, v in caps.items()} if isinstance(caps, dict) else None


def _count(conn, sql: str, params=()) -> int | None:
    rows = _tolerant(conn, sql, params)
    return rows[0][0] if rows else None


def _failures(conn, redact: Redactor, now: datetime) -> dict:
    if conn is None:
        raise RuntimeError("the database couldn't be opened")
    since = (now - timedelta(days=FAILURE_DAYS)).strftime(ISO)
    runs = []
    for r in conn.execute(
        "SELECT id, tenant_id, collector, trigger, created_at, started_at, finished_at, error FROM runs"
        " WHERE status = 'failed' AND created_at >= ? ORDER BY id DESC LIMIT ?",
        (since, MAX_FAILED_RUNS),
    ).fetchall():
        events = _tolerant(
            conn,
            "SELECT ts, level, message FROM run_events WHERE run_id = ? AND level IN ('error', 'warning')"
            " ORDER BY id DESC LIMIT ?",
            (r["id"], MAX_EVENTS_PER_RUN),
        )
        runs.append(
            {
                "run": r["id"],
                "tenant": r["tenant_id"],
                "collector": r["collector"],
                "trigger": r["trigger"],
                "created_at": r["created_at"],
                "started_at": r["started_at"],
                "finished_at": r["finished_at"],
                "error": redact(r["error"]),
                "events": [
                    {"ts": e["ts"], "level": e["level"], "message": redact(e["message"])} for e in reversed(events)
                ],
            }
        )
    tasks = [
        {
            "task": t["id"],
            "kind": t["kind"],
            "tenant": t["tenant_id"],
            "attempts": t["attempts"],
            "created_at": t["created_at"],
            "finished_at": t["finished_at"],
            "error": redact(t["error"]),
        }
        for t in _tolerant(
            conn,
            "SELECT id, kind, tenant_id, attempts, created_at, finished_at, error FROM tasks"
            " WHERE status = 'failed' AND created_at >= ? ORDER BY id DESC LIMIT ?",
            (since, MAX_FAILED_TASKS),
        )
    ]
    by_collector = {
        r["collector"]: r["n"]
        for r in _tolerant(
            conn,
            "SELECT collector, COUNT(*) AS n FROM runs WHERE status = 'failed' AND created_at >= ?"
            " GROUP BY collector ORDER BY n DESC",
            (since,),
        )
    }
    return {"since": since, "failed_runs_by_collector": by_collector, "runs": runs, "tasks": tasks}


def _worker(conn, now: datetime) -> dict:
    if conn is None:
        raise RuntimeError("the database couldn't be opened")
    from . import worker

    seen = worker.last_seen(conn)
    status = {
        s: n
        for s, n in _tolerant(
            conn,
            "SELECT status, COUNT(*) FROM runs INDEXED BY idx_runs_status WHERE status IN ('queued', 'running')"
            " GROUP BY status",
        )
    }
    running = [
        {
            "run": r["id"],
            "tenant": r["tenant_id"],
            "collector": r["collector"],
            "started_at": r["started_at"],
            "lease_until": r["lease_until"],
        }
        for r in _tolerant(
            conn,
            "SELECT id, tenant_id, collector, started_at, lease_until FROM runs INDEXED BY idx_runs_status"
            " WHERE status = 'running'",
        )
    ]
    oldest = _tolerant(conn, "SELECT MIN(created_at) FROM runs INDEXED BY idx_runs_status WHERE status = 'queued'")
    tasks = {
        s: n
        for s, n in _tolerant(
            conn, "SELECT status, COUNT(*) FROM tasks WHERE status IN ('queued', 'running') GROUP BY status"
        )
    }
    return {
        "last_heartbeat": _iso(seen),
        "alive": worker.is_alive(conn, now),
        "runs": {
            "queued": status.get("queued", 0),
            "running": status.get("running", 0),
            "oldest_queued": oldest[0][0] if oldest else None,
        },
        "running_runs": running,
        "tasks": {"queued": tasks.get("queued", 0), "running": tasks.get("running", 0)},
    }


def _connections(conn, include_identifiers: bool) -> list[dict]:
    if conn is None:
        raise RuntimeError("the database couldn't be opened")
    out = []
    for c in conn.execute("SELECT * FROM app_connections ORDER BY id").fetchall():
        keys = c.keys()
        entry = {
            "id": c["id"],
            "cloud": c["cloud"] if "cloud" in keys else "commercial",
            "has_client_id": bool(c["client_id"]),
            "certificate_not_after": c["not_after"],
            "replacement_pending": bool(c["next_thumbprint"]) if "next_thumbprint" in keys else False,
            "replacement_not_after": c["next_not_after"] if "next_not_after" in keys else None,
            "tenants": _count(conn, "SELECT COUNT(*) FROM tenants WHERE connection_id = ?", (c["id"],)),
            "created_at": c["created_at"],
        }
        if include_identifiers:
            entry["client_id"] = c["client_id"]
        out.append(entry)
    return out


def _activity(conn, redact: Redactor, include_identifiers: bool) -> list[dict]:
    if conn is None:
        raise RuntimeError("the database couldn't be opened")
    out = []
    for a in conn.execute(
        "SELECT ts, username, action, tenant_id, detail FROM audit_log ORDER BY id DESC LIMIT ?", (MAX_ACTIVITY,)
    ).fetchall():
        entry = {"ts": a["ts"], "action": a["action"], "tenant": a["tenant_id"], "detail": redact(a["detail"])}
        if include_identifiers:
            entry["user"] = a["username"]
        out.append(entry)
    return out


def _packages() -> bytes:
    lines = sorted(
        (f"{d.metadata['Name']}=={d.version}" for d in importlib.metadata.distributions() if d.metadata["Name"]),
        key=str.lower,
    )
    return ("\n".join(dict.fromkeys(lines)) + "\n").encode("utf-8")


def _readme(pkg: Package) -> str:
    lines = [
        f"Office Sentry support package, made {pkg.made_at.strftime('%Y-%m-%d %H:%M')} UTC by version {__version__}.",
        "",
    ]
    if pkg.include_identifiers:
        lines += [
            "CLIENT IDENTIFIERS INCLUDED: this package keeps client names, Microsoft tenant IDs, domains and",
            "user names, because the admin asked for them. Share it only through a private channel, never on a",
            "public issue.",
            "",
        ]
    else:
        lines += [
            "Clients appear as numbers ([client 3]), people as [user] and [email], and anything that looked like a",
            "token, key, GUID or IP address was replaced before the file was written.",
            "",
        ]
    lines += [
        "Attach the whole zip to a bug report: https://github.com/JackD99/OfficeSentry/issues/new?template=bug.yml",
        "(open it first if you want to read it; every file is text).",
        "",
        "What's inside",
        "",
    ]
    for p in pkg.parts:
        state = "" if p.ok else "  (NOT MADE: " + p.note + ")"
        lines.append(f"  {p.name:<22} {p.holds}{state}")
    lines += [
        "",
        "What's never inside",
        "",
        "  The encryption keys and secret key, certificates and private keys, passwords and two-step secrets,",
        "  session tokens, cookies, webhook URLs, SMTP settings beyond port and security, e-mail addresses,",
        "  IP addresses, and anything a report shows (users, mailboxes, devices, sign-ins, findings' detail).",
        "",
        "Made by Settings -> Support in the portal, or: python -m officesentry support-package",
        "",
    ]
    return "\n".join(lines)


# -- For the Support page --------------------------------------------------------------------------------------


def preview(conn, cfg: Config, now: datetime | None = None) -> dict:
    """Cheap figures for the Support page, without building the package."""
    now = now or datetime.now(UTC)
    since = (now - timedelta(days=FAILURE_DAYS)).strftime(ISO)
    return {
        "errors": logs.recent_errors(cfg.data_dir, 24, now),
        "log_bytes": logs.sizes(cfg.data_dir),
        "failed_runs": _count(conn, "SELECT COUNT(*) FROM runs WHERE status = 'failed' AND created_at >= ?", (since,))
        or 0,
        "failed_tasks": _count(conn, "SELECT COUNT(*) FROM tasks WHERE status = 'failed' AND created_at >= ?", (since,))
        or 0,
        "tenants": _count(conn, "SELECT COUNT(*) FROM tenants") or 0,
    }


PARTS = [
    ("Summary", "versions, the database's size and version, disk space, errors logged in the last 24 hours"),
    ("Configuration", "every OFFICESENTRY_* setting, secrets shown as set or not set"),
    ("Health checks", "the System status checks, their results and suggested fixes"),
    ("Database", "tables with row counts, applied migrations, settings by name"),
    ("Clients", "each client as a number: cloud, state, consent, last run of each data source"),
    ("Recent failures", f"failed runs and background tasks of the last {FAILURE_DAYS} days, with their error text"),
    ("Worker and queue", "the worker's last heartbeat, what's queued and running"),
    ("App connections", "each connection's cloud and certificate dates, never the certificate or key"),
    ("Recent activity", f"the last {MAX_ACTIVITY} actions in the activity log, without who or from where"),
    ("Installed packages", "every Python package and its version"),
    ("Portal and worker logs", f"the last {LOG_TAIL_BYTES // 1024} KB of each, swept for names, addresses and tokens"),
]
