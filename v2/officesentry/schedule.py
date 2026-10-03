"""Cron schedules in a timezone, and collection freshness.

Collection runs on ``OFFICESENTRY_COLLECT_CRON`` (default every night at
02:00) in ``OFFICESENTRY_TIMEZONE``. A collector whose last successful run is
younger than ``OFFICESENTRY_MAX_AGE_HOURS`` at the slot is skipped, and an
emailed report first refreshes any collector older than that.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from . import store
from .collectors import REGISTRY

DEFAULT_COLLECT_CRON = "0 2 * * *"
# An emailed report waits this long for its refresh runs, then goes out anyway.
REFRESH_WAIT = timedelta(hours=3)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def cron_problem(expr: str) -> str | None:
    """Why ``expr`` is not a usable 5-field cron expression, or None."""
    if len(expr.split()) != 5:
        return "use 5 fields: minute hour day-of-month month day-of-week, e.g. '0 2 * * *'"
    if not croniter.is_valid(expr):
        return "not a valid cron expression"
    return None


def timezone_problem(tz: str) -> str | None:
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        return f"unknown timezone {tz!r} (use a name like Europe/London)"
    return None


def previous_fire(expr: str, tz: str, now: datetime) -> datetime:
    """The most recent time ``expr`` fired at or before ``now``, in UTC."""
    local = now.astimezone(ZoneInfo(tz)).replace(microsecond=0) + timedelta(seconds=1)
    return croniter(expr, local).get_prev(datetime).astimezone(UTC)


def next_fire(expr: str, tz: str, now: datetime) -> datetime:
    """The first time ``expr`` fires after ``now``, in UTC."""
    local = now.astimezone(ZoneInfo(tz)).replace(microsecond=0)
    return croniter(expr, local).get_next(datetime).astimezone(UTC)


def nightly_collectors() -> list[str]:
    return [key for key, cls in REGISTRY.items() if cls.nightly]


def is_fresh(conn, tenant_id: int, collector: str, slot: datetime, max_age_hours: float) -> bool:
    """True if the collector succeeded within ``max_age_hours`` before ``slot`` (or since)."""
    last = store.latest_run(conn, tenant_id, collector)
    return bool(last and last["finished_at"] and last["finished_at"] >= iso(slot - timedelta(hours=max_age_hours)))


def refresh_for_report(conn, tenant_id: int, slot: datetime, max_age_hours: float,
                       now: datetime | None = None) -> bool:
    """Queue stale collectors before a report due at ``slot``. True once it can be sent.

    Each stale collector is tried once per slot; a failed refresh does not hold
    the report back, and after ``REFRESH_WAIT`` the report goes out regardless.
    """
    now = now or datetime.now(UTC)
    if now - slot > REFRESH_WAIT:
        return True
    ready = True
    for key in nightly_collectors():
        if is_fresh(conn, tenant_id, key, slot, max_age_hours):
            continue
        tried = conn.execute(
            "SELECT status FROM runs WHERE tenant_id = ? AND collector = ? AND created_at >= ?"
            " ORDER BY id DESC LIMIT 1",
            (tenant_id, key, iso(slot)),
        ).fetchone()
        if tried is None:
            store.enqueue_run(conn, tenant_id, key, trigger="schedule", scheduled_for=iso(slot))
            ready = False
        elif tried["status"] in ("queued", "running"):
            ready = False
    return ready
