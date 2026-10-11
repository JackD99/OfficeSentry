"""One page per thing Office Sentry knows about that isn't a person: a mailbox, a group or team, a site.

Each kind of page is a module (``mailbox_page.py``, ...) built the way the person page is
(``reports/profile/``): a ``Subject`` carries the snapshot and the object, every area of the page is a
function decorated ``@part(kind, order)`` returning a ``Part`` (``profile.base``), and ``flag()`` raises
items for "Needs attention". ``ObjectPage`` is what the template and the downloads read; ``object_page.html``
draws every kind. Pages read stored snapshots only and return plain data.

A page module also tells ``data_reports`` how its objects are named in reports (``PAGE_INDEXES``), so every
cell that is exactly a mailbox address, a group name or a site becomes a link to the page, as account
names already do.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote, urlencode

from .. import devices
from ..checks import RULES
from ..collectors import REGISTRY
from ..snapshot import TenantSnapshot
from .data_reports import DataReport, Highlight, Section, link_pages, link_people, page_index, people_index
from .profile.base import SEVERITY_RANK, Attention, Event, Part, _day, _when, _yes  # noqa: F401
from .views import slug


@dataclass
class ObjectPage:
    kind: str  # mailbox | group | site
    tenant: dict
    object_id: str
    generated_at: datetime
    name: str
    path: str  # the page's path in the portal; ".pdf" and ".xlsx" are its downloads
    list_title: str  # the page that lists these objects, for the breadcrumb
    list_path: str
    subtitle: str = ""  # the address or URL, under the name
    description: str = ""  # one line for the PDF's cover
    badges: list[tuple[str, str | None]] = field(default_factory=list)
    tiles: list[Highlight] = field(default_factory=list)
    attention: list[Attention] = field(default_factory=list)
    about_title: str = "About"
    about: list[tuple[str, object]] = field(default_factory=list)  # the facts in the side column
    parts: list[Part] = field(default_factory=list)
    history: list[Event] = field(default_factory=list)
    history_since: str | None = None
    findings: list[dict] = field(default_factory=list)
    reports: list[tuple[str, str]] = field(default_factory=list)  # (report title, href): where the facts came from
    external: tuple[str, str] | None = None  # (label, https address) to open the thing itself
    sources: list[tuple[str, str | None]] = field(default_factory=list)
    nothing: str = "Nothing stands out in the data collected."  # under "Nothing needs attention"

    @property
    def initial(self) -> str:
        return (self.name or "?")[0].upper()

    def as_report(self) -> DataReport:
        """The same content as a detail report, for the PDF and XLSX downloads."""
        report = DataReport(
            self.kind,
            self.name,
            self.description or f"Everything collected about {self.subtitle or self.name}.",
            self.tenant,
            self.generated_at,
            sources=self.sources,
            highlights=self.tiles,
        )
        todo = Section("Needs attention", ["What", "Area"], empty="Nothing needs attention.")
        titles = {p.key: p.title for p in self.parts}
        for a in self.attention:
            todo.add([a.text, titles.get(a.part, self.about_title)], a.severity)
        about = Section(self.about_title, ["Detail", "Value"])
        for label, value in self.about:
            about.add([label, value])
        sections = [todo, about]
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


class Subject:
    """What every part of a page needs: the snapshot, the object, and a place for "Needs attention"."""

    kind = ""

    def __init__(self, snap: TenantSnapshot, obj: dict, now: datetime):
        self.snap = snap
        self.obj = obj
        self.id = obj["_id"]
        self.now = now
        self.tenant_id = snap.tenant["id"]
        self.attention: list[Attention] = []
        self._people: dict[str, str] | None = None
        self._pages: dict[str, str] | None = None

    def flag(self, severity: str, text: str, part: str = "") -> None:
        self.attention.append(Attention(severity, text, part))

    def link(self, sections: list[Section]) -> None:
        """Link account names to their pages and other objects to theirs, as reports do."""
        if self._people is None:
            self._people = people_index(self.snap)
            self._pages = page_index(self.snap)
        link_people(sections, self.tenant_id, self._people)
        link_pages(sections, {k: v for k, v in (self._pages or {}).items() if not self.is_me(v)})

    def is_me(self, href: str) -> bool:
        return False

    @property
    def end(self) -> datetime:
        """Where histories are read up to: the snapshot's date, or now."""
        return devices.parse_ts(self.snap.as_of) or self.now

    def report(self, key: str, table: str | None = None, column: str | None = None, value=None) -> str:
        """The detail report's page, filtered to the rows naming this object when a table and value are given."""
        href = f"/tenants/{self.tenant_id}/data/{key}"
        if table and value:
            href += "?" + urlencode({"f": f"{slug(table)}/{slug(column)}:{value}"})
        return href


Builder = Callable[[Subject], Part | None]
PARTS: dict[str, list[tuple[int, Builder]]] = {}


def part(kind: str, order: int) -> Callable[[Builder], Builder]:
    """Register an area of the ``kind`` page. Orders are spaced by 10, so a new area slots in between."""

    def register(fn: Builder) -> Builder:
        parts = PARTS.setdefault(kind, [])
        parts.append((order, fn))
        parts.sort(key=lambda p: p[0])
        return fn

    return register


def build_parts(subject: Subject) -> list[Part]:
    out = []
    for _, builder in PARTS.get(subject.kind, []):
        result = builder(subject)
        if result is not None:
            subject.link(result.tables)
            out.append(result)
    return out


def object_path(tenant_id: int, kind: str, object_id: str) -> str:
    """``/tenants/<id>/<kind>s/<object id>``: the page for one object (``kind`` is the URL's plural)."""
    return f"/tenants/{tenant_id}/{kind}/{quote(str(object_id), safe='/')}"


def findings_for(snap: TenantSnapshot, ids: set[str], names: set[str]) -> list[dict]:
    """Open findings whose object is one of ``ids`` or whose subject is one of ``names`` (lower case)."""
    titles = {r.key: r.title for r in RULES}
    rows = snap.conn.execute(
        "SELECT * FROM findings WHERE tenant_id = ? AND resolved_at IS NULL", (snap.tenant["id"],)
    ).fetchall()
    out = [
        dict(r, title=titles.get(r["rule_key"], r["rule_key"]))
        for r in rows
        if r["object_id"] in ids or (r["subject"] or "").lower() in names
    ]
    out.sort(key=lambda f: (SEVERITY_RANK.get(f["severity"], 4), f["title"]))
    return out


def sources_for(snap: TenantSnapshot, collectors: tuple[str, ...]) -> list[tuple[str, str | None]]:
    return [
        (REGISTRY[c].title, (snap.runs.get(c) or {"finished_at": None})["finished_at"])
        for c in collectors
        if c in REGISTRY
    ]


def sort_attention(items: list[Attention]) -> list[Attention]:
    return sorted(items, key=lambda a: SEVERITY_RANK.get(a.severity, 4))


def attention_tile(items: list[Attention]) -> Highlight:
    worst = items[0].severity if items else None
    return Highlight(
        "Needs attention",
        len(items),
        "Nothing to do" if not items else f"Worst: {worst}",
        "bad" if worst in ("critical", "high") else ("warn" if worst else "ok"),
    )
