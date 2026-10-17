"""The finding sheet: everything about one finding, and the text to act on it.

Opened from a row of the Findings tab (``web/finding.py``). It is built from the stored finding
(``findings`` table), the client's latest snapshot (the check is run again for this one finding, watching
which data sources it reads, so "Read from" is what the check actually looked at), the person's page when
the finding is about an account, the stored check matrix rows of the other clients the viewer sees
(``rollups.check_matrix``, so "Elsewhere" costs nothing on a precomputed install), and what the team has
done with it: accepted, noted, handed to someone, named in a month-end review. The fix steps are the
check's recommendation, one sentence a step, and the ticket text puts the lot in a form that pastes into
a PSA ticket or an email.

Returns plain data; ``templates/_finding_sheet.html`` draws it. Nothing here writes anywhere, least of all
to a client's tenant.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote

from .. import projects
from ..checks import RULES, SEVERITY_RANK, evaluate
from ..collectors import REGISTRY
from ..formatting import day, when
from ..snapshot import TenantSnapshot
from ..text import plural
from . import rollups
from .data_reports import Many, Section, link_pages, page_index, people_index, user_path
from .profile import page as profile
from .profile.base import Attention

ISO = "%Y-%m-%dT%H:%M:%SZ"

# The person page's parts whose facts are the evidence for a check of this category (a part that only has a
# table gives its first rows as one fact); every other part in SUMMARY_PARTS gives its one-line summary.
# Categories not listed get the summaries alone.
PART_OF_CATEGORY = {
    "Admins": ("roles", "mfa"),
    "Identity": ("mfa",),
    "Guests": ("mfa",),
    "Devices": ("devices",),
    "Licences": ("licences",),
    "Email": ("mailbox",),
    "Apps": ("apps",),
    "Groups": ("groups",),
    "Accounts": ("activity",),
}
SUMMARY_PARTS = ("mfa", "roles", "licences", "devices")  # one line each, in this order
TABLE_ROWS = 4  # how many rows of a table-only part go in its fact
EVIDENCE_LIMIT = 12
ELSEWHERE_LIMIT = 8


@dataclass
class Elsewhere:
    tenant: dict
    count: int
    summary: str

    @property
    def href(self) -> str:
        return f"/tenants/{self.tenant['id']}/findings"


@dataclass
class Sheet:
    finding: dict
    tenant: dict
    key: str  # the check's key
    title: str  # the check's title: its goal
    category: str
    severity: str
    subject: str
    subject_href: str | None = None  # the person's or object's page, when it has one
    subject_kind: str = "Item"  # "Account" when the finding is about a person
    detail: list[str] = field(default_factory=list)  # what was found, one line per thing
    summary: str = ""  # the check's summary for this client today
    status: str = "open"  # open | accepted | lapsed | resolved
    days_open: int = 0
    evidence: list[tuple[str, object]] = field(default_factory=list)
    attention: list[Attention] = field(default_factory=list)  # other things flagged on the account
    sources: list[tuple[str, str | None]] = field(default_factory=list)  # (data source, collected at)
    history: list[tuple[str, str]] = field(default_factory=list)  # (what, when or detail)
    elsewhere: list[Elsewhere] = field(default_factory=list)
    others: int = 0  # how many other clients were looked at for "Elsewhere"
    project: tuple[str, str] | None = None  # (title, href): the project this check's work belongs to
    actions: list[dict] = field(default_factory=list)  # month-end "What we recommend next" items on it
    notes: list[dict] = field(default_factory=list)  # month-end notes on the check or this item
    fix: list[str] = field(default_factory=list)
    generated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    link: str = ""  # the sheet's own address, for the ticket text

    @property
    def collected_at(self) -> str | None:
        return max((at for _, at in self.sources if at), default=None)

    @property
    def ticket(self) -> str:
        """Plain text for a PSA ticket or an email: the client, the problem, the evidence, the fix, the source."""
        lines = [
            f"{self.tenant['name']}: {self.title}",
            f"{self.subject_kind}: {self.subject}",
            f"Severity: {self.severity.capitalize()} ({self._open_since()})",
        ]
        if self.detail:
            lines.append("What was found: " + "; ".join(self.detail))
        if self.summary:
            lines.append(f"Check: {self.summary.rstrip('.')}.")
        if self.evidence:
            lines.append("Evidence:")
            lines += [f"- {label}: {_plain(value)}" for label, value in self.evidence]
        if self.fix:
            lines.append("Fix:")
            lines += [f"{n}. {step}" for n, step in enumerate(self.fix, 1)]
        source = "Office Sentry"
        if self.sources:
            source += ", " + ", ".join(name for name, _ in self.sources)
        if self.collected_at:
            source += f", collected {when(self.collected_at)}"
        lines.append(f"Source: {source}")
        if self.link:
            lines.append(f"Link: {self.link}")
        return "\n".join(lines)

    def _open_since(self) -> str:
        first = day(self.finding["first_seen"])
        if self.status == "resolved":
            return f"first seen {first}, resolved {day(self.finding['resolved_at'])}"
        return f"open since {first}" + (f", {plural(self.days_open, 'day')}" if self.days_open else "")

    def email(self, contact: dict) -> str:
        """A ``mailto:`` link that opens the person's own mail app with the finding written up for the
        client's contact. Office Sentry sends nothing itself."""
        name = (contact["name"] or "").strip()
        greeting = f"Hello {name.split()[0]}," if name else "Hello,"
        body = (
            f"{greeting}\n\nOur checks on your Microsoft 365 found something that needs fixing:\n\n"
            f"{self.ticket}\n\nWe can take care of this for you. Reply to this email and we'll arrange it.\n"
        )
        subject = f"{self.tenant['name']}: {self.title} ({self.severity.capitalize()})"
        return f"mailto:{quote(contact['email'], safe='@')}?subject={quote(subject)}&body={quote(body)}"


