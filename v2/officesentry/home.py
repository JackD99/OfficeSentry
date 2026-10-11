"""Home pages by role (pages in ``web/home.py``): each opens with one sentence, then the detail.

- **Today**, for technicians: the clients that need someone today, and why: critical and high findings that
  are new in the last day, collections that failed or are out of date, access not granted yet, and anything
  due within a week (from ``upcoming``).
- **Clients**, for account managers: licence waste, renewals in the next 30 days, this month's reports
  waiting for approval and projects to propose, per client.

Both read stored state only (findings, runs, cached rollup rows, snapshots), never Microsoft, and only for the
tenants they're given, which the page narrows to the person's visible tenants and chosen group. A person picks
which one opens as their home page in Account settings (``users.home``); permissions don't change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import tenant_health, upcoming
from .collectors import REGISTRY
from .rules import RULES
from .text import plural

TODAY, CLIENTS = "today", "clients"
HOMES = {
    TODAY: (
        "Today",
        "What needs you today: new critical and high findings, collections that failed and anything due this week.",
    ),
    CLIENTS: (
        "Accounts",
        "Licence waste, renewals in the next 30 days, reports waiting for approval and projects to propose.",
    ),
}
DEFAULT = TODAY
NEW_SINCE = timedelta(days=1)  # what counts as a new finding on Today
DUE_WITHIN = 7  # days: Coming up items that make Today
RENEWAL_WITHIN = 30  # days: renewals on Clients
RENEWAL_TYPES = (upcoming.SUBSCRIPTION, upcoming.TRIAL)
STALE_AFTER = timedelta(hours=36)  # matches monthly_review.STALE_AFTER and the tenants list
_ISO = "%Y-%m-%dT%H:%M:%SZ"
_RULE_TITLES = {r.key: r.title for r in RULES}
_RANK = {"bad": 0, "warn": 1}
_NOUNS = {
    upcoming.SUBSCRIPTION: "subscription",
    upcoming.TRIAL: "trial",
    upcoming.APP_KEY_TYPE: "app secret",
    upcoming.APP_CERTIFICATE: "app certificate",
    upcoming.QUOTA: "mailbox",
    upcoming.DMARC: "domain",
    upcoming.RISK: "accepted risk",
}
KINDS = ("findings", "collection", "access", "due")  # the order a client's reasons show in


def home_of(user) -> str:
    """The home page key a staff member chose, or the default."""
    try:
        chosen = user["home"]
    except (KeyError, IndexError):
        chosen = None
    return chosen if chosen in HOMES else DEFAULT


def join(parts: list[str]) -> str:
    """'a', 'a and b', 'a, b and c'."""
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


# -- Today ----------------------------------------------------------------------------------------


@dataclass
class Reason:
    kind: str  # findings | collection | access | due
    tone: str  # bad | warn
    text: str  # what's wrong, in words
    detail: str = ""  # one more line: which checks, which collectors
    href: str = ""  # where to look or fix it (a portal path, set by the page)
    tenant_id: int = 0
    target: str = ""  # what href points at: findings | collection | tenant | upcoming | a path from Coming up


@dataclass
class TodayRow:
    tenant: dict
    reasons: list[Reason] = field(default_factory=list)

    @property
    def tone(self) -> str:
        return min((r.tone for r in self.reasons), key=lambda t: _RANK.get(t, 2), default="")


@dataclass
class Today:
    rows: list[TodayRow]  # clients that need someone, worst first
    tenants: int  # active clients looked at
    new_severe: int  # new critical and high findings among them
    new_critical: int
    failing: int  # clients whose collection failed or can't sign in
    stale: int  # clients whose data is out of date
    waiting: int  # clients that haven't granted access yet
    due: int  # things due within DUE_WITHIN days, or needing attention now
    since: datetime
    without: list[tuple[dict, str]] = field(default_factory=list)  # Coming up parts that couldn't be read

    @property
    def fine(self) -> int:
        return self.tenants - len(self.rows)

    def sentence(self) -> str:
        """The page's opening line."""
        if not self.tenants:
            return "No active clients to look after yet."
        parts = []
        if self.new_severe:
            word = (
                "critical"
                if self.new_critical == self.new_severe
                else ("critical or high" if self.new_critical else "high")
            )
            parts.append(plural(self.new_severe, f"new {word} finding"))
        if self.failing:
            parts.append(plural(self.failing, "failed collection"))
        if self.stale:
            parts.append(f"{self.stale} out of date")
        if self.waiting:
            parts.append(f"{self.waiting} waiting for access")
        if self.due:
            parts.append(f"{plural(self.due, 'thing')} due this week")
        if not self.rows:
            who = "Your client is" if self.tenants == 1 else f"All {self.tenants} clients are"
            return (
                f"Nothing needs you today. {who} collecting, with no new critical or high findings and nothing"
                " due this week."
            )
        lead = f"{plural(len(self.rows), 'client')} {'needs' if len(self.rows) == 1 else 'need'} you today"
        return f"{lead}: {join(parts)}."


