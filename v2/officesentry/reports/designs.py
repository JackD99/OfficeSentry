"""Report designs: client reports an account manager assembles from blocks, with no code.

A design is a document (``settings``: the cover title and subtitle) and an ordered list of blocks. Each
block is one ``Widget`` from ``WIDGETS`` (a verdict, key figures, the failing checks, a table from a
detail report, a paragraph of text, ...) with its own settings, at full or half width. The editor in
``web/designs.py`` edits that JSON; ``render`` turns it into data for ``templates/reports/design.html``,
which draws the page and the PDF from the same blocks, so the two always match.

Everything a block shows comes from what the portal already builds for a tenant: the monthly review
(``monthly_review.build``), its plain-English summary, the insurance evidence pack, the detail reports and
this month's month-end commentary. A block that can't be built (a bug, data in a shape nobody expected)
renders as "not available" and never takes the report down with it: the robustness contract applies here
as it does to checks and reports.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from ..db import utcnow
from . import data_reports, evidence, monthly_review, summary

log = logging.getLogger(__name__)

MAX_BLOCKS = 60
TEXT_MAX = 200
LONG_TEXT_MAX = 4000
WIDTHS = ("full", "half")
SEVERITIES = (("critical", "Critical"), ("high", "High"), ("medium", "Medium"), ("low", "Low"))
KPI_OPTIONS = (
    ("secure_score.pct", "Secure Score"),
    ("mfa.registered_pct", "Users with MFA"),
    ("admins.privileged_principals", "Privileged accounts"),
    ("users.licensed", "Licensed users"),
    ("stale", "Inactive accounts"),
    ("waste", "Licence waste / month"),
)
PLACEHOLDERS = "{client}, {month}, {msp} and {date}"


# -- What a widget is ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    """One setting of a widget, drawn as a form control in the editor's side panel."""

    key: str
    label: str
    type: str  # text | textarea | number | select | multiselect | checkbox
    default: object = None
    options: tuple | Callable[[], tuple] = ()  # (value, label) pairs for select and multiselect
    min: int = 0
    max: int = 100
    help: str = ""

    def choices(self) -> tuple:
        return tuple(self.options() if callable(self.options) else self.options)


@dataclass(frozen=True)
class Widget:
    key: str
    title: str
    description: str
    group: str
    fields: tuple[Field, ...]
    build: Callable  # (Context, settings) -> dict for the template; {"empty": "why"} when there's nothing
    halfable: bool = True  # may sit beside another block

    def defaults(self) -> dict:
        return {f.key: (list(f.default) if isinstance(f.default, (list, tuple)) else f.default) for f in self.fields}


@dataclass
class Block:
    kind: str
    settings: dict
    width: str = "full"
    id: str = ""


@dataclass
class Rendered:
    """One block ready for the template: its widget, settings, data (or why there's none)."""

    block: Block
    widget: Widget
    data: dict | None
    empty: str = ""  # nothing to show for this tenant (left out of the report, named in the preview)
    error: bool = False  # the widget broke: the report says so instead of crashing

    @property
    def heading(self) -> str:
        return str(self.block.settings.get("heading") or "")

    @property
    def anchor(self) -> str:
        return f"b{self.block.id}" if self.block.id else ""


# -- The data behind blocks ---------------------------------------------------------------------------


class Context:
    """Everything a tenant's blocks can draw on, built once each on first use and shared by every block."""

    def __init__(self, conn, tenant, now: datetime | None = None, brand=None, month_end: dict | None = None):
        self.conn = conn
        self.tenant = tenant
        self.now = now or datetime.now(UTC)
        self.brand = brand
        self.month_end = month_end or {}  # month_end.report_context(...)["month_end"], client-safe
        self._review = None
        self._summary = None
        self._evidence = None
        self._reports: dict[str, data_reports.DataReport] = {}

    @property
    def review(self) -> monthly_review.Report:
        if self._review is None:
            self._review = monthly_review.build(self.conn, self.tenant, self.now)
        return self._review

    @property
    def summary(self) -> summary.ExecSummary:
        if self._summary is None:
            self._summary = summary.monthly(self.review)
        return self._summary

    @property
    def evidence(self) -> evidence.EvidencePack:
        if self._evidence is None:
            self._evidence = evidence.build(self.conn, self.tenant, self.now)
        return self._evidence

    def report(self, key: str) -> data_reports.DataReport:
        if key not in self._reports:
            self._reports[key] = data_reports.build(self.conn, self.tenant, key, self.now)
        return self._reports[key]

    @property
    def since(self) -> str:
        start = self.review.period_start
        return f"{start.day} {start:%B}" if start else "last month"

    def fill(self, text: str) -> str:
        """Placeholders in a text block: the client's name, the month, the MSP and today's date."""
        values = {
            "client": self.tenant["name"],
            "month": self.now.strftime("%B %Y"),
            "msp": self.brand.display_name if self.brand else "",
            "date": f"{self.now.day} {self.now:%B %Y}",
        }
        return re.sub(r"\{(client|month|msp|date)\}", lambda m: values[m.group(1)], text)


