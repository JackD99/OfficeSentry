"""Our work this quarter: what the team got done for its clients over three months, for the management meeting,
or to show a prospect what being looked after looks like.

From what Office Sentry already keeps; nothing is read from Microsoft for it:
  * findings fixed: resolved in the quarter (a collection no longer found them), by severity, month, client and
    check. One that came back since isn't counted (coming back clears ``resolved_at``);
  * risks accepted: findings the team and the client decided to live with, accepted in the quarter;
  * Secure Score, MFA coverage and unused paid licences as the quarter began and as it ended, from ``metrics``;
  * who fixed what: the findings fixed while handed to someone on the team (``findings.handed_to``).

The quarter is the three months ending this one, as a quarterly review's (``back=0``), or the three before.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta

from ..checks import RULES, SEVERITY_RANK
from .month_changes import FIGURE_REPORTS
from .tables import Table
from .timeline import ISO, SEVERE, _add_months

MONTHS = 3
BACKS = (0, 1)  # this quarter, the one before
# How far before the quarter a figure may have been read and still count as its starting value.
START_LOOKBACK = timedelta(days=45)


@dataclass(frozen=True)
class Figure:
    key: str  # a ``metrics`` key
    label: str
    unit: str  # "%" or ""
    better: str  # "up" or "down"

    @property
    def report(self) -> str:
        """What someone must open on a client to see it."""
        return FIGURE_REPORTS[self.key]


FIGURES = (
    Figure("secure_score.pct", "Secure Score", "%", "up"),
    Figure("mfa.registered_pct", "Users with MFA", "%", "up"),
    Figure("licences.unassigned_paid", "Unused paid licences", "", "down"),
)


@dataclass
class Moved:
    """A figure as the quarter began and as it ended; either is None without a reading."""

    start: float | None = None
    end: float | None = None

    @property
    def change(self) -> float | None:
        return self.end - self.start if self.start is not None and self.end is not None else None


@dataclass
class ClientWork:
    tenant: dict
    fixed: int = 0
    severe: int = 0  # of them critical or high
    accepted: int = 0
    figures: dict[str, Moved] = field(default_factory=dict)  # by Figure.key, only those the person may see

    @property
    def unused_removed(self) -> int:
        """Unused paid licences fewer than at the start (0 when there are as many or more)."""
        m = self.figures.get("licences.unassigned_paid")
        return max(0, round(-m.change)) if m and m.change is not None else 0


@dataclass
class MonthWork:
    month: date  # the first day
    fixed: int = 0
    severe: int = 0
    accepted: int = 0


@dataclass
class CheckWork:
    key: str
    title: str
    severity: str  # the worst fixed
    fixed: int = 0
    clients: set = field(default_factory=set)


@dataclass
class PersonWork:
    name: str
    fixed: int = 0
    severe: int = 0
    clients: set = field(default_factory=set)


@dataclass
class OurWork:
    months: list[MonthWork]
    start: datetime  # midnight on the quarter's first day
    end: datetime  # midnight after its last day, or now for this quarter
    clients: list[ClientWork]  # most fixed first
    checks: list[CheckWork]  # most fixed first
    people: list[PersonWork]  # most fixed first
    figures: tuple[Figure, ...]  # those the person sees on at least one client
    back: int = 0

    @property
    def label(self) -> str:
        """``August to October 2026``."""
        first, last = self.months[0].month, self.months[-1].month
        if first.year != last.year:
            return f"{first:%B %Y} to {last:%B %Y}"
        return f"{first:%B} to {last:%B %Y}"

    @property
    def so_far(self) -> bool:
        return self.back == 0

    @property
    def fixed(self) -> int:
        return sum(c.fixed for c in self.clients)

    @property
    def severe(self) -> int:
        return sum(c.severe for c in self.clients)

    @property
    def accepted(self) -> int:
        return sum(c.accepted for c in self.clients)

    @property
    def worked(self) -> list[ClientWork]:
        """The clients with something fixed or accepted."""
        return [c for c in self.clients if c.fixed or c.accepted]

    @property
    def unused_removed(self) -> int:
        return sum(c.unused_removed for c in self.clients)

    def improved(self, key: str) -> list[ClientWork]:
        """The clients whose figure got better over the quarter."""
        figure = next(f for f in FIGURES if f.key == key)
        out = []
        for c in self.clients:
            m = c.figures.get(key)
            if m and m.change and (m.change > 0) == (figure.better == "up"):
                out.append(c)
        return out

    def to_tables(self) -> list[Table]:
        def moved(m: Moved | None) -> list:
            if m is None:
                return ["", "", ""]
            return [_num(m.start), _num(m.end), _num(m.change)]

        head = ["Client", "Tenant ID", "Findings fixed", "Critical and high fixed", "Risks accepted"]
        for f in self.figures:
            unit = f" ({f.unit})" if f.unit else ""
            head += [f"{f.label} at the start{unit}", f"{f.label} at the end{unit}", f"{f.label} change"]
        clients = [
            [c.tenant["name"], c.tenant.get("tenant_ref"), c.fixed, c.severe, c.accepted]
            + [v for f in self.figures for v in moved(c.figures.get(f.key))]
            for c in self.clients
        ]
        months = [[f"{m.month:%B %Y}", m.fixed, m.severe, m.accepted] for m in self.months]
        checks = [[c.title, c.severity.capitalize(), c.fixed, len(c.clients)] for c in self.checks]
        people = [[p.name, p.fixed, p.severe, len(p.clients)] for p in self.people]
        return [
            Table("By client", head, clients),
            Table("By month", ["Month", "Findings fixed", "Critical and high fixed", "Risks accepted"], months),
            Table("What was fixed", ["Check", "Worst severity", "Findings fixed", "Clients"], checks),
            Table("Who fixed it", ["Person", "Findings fixed", "Critical and high fixed", "Clients"], people),
        ]


def _num(value: float | None):
    if value is None:
        return ""
    return int(value) if float(value).is_integer() else round(value, 1)


def quarter(now: datetime, back: int = 0) -> tuple[list[date], datetime, datetime]:
    """The quarter's months (the first day of each), its start and its end (now, for this quarter)."""
    last = _add_months(date(now.year, now.month, 1), -MONTHS * back)
    months = [_add_months(last, -n) for n in range(MONTHS - 1, -1, -1)]
    start = datetime.combine(months[0], time.min, tzinfo=UTC)
    after = datetime.combine(_add_months(months[-1], 1), time.min, tzinfo=UTC)
    return months, start, min(after, now)