def _active(tenants) -> list[dict]:
    return [dict(t) for t in tenants if t["enabled"] and not t["archived_at"]]


def _new_findings(conn, ids: list[int], since: str, now: str) -> dict[int, list]:
    """Critical and high findings first seen (or back) since ``since``, open and not accepted, per tenant."""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT tenant_id, rule_key, severity, COUNT(*) AS n FROM findings WHERE tenant_id IN ({marks})"
        " AND resolved_at IS NULL AND severity IN ('critical', 'high')"
        " AND (first_seen >= ? OR IFNULL(reopened_at, '') >= ?)"
        " AND (accepted_at IS NULL OR (accepted_until IS NOT NULL AND accepted_until <= ?))"
        " GROUP BY tenant_id, rule_key, severity"
        " ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END, n DESC, rule_key",
        (*ids, since, since, now),
    ).fetchall()
    out: dict[int, list] = {}
    for r in rows:
        out.setdefault(r["tenant_id"], []).append(r)
    return out


def _collection(conn, tenant: dict, now: datetime) -> Reason | None:
    """Why this client's data can't be trusted today, or None. The same reading as the tenants list."""
    if not tenant["consented_at"]:
        return Reason(
            "access",
            "warn",
            "Waiting for a tenant admin to grant access",
            "Nothing can be collected until they do.",
            target="tenant",
        )
    latest = tenant_health.latest_runs(conn, tenant["id"])
    if not latest:
        return None  # added and consented, first collection still to come
    failed = [k for k, r in latest.items() if r["status"] == "failed"]
    sign_in = [latest[k]["error"] for k in failed if (latest[k]["error"] or "").startswith("Sign-in failed: ")]
    if sign_in and len(sign_in) == len(failed):
        return Reason(
            "collection",
            "bad",
            "Office Sentry can't sign in to this tenant",
            sign_in[0].removeprefix("Sign-in failed: "),
            target="collection",
        )
    if failed:
        names = [REGISTRY[k].title for k in failed if k in REGISTRY]
        return Reason(
            "collection", "bad", f"{plural(len(failed), 'collection')} failed", ", ".join(names), target="collection"
        )
    if any(r["status"] in ("queued", "running") for r in latest.values()):
        return None
    finished = [r["finished_at"] for r in latest.values() if r["finished_at"]]
    cutoff = (now - STALE_AFTER).strftime(_ISO)
    if finished and min(finished) < cutoff:
        return Reason(
            "collection",
            "warn",
            "Some data is more than 36 hours old",
            "Reports and checks may be out of date.",
            target="collection",
        )
    return None