# -- The widgets --------------------------------------------------------------------------------------


def _heading(default: str) -> Field:
    return Field("heading", "Heading", "text", default, help="Leave empty for no heading.")


def _severities(ctx: Context, settings: dict) -> list:
    wanted = set(settings.get("severities") or [])
    return [r for r in ctx.review.failing if r.severity in wanted]


def _report_options() -> tuple:
    return tuple((key, title) for key, (title, _d, _c, _b) in data_reports.REPORTS.items())


def b_verdict(ctx: Context, s: dict) -> dict:
    return {"verdict": ctx.summary.verdict}


def b_summary_text(ctx: Context, s: dict) -> dict:
    return {"paragraphs": ctx.summary.paragraphs}


def b_commentary(ctx: Context, s: dict) -> dict:
    text = (ctx.month_end.get("commentary") or "").strip()
    if not text:
        return {"empty": "No commentary has been written for this month yet (Month-end → Review and annotate)."}
    return {"paragraphs": [p for p in re.split(r"\n\s*\n", text) if p.strip()]}


def b_kpis(ctx: Context, s: dict) -> dict:
    wanted = s.get("keys") or [k for k, _ in KPI_OPTIONS]
    kpis = [k for k in ctx.review.kpis if k.key in wanted]
    if not kpis:
        return {"empty": "No key figures chosen."}
    return {
        "kpis": kpis,
        "columns": min(int(s.get("columns") or 3), len(kpis)),
        "since": ctx.since,
        "currency": ctx.review.currency,
    }


def b_breakdown(ctx: Context, s: dict) -> dict:
    failing = ctx.review.failing
    ran = len(ctx.review.results) - len(ctx.review.unavailable)
    if not ran:
        return {"empty": "No checks have run for this tenant yet."}
    bars = [(label, n, key) for key, label in SEVERITIES if (n := sum(1 for r in failing if r.severity == key))]
    return {
        "title": f"{len(failing)} of {ran} checks need attention" if failing else f"All {ran} checks passed",
        "bars": bars,
    }


def b_secure_score(ctx: Context, s: dict) -> dict:
    chart = ctx.review.secure_score_chart
    if not chart:
        return {"empty": "Not enough Secure Score history yet (two collections on different days)."}
    return {"chart": chart, "intro": bool(s.get("intro", True))}


def b_actions(ctx: Context, s: dict) -> dict:
    limit = int(s.get("max") or 5)
    curated = [a for a in ctx.month_end.get("actions") or [] if a.get("status") != "done"]
    if curated and s.get("source", "auto") != "checks":
        return {"curated": curated[:limit]}
    items = [summary.Action(r.action, r.summary, r.severity or "info") for r in ctx.review.failing][:limit]
    if not items:
        return {"empty": "Nothing to recommend: every check that could run passed."}
    return {"actions": items}


def b_findings(ctx: Context, s: dict) -> dict:
    results = _severities(ctx, s)[: int(s.get("max_checks") or 10)]
    if not results:
        return {"empty": "No failing checks at the chosen severities."}
    return {
        "results": results,
        "rows": int(s.get("max_rows") or 0),
        "style": s.get("style") or "detailed",
        "notes": ctx.month_end.get("notes") or {},
    }


def b_passing(ctx: Context, s: dict) -> dict:
    return {"results": ctx.review.passing} if ctx.review.passing else {"empty": "No check passes yet."}