def build(
    conn,
    tenants: list,
    now: datetime | None = None,
    back: int = 0,
    sees: Callable[[int, str], bool] | None = None,
) -> OurWork:
    """The quarter's work across ``tenants``. ``sees(tenant_id, report)`` says whether the person may see a
    figure's report on a client (a figure they may not is left out for that client)."""
    now = now or datetime.now(UTC)
    back = back if back in BACKS else 0
    sees = sees or (lambda tenant_id, report: True)
    months, start, end = quarter(now, back)
    lo, hi = start.strftime(ISO), end.strftime(ISO)
    ids = [t["id"] for t in tenants]
    by_id = {t["id"]: ClientWork(dict(t)) for t in tenants}
    by_month = {f"{m:%Y-%m}": MonthWork(m) for m in months}
    titles = {r.key: r.title for r in RULES}
    checks: dict[str, CheckWork] = {}
    held: dict[int, PersonWork] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for r in conn.execute(
            f"SELECT tenant_id, rule_key, severity, resolved_at, handed_to FROM findings WHERE tenant_id IN ({marks})"
            " AND resolved_at >= ? AND resolved_at < ?",
            (*ids, lo, hi),
        ):
            severe = r["severity"] in SEVERE
            c = by_id[r["tenant_id"]]
            c.fixed += 1
            c.severe += severe
            m = by_month.get(r["resolved_at"][:7])
            if m:
                m.fixed += 1
                m.severe += severe
            k = checks.get(r["rule_key"])
            if k is None:
                k = checks[r["rule_key"]] = CheckWork(
                    r["rule_key"],
                    titles.get(r["rule_key"], r["rule_key"].replace("_", " ").capitalize()),
                    r["severity"],
                )
            elif SEVERITY_RANK.get(r["severity"], 9) < SEVERITY_RANK.get(k.severity, 9):
                k.severity = r["severity"]
            k.fixed += 1
            k.clients.add(r["tenant_id"])
            if r["handed_to"]:
                p = held.setdefault(r["handed_to"], PersonWork(""))
                p.fixed += 1
                p.severe += severe
                p.clients.add(r["tenant_id"])
        for r in conn.execute(
            f"SELECT tenant_id, accepted_at FROM findings WHERE tenant_id IN ({marks})"
            " AND accepted_at >= ? AND accepted_at < ?",
            (*ids, lo, hi),
        ):
            by_id[r["tenant_id"]].accepted += 1
            m = by_month.get(r["accepted_at"][:7])
            if m:
                m.accepted += 1
        _figures(conn, ids, by_id, start, end, sees)
    if held:
        marks = ",".join("?" * len(held))
        for u in conn.execute(f"SELECT id, username FROM users WHERE id IN ({marks})", tuple(held)):
            held[u["id"]].name = u["username"]
    people = [p for p in held.values() if p.name]  # an account deleted since hands its findings back
    shown = tuple(f for f in FIGURES if any(f.key in c.figures for c in by_id.values()))
    return OurWork(
        months=list(by_month.values()),
        start=start,
        end=end,
        clients=sorted(by_id.values(), key=lambda c: (-c.fixed, -c.severe, -c.accepted, c.tenant["name"].lower())),
        checks=sorted(checks.values(), key=lambda k: (-k.fixed, SEVERITY_RANK.get(k.severity, 9), k.title)),
        people=sorted(people, key=lambda p: (-p.fixed, p.name.lower())),
        figures=shown,
        back=back,
    )


def _figures(conn, ids: list[int], by_id: dict[int, ClientWork], start: datetime, end: datetime, sees) -> None:
    """Each figure as the quarter began (the last reading before it, within ``START_LOOKBACK``, or else its first
    reading in the quarter) and as it ended (its last reading in the quarter)."""
    keys = [f.key for f in FIGURES]
    readings: dict[tuple[int, str], list] = {}
    for r in conn.execute(
        f"SELECT tenant_id, key, value, captured_at FROM metrics WHERE key IN ({','.join('?' * len(keys))})"
        f" AND tenant_id IN ({','.join('?' * len(ids))}) AND captured_at >= ? AND captured_at < ? ORDER BY captured_at",
        (*keys, *ids, (start - START_LOOKBACK).strftime(ISO), end.strftime(ISO)),
    ):
        if r["value"] is not None:
            readings.setdefault((r["tenant_id"], r["key"]), []).append((str(r["captured_at"]), r["value"]))
    lo = start.strftime(ISO)
    for f in FIGURES:
        for tid in ids:
            if not sees(tid, f.report):
                continue
            got = readings.get((tid, f.key), [])
            before = [v for at, v in got if at < lo]
            inside = [v for at, v in got if at >= lo]
            if not before and not inside:
                continue
            first = before[-1] if before else inside[0]
            # One reading in the quarter and none before it: where it ended, with nothing to compare.
            by_id[tid].figures[f.key] = Moved(
                first if before or len(inside) > 1 else None, inside[-1] if inside else None
            )