def _plain(value) -> str:
    """A fact's value as plain text (a long list whole, a link its text)."""
    if isinstance(value, Many):
        return ", ".join(str(p) for p in value.parts)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    return str(value)


def steps(recommendation: str) -> list[str]:
    """The recommendation as numbered steps: one sentence each, as ``RuleResult.action`` cuts the first."""
    text = " ".join((recommendation or "").split())
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z\"'(])", text) if s.strip()]


class _Watching:
    """A snapshot that notes which data sources a check reads, so the sheet can say where its evidence came
    from. Everything else is the snapshot's own."""

    def __init__(self, snap: TenantSnapshot):
        self._snap = snap
        self.read: list[str] = []

    def objects(self, collector: str, kind: str):
        if collector not in self.read:
            self.read.append(collector)
        return self._snap.objects(collector, kind)

    def has(self, collector: str) -> bool:
        if collector not in self.read:
            self.read.append(collector)
        return self._snap.has(collector)

    def __getattr__(self, name):
        return getattr(self._snap, name)


def _account_evidence(conn, tenant, user_id: str, category: str, now: datetime) -> tuple[list, list, str]:
    """Facts about the account from its page, the other things flagged on it, and its display name."""
    p = profile.build(conn, tenant, user_id, now)
    if p is None:
        return [], [], ""
    wanted = {"Account type", "Sign-in", "Source", "Created", "Password last changed", "Password expires"}
    facts = [(label, value) for label, value in p.account if label in wanted]
    parts = {x.key: x for x in p.parts}
    focus = PART_OF_CATEGORY.get(category, ())
    for key in focus:
        if (part := parts.get(key)) and not part.unavailable:
            facts += _part_facts(part)
    for key in SUMMARY_PARTS:
        part = parts.get(key)
        if part is None or key in focus or part.unavailable or not part.summary:
            continue
        facts.append((part.title, part.summary))
    return facts[:EVIDENCE_LIMIT], p.attention, p.name


