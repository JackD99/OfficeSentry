"""A tenant's Activity history page: how far back its sign-in and audit logs are kept, and what they hold.

Built from the activity history (``events.py``) only: the daily sign-in summaries for the figures, the chart
and the months, and the directory audit events for the admin changes worth a second look. Raw sign-ins are
only read for a month's download (``signin_rows``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from .. import adminactivity, events, retention, signins
from ..auditlog import addon
from ..text import plural
from . import charts

RECENT_DAYS = 30  # the figures at the top
CHART_DAYS = 90
REVIEW_DAYS = 90  # admin changes listed
MAX_REVIEW = 50
MAX_LISTED_RANGES = 3

SIGNIN_CSV_COLUMNS = (
    "Time (UTC)",
    "User",
    "Display name",
    "App",
    "Client app",
    "Result",
    "Error code",
    "Failure reason",
    "MFA",
    "IP address",
    "Country",
    "City",
    "Operating system",
    "Conditional Access",
    "Risk level",
    "Interactive",
    "Sign-in ID",
)


@dataclass
class Source:
    """One log's coverage, in words for the page."""

    key: str  # signins or audits
    noun: str  # "sign-in", "directory audit event"
    coverage: events.Coverage
    since: date | None  # detailed from
    not_read: list[tuple[date, date]] = field(default_factory=list)
    lost: list[tuple[date, date]] = field(default_factory=list)


@dataclass
class Month:
    month: str  # YYYY-MM
    label: str  # "Oct 2026"
    totals: events.Totals
    download: bool  # raw sign-ins kept for it

    @property
    def mfa_share(self) -> int | None:
        t = self.totals
        known = t.mfa + t.single_factor
        return round(100 * t.mfa / known) if known else None


@dataclass
class Page:
    now: datetime
    signins: Source
    audits: Source
    tiles: list[dict]
    chart: str
    chart_label: str
    months: list[Month]
    review: list[dict]  # normalised admin events with "severity" and "why"
    review_total: int
    policy: retention.Policy
    addon: dict | None = None  # the audit log add-on's state and records kept, where it was asked for

    @property
    def collected(self) -> bool:
        return self.signins.since is not None or self.audits.since is not None

    @property
    def up_to_date(self) -> bool:
        return all(s.coverage.up_to_date for s in (self.signins, self.audits) if s.since is not None)

    @property
    def read_at(self) -> str | None:
        """When the logs were last read (the older of the two, so the claim holds for both)."""
        times = [s.coverage.updated_at for s in (self.signins, self.audits) if s.since and s.coverage.updated_at]
        return min(times) if times else None


def _date(ts: int) -> date:
    return datetime.fromtimestamp(ts, UTC).date()


def _source(conn, tenant_id: int, key: str, noun: str) -> Source:
    cov = events.coverage(conn, tenant_id, key)
    first = cov.detailed_from
    src = Source(key, noun, cov, _date(first) if first is not None else None)
    src.not_read = [(_date(lo), _date(hi)) for lo, hi in sorted(cov.gaps) if first is None or hi > first]
    src.lost = [(_date(lo), _date(hi)) for lo, hi in sorted(cov.lost)]
    return src


def ranges_text(ranges: list[tuple[date, date]]) -> str:
    """ "9 Oct 2026", "1 Sep to 3 Sep 2026", and at most a few of them, oldest first."""

    def one(lo, hi):
        if lo == hi:
            return f"{lo.day} {lo:%b %Y}"
        return f"{lo.day} {lo:%b}{f' {lo:%Y}' if lo.year != hi.year else ''} to {hi.day} {hi:%b %Y}"

    shown = [one(lo, hi) for lo, hi in ranges[:MAX_LISTED_RANGES]]
    more = len(ranges) - len(shown)
    text = ", ".join(shown[:-1]) + (" and " if len(shown) > 1 else "") + shown[-1] if shown else ""
    return text + (f" (and {plural(more, 'other stretch', 'other stretches')})" if more else "")


def _chart(day_totals: dict[str, events.Totals], covered: set[str], last: date) -> tuple[str, str]:
    days = []
    for i in range(CHART_DAYS - 1, -1, -1):
        d = last - timedelta(days=i)
        t = day_totals.get(d.isoformat())
        if d.isoformat() not in covered:
            days.append((d, None, None, f"{d.day} {d:%b %Y}: no data"))
            continue
        t = t or events.Totals(d.isoformat())
        text = f"{d.day} {d:%b %Y}: {t.succeeded:,} signed in, {t.failed:,} failed" + (
            f", {t.interrupted:,} interrupted by a prompt" if t.interrupted else ""
        )
        days.append((d, t.succeeded, t.failed, text))
    label = f"Sign-ins per day for the {CHART_DAYS} days to {last.day} {last:%b %Y}: signed in and failed"
    return charts.daily_bars(days, label=label), label


