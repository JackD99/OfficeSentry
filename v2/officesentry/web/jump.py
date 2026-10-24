"""The jump box: press "/" (or Ctrl+K, ⌘K on a Mac), type a few letters and land on any client, report,
person, page or setting. ``GET /jump?q=`` answers the box's typeahead (``static/jump.js``,
``templates/_jump.html``) with JSON rows ranked for what was typed, each a link.

What it finds:

- a report for a client ("north mfa": MFA coverage at Northwind Traders), a client's own pages ("north
  users"), a report for the client whose page is open, and a report across every client;
- the MSP's custom reports and the person's own saved views, at a client named beside them or the client whose
  page is open, and saved views across every client;
- a report's PDF or workbook, when a word asks for a download ("north mfa pdf"; "contoso download" is the
  monthly review), for someone who may export it;
- clients, by name or domain;
- the main pages and the settings pages, by name or by what they hold ("prices", "logo", "retention");
- people, groups, mailboxes, forwarding addresses, sites, sign-in addresses, apps, devices and domains, from
  the search index (``search_index.py``) through the same result builders the search page uses
  (``web/search.py``), in every client the person sees.

Everything comes from what this person may open: clients from ``visible_tenants``, each report and page
through ``can()`` and ``can_open()`` on that client, and index hits through the capability map the search
page uses. Nothing secret is indexed, and a client id passed as the page's context is ignored unless it's
one of theirs.

Ranking: what was typed is split into words. A client, report or page matches when every word matches a
word of its name (the whole word 3, the start of a word 2, inside a word 1; a keyword counts for three
quarters of that and a word of the description for half a point), with a point more when the first word
typed starts its name and half a point when the words cover the whole name. A report at a client matches
when the words split between the two, and ranks above either alone; so does a report for the client whose
page is open. Index hits rank by where the typed text falls in the name. Ties go to the kind of thing people
jump to most: a report at a client, then clients, pages and the index.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from flask import Blueprint, g, jsonify, request, url_for

from .. import search_index
from ..reports import library
from ..reports.registry import KEYWORDS
from . import search
from .access import can, can_open, manages_groups, needs, visible_tenants
from .reports import _library_href, custom_reports, entry_capability, hidden_reports, my_views

bp = Blueprint("jump", __name__)

MAX_LENGTH = search.MAX_LENGTH
LIMIT = 10  # rows answered
INDEX_HITS = 6  # at most this many from the search index
INDEX_PER_TENANT = 3
SCAN_BUDGET = 0.25  # seconds spent scanning clients whose index isn't current (the search page scans them all)
PAIR_BONUS = 2  # a report at a client, when the words split between them
CONTEXT_BONUS = 1.5  # a report for the client whose page is open
FIRST_WORD_BONUS = 1  # the first word typed starts the name
WHOLE_NAME_BONUS = 0.5  # the words typed cover the whole name ("users": the Users tab before All users)
KEYWORD_WEIGHT = 0.75  # a keyword match counts for less than a word of the name
WEAK_SCORE = 0.5  # a word of the description

# Tie order between kinds of row (lower first).
ORDER = {"pair": 0, "download": 0, "client": 1, "across": 2, "report": 3, "page": 4, "setting": 5, "index": 6}

# A word that asks for a download, and the files a report has: its endpoint by library kind and format.
FORMATS = {"pdf": "PDF", "xlsx": "Excel workbook"}
DOWNLOAD_WORDS = frozenset({"download", "downloads", "export", "file", *FORMATS})
DOWNLOADS = {
    "data": {"pdf": "reports.data_report_pdf", "xlsx": "reports.data_report_xlsx"},
    "review": {"pdf": "reports.tenant_report_pdf", "xlsx": "reports.tenant_report_xlsx"},
}

_SPLIT = re.compile(r"[^a-z0-9]+")


def words_of(text: str) -> list[str]:
    """The lower-case words of a name: each space-separated piece whole (so "contoso.example" matches a domain
    typed with its dot), then its parts between letters and digits."""
    out: list[str] = []
    for piece in (text or "").lower().split():
        for word in (piece, *_SPLIT.split(piece)):
            if word and word not in out:
                out.append(word)
    return out


def tokens_of(q: str) -> list[str]:
    return [t for t in q.lower().split() if t]


def word_score(token: str, word: str) -> int:
    if word == token:
        return 3
    if word.startswith(token):
        return 2
    if len(token) >= 3 and token in word:
        return 1
    return 0


@dataclass(frozen=True)
class Thing:
    """Something with a name to match: the words of its name, keywords people type for it (a notch lower),
    and weak words (its description) that count for half a point at most."""

    name: tuple[str, ...]
    keywords: tuple[str, ...] = ()
    weak: tuple[str, ...] = ()

    @classmethod
    def of(cls, name: str, keywords: str = "", weak: str = "") -> Thing:
        words = words_of(name)
        extra = [w for w in words_of(keywords) if w not in words]
        return cls(tuple(words), tuple(extra), tuple(w for w in words_of(weak) if w not in words and w not in extra))

    def best(self, token: str) -> tuple[float, int | None]:
        """The best match for one token: its score, and which word of the name it matched (None: a keyword)."""
        score, at = 0.0, None
        for i, w in enumerate(self.name):
            s = word_score(token, w)
            if s > score:
                score, at = s, i
        for w in self.keywords:
            s = word_score(token, w) * KEYWORD_WEIGHT
            if s > score:
                score, at = s, None
        if not score and any(word_score(token, w) >= 2 for w in self.weak):
            score = WEAK_SCORE
        return score, at

    def score(self, tokens: list[str]) -> float | None:
        """Every token must match. Their scores, a point when the first token starts the name, and half a point
        when the tokens between them cover every word of the name."""
        total, covered = 0.0, set()
        for t in tokens:
            s, at = self.best(t)
            if not s:
                return None
            total += s
            if at is not None:
                covered.add(at)
        if self.name and word_score(tokens[0], self.name[0]) >= 2:
            total += FIRST_WORD_BONUS
        if self.name and len(covered) == len(self.name):
            total += WHOLE_NAME_BONUS
        return total

    def split(self, other: Thing, tokens: list[str]) -> float | None:
        """Each token goes to whichever of the two matches it best; both must take at least one."""
        mine: list[str] = []
        theirs: list[str] = []
        for t in tokens:
            a, b = self.best(t)[0], other.best(t)[0]
            if not a and not b:
                return None
            (mine if a >= b else theirs).append(t)
        if not mine or not theirs:
            return None
        return (self.score(mine) or 0) + (other.score(theirs) or 0) + PAIR_BONUS


@dataclass
class Row:
    kind: str  # the icon and the tie order: client, report, across, page, setting, person, mailbox, ...
    title: str
    href: str
    where: str = ""  # the client, after the title: "MFA coverage · Northwind Traders"
    detail: str = ""  # "Report · Identity and access"
    score: float = 0
    order: int = 0

    def json(self) -> dict:
        return {"kind": self.kind, "title": self.title, "where": self.where, "detail": self.detail, "href": self.href}


# -- What there is to jump to -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Page:
    """A page of the portal: its endpoint, name, the words people might type for it, and its area."""

    endpoint: str
    title: str
    keywords: str = ""
    area: str = ""  # "Settings · Team"; empty for a main page
    shown: Callable[[], bool] | None = None  # beyond what the route itself needs

    @property
    def kind(self) -> str:
        return "setting" if self.area else "page"

    def opens(self) -> bool:
        return can_open(self.endpoint) and (self.shown is None or self.shown())


EMAIL = "Reports and email"  # the settings area

PAGES: tuple[Page, ...] = (
    Page("home.today", "Today", "home what needs attention technicians"),
    Page("home.clients", "Accounts", "home account managers waste renewals month-end"),
    Page("tenants.dashboard", "Clients", "all clients list dashboard"),
    Page("reports.overview", "Reports", "library all reports catalogue saved views"),
    Page("across.changes", "What changed", "across all clients month history differences"),
    Page("across.checks", "Checks", "matrix every check every client"),
    Page("upcoming.index", "Coming up", "renewals expiring due dates calendar secrets certificates"),
    Page("email.overview", "Email reports", "schedules digests plans delivery sent"),
    Page("reporting.index", "Month-end", "queue approve review commentary"),
    Page("projects.index", "Projects", "recommended proposals quick fixes"),
    Page("auth.account", "Account", "my account password appearance theme dark light two-step sign-in"),
    # Settings, as base.html's SETTINGS_GROUPS lists them (tests/test_jump.py keeps the two in step).
    Page("tenant_admin.clients", "Client settings", "connection health onboarding paused add new client", "Clients"),
    Page("onboarding.index", "Onboarding", "consent pack tracker import csv approval", "Clients"),
    Page("groups.index", "Client groups", "tier region category", "Clients", manages_groups),
    Page("admin.connections", "App connection", "certificate consent entra app registration permissions", "Clients"),
    Page("audit_addon.settings", "Audit log add-on", "unified audit log feed activity", "Clients"),
    Page("autodesk.settings", "Autodesk", "autocad revit seats licences subscriptions token", "Clients"),
    Page("branding.settings", "Branding", "logo colours msp name report footer support contact favicon", EMAIL),
    Page("reports.prices", "Licence prices", "cost money currency sku", EMAIL),
    Page("email.settings", "Email delivery", "smtp mail send graph sender from", EMAIL),
    Page("alerts.settings", "Alerts", "teams slack webhook notifications", EMAIL),
    Page("admin.users", "Users", "people access roles accounts staff team sign-in", "Team"),
    Page("roles.index", "Roles", "permissions capabilities access", "Team"),
    Page("sso.settings_page", "Microsoft sign-in", "sso entra staff single sign-on", "Team"),
    Page("activity.index", "Activity log", "audit who did what", "Team"),
    Page("system.status", "System status", "health worker database disk backups", "System"),
    Page("retention.settings", "Data retention", "prune keep days history delete", "System"),
    Page("updates.index", "Updates", "upgrade release version new", "System"),
    Page("support.index", "Support", "package diagnostics zip bug help", "System"),
)


@dataclass(frozen=True)
class Tab:
    """A client's own page (a tab, or a page under one), opened for a client named in the query."""

    endpoint: str
    title: str
    keywords: str = ""
    kind: str = "page"


