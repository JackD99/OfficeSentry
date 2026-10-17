"""Home pages by role (pages in ``web/home.py``): each opens with one sentence, then the detail.

- **Today**, for technicians: a ledger of what happened across their clients, one line per thing, worst
  first, with what's new above a line and what they'd seen before (and is still open) below it: critical
  and high findings first seen or back, collections that failed or are out of date, access not granted
  yet, anything due within a week (from ``upcoming``) and the critical and high findings a collection no
  longer saw. Every line has the time it happened: the collection that found it, the run that failed, the
  day the client was added. New means since the person last looked (the last day on their first visit).
  Any line can be handed to a colleague who sees that client, or snoozed out of the list for a while: a
  finding line through its findings' own columns (``findings.hand``, ``findings.snooze``), any other line
  through ``item_state`` (migration 0049).
- **Clients**, for account managers: licence waste, renewals in the next 30 days, this month's reports
  waiting for approval and projects to propose, per client.

Both read stored state only (findings, runs, cached rollup rows, snapshots), never Microsoft, and only for the
tenants they're given, which the page narrows to the person's visible tenants and chosen group. A person picks
which one opens as their home page in Account settings (``users.home``); permissions don't change.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import findings, store, tenant_health, upcoming
from .collectors import REGISTRY
from .formatting import parse
from .rules import RULES
from .text import plural, subject_label

TODAY, CLIENTS = "today", "clients"
HOMES = {
    TODAY: (
        "Today",
        "What happened across your clients since you last looked: new critical and high findings, collections "
        "that failed, anything due this week and what was fixed.",
    ),
    CLIENTS: (
        "Accounts",
        "Licence waste, renewals in the next 30 days, reports waiting for approval and projects to propose.",
    ),
}
DEFAULT = TODAY
NEW_SINCE = timedelta(days=1)  # what's new on Today the first time someone opens it
REVISIT = timedelta(hours=2)  # opening Today again within this keeps the line where it was
LOOKBACK = timedelta(days=7)  # critical and high findings this recent stay on Today, below the line once seen
DUE_WITHIN = 7  # days: Coming up items that make Today
RENEWAL_WITHIN = 30  # days: renewals on Clients
RENEWAL_TYPES = (upcoming.SUBSCRIPTION, upcoming.TRIAL)
STALE_AFTER = timedelta(hours=36)  # matches monthly_review.STALE_AFTER and the tenants list
NAMED = 3  # accounts or items named on a line before "and N more"
_ISO = "%Y-%m-%dT%H:%M:%SZ"
_RULE_TITLES = {r.key: r.title for r in RULES}
# A line's level, worst first: a collection that failed sits between critical and high; something that needs
# attention now (a lapsing subscription, a mailbox nearly full, a renewal due today) comes after high.
LEVELS = ("critical", "bad", "high", "now", "warn", "ok")
_LEVEL_RANK = {level: i for i, level in enumerate(LEVELS)}
_TONE = {"critical": "bad", "high": "bad", "bad": "bad", "now": "bad", "warn": "warn", "ok": "ok"}
_NOUNS = {
    upcoming.SUBSCRIPTION: "subscription",
    upcoming.TRIAL: "trial",
    upcoming.APP_KEY_TYPE: "app secret",
    upcoming.APP_CERTIFICATE: "app certificate",
    upcoming.QUOTA: "mailbox",
    upcoming.DMARC: "domain",
    upcoming.RISK: "accepted risk",
}
KINDS = ("finding", "collection", "access", "due", "fixed")  # what a line can be about


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
class Entry:
    """One line of Today's ledger."""

    kind: str  # finding | fixed | collection | access | due
    level: str  # critical | high | bad | now | warn | ok: the colour of the line's key, and its place
    word: str  # the key's word: Critical, High, Failed, Due today, Fixed, ...
    when: datetime  # when it happened (UTC): the collection that found it, the run that failed
    tenant: dict
    text: str  # what happened, in words
    detail: str = ""  # one more line: which accounts or items, which collectors, what happens next
    target: str = ""  # what href points at: findings | fixed | collection | tenant | upcoming | a path from Coming up
    anchor: str = ""  # a fragment on that page (a check's group on Findings)
    count: int = 1  # findings or items behind the line
    key: str = (
        ""  # the item, for handing it to a colleague or snoozing it: finding:<check> | collection | access | due:...
    )
    new: bool = True  # happened since the line, or seen before and still open
    href: str = ""  # where to look or fix it (a portal path, set by the page)
    stamp: str = ""  # the time as the page shows it, in the MSP's timezone (set by the page)
    stamp_full: str = ""
    ids: list[int] = field(default_factory=list)  # the findings behind a finding line
    handed_to: int | None = None  # the colleague holding it (a user id)
    holder: str = ""  # their name
    snoozed_until: str = ""  # out of the list until then (ISO, UTC)
    snooze_stamp: str = ""  # that time as the page shows it (set by the page)

    @property
    def acts(self) -> bool:
        """Whether the line can be handed on or snoozed (a fixed finding can't)."""
        return bool(self.key) and self.kind != "fixed"

    @property
    def tone(self) -> str:
        return _TONE.get(self.level, "warn")

    @property
    def order(self) -> tuple:
        """New before seen, worst first, then latest first."""
        return (0 if self.new else 1, _LEVEL_RANK.get(self.level, len(LEVELS)), -self.when.timestamp())