def _due(items: list) -> list[Reason]:
    """Coming up items for one client: one line each, or one line per type when there are several."""
    by_type: dict[str, list] = {}
    for item in items:
        by_type.setdefault(item.type, []).append(item)
    out = []
    for kind, group in by_type.items():
        tone = "bad" if any(i.window == upcoming.NOW for i in group) else "warn"
        if len(group) == 1:
            item = group[0]
            when = f", {item.when}" if item.when else ""
            out.append(Reason("due", tone, f"{kind}: {item.title}{when}", item.detail, target=item.href))
        else:
            # Each part's items share a link (the report that lists them), so one line links to all of them.
            href = group[0].href if len({i.href for i in group}) == 1 else ""
            out.append(
                Reason(
                    "due",
                    tone,
                    f"{kind}: {plural(len(group), _NOUNS.get(kind, 'item'))}",
                    " · ".join(i.title for i in group),
                    target=href or "upcoming",
                )
            )
    return out


def today(conn, tenants, now: datetime | None = None) -> Today:
    """What needs someone today across ``tenants`` (paused and archived ones are left out)."""
    now = now or datetime.now(UTC)
    since = now - NEW_SINCE
    active = _active(tenants)
    found = _new_findings(conn, [t["id"] for t in active], since.strftime(_ISO), now.strftime(_ISO))
    up = upcoming.collect(conn, active, now)
    soon = {}
    for item in up.items:
        if item.tenant and (item.window == upcoming.NOW or (item.days is not None and item.days <= DUE_WITHIN)):
            soon.setdefault(item.tenant["id"], []).append(item)
    rows, totals = [], {"new": 0, "critical": 0, "failing": 0, "stale": 0, "waiting": 0, "due": 0}
    for t in active:
        row = TodayRow(t)
        news = found.get(t["id"], [])
        if news:
            n = sum(r["n"] for r in news)
            critical = sum(r["n"] for r in news if r["severity"] == "critical")
            word = "critical" if critical == n else ("critical or high" if critical else "high")
            titles = [_RULE_TITLES.get(r["rule_key"], r["rule_key"]) for r in news]
            row.reasons.append(
                Reason(
                    "findings",
                    "bad" if critical else "warn",
                    plural(n, f"new {word} finding"),
                    "Failing: " + " · ".join(dict.fromkeys(titles)),
                    target="findings",
                )
            )
            totals["new"] += n
            totals["critical"] += critical
        if (state := _collection(conn, t, now)) is not None:
            row.reasons.append(state)
            key = {"access": "waiting", "collection": "failing" if state.tone == "bad" else "stale"}[state.kind]
            totals[key] += 1
        row.reasons += _due(soon.get(t["id"], []))
        totals["due"] += len(soon.get(t["id"], []))
        if row.reasons:
            for r in row.reasons:
                r.tenant_id = t["id"]
            row.reasons.sort(key=lambda r: (KINDS.index(r.kind), _RANK.get(r.tone, 2)))
            rows.append(row)
    rows.sort(key=lambda r: (_RANK.get(r.tone, 2), -len(r.reasons), r.tenant["name"].lower()))
    return Today(
        rows,
        len(active),
        totals["new"],
        totals["critical"],
        totals["failing"],
        totals["stale"],
        totals["waiting"],
        totals["due"],
        since,
        up.without,
    )


# -- Clients --------------------------------------------------------------------------------------


@dataclass
class ClientRow:
    tenant: dict
    waste: float | None  # licence waste a month, None without prices or licence data
    renewal: upcoming.Item | None  # the next renewal or trial end within RENEWAL_WITHIN days
    renewals: int
    report_state: str | None  # this month's report: draft, in_review, approved or sent; None off a plan
    report_ready: bool
    projects: int  # recommended projects not declined
    proposed: int  # of which marked Proposed or Agreed
    score: float | None  # Secure Score, %
    note: str = ""