def b_accepted(ctx: Context, s: dict) -> dict:
    return {"accepted": ctx.review.accepted} if ctx.review.accepted else {"empty": "No accepted risks."}


def b_unavailable(ctx: Context, s: dict) -> dict:
    return {"results": ctx.review.unavailable} if ctx.review.unavailable else {"empty": "Every check could run."}


def b_changes(ctx: Context, s: dict) -> dict:
    r = ctx.review
    limit = int(s.get("max") or 15)
    data = {
        "since": ctx.since,
        "first": r.first_report,
        "resolved": [],
        "new": [],
        "changes": [],
        "more_resolved": 0,
        "more_new": 0,
        "more_changes": 0,
    }
    if s.get("findings", True):
        new = r.new_findings
        data.update(
            resolved=r.resolved[:limit],
            new=new[:limit],
            more_resolved=max(0, len(r.resolved) - limit),
            more_new=max(0, len(new) - limit),
        )
    if s.get("tenant", True):
        data.update(changes=r.changes[: limit * 2], more_changes=max(0, len(r.changes) - limit * 2))
    if not (data["resolved"] or data["new"] or data["changes"]):
        data["nothing"] = True
    return data


def b_licences(ctx: Context, s: dict) -> dict:
    lic = ctx.review.licences
    paid = [ln for ln in lic.lines if not ln.free]
    if not paid:
        return {"empty": "No paid licences in the data yet."}
    return {
        "paid": paid,
        "waste": lic.monthly_waste if s.get("waste", True) else 0,
        "stale_available": lic.stale_available,
        "currency": ctx.review.currency,
    }


def b_data_table(ctx: Context, s: dict) -> dict:
    key = s.get("report") or ""
    if key not in data_reports.REPORTS:
        return {"empty": "Choose a detail report."}
    report = ctx.report(key)
    if report.unavailable:
        return {"empty": report.unavailable}
    limit, tables = int(s.get("max_rows") or 20), int(s.get("max_tables") or 1)
    sections = []
    for section in report.sections[:tables]:
        rows = section.rows[:limit]
        sections.append(
            {
                "section": section,
                "rows": rows,
                "flags": section.flags[:limit] if section.flags else [],
                "more": max(0, len(section.rows) - limit),
            }
        )
    return {
        "report": report,
        "sections": sections,
        "figures": bool(s.get("figures", True)),
        "tiles": (report.tiles or []) + (report.highlights or []),
    }


def b_evidence(ctx: Context, s: dict) -> dict:
    pack = ctx.evidence
    answers = pack.answers[: int(s.get("max") or 40)]
    if not answers:
        return {"empty": "No questionnaire answers yet."}
    return {"answers": answers, "more": max(0, len(pack.answers) - len(answers))}


def b_heading(ctx: Context, s: dict) -> dict:
    return {"intro": ctx.fill(str(s.get("intro") or ""))}


def b_text(ctx: Context, s: dict) -> dict:
    text = ctx.fill(str(s.get("text") or ""))
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    return {"paragraphs": paragraphs} if paragraphs else {"empty": "Write the text in the panel on the right."}


def b_page_break(ctx: Context, s: dict) -> dict:
    return {}


def b_contact(ctx: Context, s: dict) -> dict:
    b = ctx.brand
    return {
        "name": b.display_name if b else "",
        "contact": b.contact if b else "",
        "text": ctx.fill(str(s.get("text") or "")),
    }


def b_about(ctx: Context, s: dict) -> dict:
    fresh = [(f.title, f.finished_at) for f in ctx.review.freshness if not f.stale]
    return {"fresh": fresh, "stale": [f for f in ctx.review.freshness if f.stale]}


