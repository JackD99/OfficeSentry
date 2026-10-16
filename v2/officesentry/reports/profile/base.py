"""What every part of a person's page is built from: ``Person`` (the account and the ways other
data names it), ``Part``, ``Profile`` and the ``@part(order)`` registry.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ... import adminactivity, devices
from ...snapshot import TenantSnapshot
from ..data_reports import DataReport, Highlight, Section, link_pages, link_people, page_index, people_index
from ..identity_reports import _roles
from ..monthly_review import setting

SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
INACTIVE_DAYS = 90
HISTORY_LIMIT = 60
SIGNIN_HISTORY_DAYS = 365  # months shown under Sign-in history


@dataclass
class Attention:
    severity: str  # critical | high | medium | low
    text: str
    part: str = ""  # key of the part with the detail, for the page's anchor link


@dataclass
class Part:
    key: str
    title: str
    summary: str = ""  # the answer in a few words, shown next to the title
    tone: str | None = None  # ok | warn | bad, for the summary
    facts: list[tuple[str, object]] = field(default_factory=list)
    tables: list[Section] = field(default_factory=list)
    empty: str = ""  # shown when there are no facts or table rows
    unavailable: str = ""  # why this couldn't be checked; replaces everything else
    note: str = ""  # what's partial or left out, in small print
    chart: str = ""  # a server-side SVG drawn above the tables (``reports/charts.py``)


@dataclass
class Event:
    day: str  # YYYY-MM-DD
    area: str
    text: str
    tone: str | None = None  # bad | warn | ok


@dataclass
class Profile:
    tenant: dict
    user_id: str
    user: dict
    generated_at: datetime
    name: str
    upn: str
    badges: list[tuple[str, str | None]] = field(default_factory=list)
    tiles: list[Highlight] = field(default_factory=list)
    attention: list[Attention] = field(default_factory=list)
    account: list[tuple[str, object]] = field(default_factory=list)
    parts: list[Part] = field(default_factory=list)
    history: list[Event] = field(default_factory=list)
    history_since: str | None = None
    findings: list[dict] = field(default_factory=list)
    sources: list[tuple[str, str | None]] = field(default_factory=list)

    def as_report(self) -> DataReport:
        """The same content as a detail report, for the PDF and XLSX downloads."""
        report = DataReport(
            "user",
            self.name,
            f"Everything collected about {self.upn or self.name}.",
            self.tenant,
            self.generated_at,
            sources=self.sources,
            highlights=self.tiles,
        )
        todo = Section("Needs attention", ["What", "Area"], empty="Nothing needs attention.")
        titles = {p.key: p.title for p in self.parts}
        for a in self.attention:
            todo.add([a.text, titles.get(a.part, "Account")], a.severity)
        account = Section("Account", ["Detail", "Value"])
        for label, value in self.account:
            account.add([label, value])
        sections = [todo, account]
        for p in self.parts:
            if p.unavailable:
                sections.append(Section(p.title, ["Note"], rows=[[p.unavailable]], flags=[None]))
                continue
            if p.facts:
                facts = Section(p.title, ["Detail", "Value"])
                for label, value in p.facts:
                    facts.add([label, value])
                sections.append(facts)
            for t in p.tables:
                sections.append(
                    Section(t.title, t.columns, list(t.rows), list(t.flags), t.intro, t.empty, compare=False)
                )
            if not p.facts and not p.tables:
                sections.append(Section(p.title, ["Note"], rows=[[p.empty or p.summary]], flags=[None]))
        if self.findings:
            found = Section("Open findings", ["Check", "Detail", "First seen"])
            for f in self.findings:
                found.add([f["title"], f["detail"], (f["first_seen"] or "")[:10]], f["severity"])
            sections.append(found)
        history = Section("History", ["Seen", "Area", "Change"], compare=False, empty="No changes recorded yet.")
        for e in self.history:
            history.add([e.day, e.area, e.text])
        sections.append(history)
        report.sections = sections
        return report


class Person:
    """What every part needs: the snapshot, the account and the ways other data names it."""

    def __init__(self, snap: TenantSnapshot, user: dict, now: datetime):
        self.snap = snap
        self.user = user
        self.id = user["_id"]
        self.now = now
        self.upn = user.get("userPrincipalName") or ""
        self.label = self.upn or user.get("displayName") or self.id
        self.guest = (user.get("userType") or "Member") == "Guest"
        self.enabled = bool(user.get("accountEnabled"))
        # Exchange, SharePoint and Intune name people by sign-in name or address.
        self.keys = {k.lower() for k in (self.upn, user.get("mail")) if isinstance(k, str) and k}
        self.attention: list[Attention] = []
        self.roles = (_roles(snap) or {}).get(self.id)
        self._index: dict[str, str] | None = None
        self._pages: list = []

    @property
    def admin(self) -> bool:
        return bool(self.roles and (self.roles["active"] or self.roles["eligible"]))

    def flag(self, severity: str, text: str, part: str = "") -> None:
        self.attention.append(Attention(severity, text, part))

    def link(self, sections: list[Section]) -> None:
        if self._index is None:
            self._index = people_index(self.snap)
            self._pages = page_index(self.snap)
        link_people(sections, self.snap.tenant["id"], self._index)
        link_pages(sections, self._pages)

    def named(self, value) -> bool:
        return isinstance(value, str) and value.lower() in self.keys

    @property
    def end(self) -> datetime:
        """Where the activity history is read up to: the snapshot's date, or now."""
        return devices.parse_ts(self.snap.as_of) or self.now

    def admin_history(self) -> tuple[list[dict], str] | None:
        """Admin changes by or to this account from the activity history (newest first) and the period they
        cover in words; None when the tenant's audit log has never been read into it."""
        if not hasattr(self, "_admin_history"):
            found = adminactivity.from_history(
                self.snap.conn, self.snap.tenant["id"], self.end - timedelta(days=adminactivity.HISTORY_DAYS), self.end
            )
            if found is None:
                self._admin_history = None
            else:
                held = self.snap.objects("admin_activity", "admin_event")
                mine = [
                    e
                    for e in adminactivity.with_snapshot(found, held)
                    if self.named(e.get("actor")) or any(self.named(t) for t in e.get("targets") or [])
                ]
                span = (self.end - datetime.fromtimestamp(found[1].detailed_from, UTC)).days
                if held is not None:  # the snapshot's own window, where the history starts later
                    window = (self.snap.objects("admin_activity", "audit_window") or [None])[0]
                    span = max(span, adminactivity.covered_days(window))
                self._admin_history = (
                    mine,
                    "the last 12 months" if span >= adminactivity.HISTORY_DAYS else f"the last {max(span, 1)} days",
                )
        return self._admin_history


Builder = Callable[[Person], Part | None]
PARTS: list[tuple[int, Builder]] = []


def part(order: int) -> Callable[[Builder], Builder]:
    def register(fn: Builder) -> Builder:
        PARTS.append((order, fn))
        PARTS.sort(key=lambda p: p[0])
        return fn

    return register


# -- Small formatting helpers ------------------------------------------------------------------


def _day(value) -> str:
    value = devices.parse_ts(value) if isinstance(value, str) else value
    return f"{value.day} {value:%b %Y}" if value else ""


def _when(value, now: datetime) -> str:
    """'3 Sep 2026 (12 days ago)'."""
    when = devices.parse_ts(value) if isinstance(value, str) else value
    if not when:
        return ""
    days = (now - when).days
    ago = "today" if days <= 0 else ("yesterday" if days == 1 else f"{days} days ago")
    return f"{_day(when)} ({ago})"


def _yes(value) -> str:
    return "Unknown" if value is None else ("Yes" if value else "No")


def _currency(snap: TenantSnapshot) -> str:
    return setting(snap.conn, "currency", "GBP")
