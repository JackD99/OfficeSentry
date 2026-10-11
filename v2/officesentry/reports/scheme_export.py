"""Downloads of an evidence pack: plain text and Word to paste from, XLSX and CSV rows.

The PDF is ``templates/reports/scheme_pack.html`` through ``web/printing``.
"""

from __future__ import annotations

from datetime import datetime

from . import questionnaires
from .docx_writer import Document
from .schemes import FAIL, MANUAL, PARTLY, UNKNOWN, VERDICTS, ControlResult, SchemeResult
from .workbook import Book, Cover

COLUMNS = [
    "Ref",
    "Theme",
    "Control",
    "Verdict",
    "What we found",
    "Gaps",
    "Fix",
    "Text to paste",
    "What Microsoft 365 can't show",
    "Decided by",
    "Data date",
]


def _day(value: str | None) -> str:
    if not value:
        return ""
    when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return f"{when.day} {when:%b %Y}"


def decided_by(c: ControlResult) -> str:
    if c.set_by:
        return f"{c.set_by}, {_day(c.answer.override.set_at)}"
    if c.verdict == MANUAL:
        return "Needs your evidence"
    if c.verdict == UNKNOWN:
        return "Not collected"
    return "Microsoft 365 data"


def _figures(c: ControlResult) -> str:
    return "; ".join(f"{label}: {value}" for label, value in c.figures)


def rows(r: SchemeResult) -> list[list[str]]:
    out = []
    for t in r.themes:
        for c in t.controls:
            found = " ".join(c.evidence)
            if c.figures:
                found = (found + " " if found else "") + _figures(c) + "."
            out.append(
                [
                    c.control.ref,
                    t.theme.title,
                    c.control.title,
                    c.verdict,
                    found or c.reason,
                    "\n".join(g.strip() for g in c.gaps),
                    c.fix,
                    c.text,
                    c.control.scope,
                    decided_by(c),
                    _day(c.answer.override.set_at)
                    if c.set_by
                    else ("" if c.verdict == MANUAL else _day(r.oldest_data)),
                ]
            )
    return out


def _cover(r: SchemeResult, brand) -> Cover:
    n = r.counts
    facts = [(v, n[v], "") for v in VERDICTS]
    notes = [
        ("Verdict", r.headline.headline),
        ("Requirements", r.scheme.version),
        (
            "How to read it",
            "Pass and Fail are judged from Microsoft 365 data alone; Your evidence marks what it can't show, "
            "with a text to complete, and Not collected what hasn't been read yet.",
        ),
    ]
    if r.oldest_data:
        notes.append(("Oldest data used", _day(r.oldest_data)))
    return Cover(
        f"{r.scheme.title} evidence",
        r.tenant["name"],
        r.generated_at,
        brand.display_name if brand else "",
        r.tenant["tenant_ref"],
        brand.accent if brand else "",
        r.scheme.intro,
        facts,
        notes,
    )


def to_xlsx(r: SchemeResult, brand=None) -> bytes:
    book = Book(brand.accent if brand else "")
    book.table("Controls", COLUMNS, rows(r), description=f"{r.scheme.title}: every control with its verdict")
    if r.sheet is not None:
        for name, sheet_rows, description in questionnaires.to_xlsx_tables([r.sheet]):
            book.table(name, questionnaires.COLUMNS, sheet_rows, description=description)
    return book.save(_cover(r, brand))