def _covered_days(src: Source, first: date, last: date) -> set[str]:
    """Days from ``first`` to ``last`` the sign-in history fully or partly covers."""
    if src.since is None:
        return set()
    out = set()
    start = max(first, src.since)
    holes = src.coverage.missing(
        int(datetime.combine(start, datetime.min.time(), UTC).timestamp()),
        int(datetime.combine(last, datetime.max.time(), UTC).timestamp()),
    )
    d = start
    while d <= last:
        lo = int(datetime.combine(d, datetime.min.time(), UTC).timestamp())
        if not any(a <= lo and b >= lo + events.DAY - 1 for a, b in holes):
            out.add(d.isoformat())
        d += timedelta(days=1)
    return out


def build(conn, tenant_id: int, policy: retention.Policy, now: datetime | None = None) -> Page:
    now = now or events.clock()
    today = now.date()
    signins_src = _source(conn, tenant_id, "signins", "sign-in")
    audits_src = _source(conn, tenant_id, "audits", "directory audit event")

    # Sign-in figures, the chart and the months, from the daily summaries.
    oldest = today - timedelta(days=31 * (policy.history_daily_months or 120))
    day_list, month_list = events.totals(conn, tenant_id, oldest.isoformat(), today.isoformat())
    by_day = {t.period: t for t in day_list}
    recent_first = today - timedelta(days=RECENT_DAYS - 1)
    recent = [t for t in day_list if t.period >= recent_first.isoformat()]
    people = {
        r.user.lower() for r in events.daily_between(conn, tenant_id, recent_first.isoformat(), today.isoformat())
    }
    covered = _covered_days(signins_src, today - timedelta(days=CHART_DAYS - 1), today)
    chart, chart_label = _chart(by_day, covered, today) if signins_src.since else ("", "")
    raw_months = set(events.months_with_signins(conn, tenant_id))
    months = [
        Month(t.period, f"{datetime.strptime(t.period, '%Y-%m'):%b %Y}", t, t.period in raw_months)
        for t in reversed(month_list)
    ]
    known = {m.month for m in months}
    months += [
        Month(m, f"{datetime.strptime(m, '%Y-%m'):%b %Y}", events.Totals(m), True)
        for m in sorted(raw_months - known, reverse=True)
    ]

    # Admin changes worth a second look.
    review: list[dict] = []
    review_recent = 0
    history = adminactivity.from_history(conn, tenant_id, now - timedelta(days=REVIEW_DAYS), now)
    for e in history[0] if history else []:
        flag = adminactivity.review(e)
        if not flag:
            continue
        if (e.get("time") or "") >= recent_first.isoformat():
            review_recent += 1
        review.append({**e, "severity": flag[0], "why": flag[1]})

    total = sum(t.sign_ins for t in recent)
    failed = sum(t.failed for t in recent)
    single = sum(t.single_factor for t in recent)
    succeeded = sum(t.succeeded for t in recent)
    tiles = []
    if signins_src.since:
        tiles += [
            {"label": "Sign-ins", "value": f"{total:,}", "foot": f"in the last {RECENT_DAYS} days"},
            {
                "label": "Failed",
                "value": f"{failed:,}",
                "tone": "warn" if failed else None,
                "foot": f"{round(100 * failed / total)}% of sign-ins" if total else "none",
            },
            {
                "label": "Password only",
                "value": f"{single:,}",
                "tone": "warn" if single else None,
                "foot": (
                    f"{round(100 * single / succeeded)}% of successful sign-ins skipped MFA"
                    if succeeded
                    else "successful sign-ins without MFA"
                ),
            },
            {"label": "People", "value": f"{len(people):,}", "foot": "signed in at least once"},
        ]
    if audits_src.since:
        tiles.append(
            {
                "label": "Admin changes to review",
                "value": f"{review_recent:,}",
                "tone": "warn" if review_recent else None,
                "foot": f"in the last {RECENT_DAYS} days",
            }
        )
    row = addon.row_for(conn, tenant_id) if addon.enabled(conn) else None
    shown = None if row is None else {"state": addon.state(row), "records": events.counts(conn, tenant_id)["activity"]}
    return Page(
        now, signins_src, audits_src, tiles, chart, chart_label, months, review[:MAX_REVIEW], len(review), policy, shown
    )


def signin_rows(conn, tenant_id: int, month: str):
    """Every stored sign-in of ``month`` (YYYY-MM) as CSV cells, oldest first, for the month's download."""
    start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    end = (start + timedelta(days=32)).replace(day=1) - timedelta(seconds=1)
    for s in events.signins_between(conn, tenant_id, start, end, oldest_first=True):
        status = s.get("status") or {}
        code = status.get("errorCode") or 0
        mfa = signins.mfa_used(s)
        location = s.get("location") or {}
        yield (
            s["createdDateTime"],
            s.get("userPrincipalName"),
            s.get("userDisplayName"),
            s.get("appDisplayName"),
            s.get("clientAppUsed"),
            signins.meaning(code, status.get("failureReason")),
            code,
            status.get("failureReason"),
            {True: "Yes", False: "No"}.get(mfa, ""),
            s.get("ipAddress"),
            location.get("countryOrRegion"),
            location.get("city"),
            (s.get("deviceDetail") or {}).get("operatingSystem"),
            s.get("conditionalAccessStatus"),
            s.get("riskLevelDuringSignIn"),
            "Yes" if s.get("isInteractive") else "No",
            s.get("id"),
        )