@dataclass
class Clients:
    rows: list[ClientRow]
    currency: str
    period: str  # the month-end period being reported
    period_label: str
    on_plan: int  # clients on a month-end report plan

    @property
    def waste(self) -> float | None:
        known = [r.waste for r in self.rows if r.waste is not None]
        return sum(known) if known else None

    @property
    def wasting(self) -> int:
        return sum(1 for r in self.rows if r.waste)

    @property
    def renewals(self) -> int:
        return sum(r.renewals for r in self.rows)

    @property
    def to_approve(self) -> int:
        return sum(1 for r in self.rows if r.report_state in ("draft", "in_review"))

    @property
    def to_propose(self) -> int:
        return sum(r.projects - r.proposed for r in self.rows)

    def sentence(self, money) -> str:
        """The page's opening line. ``money(value)`` formats an amount in the installation's currency."""
        if not self.rows:
            return "No active clients yet."
        parts = []
        if self.waste is None:
            parts.append("licence waste isn't counted until licence prices are set")
        elif self.waste:
            parts.append(f"{money(self.waste)} a month of licence waste across {plural(self.wasting, 'client')}")
        else:
            parts.append("no licence waste")
        parts.append(
            f"{plural(self.renewals, 'renewal')} in the next {RENEWAL_WITHIN} days"
            if self.renewals
            else f"no renewals in the next {RENEWAL_WITHIN} days"
        )
        if self.on_plan:
            parts.append(
                f"{plural(self.to_approve, 'report')} to approve for {self.period_label}"
                if self.to_approve
                else f"every {self.period_label} report approved or sent"
            )
        text = "; ".join(parts) + "."
        return text[0].upper() + text[1:]


def clients(
    conn,
    tenants,
    period: str,
    period_label: str,
    currency: str,
    now: datetime | None = None,
    *,
    rollup=None,
    queue=None,
    pipeline=None,
) -> Clients:
    """Per client: waste, renewals, this month's report and projects.

    ``rollup(tenant)`` gives the cached rollup row (``monthly_review.rollup_row``), ``queue(tenants)`` this
    month's month-end rows and ``pipeline(tenants)`` the recommended projects; the page passes them so this
    reads the same cached data the other pages do.
    """
    from . import month_end, projects
    from .reports.monthly_review import rollup_row

    now = now or datetime.now(UTC)
    active = _active(tenants)
    rollup = rollup or (lambda t: rollup_row(conn, t, cached=True))
    queued = {r.tenant["id"]: r for r in (queue or (lambda ts: month_end.queue(conn, ts, period, now)))(active)}
    piped = {r.tenant["id"]: r for r in (pipeline or (lambda ts: projects.pipeline(conn, ts, now)))(active).rows}
    up = upcoming.collect(conn, active, now)
    renewals: dict[int, list] = {}
    for item in up.items:
        if (
            item.tenant
            and item.type in RENEWAL_TYPES
            and (item.window == upcoming.NOW or (item.days is not None and item.days <= RENEWAL_WITHIN))
        ):
            renewals.setdefault(item.tenant["id"], []).append(item)
    rows = []
    for t in active:
        roll = rollup(t)
        waste = roll["kpis"]["waste"].value if "waste" in roll["kpis"] else None
        score = roll["kpis"]["secure_score.pct"].value if "secure_score.pct" in roll["kpis"] else None
        q = queued.get(t["id"])
        p = piped.get(t["id"])
        recs = [r for r in (p.recommendations if p else []) if r.status != "declined"]
        mine = renewals.get(t["id"], [])
        rows.append(
            ClientRow(
                t,
                waste,
                mine[0] if mine else None,
                len(mine),
                q.state if q else None,
                bool(q and q.ready),
                len(recs),
                sum(r.status in ("proposed", "agreed") for r in recs),
                score,
                p.note if p else "",
            )
        )
    rows.sort(key=lambda r: (-(r.waste or 0), -r.renewals, r.tenant["name"].lower()))
    return Clients(rows, currency, period, period_label, len(queued))