WIDGETS: dict[str, Widget] = {
    w.key: w
    for w in (
        Widget(
            "verdict",
            "Status verdict",
            "One line on how the tenant is doing, coloured by its worst finding.",
            "Summary",
            (),
            b_verdict,
        ),
        Widget(
            "summary_text",
            "Plain-English summary",
            "A few sentences on what was checked, what changed and what it costs, written from the data.",
            "Summary",
            (_heading("Summary"),),
            b_summary_text,
            halfable=False,
        ),
        Widget(
            "commentary",
            "This month's commentary",
            "What the technical lead wrote for the month under Month-end. Left out until something is written.",
            "Summary",
            (_heading("From your account team"),),
            b_commentary,
            halfable=False,
        ),
        Widget(
            "kpis",
            "Key figures",
            "Headline numbers with their change since last month.",
            "Summary",
            (
                _heading("Key figures"),
                Field("keys", "Figures", "multiselect", [k for k, _ in KPI_OPTIONS], KPI_OPTIONS),
                Field("columns", "Per row", "number", 3, min=2, max=4),
            ),
            b_kpis,
            halfable=False,
        ),
        Widget(
            "breakdown",
            "Checks by severity",
            "A bar of the failing checks by severity, with the totals.",
            "Summary",
            (_heading(""),),
            b_breakdown,
        ),
        Widget(
            "secure_score",
            "Secure Score trend",
            "Microsoft Secure Score over time, as a chart.",
            "Summary",
            (
                _heading("Microsoft Secure Score"),
                Field("intro", "Explain what Secure Score is", "checkbox", True),
            ),
            b_secure_score,
        ),
        Widget(
            "actions",
            "Recommended next steps",
            "Numbered actions: the account team's curated list for the "
            "month when there is one, else the worst failing checks.",
            "Summary",
            (
                _heading("What we recommend next"),
                Field("max", "At most", "number", 5, min=1, max=10),
                Field(
                    "source",
                    "Take them from",
                    "select",
                    "auto",
                    (("auto", "The month-end list, else the checks"), ("checks", "The failing checks only")),
                ),
            ),
            b_actions,
        ),
        Widget(
            "findings",
            "Failing checks",
            "Each failing check with what to do and the accounts or settings affected.",
            "Checks",
            (
                _heading("What needs attention"),
                Field("severities", "Severities", "multiselect", ["critical", "high"], SEVERITIES),
                Field("max_checks", "At most", "number", 10, min=1, max=50),
                Field(
                    "max_rows",
                    "Affected items per check",
                    "number",
                    5,
                    min=0,
                    max=25,
                    help="0 shows only how many are affected.",
                ),
                Field("style", "Style", "select", "detailed", (("detailed", "Detailed"), ("compact", "Compact list"))),
            ),
            b_findings,
            halfable=False,
        ),
        Widget(
            "passing",
            "Checks that pass",
            "A tick list of everything in good shape.",
            "Checks",
            (_heading("In good shape"),),
            b_passing,
        ),
        Widget(
            "accepted",
            "Accepted risks",
            "Exceptions that were reviewed and agreed, with who accepted them.",
            "Checks",
            (_heading("Accepted risks"),),
            b_accepted,
            halfable=False,
        ),
        Widget(
            "unavailable",
            "Checks not run",
            "Checks that need data or a licence the tenant doesn't have.",
            "Checks",
            (_heading("Not checked"),),
            b_unavailable,
        ),
        Widget(
            "changes",
            "What changed",
            "Findings fixed and new since last month, and changes in the tenant.",
            "Checks",
            (
                _heading("What changed"),
                Field("findings", "Findings fixed and new", "checkbox", True),
                Field("tenant", "Changes in the tenant", "checkbox", True),
                Field("max", "Items per list", "number", 15, min=3, max=50),
            ),
            b_changes,
            halfable=False,
        ),
        Widget(
            "licences",
            "Licences",
            "Paid licences: bought, assigned, unassigned and what is wasted.",
            "Licences and data",
            (
                _heading("Licences"),
                Field("waste", "Call out the monthly waste", "checkbox", True),
            ),
            b_licences,
            halfable=False,
        ),
        Widget(
            "data_table",
            "Table from a detail report",
            "The first rows of any detail report, with its headline figures.",
            "Licences and data",
            (
                _heading(""),
                Field("report", "Report", "select", "", _report_options),
                Field("max_tables", "Tables", "number", 1, min=1, max=5, help="How many of the report's tables."),
                Field("max_rows", "Rows per table", "number", 20, min=3, max=100),
                Field("figures", "Show the headline figures", "checkbox", True),
            ),
            b_data_table,
            halfable=False,
        ),
        Widget(
            "evidence",
            "Insurance questionnaire answers",
            "The questions cyber insurers ask, answered from the data.",
            "Licences and data",
            (
                _heading("Questionnaire answers"),
                Field("max", "At most", "number", 40, min=5, max=60),
            ),
            b_evidence,
            halfable=False,
        ),
        Widget(
            "heading",
            "Heading",
            "A section heading, with an optional line under it.",
            "Text and layout",
            (
                Field("heading", "Heading", "text", "New section"),
                Field("intro", "Line under it", "textarea", "", help=f"Placeholders: {PLACEHOLDERS}."),
            ),
            b_heading,
            halfable=False,
        ),
        Widget(
            "text",
            "Text",
            "Paragraphs of your own words. A blank line starts a new paragraph.",
            "Text and layout",
            (
                _heading(""),
                Field("text", "Text", "textarea", "", help=f"Placeholders: {PLACEHOLDERS}."),
            ),
            b_text,
        ),
        Widget(
            "contact",
            "Contact card",
            "Who to call: the MSP's name and contact line from Branding.",
            "Text and layout",
            (
                _heading("Questions?"),
                Field(
                    "text",
                    "Text",
                    "textarea",
                    "Reply to this report or call us, and we'll take it from there.",
                    help=f"Placeholders: {PLACEHOLDERS}.",
                ),
            ),
            b_contact,
        ),
        Widget(
            "page_break",
            "Page break",
            "Starts a new page in the PDF.",
            "Text and layout",
            (),
            b_page_break,
            halfable=False,
        ),
        Widget(
            "about",
            "About this report",
            "How and when the data was read, in the small print.",
            "Text and layout",
            (),
            b_about,
            halfable=False,
        ),
    )
}
GROUPS = ("Summary", "Checks", "Licences and data", "Text and layout")