def _part_facts(part) -> list[tuple[str, object]]:
    """A part's facts; one that only has tables (Admin roles) gives each table's first rows as one fact,
    "Global Administrator (Active, Whole directory); Exchange Administrator (Eligible (PIM), Whole directory)"."""
    if part.facts:
        return list(part.facts)
    facts = []
    for table in part.tables:
        rows = [r for r in table.rows if r]
        if not rows:
            continue
        items = []
        for row in rows[:TABLE_ROWS]:
            rest = ", ".join(t for t in (_plain(v) for v in row[1:3]) if t)
            items.append(f"{_plain(row[0])} ({rest})" if rest else _plain(row[0]))
        if len(rows) > TABLE_ROWS:
            items.append(f"+{len(rows) - TABLE_ROWS} more")
        facts.append((table.title, "; ".join(items)))
    if not facts and part.summary:
        facts.append((part.title, part.summary))
    return facts


def _in_first_collection(conn, tenant_id: int, first_seen: str | None) -> bool:
    """Was the finding already there when the client was first collected, rather than appearing later? The
    findings of one sync share a timestamp, so the first sync's is the earliest; that holds unless a collection
    was queued (the next night's, or a refresh) between the first run finishing and that sync."""
    if not first_seen:
        return False
    earliest = conn.execute("SELECT MIN(first_seen) FROM findings WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]
    if first_seen != earliest:
        return False
    first_done = conn.execute(
        "SELECT MIN(finished_at) FROM runs WHERE tenant_id = ? AND finished_at IS NOT NULL", (tenant_id,)
    ).fetchone()[0]
    if not first_done:
        return False
    later = conn.execute(
        "SELECT 1 FROM runs WHERE tenant_id = ? AND created_at > ? AND created_at < ? LIMIT 1",
        (tenant_id, first_done, first_seen),
    ).fetchone()
    return later is None


def _object_href(snap: TenantSnapshot, subject: str) -> str | None:
    """The page of the mailbox, group or site the finding names, if it has one."""
    probe = Section("Finding", ["Account or item"], rows=[[subject]], flags=[None])
    try:
        link_pages([probe], page_index(snap))
    except Exception:  # odd stored data mustn't take the sheet down
        return None
    cell = probe.rows[0][0]
    return getattr(cell, "page_href", None) or None


def _elsewhere(conn, others: list, key: str, now: datetime) -> list[Elsewhere]:
    if not others:
        return []
    matrix = rollups.check_matrix(conn, others)
    found = []
    for row in matrix.rows:
        cell = row.cells.get(key)
        if cell and cell.status == "fail":
            found.append(Elsewhere(row.tenant, cell.count, cell.summary))
    found.sort(key=lambda e: (-e.count, e.tenant["name"].lower()))
    return found


def build(
    conn, tenant, finding, others: list = (), staff: bool = True, now: datetime | None = None, link: str = ""
) -> Sheet:
    """The sheet for one stored finding. ``others`` are the other clients the viewer sees (for "Elsewhere");
    ``staff`` False leaves out the team's own notes. ``link`` is the sheet's address for the ticket text."""
    now = now or datetime.now(UTC)
    f = dict(finding)
    rule = next((r for r in RULES if r.key == f["rule_key"]), None)
    severity = f["severity"] if f["severity"] in SEVERITY_RANK else "info"
    sheet = Sheet(
        f,
        dict(tenant),
        f["rule_key"],
        rule.title if rule else f["rule_key"],
        rule.category if rule else "",
        severity,
        f["subject"],
        detail=[d for d in (f["detail"] or "").split("; ") if d],
        generated_at=now,
        link=link,
    )
    ts = now.strftime(ISO)
    first = datetime.strptime(f["first_seen"], ISO).replace(tzinfo=UTC) if f["first_seen"] else now
    sheet.days_open = max((now - first).days, 0)
    accepted = bool(f["accepted_at"]) and (f["accepted_until"] is None or f["accepted_until"] > ts)
    if f["resolved_at"]:
        sheet.status = "resolved"
    elif accepted:
        sheet.status = "accepted"
    elif f["accepted_at"]:
        sheet.status = "lapsed"

    snap = TenantSnapshot(conn, tenant)
    watched = _Watching(snap)
    if rule and snap.runs:
        result = evaluate(watched, now, only={rule.key})
        if result:
            sheet.summary = result[0].summary if result[0].status != "unavailable" else ""
            sheet.fix = steps(result[0].recommendation)
    if not sheet.fix and rule:
        sheet.fix = steps(rule.recommendation)
    sheet.sources = [
        (REGISTRY[c].title, (snap.runs.get(c) or {"finished_at": None})["finished_at"])
        for c in watched.read
        if c in REGISTRY
    ]

    # The account or item: a person's page and its facts, or an object's page.
    people = people_index(snap)
    user_id = f["object_id"] if f["object_id"] in set(people.values()) else people.get(f["subject"].lower())
    if user_id:
        sheet.subject_kind = "Account"
        sheet.subject_href = user_path(tenant["id"], user_id)
        sheet.evidence, sheet.attention, _ = _account_evidence(conn, tenant, user_id, sheet.category, now)
        sheet.attention = [a for a in sheet.attention if a.text not in sheet.detail][:3]
    else:
        sheet.subject_href = _object_href(snap, f["subject"])

    # History: the finding's own dates, then what the team did with it.
    first_note = " (in the first collection)" if _in_first_collection(conn, tenant["id"], f["first_seen"]) else ""
    sheet.history.append(("First seen", day(f["first_seen"]) + first_note))
    if f["reopened_at"]:
        sheet.history.append(("Came back", f"{day(f['reopened_at'])}, after being fixed once"))
    if f["resolved_at"]:
        sheet.history.append(("Resolved", day(f["resolved_at"])))
    elif f["last_seen"]:
        sheet.history.append(("Last seen", when(f["last_seen"])))
    if f["accepted_at"]:
        until = f" until {day(f['accepted_until'])}" if f["accepted_until"] else ""
        text = f"by {f['accepted_by']} on {day(f['accepted_at'])}{until}"
        if sheet.status == "lapsed":
            text += ", now due for review"
        if f["accepted_note"]:
            text += f": {f['accepted_note']}"
        sheet.history.append(("Accepted" if accepted else "Was accepted", text))
    if staff and f.get("handed_to"):  # who holds it is the team's business, never a client's
        holder = conn.execute("SELECT username FROM users WHERE id = ?", (f["handed_to"],)).fetchone()
        if holder:
            by = f" by {f['handed_by']}" if f["handed_by"] and f["handed_by"] != holder["username"] else ""
            sheet.history.append(("Handed to", f"{holder['username']}{by} on {day(f['handed_at'])}"))
    if staff and f.get("snoozed_until") and f["snoozed_until"] > ts:
        sheet.history.append(("Snoozed", f"until {day(f['snoozed_until'])} by {f['snoozed_by']}, out of Today's list"))

    sheet.actions = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM report_actions WHERE tenant_id = ? AND rule_key = ? ORDER BY position, id",
            (tenant["id"], f["rule_key"]),
        )
    ]
    where = "" if staff else " AND visibility = 'client'"
    sheet.notes = [
        dict(r)
        for r in conn.execute(
            f"SELECT * FROM finding_notes WHERE tenant_id = ? AND rule_key = ? AND subject IN ('', ?){where}"
            " ORDER BY id",
            (tenant["id"], f["rule_key"], f["subject"]),
        )
    ]
    for p in projects.CATALOG:
        if f["rule_key"] in p.checks:
            sheet.project = (p.title, f"/tenants/{tenant['id']}/projects#project-{p.key}")
            break

    sheet.others = len(others)
    sheet.elsewhere = _elsewhere(conn, list(others), f["rule_key"], now)
    return sheet
