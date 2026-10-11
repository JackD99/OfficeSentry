"""The quarter behind a quarterly review: three months of Secure Score and findings, what was fixed,
the projects on the table, licence spend and what's coming up. Built once per tenant for the report
designer's quarterly blocks (``designs.Context.quarter``); plain data, formatted by the widgets."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from .. import projects, upcoming
from ..findings import resolved_since
from ..licences import SkuLine
from ..snapshot import TenantSnapshot
from .charts import month_columns
from .monthly_review import (
    ISO,
    FixedGroup,
    MonthCount,
    Report,
    _baseline_end,
    _month_starts,
    fixed_groups,
    found_and_fixed,
)

MONTHS = 3  # a quarter: this month and the two before it
SCORE_KEY = "secure_score.pct"


@dataclass
class QuarterMonth:
    month: date  # the first day
    score: float | None  # Secure Score at the end of the month (today for this month), None without data
    found: int | None  # findings found (new or back) in the month, None before the first collection
    fixed: int | None


@dataclass
class SpendLine:
    name: str
    purchased: int
    assigned: int
    monthly_price: float | None
    monthly_waste: float | None

    @property
    def monthly_cost(self) -> float | None:
        return round(self.purchased * self.monthly_price, 2) if self.monthly_price is not None else None


@dataclass
class Quarter:
    start: date  # the first day of the first month
    end: date  # today
    months: list[QuarterMonth]
    fixed: list[FixedGroup]  # the quarter's resolved findings by check, most fixed first
    fixed_count: int
    projects: projects.Assessment
    spend: list[SpendLine] = field(default_factory=list)  # paid licences, dearest first
    currency: str = "GBP"
    upcoming: list[upcoming.Item] = field(default_factory=list)  # the next 90 days, soonest first

    @property
    def label(self) -> str:
        """``August to October 2026``, the months the review covers."""
        return label(self.start, self.end)

    @property
    def score_from(self) -> float | None:
        known = [m.score for m in self.months if m.score is not None]
        return known[0] if len(known) > 1 else None

    @property
    def score_to(self) -> float | None:
        known = [m.score for m in self.months if m.score is not None]
        return known[-1] if known else None

    @property
    def chart(self) -> str:
        """Found and fixed per month, "" until a month has data."""
        if not any(m.found is not None for m in self.months):
            return ""
        return month_columns(
            [(m.month, m.found, m.fixed) for m in self.months],
            label=f"Findings found and fixed each month, {self.label}",
            series=("Found", "Fixed"),
        )

    @property
    def monthly_spend(self) -> float:
        return round(sum(ln.monthly_cost or 0 for ln in self.spend), 2)

    @property
    def monthly_waste(self) -> float:
        return round(sum(ln.monthly_waste or 0 for ln in self.spend), 2)

    @property
    def unpriced(self) -> int:
        return sum(1 for ln in self.spend if ln.monthly_price is None)


def label(start: date, end: date) -> str:
    if start.year != end.year:
        return f"{start:%B %Y} to {end:%B %Y}"
    return f"{start:%B} to {end:%B %Y}"


def quarter_label(now: datetime) -> str:
    """The ``{quarter}`` placeholder: the three months ending this one."""
    return label(_month_starts(now, MONTHS)[0], now.date())


def _score_by_month(snap: TenantSnapshot, starts: list[date], now: datetime) -> list[float | None]:
    """The last Secure Score stored in each month (this month's latest), None for a month without one."""
    history = snap.history(SCORE_KEY)
    out: list[float | None] = []
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else (now + timedelta(days=1)).date()
        lo, hi = f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}"
        inside = [v for ts, v in history if lo <= ts[:10] < hi]
        out.append(inside[-1] if inside else None)
    return out


def _spend(lines: list[SkuLine]) -> list[SpendLine]:
    paid = [ln for ln in lines if not ln.free]
    out = [SpendLine(ln.name, ln.purchased, ln.assigned, ln.monthly_price, ln.monthly_waste) for ln in paid]
    return sorted(out, key=lambda ln: (-(ln.monthly_cost or 0), ln.name.lower()))


def build(conn, tenant, review: Report, now: datetime | None = None) -> Quarter:
    """The tenant's quarter, from stored data and the monthly review already built for it."""
    now = now or datetime.now(UTC)
    starts = _month_starts(now, MONTHS)
    since = datetime(starts[0].year, starts[0].month, 1, tzinfo=UTC).strftime(ISO)
    snap = TenantSnapshot(conn, tenant)
    if snap.runs:
        counts = found_and_fixed(conn, tenant["id"], now, _baseline_end(conn, tenant["id"], now), MONTHS)
        scores = _score_by_month(snap, starts, now)
    else:  # nothing collected yet: no months to speak of
        counts = [MonthCount(start, None, None) for start in starts]
        scores = [None] * MONTHS
    months = [QuarterMonth(c.month, s, c.found, c.fixed) for c, s in zip(counts, scores, strict=True)]
    resolved = resolved_since(conn, tenant["id"], since)
    tenant = dict(tenant)
    coming = (
        upcoming.collect(conn, [tenant], now).items if tenant.get("enabled") and not tenant.get("archived_at") else []
    )
    return Quarter(
        start=starts[0],
        end=now.date(),
        months=months,
        fixed=fixed_groups(resolved, review.results),
        fixed_count=len(resolved),
        projects=projects.assess(conn, tenant, now, snap),
        spend=_spend(review.licences.lines),
        currency=review.currency,
        upcoming=[i for i in coming if i.window != upcoming.NO_DATE],
    )