def palette() -> list[dict]:
    """The widgets for the editor, by group, with their fields as plain data."""
    out = []
    for group in GROUPS:
        items = []
        for w in WIDGETS.values():
            if w.group != group:
                continue
            fields = [
                {
                    "key": f.key,
                    "label": f.label,
                    "type": f.type,
                    "default": f.default,
                    "min": f.min,
                    "max": f.max,
                    "help": f.help,
                    "options": [list(o) for o in f.choices()],
                }
                for f in w.fields
            ]
            items.append(
                {"key": w.key, "title": w.title, "description": w.description, "halfable": w.halfable, "fields": fields}
            )
        out.append({"group": group, "widgets": items})
    return out


# -- Validation ---------------------------------------------------------------------------------------


def _clean_value(f: Field, value) -> object:
    if f.type == "checkbox":
        return bool(value) if value is not None else bool(f.default)
    if f.type == "number":
        try:
            n = int(value)
        except (TypeError, ValueError):
            n = int(f.default or f.min)
        return max(f.min, min(f.max, n))
    if f.type == "select":
        keys = [str(v) for v, _ in f.choices()]
        return str(value) if str(value) in keys else (f.default if f.default in keys else (keys[0] if keys else ""))
    if f.type == "multiselect":
        keys = [str(v) for v, _ in f.choices()]
        chosen = value if isinstance(value, list) else []
        return [k for k in keys if k in {str(v) for v in chosen}]
    limit = LONG_TEXT_MAX if f.type == "textarea" else TEXT_MAX
    return str(value if value is not None else (f.default or "")).replace("\r\n", "\n").strip()[:limit]


def clean_block(raw: dict, n: int) -> Block | None:
    """A block as the editor sent it, with unknown kinds dropped and every setting coerced to its field."""
    if not isinstance(raw, dict):
        return None
    widget = WIDGETS.get(str(raw.get("kind") or ""))
    if widget is None:
        return None
    given = raw.get("settings") if isinstance(raw.get("settings"), dict) else {}
    settings = {f.key: _clean_value(f, given.get(f.key, f.default)) for f in widget.fields}
    width = str(raw.get("width") or "full")
    if width not in WIDTHS or not widget.halfable:
        width = "full"
    ident = re.sub(r"[^a-z0-9]", "", str(raw.get("id") or "").lower())[:12] or f"k{n}"
    return Block(widget.key, settings, width, ident)


