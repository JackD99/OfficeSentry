"""Licence changes: who was given or lost a licence and when, and licences bought and assigned month by month.

Microsoft only says who holds which licence now. Office Sentry keeps each night's Users and licence
collections (the snapshots while retention keeps them, the daily history after that), so comparing them shows
every licence assigned or removed: to the day over the last month, and to the month before that. Where the
directory audit log has been read into the activity history, it adds who made each change and exactly when.

For an account manager checking a client's bill against last month's, or a technician asked who gave someone
a licence. Free licences (Power Automate Free and the like) are left out: nobody pays for them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .. import adminactivity
from ..devices import parse_ts
from ..licences import Assignment, assignments, is_free, load_prices, sku_name, trial_seats
from ..snapshot import TenantSnapshot
from .charts import line_chart
from .data_reports import DataReport, Highlight, Rows, Section
from .identity_reports import Money
from .monthly_review import setting
from .registry import report
from .subscriptions import _consumed, _units

CHANGES = "Assigned and removed"
MONTHS = "Licences by month"
ASSIGNED, REMOVED = "Assigned", "Removed"
DAILY_DAYS = 31  # changes this recent are dated to the day's collection; older ones to the month's
HISTORY_DAYS = 366
RECENT_DAYS = 30  # what the headline figures count
AUDIT_DAYS = 92  # how far back changes are matched to the audit log ("Update user" is busy on synced tenants)
MIN_SPAN = 7  # a month-on-month comparison needs at least this many days of history
SLACK = timedelta(hours=1)  # an audit event can land while the collection that misses it is still running
LICENCE_ACTIVITIES = ("change user license", "update user")
SKU_ID = re.compile(r"SkuId=([^,\]\s\"\\]+)")


@dataclass(frozen=True)
class Held:
    """One account's licences in one collection."""

    label: str
    enabled: bool
    skus: frozenset[str]
    paths: dict[str, Assignment] | None  # how each is assigned; None before Users collected that


@dataclass
class Move:
    """A licence given to or taken from one account, between two collections."""

    seen: datetime  # the first collection that showed it, or the audit log's time
    before: datetime  # the collection before it, which didn't
    uid: str
    account: str
    sku: str
    change: str  # ASSIGNED | REMOVED
    how: str
    note: str
    blocked: bool
    by: str = ""


@dataclass(frozen=True)
class Audited:
    """A directory audit event that changed an account's licences."""

    when: datetime
    actor: str
    skus: frozenset[str]  # the SKUs its AssignedLicense values name; empty when they name none
    named: bool  # "Change user license", which says what it is without naming the SKU


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _runs(snap: TenantSnapshot, collector: str) -> list[tuple[datetime, int]]:
    """(finished at, run ID) of the collector's readable runs over the last year, up to the snapshot's own: the
    last of each day for the last month and the last of each month before that, oldest first."""
    from .. import state_history  # here: the history imports the worker's schedule

    latest = snap.runs.get(collector)
    end = parse_ts(latest["finished_at"]) if latest else None
    if end is None:
        return []
    rows = snap.conn.execute(
        "SELECT id, finished_at FROM runs WHERE tenant_id = ? AND collector = ? AND status IN ('succeeded', 'partial')"
        f" AND finished_at >= ? AND finished_at <= ? AND id != ? AND {state_history.READABLE}"
        " ORDER BY finished_at, id",
        (snap.tenant["id"], collector, _iso(end - timedelta(days=HISTORY_DAYS)), latest["finished_at"], latest["id"]),
    ).fetchall()
    daily_from = end - timedelta(days=DAILY_DAYS)
    picked: dict[str, tuple[datetime, int]] = {}
    for r in rows:
        when = parse_ts(r["finished_at"])
        if when is not None:
            picked[when.strftime("%Y-%m-%d" if when > daily_from else "%Y-%m")] = (when, r["id"])
    picked[end.strftime("%Y-%m-%d")] = (end, latest["id"])  # the snapshot's own run ends the history
    return sorted(picked.values())