TABS: tuple[Tab, ...] = (
    Tab("tenants.tenant", "Overview", "dashboard summary home open"),
    Tab("reports.data_index", "Reports", "all reports detail data", "report"),
    Tab("people.people", "Users", "people accounts person directory"),
    Tab("email.tenant_emails", "Emails", "emailed reports schedules contacts report history"),
    Tab("tenants.collection", "Collection", "runs collect now refresh data sources settings"),
    Tab("tenant_admin.manage", "Settings", "manage connection consent pause archive"),
    Tab("projects.tenant", "Projects", "quick fixes recommended proposal"),
    Tab("history.activity", "Activity history", "sign-ins audit log kept"),
)


@dataclass(frozen=True)
class Report:
    """A report from the library, with its words and where it opens."""

    entry: library.Entry
    thing: Thing
    kind: str  # the row's icon: report, findings, changes, review, evidence

    def opens(self, tenant_id: int | None, hidden: frozenset[str]) -> bool:
        if self.entry.kind == "data" and self.entry.key in hidden:
            return False
        return can(entry_capability(self.entry), tenant_id)

    def href(self, tenant_id: int | None) -> str:
        return _library_href(self.entry, tenant_id)

    @property
    def detail(self) -> str:
        return "Report · " + self.entry.area if self.entry.kind == "data" else self.entry.area

    def row(self, tenant, score: float, order: int) -> Row:
        """The report at this client, or across every client (None)."""
        if tenant is None:
            detail = "One row per client" if self.entry.kind == "data" else self.detail
            return Row("across", self.entry.title, self.href(None), "All clients", detail, score, order)
        return Row(self.kind, self.entry.title, self.href(tenant["id"]), tenant["name"], self.detail, score, order)

    def download(self, tenant, fmt: str, score: float) -> Row:
        """The report's file for this client, in one of ``FORMATS``."""
        data = self.entry.kind == "data"
        args = {"tenant_id": tenant["id"], "key": self.entry.key} if data else {"tenant_id": tenant["id"]}
        title = f"Download {self.entry.title}" if data else f"Download the {self.entry.title.lower()}"
        href = url_for(DOWNLOADS[self.entry.kind][fmt], **args)
        detail = f"{FORMATS[fmt]} · {self.entry.area}"
        return Row("download", title, href, tenant["name"], detail, score, ORDER["download"])


