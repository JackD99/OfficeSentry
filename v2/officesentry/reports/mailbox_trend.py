"""Mailbox size trend: how mailboxes have grown, from Office Sentry's own collections.

Microsoft only says how big a mailbox is now. Every night's ``mailbox_size`` objects (``collectors/mail_usage.py``)
are kept in the snapshots and then the daily history (``state_history.py``, 25 months by default), so each
mailbox's size a month and three months ago is read from those, and the tenant's total from the
``mail.storage_bytes`` metric, which is never pruned. Growth over the last month gives each mailbox's forecast
date for reaching its quota.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from ..devices import parse_ts
from ..formatting import day
from ..snapshot import TenantSnapshot
from ..text import plural, verb
from .data_reports import DataReport, Rows, Section, Tile, _type
from .mail_reports import _no_exchange, _size
from .registry import report
from .sharepoint import storage_chart

NOT_COLLECTED = "Mailbox sizes haven't been collected yet. Refresh Mailbox size on the client page."
SPANS = (30, 90)  # days back each mailbox is compared with
MIN_SPAN = 7  # a comparison needs at least this many days of history
SOON_DAYS = 30
WATCH_DAYS = 90

GROWTH = "Growth by mailbox"
MONTHS = "Total size by month"


def _signed(n: float) -> str:
    if not n:
        return "No change"
    return ("+" if n > 0 else "-") + _size(abs(int(n)))


def _collections(snap: TenantSnapshot, now: datetime) -> list[tuple[datetime, int]]:
    """(collected at, run ID) of each Mailbox size collection that can still be read, from its snapshot or the
    daily history once retention has removed that, oldest first, up to ``now``."""
    from .. import state_history  # here: the history imports the worker's schedule

    cutoff = snap.as_of or now.strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = snap.conn.execute(
        "SELECT id, finished_at FROM runs WHERE tenant_id = ? AND collector = 'mailbox_usage'"
        " AND status IN ('succeeded', 'partial') AND finished_at <= ? AND object_count > 0"
        f" AND {state_history.READABLE} ORDER BY finished_at, id",
        (snap.tenant["id"], cutoff),
    ).fetchall()
    return [(when, run["id"]) for run in runs if (when := parse_ts(run["finished_at"]))]


def _sizes(snap: TenantSnapshot, run_id: int) -> dict[str, int]:
    """Sizes by mailbox in one collection."""
    from .. import state_history

    return {
        m["_id"]: m["totalBytes"]
        for m in state_history.objects_of(snap.conn, run_id, "mailbox_size")
        if isinstance(m.get("totalBytes"), int)
    }


def _baseline(history: list[tuple[datetime, int]], latest: datetime, days: int, fallback: bool):
    """The newest collection at least ``days`` old; with ``fallback``, else the oldest one if it's at least
    ``MIN_SPAN`` days old (so the month's growth shows from the first week)."""
    target = latest - timedelta(days=days)
    older = [h for h in history if h[0] <= target]
    if older:
        return older[-1]
    if fallback and history and (latest - history[0][0]).days >= MIN_SPAN:
        return history[0]
    return None


def _monthly(points: list[tuple[date, float]]) -> list[tuple[str, float]]:
    """The last value of each month, oldest first, as "October 2026"."""
    months: dict[tuple[int, int], float] = {}
    for d, v in points:
        months[(d.year, d.month)] = v
    return [(f"{date(y, m, 1):%B %Y}", v) for (y, m), v in sorted(months.items())]


@report(
    "mailbox-trend",
    "Mailbox size trend",
    "How each mailbox has grown over the last one and three months, which will fill up and when, "
    "and the tenant's total mailbox size month by month.",
    ("mailbox_usage",),
    group="email",
    order=55,
    keywords="growth growing history over time forecast quota full storage",
    uses=("storage",),
)
def trend_report(snap: TenantSnapshot, now: datetime, out: DataReport) -> None:
    current = snap.objects("mailbox_usage", "mailbox_size")
    if current is None:
        out.unavailable = NOT_COLLECTED
        return
    if _no_exchange(snap):
        out.unavailable = "This tenant has no Exchange Online licence."
        return
    history = _collections(snap, now)
    latest = history[-1][0] if history else now
    bases = {}
    for days in SPANS:
        base = _baseline(history, latest, days, days == SPANS[0])
        bases[days] = (base[0], _sizes(snap, base[1])) if base else None
    month_base = bases[SPANS[0]]
    span = (latest - month_base[0]).days if month_base else 0

    growth = Section(
        GROWTH,
        [
            "Mailbox",
            "Type",
            "Size now",
            f"{SPANS[0]} days ago",
            f"{SPANS[0]}-day change",
            f"{SPANS[1]} days ago",
            "Growth a month",
            "Stops sending at",
            "Forecast full",
        ],
        empty="No mailboxes.",
        compare=False,
        optional=(f"{SPANS[0]} days ago", f"{SPANS[1]} days ago"),
    )
    entries = []
    soon = 0
    fastest: tuple[float, str] | None = None
    for m in current:
        size = m.get("totalBytes") if isinstance(m.get("totalBytes"), int) else None
        mailbox_id = m.get("_id")
        name = m.get("mailbox") if isinstance(m.get("mailbox"), str) and m.get("mailbox") else str(mailbox_id)
        quota = m.get("sendQuotaBytes") if isinstance(m.get("sendQuotaBytes"), int) else None
        before = {d: (b[1].get(mailbox_id) if b else None) for d, b in bases.items()}
        change = size - before[SPANS[0]] if size is not None and before[SPANS[0]] is not None else None
        per_month = change / span * 30 if change is not None and span else None
        forecast, flag = "", None
        if size is not None and quota:
            if size >= quota:
                forecast, flag = "Full now", "high"
            elif per_month and per_month > 0:
                full = latest + timedelta(days=(quota - size) / per_month * 30)
                forecast = full.date().isoformat()
                days = (full - now).days
                flag = "high" if days <= SOON_DAYS else ("medium" if days <= WATCH_DAYS else None)
        if flag:
            soon += 1
        if per_month and per_month > 0 and (fastest is None or per_month > fastest[0]):
            fastest = (per_month, name)
        entries.append(
            (
                (-(per_month or 0), -(size or 0)),
                [
                    name,
                    _type(m.get("mailboxType")),
                    _size(size),
                    _size(before[SPANS[0]]),
                    "" if change is None else _signed(change),
                    _size(before[SPANS[1]]),
                    "" if per_month is None else _signed(per_month),
                    _size(quota) or "No limit",
                    forecast,
                ],
                flag,
            )
        )
    for _, row, flag in sorted(entries, key=lambda e: e[0]):
        growth.add(row, flag)

    totals = [(d, v) for d, v in ((parse_ts(at), v) for at, v in snap.history("mail.storage_bytes")) if d is not None]
    points = sorted({d.date(): v for d, v in totals}.items())
    months = Section(MONTHS, ["Month", "Total size", "Change"], empty="No collections yet.", compare=False)
    previous = None
    for month, value in _monthly(points):
        months.add([month, _size(int(value)), "" if previous is None else _signed(value - previous)])
        previous = value
    months.rows.reverse()
    months.flags.reverse()

    size_now = sum(m.get("totalBytes") for m in current if isinstance(m.get("totalBytes"), int))
    if not month_base:
        out.notice = (
            "The trend needs a collection at least a week old to compare with"
            + (f"; the first was on {history[0][0].date().isoformat()}" if history else "")
            + ". Sizes show now and growth fills in as nightly collections build up."
        )
    growth.intro = (
        f"Each mailbox now, against Office Sentry's collection from {span} days earlier"
        f" ({month_base[0].date().isoformat()}), fastest-growing first. "
        if month_base
        else "Each mailbox now. "
    ) + (
        f"{plural(soon, 'mailbox')} {verb(soon, 'is', 'are')} full or forecast to fill within {WATCH_DAYS} days at "
        "its current rate: turn on the archive (or auto-expanding archiving) or clear old mail before sending "
        "stops."
        if soon
        else f"None is forecast to fill within {WATCH_DAYS} days."
    )
    months.intro = "The tenant's total mailbox size at the last collection of each month."
    out.chart = storage_chart(points, "Mailbox storage over time")
    out.chart_title = "Mailbox storage over time" if out.chart else ""
    out.tiles = [
        Tile(
            "Total mailbox size",
            _size(size_now) or "0 bytes",
            f"Across {plural(len(current), 'mailbox')}",
            rows=Rows(GROWTH),
        ),
        Tile(
            "Change in the last month",
            _signed(size_now - sum(month_base[1].values())) if month_base else "Not yet",
            f"Since {day(month_base[0])}" if month_base else "Needs a week of history",
        ),
        Tile(
            "Fastest growing",
            f"{_signed(fastest[0])} a month" if fastest else "None",
            fastest[1] if fastest else "No mailbox grew",
            rows=Rows(GROWTH),
        ),
        Tile(
            f"Full within {WATCH_DAYS} days",
            soon,
            "At the current rate, or full now",
            "bad" if soon else "ok",
            Rows(GROWTH, flagged=True),
        ),
    ]
    out.sections = [growth, months]
