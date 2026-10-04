"""Monthly Security & Licensing Review: the flagship per-tenant report.

``build()`` turns stored snapshots into a plain data structure; renderers
(HTML, PDF, XLSX) only format it. Nothing here calls Microsoft.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from ..changes import Change, all_changes
from ..collectors import REGISTRY
from ..devices import parse_day
from ..findings import finding_key, lifecycle_map, resolved_since
from ..licences import LicenceSummary, licence_summary, load_prices
from ..rules import SEVERITY_RANK, Finding, RuleResult, evaluate
from ..snapshot import TenantSnapshot
from ..text import plural
from .charts import line_chart

TITLE = "Monthly Security & Licensing Review"
STALE_AFTER = timedelta(hours=36)  # data older than this is flagged in the report
PERIOD = timedelta(days=28)
# Findings first seen this soon after a tenant's first collection are its baseline, not "new".
BASELINE = timedelta(days=1)
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

    @property
    def failing(self) -> list[RuleResult]:
        return sorted((r for r in self.results if r.status == "fail"),
                      key=lambda r: (SEVERITY_RANK.get(r.severity or "info", 9), r.category, r.title))

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
    return Kpi(key, label, value, previous=previous, **kwargs)


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
    row = conn.execute("SELECT MIN(finished_at) AS first FROM runs WHERE tenant_id = ?"
                       " AND status IN ('succeeded', 'partial')", (tenant_id,)).fetchone()
    first = datetime.fromisoformat(row["first"].replace("Z", "+00:00")) if row and row["first"] else now
    return (first + BASELINE).strftime(ISO)


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
                    life.first_seen > baseline_end or (life.reopened_at or "") >= since)
            active.append(f)
        if r.status == "fail" and not active:
            r.status = "accepted"
        r.findings = active

    skus = snap.objects("tenant_profile", "subscribed_sku") or []
    users = snap.objects("users", "user")
    licences = licence_summary(skus, users, load_prices(conn), now)

    stale = by_key["stale_users"]
    kpis = [
        _kpi(snap, "secure_score.pct", "Secure Score", now, unit="%"),
        _kpi(snap, "mfa.registered_pct", "Users with MFA", now, unit="%"),
        _kpi(snap, "admins.privileged_principals", "Privileged accounts", now, better="down"),
        _kpi(snap, "users.licensed", "Licensed users", now, better="none"),
        Kpi("stale", "Inactive accounts",
            None if stale.status == "unavailable" else len(stale.findings), better="down",
            note="Needs Entra ID P1" if stale.status == "unavailable" else ""),
        Kpi("waste", "Licence waste / month", licences.monthly_waste if licences.lines else None,
            unit="currency", better="down",
            note=f"{plural(licences.unpriced_waste_units, 'unpriced licence')} not counted"
            if licences.unpriced_waste_units else ""),
    ]

    freshness = []
    for key, cls in REGISTRY.items():
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

    return Report(
        title=TITLE, tenant=dict(tenant), generated_at=now, currency=setting(conn, "currency", "GBP"),
        kpis=kpis, results=results, licences=licences,
        secure_score_chart=line_chart(points, label="Secure Score over time"), freshness=freshness, counts=counts,
        period_start=period_start, changes=all_changes(conn, tenant["id"], since),
        resolved=list(resolved_since(conn, tenant["id"], since)), accepted=accepted, new_count=new_count,
        first_report=first_report,
    )


def rollup_row(conn, tenant, now: datetime | None = None) -> dict:
    """One row of the cross-tenant overview."""
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