@dataclass
class Today:
    entries: list[Entry]  # new lines first (worst first), then what was seen before and is still open
    tenants: int  # active clients looked at
    since: datetime  # the line: what happened after this is new
    new_severe: int = 0  # new critical and high findings
    new_critical: int = 0
    failing: int = 0  # clients whose collection failed or can't sign in
    stale: int = 0  # clients whose data is out of date
    waiting: int = 0  # clients that haven't granted access yet
    due: int = 0  # things due within DUE_WITHIN days, or needing attention now
    fixed: int = 0  # critical and high findings no longer seen since the line
    clients: int = 0  # clients with a line
    since_label: str = "yesterday"  # how the sentence names the line
    without: list[tuple[dict, str]] = field(default_factory=list)  # Coming up parts that couldn't be read
    snoozed: list[Entry] = field(default_factory=list)  # lines out of the list for now, not counted
    cleared: list[tuple[int, str]] = field(default_factory=list)  # (tenant, key) of item_state rows whose line went

    @property
    def new(self) -> list[Entry]:
        return [e for e in self.entries if e.new]

    @property
    def seen(self) -> list[Entry]:
        return [e for e in self.entries if not e.new]

    @property
    def fine(self) -> int:
        return self.tenants - self.clients

    def sentence(self) -> str:
        """The page's opening line: what's new, counted, then what was fixed."""
        if not self.tenants:
            return "No active clients to look after yet."
        new = self.new
        parts = []
        if self.new_severe:
            word = (
                "critical"
                if self.new_critical == self.new_severe
                else ("critical or high" if self.new_critical else "high")
            )
            parts.append((self.new_severe, f"new {word} finding"))
        failed = [e for e in new if e.kind == "collection" and e.level == "bad"]
        if failed:
            parts.append((len(failed), "failed collection"))
        retrying = [e for e in new if e.kind == "collection" and e.level == "warn" and e.word == "Retrying"]
        if retrying:
            parts.append((len(retrying), "collection being tried again", "collections being tried again"))
        stale = [e for e in new if e.kind == "collection" and e.word == "Out of date"]
        if stale:
            parts.append((len(stale), "client out of date", "clients out of date"))
        waiting = [e for e in new if e.kind == "access"]
        if waiting:
            parts.append((len(waiting), "client waiting for access", "clients waiting for access"))
        due = sum(e.count for e in new if e.kind == "due")
        if due:
            parts.append((due, "thing due this week", "things due this week"))
        fixed = f" {plural(self.fixed, 'finding')} {'was' if self.fixed == 1 else 'were'} fixed." if self.fixed else ""
        if not parts:
            seen = self.seen
            if seen:
                still = f"{plural(len(seen), 'thing')} you'd seen {'is' if len(seen) == 1 else 'are'} still open."
                return f"Nothing new since {self.since_label}. {still}{fixed}"
            who = "Your client is" if self.tenants == 1 else f"All {self.tenants} clients are"
            return (
                f"Nothing needs you today. {who} collecting, with no new critical or high findings and nothing"
                f" due this week.{fixed}"
            )
        n = sum(p[0] for p in parts)
        lead = f"{plural(n, 'thing')} {'needs' if n == 1 else 'need'} someone today"
        return f"{lead}: {join([plural(*p) for p in parts])}.{fixed}"


def _active(tenants) -> list[dict]:
    return [dict(t) for t in tenants if t["enabled"] and not t["archived_at"]]


def _at(value, fallback: datetime) -> datetime:
    return parse(value) or fallback


def _named(subjects: list[str]) -> str:
    """'a · b · c and 2 more'."""
    text = " · ".join(subjects[:NAMED])
    return text + (f" and {len(subjects) - NAMED} more" if len(subjects) > NAMED else "")