def _lines(r: SchemeResult, brand_name: str) -> list[tuple[str, str]]:
    """The pack as (kind, text) lines shared by the text and Word files: kinds title, h1, h2, h3, p, muted,
    label (a "Label: value" line) and bullet."""
    out: list[tuple[str, str]] = [
        ("title", f"{r.scheme.title}: {r.tenant['name']}"),
        (
            "muted",
            f"Prepared {r.generated_at.day} {r.generated_at:%B %Y}"
            + (f" by {brand_name}" if brand_name else "")
            + f". Microsoft 365 data from {_day(r.oldest_data) if r.oldest_data else 'no collection yet'}.",
        ),
        ("p", f"{r.headline.label}. {r.headline.headline}"),
    ]
    n = r.counts
    out.append(("label", "Controls: " + ", ".join(f"{n[v]} {v}" for v in VERDICTS if n[v])))
    if r.since is not None:
        out.append(("h1", f"Since {r.since.since.day} {r.since.since:%B %Y}"))
        if not r.since.available:
            out.append(("p", r.since.note))
        elif not r.since.changes:
            out.append(("p", "No control's verdict has changed."))
        else:
            for ch in r.since.changes:
                out.append(("bullet", f"{ch.control.ref} {ch.control.title}: {ch.before} then, {ch.after} now."))
    if r.to_fix:
        out.append(("h1", "Fix before you apply"))
        for c in r.to_fix:
            out.append(("h3", f"{c.control.ref} {c.control.title}" + (" (auto-fail)" if c.control.autofail else "")))
            for g in c.gaps:
                out.append(("bullet", g.strip()))
            if c.fix:
                out.append(("label", f"Fix: {c.fix_detail or c.fix}"))
    for t in r.themes:
        out.append(("h1", f"{t.theme.title} ({t.verdict})"))
        for c in t.controls:
            out.append(("h2", f"{c.control.ref} {c.control.title}"))
            out.append(("label", f"Verdict: {c.verdict}" + (f" (decided by {c.set_by})" if c.set_by else "")))
            out.append(("muted", f"Requirement: {c.control.requirement}"))
            for e in c.evidence:
                out.append(("bullet", e))
            for label, value in c.figures:
                out.append(("bullet", f"{label}: {value}"))
            for g in c.gaps:
                out.append(("bullet", g.strip()))
            if c.fix and c.verdict in (FAIL, PARTLY):
                out.append(("label", f"Fix: {c.fix_detail or c.fix}"))
            if c.text and c.text != " ".join(c.evidence):
                out.append(("label", f"Text to paste: {c.text}"))
            out.append(("muted", f"What Microsoft 365 can't show: {c.control.scope}"))
    if r.scheme.audit:
        out.append(("h1", "What the assessor checks"))
        for a in r.scheme.audit:
            out.append(("h2", a.title))
            out.append(("p", a.what))
            out.append(("label", f"What Microsoft 365 shows: {a.m365}"))
            verdicts = ", ".join(
                f"{r.by_key[k].control.ref or r.by_key[k].control.title} {r.by_key[k].verdict}"
                for k in a.controls
                if k in r.by_key
            )
            if verdicts:
                out.append(("label", f"Today: {verdicts}"))
    if r.sheet is not None:
        out.append(("h1", f"Answers for the form: {r.sheet.qset.title}"))
        for part, part_rows in r.sheet.parts:
            out.append(("h2", part))
            for row in part_rows:
                a = row.answer
                out.append(("h3", row.label + (" (auto-fail)" if row.item.autofail else "")))
                out.append(("label", f"Answer: {row.state}"))
                out.append(("p", a.text))
                if a.override:
                    out.append(("muted", f"Answered by {a.override.set_by} on {_day(a.override.set_at)}"))
                if row.based_on and not a.override:
                    out.append(("muted", f"Based on: {row.based_on}"))
                if row.beyond:
                    out.append(("muted", f"Still to check: {row.beyond}"))
    out.append(("h1", "About this evidence"))
    for note in r.scheme.notes:
        out.append(("p", note))
    out.append(("p", f"Requirements: {r.scheme.version}. {r.scheme.source}"))
    out.append(
        (
            "p",
            "Pass and Fail are judged from Microsoft 365 settings and accounts read with read-only access; nothing "
            "was changed. Answers marked [in brackets] still need completing.",
        )
    )
    return out


def to_text(r: SchemeResult, brand_name: str = "") -> str:
    lines = []
    for kind, text in _lines(r, brand_name):
        if kind == "title":
            lines += [text, "=" * len(text), ""]
        elif kind == "h1":
            lines += ["", text.upper(), ""]
        elif kind == "h2":
            lines += ["", text]
        elif kind == "h3":
            lines += ["", text]
        elif kind == "bullet":
            lines.append(f"- {text}")
        else:
            lines.append(text)
    return "\n".join(lines).rstrip() + "\n"


def to_docx(r: SchemeResult, brand_name: str = "") -> bytes:
    doc = Document(f"{r.scheme.title}: {r.tenant['name']}", brand_name)
    for kind, text in _lines(r, brand_name):
        if kind == "title":
            doc.heading(text, 0)
        elif kind in ("h1", "h2", "h3"):
            doc.heading(text, int(kind[1]))
        elif kind == "bullet":
            doc.bullets([text])
        elif kind == "label":
            label, _, value = text.partition(": ")
            doc.paragraph(value, lead=f"{label}: ")
        elif kind == "muted":
            doc.paragraph(text, muted=True)
        else:
            doc.paragraph(text)
    return doc.save()
