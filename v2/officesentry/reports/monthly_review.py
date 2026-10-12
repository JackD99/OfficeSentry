"""Monthly Security & Licensing Review: the flagship per-tenant report.

``build()`` turns stored snapshots into a plain data structure; renderers
(HTML, PDF, XLSX) only format it. Nothing here calls Microsoft.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from functools import cache
from pathlib import Path

from ..changes import Change, all_changes
from ..checks import SEVERITY_RANK, Finding, RuleResult, evaluate
from ..collectors import REGISTRY, core
from ..db import try_write, utcnow
from ..devices import parse_day
from ..findings import finding_key, lifecycle_map, resolved_since
from ..licences import LicenceSummary, licence_summary, load_prices
from ..snapshot import TenantSnapshot
from ..text import plural
from .charts import line_chart, month_columns

TITLE = "Monthly Security & Licensing Review"
# The review without its appendix: the two summary pages, as its own report a plan or schedule can send.
SUMMARY_KEY = "review-summary"
SUMMARY_TITLE = "Monthly review: summary only"
SUMMARY_STEM = "security-review-summary"
STALE_AFTER = timedelta(hours=36)  # data older than this is flagged in the report
PERIOD = timedelta(days=28)
SPARK_DAYS = 90  # how far back a figure's sparkline goes
# Findings first seen this soon after a tenant's first collection are its baseline, not "new".
BASELINE = timedelta(days=1)
TREND_MONTHS = 12  # months in the summary's "found and fixed" chart
MIN_CHART_MONTHS = 6  # columns it keeps for a client with less history
ISO = "%Y-%m-%dT%H:%M:%SZ"


@dataclass
class Kpi:
    key: str
    label: str
    value: float | None
    unit: str = ""
    previous: float | None = None
    better: str = "up"  # which direction is good: up | down
    note: str = ""
    history: list = field(default_factory=list)  # [[day, value], ...] for a sparkline: the last 90 days, oldest first

    @property
    def spark(self) -> list[float]:
        return [v for _, v in self.history]

    @property
    def spark_label(self) -> str:
        if len(self.history) < 3:
            return ""
        unit = "%" if self.unit == "%" else ""
        first, last = self.history[0], self.history[-1]
        day = date.fromisoformat(first[0])
        return f"{self.label} since {day.day} {day:%b %Y}: from {first[1]:g}{unit} to {last[1]:g}{unit}"

    @property
    def delta(self) -> float | None:
        if self.value is None or self.previous is None:
            return None
        return round(self.value - self.previous, 1)

    @property
    def trend(self) -> str:
        d = self.delta
        if not d:
            return "flat"
        good = (d > 0) == (self.better == "up")
        return "better" if good else "worse"


@dataclass
class Freshness:
    title: str
    finished_at: str | None
    status: str | None
    stale: bool


@dataclass
class MonthCount:
    """Findings found (first seen or back again) and fixed in one calendar month; None before collection began."""

    month: date  # the first day
    found: int | None
    fixed: int | None


@dataclass
class FixedGroup:
    """The findings of one check fixed in the period, for "What we did this month"."""

    key: str
    title: str
    severity: str
    subjects: list[str]


@dataclass
class Report:
    title: str
    tenant: dict
    generated_at: datetime
    currency: str
    kpis: list[Kpi]
    results: list[RuleResult]
    licences: LicenceSummary
    secure_score_chart: str
    freshness: list[Freshness]
    counts: dict = field(default_factory=dict)
    period_start: datetime | None = None
    changes: list[Change] = field(default_factory=list)
    resolved: list = field(default_factory=list)  # findings rows resolved in the period
    accepted: list[tuple[RuleResult, Finding, object]] = field(default_factory=list)
    new_count: int = 0
    first_report: bool = False  # history starts inside this period, so there's nothing to compare with
    months: list[MonthCount] = field(default_factory=list)  # oldest first, ending with this month
    fixed: list[FixedGroup] = field(default_factory=list)  # most fixed first

    @property
    def months_chart(self) -> str:
        """Found and fixed by month, once there are two months with data; "" before."""
        known = [m for m in self.months if m.found is not None]
        if len(known) < 2:
            return ""
        # Months before the first collection are left out, keeping at least six columns so a new client's
        # chart isn't two giant slots.
        shown = self.months[min(self.months.index(known[0]), max(len(self.months) - MIN_CHART_MONTHS, 0)) :]
        return month_columns(
            [(m.month, m.found, m.fixed) for m in shown],
            label=f"Findings found and fixed each month since {known[0].month:%B %Y}",
            series=("Found", "Fixed"),
        )

    @property
    def failing(self) -> list[RuleResult]:
        return sorted(
            (r for r in self.results if r.status == "fail"),
            key=lambda r: (SEVERITY_RANK.get(r.severity or "info", 9), r.category, r.title),
        )

    @property
    def new_findings(self) -> list[tuple[RuleResult, Finding]]:
        """The findings first seen (or back) in the period, most severe first: the ``new_count`` items."""
        found = [(r, f) for r in self.results for f in r.findings if f.new]
        return sorted(found, key=lambda rf: SEVERITY_RANK.get(rf[1].severity or "info", 9))

    @property
    def passing(self) -> list[RuleResult]:
        return [r for r in self.results if r.status == "pass"]

    @property
    def unavailable(self) -> list[RuleResult]:
        return [r for r in self.results if r.status == "unavailable"]


def _value_on_or_before(history: list[tuple[str, float]], when: datetime) -> float | None:
    cutoff = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    earlier = [v for ts, v in history if ts <= cutoff]
    return earlier[-1] if earlier else None


def _kpi(snap: TenantSnapshot, key: str, label: str, now: datetime, **kwargs) -> Kpi:
    history = snap.history(key)
    value = history[-1][1] if history else None
    previous = _value_on_or_before(history, now - timedelta(days=28))
    since = (now - timedelta(days=SPARK_DAYS)).strftime(ISO)
    daily = {ts[:10]: v for ts, v in history if ts >= since}  # each day's last value
    return Kpi(key, label, value, previous=previous, history=[[d, daily[d]] for d in sorted(daily)], **kwargs)


def _secure_score_points(snap: TenantSnapshot) -> list[tuple[date, float]]:
    """Microsoft's own daily history (up to 90 days) plus every score we've stored."""
    points: dict[date, float] = {}
    for ts, value in snap.history("secure_score.pct"):
        points[date.fromisoformat(ts[:10])] = value
    for row in snap.objects("secure_score", "secure_score_daily") or []:
        day, current, most = parse_day(row.get("date")), row.get("currentScore"), row.get("maxScore")
        if day and most and current is not None:
            points.setdefault(day, round(100 * current / most, 1))
    return sorted(points.items())


def setting(conn, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def _baseline_end(conn, tenant_id: int, now: datetime) -> str:
    """End of the tenant's first collection window; findings seen before it aren't new."""
    row = conn.execute(
        "SELECT MIN(finished_at) AS first FROM runs WHERE tenant_id = ? AND status IN ('succeeded', 'partial')",
        (tenant_id,),
    ).fetchone()
    first = datetime.fromisoformat(row["first"].replace("Z", "+00:00")) if row and row["first"] else now
    return (first + BASELINE).strftime(ISO)


def _month_starts(now: datetime, count: int) -> list[date]:
    first = date(now.year, now.month, 1)
    out = [first]
    while len(out) < count:
        d = out[-1]
        out.append(date(d.year - (d.month == 1), 12 if d.month == 1 else d.month - 1, 1))
    return out[::-1]


def found_and_fixed(conn, tenant_id: int, now: datetime, baseline_end: str, months: int = TREND_MONTHS):
    """``MonthCount`` per calendar month, the last ``months`` up to this one. A finding counts as found in the
    month it was first seen (after the first collection's baseline) or came back, and fixed in the month it was
    resolved. Months before the first collection have no data."""
    starts = _month_starts(now, months)
    since = datetime(starts[0].year, starts[0].month, 1, tzinfo=UTC).strftime(ISO)
    rows = conn.execute(
        "SELECT first_seen, reopened_at, resolved_at FROM findings WHERE tenant_id = ?"
        " AND (first_seen >= ? OR reopened_at >= ? OR resolved_at >= ?)",
        (tenant_id, since, since, since),
    ).fetchall()
    found: dict[str, int] = {}
    fixed: dict[str, int] = {}
    for r in rows:
        if r["first_seen"] > baseline_end:
            found[r["first_seen"][:7]] = found.get(r["first_seen"][:7], 0) + 1
        if r["reopened_at"]:
            found[r["reopened_at"][:7]] = found.get(r["reopened_at"][:7], 0) + 1
        if r["resolved_at"]:
            fixed[r["resolved_at"][:7]] = fixed.get(r["resolved_at"][:7], 0) + 1
    began = (datetime.fromisoformat(baseline_end.replace("Z", "+00:00")) - BASELINE).strftime("%Y-%m")
    out = []
    for start in starts:
        key = f"{start:%Y-%m}"
        known = key >= began and key <= now.strftime("%Y-%m")
        out.append(MonthCount(start, found.get(key, 0) if known else None, fixed.get(key, 0) if known else None))
    return out


def fixed_groups(resolved, results: list[RuleResult]) -> list[FixedGroup]:
    """The period's resolved findings by check, most fixed first, then most severe."""
    titles = {r.key: r.title for r in results}
    groups: dict[str, FixedGroup] = {}
    for row in resolved:
        g = groups.get(row["rule_key"])
        if g is None:
            title = titles.get(row["rule_key"], row["rule_key"].replace("_", " ").capitalize())
            g = groups[row["rule_key"]] = FixedGroup(row["rule_key"], title, row["severity"], [])
        elif SEVERITY_RANK.get(row["severity"], 9) < SEVERITY_RANK.get(g.severity, 9):
            g.severity = row["severity"]
        g.subjects.append(row["subject"])
    return sorted(groups.values(), key=lambda g: (-len(g.subjects), SEVERITY_RANK.get(g.severity, 9), g.title))


def build(conn, tenant, now: datetime | None = None) -> Report:
    now = now or datetime.now(UTC)
    snap = TenantSnapshot(conn, tenant)
    results = evaluate(snap, now)
    by_key = {r.key: r for r in results}
    period_start = now - PERIOD
    since = period_start.strftime(ISO)
    baseline_end = _baseline_end(conn, tenant["id"], now)
    first_report = baseline_end > since

    # Apply lifecycle: mark new findings, move accepted risks out of the totals.
    lifecycle = lifecycle_map(conn, tenant["id"], now)
    accepted = []
    for r in results:
        active = []
        for f in r.findings:
            life = lifecycle.get((r.key, finding_key(f)))
            if life and life.accepted:
                accepted.append((r, f, life))
                continue
            if life is None:  # not synced into findings yet: new unless still in the baseline window
                f.new = now.strftime(ISO) > baseline_end
            else:
                f.new = life.is_new_since(since) and (
                    life.first_seen > baseline_end or (life.reopened_at or "") >= since
                )
            active.append(f)
        if r.status == "fail" and not active:
            r.status = "accepted"
        r.findings = active

    skus = snap.objects("tenant_profile", "subscribed_sku") or []
    users = snap.objects("users", "user")
    subscriptions = snap.objects("tenant_profile", "company_subscription")
    licences = licence_summary(skus, users, load_prices(conn), now, subscriptions)

    stale = by_key["stale_users"]
    kpis = [
        _kpi(snap, "secure_score.pct", "Secure Score", now, unit="%"),
        _kpi(snap, "mfa.registered_pct", "Users with MFA", now, unit="%"),
        _kpi(snap, "admins.privileged_principals", "Privileged accounts", now, better="down"),
        _kpi(snap, "users.licensed", "Licensed users", now, better="none"),
        Kpi(
            "stale",
            "Inactive accounts",
            None if stale.status == "unavailable" else len(stale.findings),
            better="down",
            # Why it's missing, in the check's words: users not collected yet, or no Entra ID P1 for sign-ins.
            note=(stale.summary or "Not available") if stale.status == "unavailable" else "",
        ),
        Kpi(
            "waste",
            "Licence waste / month",
            licences.monthly_waste if licences.lines else None,
            unit="currency",
            better="down",
            note=f"{plural(licences.unpriced_waste_units, 'unpriced licence')} not counted"
            if licences.unpriced_waste_units
            else "",
        ),
    ]

    freshness = []
    for key, cls in ((k, REGISTRY[k]) for k in core()):
        run = snap.runs.get(key)
        finished = run["finished_at"] if run else None
        is_stale = finished is None or datetime.fromisoformat(finished.replace("Z", "+00:00")) < now - STALE_AFTER
        freshness.append(Freshness(cls.title, finished, run["status"] if run else None, is_stale))

    points = _secure_score_points(snap)
    counts = {s: 0 for s in SEVERITY_RANK}
    new_count = 0
    for r in results:
        for f in r.findings:
            counts[f.severity] += 1
            new_count += f.new

    resolved = list(resolved_since(conn, tenant["id"], since))
    return Report(
        title=TITLE,
        tenant=dict(tenant),
        generated_at=now,
        currency=setting(conn, "currency", "GBP"),
        kpis=kpis,
        results=results,
        licences=licences,
        secure_score_chart=line_chart(points, label="Secure Score over time"),
        freshness=freshness,
        counts=counts,
        period_start=period_start,
        changes=all_changes(conn, tenant["id"], since),
        resolved=resolved,
        accepted=accepted,
        new_count=new_count,
        first_report=first_report,
        months=found_and_fixed(conn, tenant["id"], now, baseline_end),
        fixed=fixed_groups(resolved, results),
    )


def leave_out(report: Report, hidden) -> Report:
    """The report without what a month-end reviewer left out of the client's copy (``month_end.hidden``):
    ``check:<key>`` drops that check everywhere (lists, counts, the summary, what was fixed and accepted) and
    ``kpi:<key>`` drops a key figure. Other items (parts of the report, recommendations) are the templates'.
    Returns the same report when nothing applies; never changes the one it's given."""
    hidden = set(hidden or ())
    checks = {item[6:] for item in hidden if item.startswith("check:")}
    kpis = {item[4:] for item in hidden if item.startswith("kpi:")}
    if not checks and not kpis:
        return report
    results = [r for r in report.results if r.key not in checks]
    counts = {s: 0 for s in report.counts} if report.counts else {s: 0 for s in SEVERITY_RANK}
    new_count = 0
    for r in results:
        for f in r.findings:
            counts[f.severity] = counts.get(f.severity, 0) + 1
            new_count += f.new
    return replace(
        report,
        results=results,
        kpis=[k for k in report.kpis if k.key not in kpis],
        counts=counts,
        new_count=new_count,
        accepted=[a for a in report.accepted if a[0].key not in checks],
        resolved=[row for row in report.resolved if _rule_key(row) not in checks],
        fixed=[g for g in report.fixed if g.key not in checks],
    )


def _rule_key(row) -> str:
    try:
        return row["rule_key"]
    except (KeyError, IndexError, TypeError):
        return ""


def rollup_row(conn, tenant, now: datetime | None = None, cached: bool = False) -> dict:
    """One row of the cross-tenant overview.

    ``cached`` (live pages only, so ``now`` is the present) reuses the row in
    ``rollup_cache`` while nothing it was built from has changed (see
    ``_fingerprint``) and stores it when something has. Data freshness is
    always worked out now.
    """
    if not cached:
        return _rollup_row(conn, tenant, now)
    fingerprint = _fingerprint(conn, tenant, datetime.now(UTC))
    hit = conn.execute("SELECT fingerprint, data FROM rollup_cache WHERE tenant_id = ?", (tenant["id"],)).fetchone()
    if hit and hit["fingerprint"] == fingerprint:
        data = json.loads(hit["data"])
    else:
        row = _rollup_row(conn, tenant)
        data = {k: v for k, v in row.items() if k not in ("tenant", "stale_data")}
        data["kpis"] = [asdict(k) for k in row["kpis"].values()]
        # The row is what the page needs; storing it can wait for the next view if the database is busy.
        try_write(
            conn,
            "INSERT INTO rollup_cache (tenant_id, fingerprint, data, built_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(tenant_id) DO UPDATE SET fingerprint = excluded.fingerprint,"
            " data = excluded.data, built_at = excluded.built_at",
            (tenant["id"], fingerprint, json.dumps(data), utcnow()),
        )
        return row
    kpis = [Kpi(**k) for k in data.pop("kpis")]
    return {
        **data,
        "tenant": dict(tenant),
        "kpis": {k.key: k for k in kpis},
        "stale_data": _stale(conn, tenant["id"], datetime.now(UTC)),
    }


def _rollup_row(conn, tenant, now: datetime | None = None) -> dict:
    report = build(conn, tenant, now)
    return {
        "tenant": report.tenant,
        "kpis": {k.key: k for k in report.kpis},
        "counts": report.counts,
        "failing": len(report.failing),
        "new": report.new_count,
        "first_report": report.first_report,
        "resolved": len(report.resolved),
        "unavailable": len(report.unavailable),
        "stale_data": any(f.stale for f in report.freshness),
    }


@cache
def _code_version() -> str:
    """A hash of Office Sentry's code, so an upgrade (new checks, say) rebuilds every cached rollup row."""
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).resolve().parents[1].rglob("*.py")):
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _fingerprint(conn, tenant, now: datetime) -> str:
    """What a tenant's rollup row is built from, cheaply: when this changes, the row is rebuilt.

    The tenant's latest run (its snapshots and metrics) and licence flags, its
    findings (including accepted risks), licence prices, currency and app
    connections, the code, and the day (what counts as "new" and the 4-week
    comparison move daily).
    """
    run = conn.execute(
        "SELECT finished_at, id FROM runs WHERE tenant_id = ? AND finished_at IS NOT NULL"
        " ORDER BY finished_at DESC, id DESC LIMIT 1",
        (tenant["id"],),
    ).fetchone()
    findings = conn.execute(
        "SELECT COUNT(*), MAX(last_seen), MAX(resolved_at), MAX(reopened_at), COUNT(accepted_at),"
        " MAX(accepted_at), MAX(accepted_until) FROM findings WHERE tenant_id = ?",
        (tenant["id"],),
    ).fetchone()
    prices = conn.execute("SELECT COUNT(*), MAX(updated_at), TOTAL(monthly_price) FROM sku_prices").fetchone()
    apps = conn.execute("SELECT GROUP_CONCAT(client_id) FROM app_connections").fetchone()[0]
    return json.dumps(
        [
            _code_version(),
            now.date().isoformat(),
            list(run or ()),
            tenant["capabilities"],
            list(findings),
            list(prices),
            setting(conn, "currency", "GBP"),
            apps,
        ]
    )