def _finding_entries(conn, tenants: dict[int, dict], since: datetime, now: datetime) -> list[Entry]:
    """Critical and high findings first seen (or back) in the last LOOKBACK, open and not accepted: one line
    per client, check and severity, split at the line so what's new since it isn't mixed with what was seen."""
    if not tenants:
        return []
    marks = ",".join("?" * len(tenants))
    rows = conn.execute(
        f"SELECT id, tenant_id, rule_key, severity, subject, detail, first_seen, reopened_at, handed_to,"
        f" snoozed_until FROM findings"
        f" WHERE tenant_id IN ({marks}) AND resolved_at IS NULL AND severity IN ('critical', 'high')"
        " AND (first_seen >= ? OR IFNULL(reopened_at, '') >= ?)"
        " AND (accepted_at IS NULL OR (accepted_until IS NOT NULL AND accepted_until <= ?))"
        " ORDER BY rule_key, subject COLLATE NOCASE",
        (*tenants, (now - LOOKBACK).strftime(_ISO), (now - LOOKBACK).strftime(_ISO), now.strftime(_ISO)),
    ).fetchall()
    cut = since.strftime(_ISO)
    groups: dict[tuple, list] = {}
    for r in rows:
        at = max(r["first_seen"], r["reopened_at"] or "")
        key = (r["tenant_id"], r["rule_key"], r["severity"], at >= cut, findings.is_snoozed(r, now))
        groups.setdefault(key, []).append((at, r))
    out = []
    for (tenant_id, rule_key, severity, _, snoozed), found in groups.items():
        found.sort(key=lambda f: f[0], reverse=True)
        first = found[0][1]
        subjects = [subject_label(f[1]["subject"]) for f in found]
        if len(found) == 1:
            detail = f"{subjects[0]}: {first['detail']}" if first["detail"] else subjects[0]
        else:
            detail = f"{plural(len(found), 'finding')}: {_named(subjects)}"
        out.append(
            Entry(
                "finding",
                severity,
                severity.capitalize(),
                _at(found[0][0], now),
                tenants[tenant_id],
                _RULE_TITLES.get(rule_key, rule_key),
                detail,
                target="findings",
                anchor=f"#check-{rule_key}",
                count=len(found),
                key=f"finding:{rule_key}",
                ids=[f[1]["id"] for f in found],
                handed_to=_holder([f[1]["handed_to"] for f in found]),
                snoozed_until=max(f[1]["snoozed_until"] for f in found) if snoozed else "",
            )
        )
    return out


def _holder(handed: list[int | None]) -> int | None:
    """Who holds a line of several findings: the one colleague they're all handed to, else the latest's."""
    held = [h for h in handed if h]
    return held[0] if held else None


def _fixed_entries(conn, tenants: dict[int, dict], since: datetime, now: datetime) -> list[Entry]:
    """Critical and high findings a collection no longer saw since the line: one line per client."""
    if not tenants:
        return []
    marks = ",".join("?" * len(tenants))
    rows = conn.execute(
        f"SELECT tenant_id, rule_key, subject, resolved_at FROM findings WHERE tenant_id IN ({marks})"
        " AND resolved_at >= ? AND severity IN ('critical', 'high') ORDER BY resolved_at DESC, rule_key",
        (*tenants, since.strftime(_ISO)),
    ).fetchall()
    by_tenant: dict[int, list] = {}
    for r in rows:
        by_tenant.setdefault(r["tenant_id"], []).append(r)
    out = []
    for tenant_id, fixed in by_tenant.items():
        names = [f"{_RULE_TITLES.get(r['rule_key'], r['rule_key'])} ({subject_label(r['subject'])})" for r in fixed]
        out.append(
            Entry(
                "fixed",
                "ok",
                "Fixed",
                _at(fixed[0]["resolved_at"], now),
                tenants[tenant_id],
                f"{plural(len(fixed), 'finding')} no longer seen",
                _named(names),
                target="fixed",
                count=len(fixed),
            )
        )
    return out


def _collected_at(latest: dict, now: datetime) -> datetime:
    """When the client's latest collection finished: the time of what it found."""
    finished = [
        r["finished_at"] for r in latest.values() if r["status"] in ("succeeded", "partial") and r["finished_at"]
    ]
    return _at(max(finished), now) if finished else now


