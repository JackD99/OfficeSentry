"""Over time, across clients: one measure for every client, month by month, the clients slipping first.

The value at each month's end (this month's is the latest so far) from what Office Sentry already keeps: the
figures in ``metrics``, which are never pruned, and the findings table as it stood at each month's end. The
measures are a client's timeline's (``timeline.MEASURES``). Nothing is read from Microsoft for it.

Each value is shaded by how good it is (``Measure.cuts``: unshaded is good, then four shades to the worst),
and the number always shows, so the shading is never the only way to tell. A client has slipped when its
latest value is worse than its first in the months shown.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from .month_changes import FIGURE_REPORTS, FINDINGS
from .tables import Table
from .timeline import ISO, OPEN_SEVERE, SEVERE, _add_months, _iso
from .timeline import MEASURES as TIMELINE_MEASURES

MONTHS = 12
MIN_MONTHS = 3  # what a phone shows
SHADES = ("unshaded", "green", "amber", "orange", "red")

# Where each shade starts, best first: a value at least as good as a bound gets that shade, and one worse
# than the last gets red. Secure Score and MFA are percentages; the others are counts.
CUTS = {
    "secure_score.pct": (80, 65, 50, 35),
    "mfa.registered_pct": (98, 90, 75, 50),
    OPEN_SEVERE: (0, 4, 9, 19),
    "licences.unassigned_paid": (0, 2, 5, 10),
}


@dataclass(frozen=True)
class Measure:
    key: str
    label: str
    unit: str  # "%" or ""
    better: str  # "up" or "down"
    cuts: tuple[float, ...]
    report: str  # what someone must open on a client to see it: a report key, or FINDINGS

    def level(self, value: float | None) -> int | None:
        """0 (good, unshaded) to 4 (worst), or None without a value."""
        if value is None:
            return None
        value = round(value)  # as the grid shows it
        for i, bound in enumerate(self.cuts):
            if (value >= bound) if self.better == "up" else (value <= bound):
                return i
        return len(self.cuts)

    def worse(self, change: float | None) -> bool:
        return change is not None and (change < 0 if self.better == "up" else change > 0)

    def better_by(self, change: float | None) -> bool:
        return change is not None and (change > 0 if self.better == "up" else change < 0)

    def figure(self, value: float | None, unit: bool = False) -> str:
        """A value as the grid shows it: whole numbers, with "%" when asked."""
        if value is None:
            return "–"
        return f"{round(value):,}" + (self.unit if unit else "")

    def change_text(self, change: float | None) -> str:
        if change is None:
            return "–"
        whole = round(change)  # percentage points for a percentage, so no "%"
        return "0" if whole == 0 else f"{'+' if whole > 0 else '−'}{abs(whole):,}"

    def legend(self) -> str:
        """What each shade means, in words."""

        def bound(b) -> str:
            return f"{b:g}{self.unit}"

        if self.better == "up":
            parts = [f"{bound(self.cuts[0])} or more unshaded"]
            parts += [
                f"{bound(lo)} to {self.cuts[i] - 1:g}{self.unit} {SHADES[i + 1]}" for i, lo in enumerate(self.cuts[1:])
            ]
            parts.append(f"under {bound(self.cuts[-1])} red")
        else:
            parts = [f"{bound(self.cuts[0])} unshaded"]
            parts += [f"{self.cuts[i] + 1:g} to {bound(hi)} {SHADES[i + 1]}" for i, hi in enumerate(self.cuts[1:])]
            parts.append(f"over {bound(self.cuts[-1])} red")
        return ", ".join(parts)


MEASURES: dict[str, Measure] = {
    key: Measure(key, label, unit, better, CUTS[key], FINDINGS if key == OPEN_SEVERE else FIGURE_REPORTS[key])
    for key, label, unit, better in TIMELINE_MEASURES
}
DEFAULT = OPEN_SEVERE


@dataclass
class Row:
    tenant: dict
    values: list[float | None]
    note: str = ""  # why there's nothing to show

    @property
    def known(self) -> list[float]:
        return [v for v in self.values if v is not None]

    @property
    def latest(self) -> float | None:
        return self.known[-1] if self.known else None

    @property
    def change(self) -> float | None:
        """The latest value less the first in the months shown; None with fewer than two months of values."""
        known = self.known
        return known[-1] - known[0] if len(known) >= 2 else None


@dataclass
class Grid:
    measure: Measure
    months: list[date]  # first day of each month, oldest first; the last is this month
    rows: list[Row]
    now: datetime
    slipped: list[Row] = field(default_factory=list)
    improved: list[Row] = field(default_factory=list)

    @property
    def counted(self) -> list[Row]:
        """The rows with a change to judge (two months of values or more)."""
        return [r for r in self.rows if r.change is not None]

    @property
    def averages(self) -> bool:
        """Percentages are averaged across clients; counts are added up."""
        return self.measure.unit == "%"

    def _across(self, values: list[float]) -> float | None:
        if not values:
            return None
        return sum(values) / len(values) if self.averages else sum(values)

    def totals(self) -> list[float | None]:
        """Each month's total (counts) or average (percentages) over the clients with a value that month."""
        return [
            self._across([r.values[i] for r in self.rows if r.values[i] is not None]) for i in range(len(self.months))
        ]

    def now_and_change(self) -> tuple[float | None, float | None]:
        """The latest total or average across clients, and how far it moved since the first month shown, over
        the clients with both."""
        both = [r for r in self.rows if r.change is not None]
        latest = self._across([r.latest for r in self.rows if r.latest is not None])
        if not both:
            return latest, None
        before = self._across([r.known[0] for r in both])
        after = self._across([r.latest for r in both])
        return latest, (after - before if before is not None and after is not None else None)

    def to_table(self) -> Table:
        m = self.measure
        header = ["Client", "Tenant ID"] + [f"{d:%b %Y}" + (f" ({m.unit})" if m.unit else "") for d in self.months]
        header += ["Change", "Note"]
        rows = [
            [r.tenant["name"], r.tenant.get("tenant_ref")]
            + ["" if v is None else _round(v) for v in r.values]
            + ["" if r.change is None else _round(r.change), r.note]
            for r in self.rows
        ]
        return Table(f"{m.label} by month", header, rows)


