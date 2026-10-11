"""The day a page shows a tenant as it was on (``?on=YYYY-MM-DD``), and the two days a change report compares
(``?from=...&to=...``), checked against the data Office Sentry still holds for the tenant.

Days are UTC days, like the dates the portal shows; a day means the data held at its end.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time

from .. import state_history


def _parse(raw) -> date | None:
    try:
        return date.fromisoformat(str(raw or "")[:10]) if raw else None
    except ValueError:
        return None


def end_of(day: date) -> datetime:
    return datetime.combine(day, time(23, 59, 59), UTC)


def earliest(conn, tenant_id: int) -> date | None:
    first = state_history.earliest(conn, tenant_id)
    return date.fromisoformat(first[:10]) if first else None


def _label(day: date) -> str:
    return f"{day:%A} {day.day} {day:%B %Y}"


@dataclass
class Day:
    """A past day to show (``on`` None: today, the latest data)."""

    on: date | None
    earliest: date | None
    today: date
    note: str = ""

    @property
    def as_of(self) -> datetime | None:
        return end_of(self.on) if self.on else None

    @property
    def label(self) -> str:
        return _label(self.on) if self.on else "today"

    @property
    def query(self) -> dict:
        return {"on": self.on.isoformat()} if self.on else {}


def day(conn, tenant_id: int, raw, today: date | None = None) -> Day:
    """The day asked for, moved to the first day with data when it's earlier, and to today when it's today or
    later (or not a date)."""
    today = today or datetime.now(UTC).date()
    first = earliest(conn, tenant_id)
    on = _parse(raw)
    if on is None or on >= today or first is None:
        return Day(None, first, today)
    if on < first:
        return Day(
            first,
            first,
            today,
            f"Office Sentry's data starts on {_label(first)}, so this is how it was then.",
        )
    return Day(on, first, today)


@dataclass
class Period:
    """The two days a change report compares: ``since`` (the start of ``start``) and the end of ``end``."""

    start: date
    end: date | None  # None: up to the latest data
    earliest: date | None
    today: date
    note: str = ""

    @property
    def since(self) -> datetime:
        # From the start of the day, as the data stood at the end of the one before; from the first day with
        # data, as it stood at the end of that day (there's nothing from before it to compare with).
        if self.earliest and self.start <= self.earliest:
            return end_of(self.start)
        return datetime.combine(self.start, time(0, 0), UTC)

    @property
    def until(self) -> datetime | None:
        return end_of(self.end) if self.end else None

    @property
    def query(self) -> dict:
        return {"from": self.start.isoformat(), **({"to": self.end.isoformat()} if self.end else {})}


def period(conn, tenant_id: int, raw_from, raw_to, today: date | None = None) -> Period | None:
    """The custom period asked for, or None when ``from`` isn't a date. The two are put in order, ``to`` is
    left open when it's today or later, and ``from`` moves to the first day with data when it's earlier."""
    today = today or datetime.now(UTC).date()
    start, end = _parse(raw_from), _parse(raw_to)
    if start is None:
        return None
    if end is not None and end < start:
        start, end = end, start
    start = min(start, today)
    if end is not None and end >= today:
        end = None
    first = earliest(conn, tenant_id)
    note = ""
    if first and start < first:
        note = f"Office Sentry's data starts on {_label(first)}, so that's the starting point."
        start = first
    if end is not None and end < start:
        end = start
    return Period(start, end, first, today, note)
