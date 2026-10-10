"""Cron schedules in a timezone, and collection freshness.

Collection runs on ``OFFICESENTRY_COLLECT_CRON`` (default every night at
02:00) in ``OFFICESENTRY_TIMEZONE``. A collector whose last successful run is
younger than ``OFFICESENTRY_MAX_AGE_HOURS`` at the slot is skipped, and an
emailed report first refreshes any collector older than that.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

from . import store
from .collectors import REGISTRY

log = logging.getLogger(__name__)

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


def due_slot(
    expr: str, tz: str, now: datetime, last_slot: str | None = None, not_before: str | None = None
) -> datetime | None:
    """The latest slot of ``expr`` if it still needs doing, else None.

    ``last_slot`` is the ``iso()`` of the slot the caller last completed, and
    ``not_before`` (e.g. the job's created_at) stops a new job running for a
    slot that passed before it existed. Only the latest missed slot is
    returned, so a worker that was down catches up once, not once per slot.
    """
    slot = previous_fire(expr, tz, now)
    stamp = iso(slot)
    if (last_slot and last_slot >= stamp) or (not_before and stamp < not_before):
        return None
    return slot


# -- Periodic jobs -------------------------------------------------------------


@dataclass
class TickContext:
    """What a periodic job gets each minute. ``app`` renders templates (emails)."""

    app: object
    conn: object
    vault: object
    cfg: object
    now: datetime


PERIODIC: dict[str, Callable[[TickContext], object]] = {}


def periodic(name: str):
    """Register ``fn(ctx: TickContext)`` to run on every worker tick (once a minute).

    The job decides for itself whether anything is due, typically with
    ``due_slot()`` and its own record of the last slot done. An exception is
    logged and doesn't stop collections or other jobs. Register at import time
    in a module ``worker.JOB_MODULES`` imports.
    """

    def decorator(fn):
        if name in PERIODIC:
            raise ValueError(f"duplicate periodic job {name!r}")
        PERIODIC[name] = fn
        return fn

    return decorator


def run_periodic(ctx: TickContext) -> None:
    for name, fn in PERIODIC.items():
        try:
            fn(ctx)
        except Exception:
            log.exception("Periodic job %s failed", name)


def nightly_collectors() -> list[str]:
    return [key for key, cls in REGISTRY.items() if cls.nightly]


def is_fresh(conn, tenant_id: int, collector: str, slot: datetime, max_age_hours: float) -> bool:
    """True if the collector succeeded within ``max_age_hours`` before ``slot`` (or since)."""
    last = store.latest_run(conn, tenant_id, collector)
    return bool(last and last["finished_at"] and last["finished_at"] >= iso(slot - timedelta(hours=max_age_hours)))


def refresh_for_report(conn, tenant_id: int, slot: datetime, max_age_hours: float, now: datetime | None = None) -> bool:
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
            store.enqueue_run(
                conn, tenant_id, key, trigger="schedule", scheduled_for=iso(slot), priority=store.PRIORITY_WAITING
            )  # a report is waiting for it
            ready = False
        elif tried["status"] in ("queued", "running"):
            ready = False
    return ready