def clean(document: dict) -> dict:
    """The stored shape of a design document: {"settings": {...}, "blocks": [...]}. Never raises on bad input."""
    if not isinstance(document, dict):
        document = {}
    raw_settings = document.get("settings") if isinstance(document.get("settings"), dict) else {}
    settings = {
        "title": str(raw_settings.get("title") or "").strip()[:120],
        "subtitle": str(raw_settings.get("subtitle") or "").strip()[:TEXT_MAX],
        "cover_verdict": bool(raw_settings.get("cover_verdict", True)),
        "contents": bool(raw_settings.get("contents", True)),
    }
    blocks, seen = [], set()
    raw_blocks = document.get("blocks") if isinstance(document.get("blocks"), list) else []
    for n, raw in enumerate(raw_blocks[:MAX_BLOCKS], start=1):
        block = clean_block(raw, n)
        if block is None:
            continue
        while block.id in seen:
            block.id += "x"
        seen.add(block.id)
        blocks.append(block)
    return {
        "settings": settings,
        "blocks": [{"id": b.id, "kind": b.kind, "width": b.width, "settings": b.settings} for b in blocks],
    }


# -- Storage --------------------------------------------------------------------------------------------


def key_of(design_id: int) -> str:
    return f"design:{design_id}"


def id_of(key: str) -> int | None:
    """The design ID in a report key ("design:12"), or None for any other key."""
    m = re.fullmatch(r"design:(\d+)", key or "")
    return int(m.group(1)) if m else None


def get(conn, design_id: int):
    return conn.execute("SELECT * FROM report_designs WHERE id = ?", (design_id,)).fetchone()


def list_designs(conn) -> list:
    return conn.execute("SELECT * FROM report_designs ORDER BY name COLLATE NOCASE, id").fetchall()


def document(row) -> dict:
    """The design row's document, cleaned (so an older stored shape still renders)."""
    try:
        settings = json.loads(row["settings"] or "{}")
        blocks = json.loads(row["blocks"] or "[]")
    except ValueError:
        settings, blocks = {}, []
    return clean({"settings": settings, "blocks": blocks})


def create(conn, name: str, user: str, document: dict | None = None, description: str = "") -> int:
    doc = clean(document or {})
    cur = conn.execute(
        "INSERT INTO report_designs (name, description, settings, blocks, created_by, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (
            name.strip()[:100] or "Untitled report",
            description.strip()[:TEXT_MAX],
            json.dumps(doc["settings"]),
            json.dumps(doc["blocks"]),
            user,
            utcnow(),
        ),
    )
    return cur.lastrowid


def save(conn, design_id: int, document: dict, name: str | None = None, description: str | None = None) -> dict:
    doc = clean(document)
    conn.execute(
        "UPDATE report_designs SET settings = ?, blocks = ?, name = COALESCE(?, name),"
        " description = COALESCE(?, description), updated_at = ? WHERE id = ?",
        (
            json.dumps(doc["settings"]),
            json.dumps(doc["blocks"]),
            name.strip()[:100] or None if name is not None else None,
            description.strip()[:TEXT_MAX] if description is not None else None,
            utcnow(),
            design_id,
        ),
    )
    return doc


def remove(conn, design_id: int) -> None:
    """Remove the design, and its key from every plan and schedule that sent it."""
    key = key_of(design_id)
    for table in ("report_plans", "email_schedules"):
        for row in conn.execute(f"SELECT id, reports FROM {table} WHERE reports LIKE ?", (f'%"{key}"%',)):
            try:
                keys = [k for k in json.loads(row["reports"] or "[]") if k != key]
            except ValueError:
                continue
            conn.execute(f"UPDATE {table} SET reports = ? WHERE id = ?", (json.dumps(keys), row["id"]))
    conn.execute("DELETE FROM report_designs WHERE id = ?", (design_id,))


def usage(conn) -> dict[int, int]:
    """How many report plans and schedules send each design."""
    counts: dict[int, int] = {}
    for table in ("report_plans", "email_schedules"):
        for row in conn.execute(f"SELECT reports FROM {table} WHERE reports LIKE '%\"design:%'"):
            try:
                keys = json.loads(row["reports"] or "[]")
            except ValueError:
                continue
            for k in keys:
                if (did := id_of(k)) is not None:
                    counts[did] = counts.get(did, 0) + 1
    return counts


# -- Starting points -----------------------------------------------------------------------------------


