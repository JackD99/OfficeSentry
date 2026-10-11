"""Office Sentry's own health: is the portal able to do its job, and what needs the MSP's attention.

``assess()`` runs a handful of cheap checks against stored state only (no Microsoft calls):

* **database**: it answers;
* **schema**: its version matches this code's migrations;
* **worker**: the background worker's heartbeat (``worker.is_alive``);
* **collection**: the last successful collection, and whether last night's scheduled
  collection failed for most tenants (``last_slot``);
* **backup**: the newest backup and the last attempt (``backups.read_status``);
* **disk**: how full the data folder's disk is;
* **certificates**: app certificates near expiry (``cert_lifecycle.expiring``);
* **erase**: whether what deleted tenants left on disk has been erased (``lifecycle.erase``).

Each check is ``ok``, ``warn`` (needs attention, the portal still works) or ``fail``
(the portal or worker can't do its job). ``/health/ready`` answers 503 when any check
fails, so an uptime monitor alerts; the System status page leads with whatever isn't ok;
``system_alerts`` emails and posts the same problems. Nothing here names a tenant or
carries an error message from Microsoft, so the JSON is safe for an uptime checker that
holds the monitoring token.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import shutil
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import __version__, backups, cert_lifecycle, lifecycle, schedule, worker
from .db import latest_version, schema_version
from .devices import parse_ts
from .text import duration, plural

log = logging.getLogger(__name__)

OK, WARN, FAIL = "ok", "warn", "fail"
RANK = {OK: 0, WARN: 1, FAIL: 2}

DISK_WARN = 85  # percent used: needs attention, and a system alert
DISK_FAIL = 95  # percent used: writes are about to fail, so /health/ready answers 503
COLLECTION_GRACE = timedelta(hours=12)  # on top of the time between two collection slots
TOKEN_KEY = "health.token_hash"  # noqa: S105 - a settings key; the value is the token's SHA-256
ISO = "%Y-%m-%dT%H:%M:%SZ"


@dataclass
class Check:
    key: str
    label: str
    status: str
    summary: str  # one line for people, never tenant data: "Checked in 20 seconds ago"
    fix: str = ""  # what to do when it isn't ok (only on the page and in alerts, not in the JSON)
    data: dict = field(default_factory=dict)  # figures for the JSON


@dataclass
class Health:
    checks: list[Check]
    checked_at: datetime

    @property
    def status(self) -> str:
        return max((c.status for c in self.checks), key=RANK.get, default=OK)

    @property
    def ok(self) -> bool:
        """Ready: nothing has failed (warnings don't stop the portal working)."""
        return self.status != FAIL

    @property
    def attention(self) -> list[Check]:
        """What isn't ok, worst first."""
        return sorted((c for c in self.checks if c.status != OK), key=lambda c: -RANK[c.status])

    def check(self, key: str) -> Check | None:
        return next((c for c in self.checks if c.key == key), None)

    def as_json(self) -> dict:
        return {
            "ok": self.ok,
            "status": self.status,
            "version": __version__,
            "checked_at": self.checked_at.strftime(ISO),
            "checks": {c.key: {"status": c.status, "summary": c.summary, **c.data} for c in self.checks},
        }


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime(ISO) if dt else None


def _hours(delta: timedelta) -> float:
    return round(delta.total_seconds() / 3600, 1)


# -- The checks ------------------------------------------------------------------------------


def check_database(conn, cfg, now) -> Check:
    conn.execute("SELECT 1 FROM settings LIMIT 1").fetchone()
    return Check("database", "Database", OK, "Answering")


def check_schema(conn, cfg, now) -> Check:
    have, want = schema_version(conn), latest_version()
    data = {"version": have, "expected": want}
    if have == want:
        return Check("schema", "Database version", OK, f"Up to date (version {have})", data=data)
    if have < want:
        return Check(
            "schema",
            "Database version",
            FAIL,
            f"Version {have}, this release needs {want}",
            "Restart the portal: it upgrades the database when it starts.",
            data,
        )
    return Check(
        "schema",
        "Database version",
        FAIL,
        f"Version {have} is newer than this release knows ({want})",
        "The database was upgraded by a newer Office Sentry. Go back to that release, or restore the "
        'pre-upgrade copy (DEPLOY.md, "Rolling back an update").',
        data,
    )


WORKER_FIX = (
    "On the server, run docker compose up -d (or start the worker service). If it stops again, "
    "docker compose logs worker shows why."
)


def check_worker(conn, cfg, now) -> Check:
    seen = worker.last_seen(conn)
    data = {"last_seen": _iso(seen), "age_seconds": int((now - seen).total_seconds()) if seen else None}
    if worker.is_alive(conn, now):
        return Check(
            "worker", "Background worker", OK, f"Checked in {duration((now - seen).total_seconds())} ago", data=data
        )
    summary = f"Stopped, last checked in {duration((now - seen).total_seconds())} ago" if seen else "Not running"
    return Check(
        "worker",
        "Background worker",
        FAIL,
        summary,
        "Nothing is collected and no scheduled email goes out until it runs. " + WORKER_FIX,
        data,
    )


@dataclass
class Slot:
    """One scheduled collection slot: how many tenants it covered and how many failed."""

    at: str
    tenants: int
    failed: int
    pending: int  # tenants with runs still queued or running
    top_error: str | None = None  # the most common first line of a failed run's error (alerts only)

    @property
    def complete(self) -> bool:
        return self.pending == 0

    @property
    def mass_failure(self) -> bool:
        """Most tenants failed: something every tenant shares (the certificate, the app, the network)."""
        return self.complete and self.failed >= 2 and self.failed * 2 > self.tenants


def collection_interval(cfg, now: datetime) -> timedelta:
    """How long data may go without a successful collection: the time between the last two slots of the
    collection schedule (a day, for the nightly default), plus a grace."""
    try:
        last = schedule.previous_fire(cfg.collect_cron, cfg.timezone, now)
        before = schedule.previous_fire(cfg.collect_cron, cfg.timezone, last - timedelta(seconds=1))
        gap = last - before
    except Exception:  # an invalid cron is refused at start-up; assume nightly here
        gap = timedelta(days=1)
    return max(gap, timedelta(days=1)) + COLLECTION_GRACE


SLOTS_BACK = 3  # schedule slots to look back through for the newest one that queued anything


def last_slot(conn, cfg, now: datetime, newer_than: datetime) -> Slot | None:
    """The newest scheduled collection slot after ``newer_than`` that queued runs.

    The slot times come from the collection schedule, as ``worker.schedule_tick`` makes them, never from the
    newest run carrying a ``scheduled_for``: a report's refresh runs carry the report's own due time, and one
    tenant's refresh must not stand in for a night every tenant failed. (A slot can queue nothing when every
    collector was still fresh; the one before it counts then.) A tenant counts as failed when more than half
    of its runs in the slot failed (a single collector that keeps failing for one tenant doesn't). Reads only
    the runs created since the slot, walking the newest rows by id, so it stays fast on years of history.
    """
    fire = now
    for _ in range(SLOTS_BACK):
        try:
            fire = schedule.previous_fire(cfg.collect_cron, cfg.timezone, fire)
        except Exception:  # an invalid cron is refused at start-up
            return None
        if fire <= newer_than:
            return None
        if slot := _slot(conn, fire.strftime(ISO)):
            return slot
        fire -= timedelta(seconds=1)
    return None


def _slot(conn, at: str) -> Slot | None:
    floor = conn.execute("SELECT id FROM runs WHERE created_at < ? ORDER BY id DESC LIMIT 1", (at,)).fetchone()
    rows = conn.execute(
        "SELECT tenant_id, status, error FROM runs WHERE id > ? AND scheduled_for = ? AND trigger = 'schedule'",
        (floor["id"] if floor else 0, at),
    ).fetchall()
    if not rows:
        return None
    per: dict[int, list] = {}
    for r in rows:
        per.setdefault(r["tenant_id"], []).append(r)
    failed = pending = 0
    errors: Counter = Counter()
    for runs in per.values():
        if any(r["status"] in ("queued", "running") for r in runs):
            pending += 1
            continue
        bad = [r for r in runs if r["status"] == "failed"]
        if len(bad) * 2 > len(runs):
            failed += 1
            errors.update({(r["error"] or "no details").splitlines()[0][:200] for r in bad})
    return Slot(at, len(per), failed, pending, errors.most_common(1)[0][0] if errors else None)


def tenants_due(conn) -> int:
    """Tenants the schedule collects (as ``worker.schedule_tick`` selects them)."""
    return conn.execute(
        "SELECT COUNT(*) FROM tenants WHERE enabled = 1 AND archived_at IS NULL"
        " AND connection_id IS NOT NULL AND consented_at IS NOT NULL"
    ).fetchone()[0]


COLLECTION_FIX = (
    "When every client fails at once it's usually the app certificate (App connection), the app "
    "registration in Entra, or the server's internet access. Open a failed run from a client's "
    "Overview to see Microsoft's answer."
)


def check_collection(conn, cfg, now) -> Check:
    due = tenants_due(conn)
    row = conn.execute(
        "SELECT finished_at FROM runs WHERE status IN ('succeeded', 'partial') ORDER BY id DESC LIMIT 1"
    ).fetchone()
    last = parse_ts(row["finished_at"]) if row else None
    interval = collection_interval(cfg, now)
    slot = last_slot(conn, cfg, now, now - interval)
    data = {
        "last_success": _iso(last),
        "age_hours": _hours(now - last) if last else None,
        "tenants": due,
        "last_slot": (
            {"at": slot.at, "tenants": slot.tenants, "failed": slot.failed, "complete": slot.complete} if slot else None
        ),
    }
    if not due:
        return Check("collection", "Collection", OK, "No clients to collect yet", data=data)
    if slot and slot.mass_failure:
        return Check(
            "collection",
            "Collection",
            WARN,
            f"The last scheduled collection failed for {slot.failed} of {plural(slot.tenants, 'client')}",
            COLLECTION_FIX + (f" Most common error: {slot.top_error}" if slot.top_error else ""),
            data,
        )
    if last is None:
        return Check(
            "collection",
            "Collection",
            WARN,
            "Nothing has been collected yet",
            "Collection starts when a client has consented and the worker is running.",
            data,
        )
    if now - last > interval:
        return Check(
            "collection",
            "Collection",
            WARN,
            f"Nothing collected for {duration((now - last).total_seconds())}",
            COLLECTION_FIX,
            data,
        )
    summary = f"Last data collected {duration((now - last).total_seconds())} ago"
    if slot and slot.complete and slot.failed:
        summary += f"; {slot.failed} of {plural(slot.tenants, 'client')} failed in the last scheduled run"
    return Check("collection", "Collection", OK, summary, data=data)


BACKUP_SETUP = (
    'Back up every night: DEPLOY.md, "Backups", has the one line to add to the server\'s crontab. '
    "Keep the copies off the data volume."
)
# How old each backup job's last success may get: nightly copies, and the weekly restore drill.
# (what it's called, how old its last success may get, how to see the whole error)
BACKUP_JOBS = {
    "local": (
        "backup",
        timedelta(hours=26),
        "Run the backup by hand to see the whole message: docker compose exec web python -m officesentry backup",
    ),
    "offsite": (
        "off-site backup",
        timedelta(hours=26),
        'Run Deploy with the backup action to see the whole message (DEPLOY.md, "Off-site backups").',
    ),
    "restore_check": (
        "restore check",
        timedelta(days=8),
        'Run Deploy with the restore-drill action (DEPLOY.md, "Restore drill").',
    ),
}


def check_backup(conn, cfg, now) -> Check:
    jobs = {name: j for name, j in backups.read_status(cfg.data_dir).items() if not j.get("off")}
    if "local" not in jobs and (newest := backups.newest_backup(cfg.data_dir / "backups")):
        # Backups made before the status file existed, into the default folder.
        made = datetime.fromtimestamp(newest.stat().st_mtime, UTC).strftime(ISO)
        jobs["local"] = {"ok": True, "last_attempt": made, "last_success": made, "detail": "", "error": ""}
    data = {
        "configured": bool(jobs),
        "jobs": {
            name: {
                "ok": j.get("ok"),
                "last_attempt": j.get("last_attempt"),
                "last_success": j.get("last_success"),
                "age_hours": _hours(now - t) if (t := parse_ts(j.get("last_success"))) else None,
            }
            for name, j in jobs.items()
        },
    }
    if not jobs:
        return Check("backup", "Backups", WARN, "No backup has been made yet", BACKUP_SETUP, data)
    worst: tuple[int, str, str] | None = None
    newest = None
    for name, j in sorted(jobs.items()):
        label, stale, rerun = BACKUP_JOBS.get(
            name, (f"{name} backup", timedelta(hours=26), "Run it by hand to see the whole message.")
        )
        done = parse_ts(j.get("last_success"))
        if done and name != "restore_check" and (newest is None or done > newest):
            newest = done
        if j.get("ok") is False:
            problem = (
                2,
                f"The last {label} failed",
                (j.get("error") or "No details were recorded").rstrip(".") + ". " + rerun,
            )
        elif done is None or now - done > stale:
            problem = (
                1,
                f"No {label} for {duration((now - done).total_seconds())}" if done else f"No {label} has worked yet",
                "Check that it still runs (crontab -l on the server). " + BACKUP_SETUP,
            )
        else:
            continue
        if worst is None or problem[0] > worst[0]:
            worst = problem
    data["age_hours"] = _hours(now - newest) if newest else None
    if worst:
        return Check("backup", "Backups", WARN, worst[1], worst[2], data)
    if newest is None:
        return Check("backup", "Backups", OK, "Restore check passed", data=data)
    return Check("backup", "Backups", OK, f"Last backup {duration((now - newest).total_seconds())} ago", data=data)


def disk_usage(path: Path) -> tuple[float, int, int]:
    """Percent used, bytes free and bytes in total on ``path``'s disk."""
    usage = shutil.disk_usage(path)
    # As df counts it: blocks reserved for root are neither used nor free to Office Sentry.
    room = usage.used + usage.free
    return (round(100 * usage.used / room, 1) if room else 0.0), usage.free, usage.total


def size_text(n: int) -> str:
    for unit, size in (("TB", 2**40), ("GB", 2**30), ("MB", 2**20)):
        if n >= size:
            return f"{n / size:,.1f} {unit}"
    return f"{n // 1024:,} KB"


DISK_FIX = (
    "Free some space: old backups in the data folder, old Docker images (docker image prune), or shorter "
    "periods under Data retention. Or give the server a bigger disk."
)


def check_disk(conn, cfg, now) -> Check:
    used, free, total = disk_usage(cfg.data_dir)
    data = {"used_percent": used, "free_bytes": free, "total_bytes": total}
    summary = f"{used:.0f}% used, {size_text(free)} free"
    if used >= DISK_FAIL:
        return Check(
            "disk", "Disk space", FAIL, summary + ". Collections and backups fail when it fills.", DISK_FIX, data
        )
    if used >= DISK_WARN:
        return Check("disk", "Disk space", WARN, summary, DISK_FIX, data)
    return Check("disk", "Disk space", OK, summary, data=data)


def check_certificates(conn, cfg, now) -> Check:
    found = cert_lifecycle.expiring(conn, now)
    data = {
        "expiring": [{"kind": e.kind, "name": e.name, "days_left": e.days, "not_after": e.not_after} for e in found]
    }
    if not found:
        return Check("certificates", "Certificates", OK, "None expires in the next 60 days", data=data)
    soonest = found[0]
    more = f" (and {plural(len(found) - 1, 'other')})" if len(found) > 1 else ""
    return Check(
        "certificates",
        "Certificates",
        WARN,
        soonest.headline.rstrip(".") + more,
        cert_lifecycle.fix_steps(soonest),
        data,
    )


ERASE_LATE = timedelta(hours=6)  # a delete still not erased after this, with no failure recorded, needs a look


def check_erase(conn, cfg, now) -> Check:
    state = lifecycle.erase_state(conn)
    waited = lifecycle.erase_waiting(state, now)
    data = {"waiting": state is not None, "waiting_hours": _hours(waited) if waited else None}
    if state is None:
        return Check("erase", "Deleted clients", OK, "Everything deleted has been erased", data=data)
    if state.get("error"):
        return Check(
            "erase",
            "Deleted clients",
            WARN,
            "A deleted client's data isn't fully erased from the server yet",
            state["error"].rstrip(".") + ". It's tried again every hour, and each try is in the Activity log.",
            data,
        )
    if waited and waited > ERASE_LATE:
        return Check(
            "erase",
            "Deleted clients",
            WARN,
            f"Erasing a deleted client's data has waited {duration(waited.total_seconds())}",
            "The background worker does it. " + WORKER_FIX,
            data,
        )
    return Check("erase", "Deleted clients", OK, "Erasing a deleted client's data now", data=data)


CHECKS = (
    ("database", check_database),
    ("schema", check_schema),
    ("worker", check_worker),
    ("collection", check_collection),
    ("backup", check_backup),
    ("disk", check_disk),
    ("certificates", check_certificates),
    ("erase", check_erase),
)
LABELS = {
    "database": "Database",
    "schema": "Database version",
    "worker": "Background worker",
    "collection": "Collection",
    "backup": "Backups",
    "disk": "Disk space",
    "certificates": "Certificates",
    "erase": "Deleted clients",
}
# A check that can't even run counts as failed when the portal can't work without it.
CRITICAL = {"database", "schema", "worker", "disk"}


def assess(conn, cfg, now: datetime | None = None, only: tuple[str, ...] | None = None) -> Health:
    now = now or datetime.now(UTC)
    checks = []
    for key, fn in CHECKS:
        if only is not None and key not in only:
            continue
        try:
            checks.append(fn(conn, cfg, now))
        except Exception as exc:  # report it rather than break the endpoint an uptime monitor reads
            log.warning("Health check %s couldn't run", key, exc_info=True)
            checks.append(
                Check(
                    key, LABELS[key], FAIL if key in CRITICAL else WARN, f"Couldn't be checked ({type(exc).__name__})"
                )
            )
    return Health(checks, now)


# -- The monitoring token ---------------------------------------------------------------------
#
# Anyone may ask /health/ready whether the portal is ready ({"ok": true}, or 503). The details
# need the monitoring token (an admin makes it on the System status page; only its hash is kept)
# or an admin's session.


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token(conn) -> str:
    token = "osh_" + secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (TOKEN_KEY, _digest(token)),
    )
    return token


def remove_token(conn) -> None:
    conn.execute("DELETE FROM settings WHERE key = ?", (TOKEN_KEY,))


def has_token(conn) -> bool:
    return conn.execute("SELECT 1 FROM settings WHERE key = ? AND value != ''", (TOKEN_KEY,)).fetchone() is not None


def token_matches(conn, token: str | None) -> bool:
    if not token or len(token) > 200:
        return False
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (TOKEN_KEY,)).fetchone()
    return bool(row and row["value"]) and hmac.compare_digest(row["value"], _digest(token))