def _objects(snap: TenantSnapshot, collector: str, kind: str, run_id: int) -> list[dict]:
    from .. import state_history

    if run_id == snap.runs[collector]["id"]:
        return snap.objects(collector, kind) or []
    return state_history.objects_of(snap.conn, run_id, kind)


def _text(*values) -> str:
    return next((v for v in values if isinstance(v, str) and v), "")


def _holdings(users: list[dict]) -> dict[str, Held]:
    out = {}
    for u in users:
        uid = u.get("_id")
        if not isinstance(uid, str):
            continue
        ids = u.get("skuIds") if isinstance(u.get("skuIds"), list) else []
        out[uid] = Held(
            _text(u.get("userPrincipalName"), u.get("displayName")) or uid,
            u.get("accountEnabled") is not False,
            frozenset(s for s in ids if isinstance(s, str)),
            assignments(u),
        )
    return out


def _how(held: Held | None, sku: str, groups: dict[str, str]) -> str:
    """'Directly', 'By group: Sales' or 'Directly and by group: Sales'; blank when it isn't known."""
    a = (held.paths or {}).get(sku) if held else None
    if a is None:
        return ""
    return a.path + (f": {', '.join(groups.get(g) or g for g in a.groups)}" if a.groups else "")


def _moves(history: list[tuple[datetime, dict[str, Held]]], groups: dict[str, str], paid) -> list[Move]:
    """Every paid licence assigned or removed between consecutive collections, oldest first."""
    out = []
    for (t0, old), (t1, new) in zip(history, history[1:], strict=False):
        for uid in sorted(old.keys() | new.keys()):
            a, b = old.get(uid), new.get(uid)
            before, after = (a.skus if a else frozenset()), (b.skus if b else frozenset())
            if before == after:
                continue
            who = b or a
            note = "New account" if a is None else ("Account deleted" if b is None else "")
            blocked = b is not None and not b.enabled
            note = note or ("Sign-in blocked" if blocked else "")
            for sku in sorted(after - before):
                if paid(sku):
                    out.append(Move(t1, t0, uid, who.label, sku, ASSIGNED, _how(b, sku, groups), note, blocked))
            for sku in sorted(before - after):
                if paid(sku):
                    out.append(Move(t1, t0, uid, who.label, sku, REMOVED, _how(a, sku, groups), note, blocked))
    return out


# -- Who made each change, from the activity history ------------------------------------------------


def _audits(snap: TenantSnapshot, start: datetime, end: datetime) -> dict[str, list[Audited]] | None:
    """Licence changes in the stored directory audit log by account ID; None when it has never been read."""
    from .. import events

    if events.coverage(snap.conn, snap.tenant["id"], "audits").detailed_from is None:
        return None
    out: dict[str, list[Audited]] = {}
    keep = lambda category, activity: adminactivity._norm(activity) in LICENCE_ACTIVITIES  # noqa: E731
    for e in events.audits_between(snap.conn, snap.tenant["id"], start, end, keep=keep):
        when = parse_ts(e.get("activityDateTime")) if isinstance(e, dict) else None
        if when is None:
            continue
        targets = [t for t in e.get("targetResources") or [] if isinstance(t, dict)]
        values = [
            f"{p.get('oldValue') or ''} {p.get('newValue') or ''}"
            for t in targets
            for p in t.get("modifiedProperties") or []
            if isinstance(p, dict) and p.get("displayName") == "AssignedLicense"
        ]
        named = adminactivity._norm(e.get("activityDisplayName")) == "change user license"
        if not values and not named:
            continue  # an "Update user" that changed something else
        found = Audited(when, adminactivity._actor(e)[0], frozenset(SKU_ID.findall(" ".join(values))), named)
        for t in targets:
            if str(t.get("type") or "").lower() == "user" and isinstance(t.get("id"), str):
                out.setdefault(t["id"], []).append(found)
    return out