def reports() -> list[Report]:
    """Every report in the library, with the words people type for it (its title and keywords; its
    description counts for less)."""
    extra = {key: words for key, _title, _desc, _kind, words in library.SUMMARIES}
    out = []
    for area in library.catalogue():
        for e in area.entries:
            keywords = KEYWORDS.get(e.key, "") if e.kind == "data" else extra.get(e.key, "")
            kind = "report" if e.kind == "data" else e.kind
            out.append(Report(e, Thing.of(e.title, keywords, weak=e.description), kind))
    return out


@dataclass
class Found:
    rows: list[Row] = field(default_factory=list)
    partial: bool = False  # some clients weren't scanned (their index isn't current and time ran out)


def _client_row(t, score: float) -> Row:
    state = " · Paused" if not t["enabled"] else ""
    return Row(
        "client",
        t["name"],
        url_for("tenants.tenant", tenant_id=t["id"]),
        detail=f"Client · {t['tenant_ref']}{state}",
        score=score,
        order=ORDER["client"],
    )


def results(conn, tenants: list, q: str, here=None) -> Found:
    """Rows for ``q`` over the clients the person sees (``here`` is the client whose page is open, or None),
    ranked, at most ``LIMIT``."""
    tokens = tokens_of(q)
    found = Found()
    if not tokens:
        return found
    rows = found.rows
    hidden: dict[int, frozenset[str]] = {}

    def hidden_on(tid: int) -> frozenset[str]:
        if tid not in hidden:
            hidden[tid] = hidden_reports(tid)
        return hidden[tid]

    clients = [(t, Thing.of(f"{t['name']} {t['tenant_ref']}")) for t in tenants]
    named = [(t, thing) for t, thing in clients if any(thing.best(tok)[0] for tok in tokens)]
    downloads = _downloads(tokens, named, here, hidden_on)
    if downloads is not None:  # a file was asked for: nothing else
        rows.extend(downloads)
        return _ranked(found)
    for t, thing in named:
        score = thing.score(tokens)
        if score is not None:
            rows.append(_client_row(t, score))

    across = can("clients.compare")
    for report in reports():
        for t, thing in named:  # the report at a client named beside it
            score = thing.split(report.thing, tokens)
            if score is not None and report.opens(t["id"], hidden_on(t["id"])):
                rows.append(report.row(t, score, ORDER["pair"]))
        score = report.thing.score(tokens)
        if score is None:
            continue
        if here is not None and report.opens(here["id"], hidden_on(here["id"])):  # at the client whose page is open
            rows.append(report.row(here, score + CONTEXT_BONUS, ORDER["pair"]))
        if across and report.opens(None, frozenset()):
            rows.append(report.row(None, score, ORDER["across"]))
        elif not across and here is None:  # someone limited to a few clients: the report on each of them
            rows.extend(
                report.row(t, score, ORDER["report"]) for t in tenants[:5] if report.opens(t["id"], hidden_on(t["id"]))
            )
    _own(tokens, named, here, across, rows)
    for tab in TABS:
        thing = Thing.of(tab.title, tab.keywords)
        for t, client_thing in named:
            score = client_thing.split(thing, tokens)
            if score is not None and can_open(tab.endpoint, tenant_id=t["id"]):
                href = url_for(tab.endpoint, tenant_id=t["id"])
                rows.append(Row(tab.kind, tab.title, href, t["name"], "Page", score, ORDER["pair"]))
    for page in PAGES:
        score = Thing.of(page.title, page.keywords).score(tokens)
        if score is not None and page.opens():
            detail = f"Settings · {page.area}" if page.area else "Page"
            rows.append(Row(page.kind, page.title, url_for(page.endpoint), "", detail, score, ORDER[page.kind]))
    if len(q) >= search.MIN_LENGTH:
        _index(conn, tenants, q.lower(), found)
    return _ranked(found)