def _collection(tenant: dict, latest: dict, now: datetime) -> Entry | None:
    """Why this client's data can't be trusted today, or None. The same reading as the tenants list."""
    if not tenant["consented_at"]:
        return Entry(
            "access",
            "warn",
            "Waiting",
            _at(tenant["created_at"], now),
            tenant,
            "Waiting for a tenant admin to grant access",
            "Nothing can be collected until they do.",
            target="tenant",
            key="access",
        )
    if not latest:
        return None  # added and consented, first collection still to come
    failed = [r for r in latest.values() if r["status"] == "failed"]
    if failed:
        when = _at(max(r["finished_at"] or "" for r in failed), now)
        sign_in = [r["error"] for r in failed if (r["error"] or "").startswith("Sign-in failed: ")]
        if sign_in and len(sign_in) == len(failed):
            return Entry(
                "collection",
                "bad",
                "Failed",
                when,
                tenant,
                "Office Sentry can't sign in to this tenant",
                sign_in[0].removeprefix("Sign-in failed: "),
                target="collection",
                key="collection",
            )
        names = ", ".join(REGISTRY[r["collector"]].title for r in failed if r["collector"] in REGISTRY)
        if all(store.retry_pending(r, now) for r in failed):  # Microsoft was busy: tried again by itself
            return Entry(
                "collection",
                "warn",
                "Retrying",
                when,
                tenant,
                f"{plural(len(failed), 'collection')} failed and will be tried again",
                names,
                target="collection",
                key="collection",
            )
        return Entry(
            "collection",
            "bad",
            "Failed",
            when,
            tenant,
            f"{plural(len(failed), 'collection')} failed",
            names,
            target="collection",
            key="collection",
        )
    if any(r["status"] in ("queued", "running") for r in latest.values()):
        return None
    finished = [r["finished_at"] for r in latest.values() if r["finished_at"]]
    cutoff = (now - STALE_AFTER).strftime(_ISO)
    if finished and min(finished) < cutoff:
        return Entry(
            "collection",
            "warn",
            "Out of date",
            _at(min(finished), now) + STALE_AFTER,  # when the oldest data passed the limit
            tenant,
            "Some data is more than 36 hours old",
            "Reports and checks may be out of date.",
            target="collection",
            key="collection",
        )
    return None


def _due_word(item) -> str:
    """The key's word for a Coming up item: Now for what needs attention whatever its date, else when it's due."""
    if item.window == upcoming.NOW:
        return "Now"
    return f"Due {item.when}" if item.when else "Due"


def _due(tenant: dict, items: list, when: datetime) -> list[Entry]:
    """Coming up items for one client: one line each, or one line per type when there are several. They
    carry the time of the collection that found them."""
    by_type: dict[str, list] = {}
    for item in items:
        by_type.setdefault(item.type, []).append(item)
    out = []
    for kind, group in by_type.items():
        level = "now" if any(i.window == upcoming.NOW for i in group) else "warn"
        if len(group) == 1:
            item = group[0]
            # The key says when it's due; a line that needs attention now says its date, if it has one, in words.
            dated = f", {item.when}" if level == "now" and item.when else ""
            out.append(
                Entry(
                    "due",
                    level,
                    _due_word(item),
                    when,
                    tenant,
                    f"{kind}: {item.title}{dated}",
                    item.detail,
                    target=item.href or "upcoming",
                    key=f"due:{kind}:{item.title}",
                )
            )
        else:
            # Each part's items share a link (the report that lists them), so one line links to all of them.
            href = group[0].href if len({i.href for i in group}) == 1 else ""
            out.append(
                Entry(
                    "due",
                    level,
                    "Now" if level == "now" else "This week",
                    when,
                    tenant,
                    f"{kind}: {plural(len(group), _NOUNS.get(kind, 'item'))}",
                    _named([i.title for i in group]),
                    target=href or "upcoming",
                    count=len(group),
                    key=f"due:{kind}",
                )
            )
    return out


def since_for(user, now: datetime) -> tuple[datetime, str, tuple[str, str]]:
    """Where Today's line falls for this person, how the page names it, and what to store for next time
    (``users.today_opened_at`` and ``today_since``). The first visit shows the last day; opening Today again
    within REVISIT keeps the line where it was; coming back later moves it to the last visit. It never
    reaches further back than LOOKBACK."""
    opened, kept = parse(_get(user, "today_opened_at")), parse(_get(user, "today_since"))
    if opened is None:
        since, label = now - NEW_SINCE, "yesterday"
    elif now - opened < REVISIT and kept is not None:
        since, label = kept, "you last looked"
    else:
        since, label = opened, "you last looked"
    since = max(since, now - LOOKBACK)
    return since, label, (now.strftime(_ISO), since.strftime(_ISO))