def _attribute(moves: list[Move], audits: dict[str, list[Audited]]) -> None:
    """Date each change by the audit event that made it and name who did, where the log has one."""
    for m in moves:
        window = [a for a in audits.get(m.uid, []) if m.before - SLACK < a.when <= m.seen]
        best = [a for a in window if m.sku in a.skus] or [a for a in window if a.named and not a.skus]
        if best:
            chosen = max(best, key=lambda a: a.when)
            m.seen, m.by = chosen.when, chosen.actor


# -- Licences bought and assigned, month by month ---------------------------------------------------


@dataclass
class Count:
    name: str
    bought: int
    assigned: int
    paid: int  # bought, less active trials' licences, which cost nothing yet
    trial: bool = False  # every licence bought is an active trial's
    trial_seats: int = 0  # of those bought, how many are an active trial's

    @property
    def paid_bought(self) -> int:
        """Licences bought, not counting an active trial's (which may never be bought)."""
        return self.bought - self.trial_seats


def _counts(skus: list[dict], subs: list[dict] | None) -> dict[str, Count]:
    trials = trial_seats(subs) if subs else {}
    out = {}
    for s in skus:
        sid = s.get("skuId") if isinstance(s, dict) else None
        if not isinstance(sid, str) or is_free(s) or s.get("appliesTo") == "Company":
            continue
        bought, used = _units(s, "enabled") + _units(s, "warning"), _consumed(s)
        if bought or used:
            paid = max(_units(s, "enabled") - trials.get(sid, 0), 0)
            out[sid] = Count(
                sku_name(s.get("skuPartNumber")),
                bought,
                used,
                paid,
                bool(trials.get(sid)) and not paid,
                _units(s, "enabled") - paid,
            )
    return out


def _signed(n: int) -> str:
    return f"+{n}" if n > 0 else (str(n) if n else "No change")


def _signed_money(n: float, currency: str) -> str:
    if not round(n, 2):
        return "No change"
    return ("+" if n > 0 else "-") + str(Money(abs(n), currency))