OWN_CLIENTS = 5  # custom reports and saved views are looked up for at most this many clients named


def _own(tokens: list[str], named: list, here, across: bool, rows: list[Row]) -> None:
    """The MSP's custom reports and the person's saved views (``reports.custom_reports``, ``my_views``, which
    already leave out what they can't open), at a client named beside them or the client whose page is open,
    and saved views across every client for someone who compares clients."""
    clients = {t["id"]: (t, thing) for t, thing in named[:OWN_CLIENTS]}
    if here is not None:
        clients.setdefault(here["id"], (here, None))
    for tid, (t, client_thing) in clients.items():
        things = [
            ("design", c["name"], c["href"], "Custom report", Thing.of(c["name"], "custom report", c["description"]))
            for c in custom_reports(tid)
            if c["href"]
        ]
        things += [_view(v) for v in my_views(tid)]
        for kind, title, href, detail, thing in things:
            scores = [client_thing.split(thing, tokens) if client_thing is not None else None]
            if here is not None and tid == here["id"] and (alone := thing.score(tokens)) is not None:
                scores.append(alone + CONTEXT_BONUS)
            score = max((s for s in scores if s is not None), default=None)
            if score is not None:
                rows.append(Row(kind, title, href, t["name"], detail, score, ORDER["pair"]))
    if across and here is None:
        for kind, title, href, detail, thing in map(_view, my_views(None)):
            score = thing.score(tokens)
            if score is not None:
                rows.append(Row(kind, title, href, "All clients", detail, score, ORDER["across"]))