def _round(value: float) -> float | int:
    """Downloads keep a decimal for percentages and whole numbers for counts."""
    return int(value) if float(value).is_integer() else round(value, 1)


def months_to(now: datetime, count: int = MONTHS) -> list[date]:
    this = date(now.year, now.month, 1)
    return [_add_months(this, -n) for n in range(count - 1, -1, -1)]


def _ends(months: list[date], now: datetime) -> list[str]:
    """Each month's end, as stored times are written (this month's is now)."""
    return [
        min(datetime.combine(_add_months(m, 1), datetime.min.time(), tzinfo=UTC), now).strftime(ISO) for m in months
    ]


def _metric_values(conn, ids: list[int], key: str, months: list[date]) -> dict[int, list]:
    """Each tenant's last figure in each month."""
    found: dict[int, list] = {i: [None] * len(months) for i in ids}
    if not ids:
        return found
    index = {f"{m:%Y-%m}": i for i, m in enumerate(months)}
    for r in conn.execute(
        "SELECT tenant_id, value, captured_at FROM metrics WHERE key = ? AND captured_at >= ?"
        f" AND tenant_id IN ({','.join('?' * len(ids))}) ORDER BY captured_at",
        (key, _iso(months[0]), *ids),
    ):
        i = index.get(str(r["captured_at"])[:7])
        if i is not None and r["value"] is not None:
            found[r["tenant_id"]][i] = r["value"]  # in time order, so each month's last figure stays
    return found


def _open_severe(conn, ids: list[int], ends: list[str]) -> dict[int, list]:
    """Critical and high findings open at each month's end (found by then, not fixed or accepted by then), from
    the tenant's first collection on; None before it."""
    found: dict[int, list] = {i: [None] * len(ends) for i in ids}
    if not ids:
        return found
    marks = ",".join("?" * len(ids))
    first = dict(
        conn.execute(
            f"SELECT tenant_id, MIN(finished_at) FROM runs WHERE tenant_id IN ({marks})"
            " AND status IN ('succeeded', 'partial') GROUP BY tenant_id",
            ids,
        ).fetchall()
    )
    rows: dict[int, list] = {i: [] for i in ids}
    for r in conn.execute(
        "SELECT tenant_id, first_seen, resolved_at, accepted_at, accepted_until FROM findings"
        f" WHERE tenant_id IN ({marks}) AND severity IN ({','.join('?' * len(SEVERE))})",
        (*ids, *SEVERE),
    ):
        rows[r["tenant_id"]].append(r)
    for tid in ids:
        start = first.get(tid)
        for i, end in enumerate(ends):
            if not start or start >= end:
                continue
            found[tid][i] = sum(
                1
                for r in rows[tid]
                if r["first_seen"] < end
                and (not r["resolved_at"] or r["resolved_at"] >= end)
                and not (
                    r["accepted_at"]
                    and r["accepted_at"] < end
                    and (not r["accepted_until"] or r["accepted_until"] >= end)
                )
            )
    return found


def _badness(measure: Measure, value: float | None) -> float:
    if value is None:
        return float("-inf")
    return value if measure.better == "down" else -value


def build(conn, tenants: list, measure: Measure, now: datetime | None = None) -> Grid:
    now = now or datetime.now(UTC)
    months = months_to(now)
    ids = [t["id"] for t in tenants]
    if measure.key == OPEN_SEVERE:
        values = _open_severe(conn, ids, _ends(months, now))
    else:
        values = _metric_values(conn, ids, measure.key, months)
    # Months before any of these clients has a value aren't shown (a new install has a month or two), but the
    # last MIN_MONTHS always are.
    start = min((i for v in values.values() for i, x in enumerate(v) if x is not None), default=len(months))
    start = min(start, len(months) - MIN_MONTHS)
    months = months[start:]
    values = {tid: v[start:] for tid, v in values.items()}
    rows = []
    for t in tenants:
        row = Row(dict(t), values[t["id"]])
        if not row.known:
            row.note = "Nothing collected for this yet"
        rows.append(row)
    grid = Grid(measure, months, rows, now)
    grid.slipped = [r for r in rows if measure.worse(r.change)]
    grid.improved = [r for r in rows if measure.better_by(r.change)]

    def order(r: Row):
        slip = measure.worse(r.change)
        return (
            not r.known,
            not slip,
            -abs(r.change) if slip else 0,
            -_badness(measure, r.latest),
            r.tenant["name"].lower(),
        )

    rows.sort(key=order)
    grid.slipped.sort(key=order)
    return grid