@report(
    "licence-changes",
    "Licence changes",
    "Who was given or lost a licence and when, who made the change, and licences bought and assigned "
    "month by month, from Office Sentry's own history.",
    ("users", "tenant_profile"),
    group="tenant",
    order=25,
    keywords="license changes history audit assigned removed added billing reconciliation invoice seats "
    "month over month joiners leavers true-up who gave",
    uses=("billing", "renewal"),
)
def build(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    if not snap.has("users") and not snap.has("tenant_profile"):
        report.unavailable = "Users and licences haven't been collected yet. Refresh Users on the client page."
        return
    conn = snap.conn
    prices = load_prices(conn)
    currency = setting(conn, "currency", "GBP")

    # Licences bought and assigned at each kept Tenant profile collection; the newest name for each SKU.
    profiles = [
        (
            when,
            _objects(snap, "tenant_profile", "subscribed_sku", rid),
            _objects(snap, "tenant_profile", "company_subscription", rid),
        )
        for when, rid in _runs(snap, "tenant_profile")
    ]
    by_sku: dict[str, dict] = {}
    for _, skus, _ in profiles:
        by_sku.update({s["skuId"]: s for s in skus if isinstance(s, dict) and isinstance(s.get("skuId"), str)})

    def paid(sid: str) -> bool:
        return sid not in by_sku or not is_free(by_sku[sid])

    def name(sid: str) -> str:
        return sku_name(by_sku[sid].get("skuPartNumber")) if sid in by_sku else "Unknown licence"

    def price(sid: str) -> float | None:
        return prices.get(by_sku.get(sid, {}).get("skuPartNumber"))

    groups = {
        g["_id"]: _text(g.get("displayName")) or g["_id"]
        for g in snap.objects("groups", "group") or []
        if isinstance(g.get("_id"), str)
    }
    history = [(when, _holdings(_objects(snap, "users", "user", rid))) for when, rid in _runs(snap, "users")]
    moves = _moves(history, groups, paid)
    end = history[-1][0] if history else None
    audits = None
    if moves and end:
        audits = _audits(snap, max(min(m.before for m in moves), end - timedelta(days=AUDIT_DAYS)) - SLACK, end)
    if audits is not None:
        _attribute(moves, audits)
    elif moves:
        report.notes.append(
            "Who made each change shows once Office Sentry has read the tenant's directory audit log: refresh "
            "Admin activity on the client page."
        )
    if not snap.has("users"):
        report.notes.append("Users haven't been collected yet, so who was given or lost a licence isn't shown.")
    elif len(history) < 2:
        report.notice = (
            "Licence changes show once Users has been collected on two different days: Office Sentry compares "
            "each night's collection with the one before."
        )

    changes = Section(
        CHANGES,
        ["Seen", "Account", "Licence", "Change", "How", "Changed by", "Note", "Price / month"],
        intro="Every paid licence given to or taken from an account, newest first. Seen is the time in the "
        "audit log when it records the change, otherwise the first collection that showed it: one a day for "
        "the last month, one a month before that.",
        empty="No licence was assigned or removed."
        if len(history) > 1
        else "Nothing to compare yet: Users has been collected once.",
        compare=False,
    )
    for m in sorted(moves, key=lambda m: (m.seen, m.account.lower(), m.change), reverse=True):
        each = price(m.sku)
        changes.add(
            [
                m.seen.strftime("%Y-%m-%d"),
                m.account,
                name(m.sku),
                m.change,
                m.how,
                m.by,
                m.note,
                Money(each, currency) if each is not None else "Not priced",
            ],
            "low" if m.change == ASSIGNED and m.blocked else None,
        )

    _drop_empty(changes, ("How", "Changed by", "Note"))
    months, points, latest, month_ago = _months(profiles, by_sku, prices, currency, end or now)
    report.sections = [changes, months]
    if len(points) >= 2 and latest is not None:
        report.chart = line_chart(
            points,
            label="Paid licences assigned",
            unit="",
            y_max=None,
            limit=(sum(c.bought for c in latest.values()), "Bought"),
        )
        report.chart_title = "Paid licences assigned over the last year"

    since = (end or now) - timedelta(days=RECENT_DAYS)
    recent = [m for m in moves if m.seen > since]
    compared = len(history) > 1
    report.highlights = [
        Highlight(
            "Assigned in the last 30 days",
            sum(m.change == ASSIGNED for m in recent) if compared else "Not yet",
            "Paid licences given to people",
            rows=Rows(CHANGES, {"Change": ASSIGNED}, age=("Seen", "lt30")),
        ),
        Highlight(
            "Removed in the last 30 days",
            sum(m.change == REMOVED for m in recent) if compared else "Not yet",
            "Paid licences taken away",
            rows=Rows(CHANGES, {"Change": REMOVED}, age=("Seen", "lt30")),
        ),
        *_month_on_month(latest, month_ago, prices, by_sku, currency),
    ]
    report.footnote = (
        "Bought, assigned and cost come from Microsoft's licence counts at each month's last collection; costs use "
        "the monthly prices on the Licence prices page, and active trials cost nothing until they're bought. Free "
        "licences are left out."
    )


def _drop_empty(section: Section, optional: tuple[str, ...]) -> None:
    """Leave out optional columns no row fills (how licences are assigned before Users collected it, who made
    changes before the audit log was read), so the table keeps room for the rest."""
    if not section.rows:
        return
    keep = [
        i
        for i, column in enumerate(section.columns)
        if column not in optional or any(row[i] != "" for row in section.rows)
    ]
    section.columns = [section.columns[i] for i in keep]
    section.rows = [[row[i] for i in keep] for row in section.rows]


def _months(profiles, by_sku, prices, currency, end):
    """The month-by-month table, the chart's points, and the counts now and about a month ago."""
    section = Section(
        MONTHS,
        ["Month", "Licence", "Bought", "Assigned", "Available", "Change in assigned", "Cost / month"],
        intro="Paid licences bought and assigned at the end of each month (this month so far), and the change "
        "in assigned licences from the month before.",
        empty="Licences haven't been collected yet.",
        compare=False,
        group="Month",
    )
    counts = [(when, _counts(skus, subs)) for when, skus, subs in profiles]
    points = [(when.date(), float(sum(c.assigned for c in day.values()))) for when, day in counts]
    by_month: dict[str, tuple[datetime, dict[str, Count]]] = {}
    for when, day in counts:
        by_month[when.strftime("%Y-%m")] = (when, day)
    ordered = [by_month[k] for k in sorted(by_month)]
    previous: dict[str, Count] | None = None
    rows = []
    for when, day in ordered:
        label = f"{when:%B %Y}"
        gone = {sid: Count(c.name, 0, 0, 0) for sid, c in (previous or {}).items() if sid not in day}
        for sid, c in sorted({**day, **gone}.items(), key=lambda item: item[1].name.lower()):
            before = (previous or {}).get(sid)
            change = _signed(c.assigned - before.assigned) if before else ("New" if previous is not None else "")
            each = prices.get(by_sku.get(sid, {}).get("skuPartNumber"))
            if c.trial:
                cost = "Trial"  # nothing to pay until the trial is bought
            else:
                cost = Money(c.paid * each, currency) if each is not None else "Not priced"
            rows.append((when, [label, c.name, c.bought, c.assigned, c.bought - c.assigned, change, cost]))
        previous = day
    for _, row in sorted(rows, key=lambda r: r[0], reverse=True):
        section.add(row, "medium" if row[3] > row[2] else None)  # more assigned than bought: some don't work
    latest = counts[-1][1] if counts else None
    target = end - timedelta(days=RECENT_DAYS)
    older = [c for c in counts if c[0] <= target]
    if older:
        month_ago = older[-1]
    elif counts and (counts[-1][0] - counts[0][0]).days >= MIN_SPAN:
        month_ago = counts[0]
    else:
        month_ago = None
    return section, points, latest, month_ago


def _month_on_month(latest, month_ago, prices, by_sku, currency) -> list[Highlight]:
    if latest is None:
        return [Highlight("Licences bought", "Not collected", "Refresh Tenant profile and licences", rows=Rows(MONTHS))]
    bought = sum(c.paid_bought for c in latest.values())
    on_trial = sum(c.trial_seats for c in latest.values())
    trials = f", plus {on_trial:,} on trial" if on_trial else ""

    def cost(counts: dict[str, Count]) -> tuple[float, bool]:
        total, priced = 0.0, False
        for sid, c in counts.items():
            each = prices.get(by_sku.get(sid, {}).get("skuPartNumber"))
            if each is not None:
                total, priced = total + c.paid * each, True
        return total, priced

    now_cost, priced = cost(latest)
    if month_ago is None:
        return [
            Highlight(
                "Licences bought",
                bought,
                f"Paid licences{trials}; the change shows after a week",
                rows=Rows(MONTHS),
            ),
            Highlight(
                "Monthly cost",
                Money(now_cost, currency) if priced else "Not priced",
                "At your prices" if priced else "Add prices on the Licence prices page",
                rows=Rows(MONTHS),
            ),
        ]
    when, before = month_ago
    then_bought = sum(c.paid_bought for c in before.values())
    then_cost, _ = cost(before)
    since = f"{when.day} {when:%b}"
    return [
        Highlight(
            "Licences bought",
            _signed(bought - then_bought),
            f"{bought} now, {then_bought} on {since}{trials}",
            rows=Rows(MONTHS),
        ),
        Highlight(
            "Monthly cost",
            _signed_money(now_cost - then_cost, currency) if priced else "Not priced",
            f"{Money(now_cost, currency)} now, {Money(then_cost, currency)} on {since}"
            if priced
            else "Add prices on the Licence prices page",
            rows=Rows(MONTHS),
        ),
    ]