def _view(v: dict) -> tuple:
    return "view", v["name"], v["href"], f"Saved view · {v['report']}", Thing.of(v["name"], f"saved view {v['report']}")


def _ranked(found: Found) -> Found:
    found.rows.sort(key=lambda r: (-r.score, r.order, r.title.lower(), r.where.lower()))
    del found.rows[LIMIT:]
    return found


def _downloads(tokens: list[str], named: list, here, hidden_on) -> list[Row] | None:
    """When a word typed asks for a file ("north mfa pdf", "contoso xlsx"): the report named, for the client
    named beside it or the client whose page is open, in the formats asked for (both for "download"), for
    someone who may export it. With no report named, the client's monthly review. None when no file was
    asked for."""
    asked = [t for t in tokens if t in DOWNLOAD_WORDS]
    if not asked:
        return None
    rest = [t for t in tokens if t not in DOWNLOAD_WORDS]
    formats = [f for f in FORMATS if f in asked] or list(FORMATS)
    rows: list[Row] = []

    def add(report: Report, t, score: float) -> None:
        if report.opens(t["id"], hidden_on(t["id"])) and can("export", t["id"]):
            rows.extend(report.download(t, fmt, score + len(asked) * 3) for fmt in formats)

    files = [r for r in reports() if r.entry.kind in DOWNLOADS]
    for report in files:
        for t, thing in named:
            score = thing.split(report.thing, rest) if rest else None
            if score is not None:
                add(report, t, score)
        score = report.thing.score(rest) if rest and here is not None else None
        if score is not None:
            add(report, here, score + CONTEXT_BONUS)
    if not rows:  # no report named: the client's monthly review
        review = next((r for r in files if r.entry.kind == "review"), None)
        at = [(t, thing.score(rest)) for t, thing in named] if rest else [(here, CONTEXT_BONUS)] if here else []
        for t, score in at:
            if review is not None and score is not None:
                add(review, t, score)
    return rows


# What a search-index hit of each label is drawn as.
INDEX_KINDS = {
    "Person": "person",
    "Group": "group",
    "Mailbox": "mailbox",
    "Forwarding": "mailbox",
    "Inbox rule": "mailbox",
    "Site": "site",
    "Sign-in address": "signin",
    "Failed sign-ins": "signin",
    "Risky sign-in": "signin",
    "App": "app",
    "App registration": "app",
    "Device": "device",
    "Domain": "domain",
}


def _hit_score(hit: search.Hit, q: str) -> float:
    title = hit.title.lower()
    if title.startswith(q):
        return 3.5
    if any(w.startswith(q) for w in words_of(title)):
        return 2.5
    return 1.5


def _index(conn, tenants: list, q: str, found: Found) -> None:
    """People and the other things in the search index that match ``q`` (the whole query, as the search page
    matches it), through the index where it's current and by scanning for a short while where it isn't."""
    current = search_index.current(conn, tenants)
    matches = search_index.find(conn, sorted(current), q, search.looks_like_ip(q)) if current else {}
    deadline = time.monotonic() + SCAN_BUDGET
    total = 0
    for t in tenants:
        if total >= INDEX_HITS:
            break
        allowed = lambda cap, tid=t["id"]: can(cap, tid)  # noqa: E731
        if t["id"] in current:
            group = search.indexed_tenant(conn, t, q, matches.get(t["id"], []), limit=INDEX_PER_TENANT, allowed=allowed)
        elif time.monotonic() < deadline:
            group = search.search_tenant(conn, t, q, limit=INDEX_PER_TENANT, allowed=allowed)
        else:
            found.partial = True
            continue
        for hit in group.hits:
            found.rows.append(
                Row(
                    INDEX_KINDS.get(hit.label, "person"),
                    hit.title,
                    hit.href,
                    t["name"],
                    ", ".join(w for w in (hit.label, hit.detail) if w),
                    _hit_score(hit, q),
                    ORDER["index"],
                )
            )
            total += 1


@bp.get("/jump")
@needs(kind="staff")
def index():
    """The jump box's rows for ``q``, as JSON. ``tenant`` names the client whose page is open."""
    q = request.args.get("q", "").strip()[:MAX_LENGTH]
    tenants = visible_tenants(g.db, g.user)
    wanted = request.args.get("tenant", type=int)
    here = next((t for t in tenants if t["id"] == wanted), None)  # a client they don't see is no context
    found = results(g.db, tenants, q, here)
    more = url_for("search.index", q=q) if q and len(q) >= search.MIN_LENGTH and can("clients.compare") else None
    return jsonify(q=q, rows=[r.json() for r in found.rows], more=more, partial=found.partial)