def _stale(conn, tenant_id: int, now: datetime) -> bool:
    """True when a data source has never succeeded or last did longer than ``STALE_AFTER`` ago."""
    keys = core()
    # One MAX per collector and status, each a single index lookup (idx_runs_latest).
    rows = conn.execute(
        f"WITH c(key) AS (VALUES {','.join(f'(:k{i})' for i in range(len(keys)))})"
        " SELECT MAX(IFNULL((SELECT MAX(finished_at) FROM runs WHERE tenant_id = :t AND collector = c.key"
        "                    AND status = 'succeeded'), ''),"
        "            IFNULL((SELECT MAX(finished_at) FROM runs WHERE tenant_id = :t AND collector = c.key"
        "                    AND status = 'partial'), '')) AS at FROM c",
        {**{f"k{i}": k for i, k in enumerate(keys)}, "t": tenant_id},
    ).fetchall()
    cutoff = (now - STALE_AFTER).strftime(ISO)
    return any(not r["at"] or r["at"] < cutoff for r in rows)


def dashboard_trends(conn, rows: list[dict], now: datetime | None = None) -> dict:
    """Weekly sparklines for the dashboard's tiles, over the last ``SPARK_DAYS``: the average Secure Score of
    the tenants shown (each tenant's latest score at each week) and their open critical and high findings
    (from the findings table's dates, leaving out accepted risks, from the first collection on), ending on
    today's figures."""
    from .data_reports import Trend

    now = now or datetime.now(UTC)
    weeks = [now - timedelta(days=7 * n) for n in range(SPARK_DAYS // 7, -1, -1)]

    def since(when: datetime) -> str:
        return f"{when.day} {when:%b %Y}"

    out = {}
    scores: list[tuple[datetime, float]] = []
    for when in weeks:
        day = when.date().isoformat()
        known = [next((v for d, v in reversed(r["kpis"]["secure_score.pct"].history) if d <= day), None) for r in rows]
        known = [v for v in known if v is not None]
        if known:
            scores.append((when, round(sum(known) / len(known), 1)))
    if len(scores) >= 3:
        out["score_trend"] = Trend(
            [v for _, v in scores], f"{scores[0][1]:.0f}%", f"{scores[-1][1]:.0f}%", since(scores[0][0])
        )

    ids = [r["tenant"]["id"] for r in rows]
    if not ids:
        return out
    marks = ",".join("?" * len(ids))
    first = conn.execute(f"SELECT MIN(finished_at) FROM runs WHERE tenant_id IN ({marks})", ids).fetchone()[0]
    found = conn.execute(
        f"SELECT first_seen, resolved_at, accepted_at, accepted_until FROM findings WHERE tenant_id IN ({marks})"
        " AND severity IN ('critical', 'high') AND (resolved_at IS NULL OR resolved_at >= ?)",
        (*ids, weeks[0].strftime(ISO)),
    ).fetchall()

    def open_at(at: str) -> int:
        return sum(
            1
            for f in found
            if f["first_seen"] <= at
            and (f["resolved_at"] is None or f["resolved_at"] > at)
            and not (
                f["accepted_at"]
                and f["accepted_at"] <= at
                and (f["accepted_until"] is None or f["accepted_until"] > at)
            )
        )

    shown = [w for w in weeks[:-1] if first and w.strftime(ISO) >= first]
    counts = [float(open_at(w.strftime(ISO))) for w in shown]
    counts.append(float(sum(r["counts"].get("critical", 0) + r["counts"].get("high", 0) for r in rows)))
    if len(counts) >= 3:
        out["severe_trend"] = Trend(counts, f"{counts[0]:,.0f}", f"{counts[-1]:,.0f}", since(shown[0]))
    return out