def _b(kind: str, width: str = "full", **settings) -> dict:
    return {"kind": kind, "width": width, "settings": settings}


STARTERS: dict[str, tuple[str, str, dict]] = {
    "blank": ("Blank", "Start from nothing and add blocks.", {"settings": {}, "blocks": []}),
    "executive": (
        "Executive summary",
        "One page for a business owner: verdict, figures, next steps and who to call.",
        {
            "settings": {"title": "Microsoft 365 executive summary", "subtitle": "{month}"},
            "blocks": [
                _b("summary_text"),
                _b("kpis", keys=["secure_score.pct", "mfa.registered_pct", "stale", "waste"], columns=4),
                _b("actions", max=3),
                _b("breakdown", "half", heading="Checks by severity"),
                _b("contact", "half"),
                _b("about"),
            ],
        },
    ),
    "full": (
        "Full security review",
        "Everything the monthly review has, block by block, to rearrange.",
        {
            "settings": {"title": "Security and licensing review", "subtitle": "{month}"},
            "blocks": [
                _b("summary_text"),
                _b("kpis"),
                _b("breakdown", heading=""),
                _b("actions"),
                _b("changes"),
                _b("secure_score"),
                _b("findings", heading="Fix first"),
                _b("findings", heading="Also worth fixing", severities=["medium", "low"], max_rows=0, style="compact"),
                _b("licences"),
                _b("passing"),
                _b("accepted"),
                _b("unavailable"),
                _b("about"),
            ],
        },
    ),
    "licensing": (
        "Licensing review",
        "Licence spend and waste, with the right-sizing suggestions.",
        {
            "settings": {"title": "Licensing review", "subtitle": "{month}"},
            "blocks": [
                _b("kpis", keys=["users.licensed", "stale", "waste"], columns=3),
                _b("licences"),
                _b("data_table", heading="Where licences could be cheaper", report="licence-rightsizing", max_rows=25),
                _b("data_table", heading="Inactive accounts holding licences", report="inactive", max_rows=25),
                _b("contact"),
                _b("about"),
            ],
        },
    ),
}


# -- Rendering ------------------------------------------------------------------------------------------


def render(ctx: Context, doc: dict) -> list[list[Rendered]]:
    """Each block built for the tenant, in rows: two half-width blocks side by side make one row."""
    rows: list[list[Rendered]] = []
    for raw in doc["blocks"]:
        block = Block(raw["kind"], raw["settings"], raw["width"], raw["id"])
        widget = WIDGETS[block.kind]
        try:
            data = widget.build(ctx, block.settings) or {}
            item = Rendered(block, widget, None if "empty" in data else data, empty=str(data.get("empty") or ""))
        except Exception:  # a widget's own bug never takes the report down (the robustness contract)
            log.exception("Report design block %s failed for tenant %s", block.kind, ctx.tenant["id"])
            item = Rendered(block, widget, None, error=True)
        if block.width == "half" and rows and len(rows[-1]) == 1 and rows[-1][0].block.width == "half":
            rows[-1].append(item)
        else:
            rows.append([item])
    return rows


def contents(rows: list[list[Rendered]]) -> list[tuple[str, str]]:
    """(anchor, heading) for the cover's contents list: every headed block with something to show."""
    return [(r.anchor, r.heading) for row in rows for r in row if r.heading and r.data is not None and r.anchor]


def context(
    conn, tenant, row, now: datetime | None = None, *, brand=None, month_end: dict | None = None, preview: bool = False
) -> dict:
    """Template context for ``reports/design.html``."""
    doc = document(row)
    ctx = Context(conn, tenant, now, brand=brand, month_end=month_end)
    rows = render(ctx, doc)
    settings = doc["settings"]
    return {
        "design": row,
        "doc": doc,
        "rows": rows,
        "contents": contents(rows),
        "tenant": tenant,
        "title": ctx.fill(settings["title"]) or row["name"],
        "subtitle": ctx.fill(settings["subtitle"]),
        "cover_verdict": ctx.summary.verdict if settings["cover_verdict"] else None,
        "show_contents": settings["contents"],
        "generated_at": ctx.now,
        "preview": preview,
        "report_currency": ctx.review.currency,
        "period_since": ctx.since,
        "fresh": [(f.title, f.finished_at) for f in ctx.review.freshness],
    }