def _get(row, key: str):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _states(conn, tenant_ids) -> dict[tuple[int, str], dict]:
    """The hand-offs and snoozes on lines that aren't findings, by (tenant, item key)."""
    if not tenant_ids:
        return {}
    marks = ",".join("?" * len(tenant_ids))
    rows = conn.execute(f"SELECT * FROM item_state WHERE tenant_id IN ({marks})", tuple(tenant_ids)).fetchall()
    return {(r["tenant_id"], r["item_key"]): dict(r) for r in rows}


def today(
    conn,
    tenants,
    now: datetime | None = None,
    shows: Callable[[str, int], bool] | None = None,
    since: datetime | None = None,
    since_label: str = "yesterday",
) -> Today:
    """Today's ledger across ``tenants`` (paused and archived ones are left out): what happened since
    ``since`` (the last day without it) above the line, what was seen before and is still open below it.
    ``shows(kind, tenant id)`` says which kinds of line the person may see on each client; the rest aren't
    counted. Snoozed lines are kept apart and not counted either."""
    now = now or datetime.now(UTC)
    shows = shows or (lambda kind, tenant_id: True)
    since = since or now - NEW_SINCE
    active = _active(tenants)
    by_id = {t["id"]: t for t in active}
    entries = [e for e in _finding_entries(conn, by_id, since, now) if shows("finding", e.tenant["id"])]
    entries += [e for e in _fixed_entries(conn, by_id, since, now) if shows("fixed", e.tenant["id"])]
    up = upcoming.collect(conn, active, now)
    soon: dict[int, list] = {}
    for item in up.items:
        if item.tenant and (item.window == upcoming.NOW or (item.days is not None and item.days <= DUE_WITHIN)):
            soon.setdefault(item.tenant["id"], []).append(item)
    for t in active:
        latest = tenant_health.latest_runs(conn, t["id"])
        if (state := _collection(t, latest, now)) is not None and shows(state.kind, t["id"]):
            entries.append(state)
        if soon.get(t["id"]) and shows("due", t["id"]):
            entries += _due(t, soon[t["id"]], _collected_at(latest, now))
    states = _states(conn, list(by_id))
    for e in entries:
        e.new = e.when >= since
        state = states.get((e.tenant["id"], e.key)) if e.kind != "finding" else None
        if state:
            e.handed_to = state["handed_to"]
            if (state["snoozed_until"] or "") > now.strftime(_ISO):
                e.snoozed_until = state["snoozed_until"]
    names = _names(conn, {e.handed_to for e in entries if e.handed_to})
    for e in entries:
        e.holder = names.get(e.handed_to, "")
    # Hand-offs and snoozes whose line has gone (the collection works again, access was granted, the item is
    # no longer due), on clients and kinds of line this person sees whole: the page forgets them.
    present = {(e.tenant["id"], e.key) for e in entries}
    unread = {t["id"] for t, _ in up.without}
    cleared = [
        (tenant_id, key)
        for tenant_id, key in states
        if (tenant_id, key) not in present
        and shows(key.partition(":")[0], tenant_id)
        and not (key.startswith("due") and tenant_id in unread)
    ]
    snoozed = sorted((e for e in entries if e.snoozed_until), key=lambda e: e.order)
    entries = [e for e in entries if not e.snoozed_until]
    entries.sort(key=lambda e: e.order)
    new = [e for e in entries if e.new]
    collections = [e for e in entries if e.kind == "collection"]
    return Today(
        entries,
        len(active),
        since,
        new_severe=sum(e.count for e in new if e.kind == "finding"),
        new_critical=sum(e.count for e in new if e.kind == "finding" and e.level == "critical"),
        failing=sum(1 for e in collections if e.level == "bad"),
        stale=sum(1 for e in collections if e.word == "Out of date"),
        waiting=sum(1 for e in entries if e.kind == "access"),
        due=sum(e.count for e in entries if e.kind == "due"),
        fixed=sum(e.count for e in new if e.kind == "fixed"),
        clients=len({e.tenant["id"] for e in entries if e.kind != "fixed"}),
        since_label=since_label,
        without=up.without,
        snoozed=snoozed,
        cleared=cleared,
    )


def _names(conn, user_ids: set[int]) -> dict[int, str]:
    if not user_ids:
        return {}
    marks = ",".join("?" * len(user_ids))
    rows = conn.execute(f"SELECT id, username FROM users WHERE id IN ({marks})", tuple(user_ids)).fetchall()
    return {r["id"]: r["username"] for r in rows}


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
    queued = {
        r.tenant["id"]: r for r in (queue or (lambda ts: month_end.queue(conn, ts, period, now, cached=True)))(active)
    }
    piped = {
        r.tenant["id"]: r for r in (pipeline or (lambda ts: projects.pipeline(conn, ts, now, cached=True)))(active).rows
    }
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
