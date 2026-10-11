"""PDF downloads for client-facing templates (anything extending ``reports/_layout.html``).

With WeasyPrint installed (the Docker image) the server renders the PDF. Without it (a Windows
PC running the portal straight from Python, say) the same template is served as a print-ready
page that opens the browser's print window, where "Save as PDF" gives the same layout. Either
way the PDF button always works.

A table report's PDF keeps the first ``PDF_MAX_ROWS`` rows of each table and says so: WeasyPrint's time
grows faster than the row count (about 5 s for 1,000 rows, 36 s for 5,000), and a request mustn't run
into the server's timeout. The same cap applies to a rollup's tenants, a tenant's change report (its
changes, new and fixed findings) and the cross-tenant change list. The XLSX and CSV downloads always
have every row.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from flask import Response, render_template, url_for

from ..reports import export
from ..reports.data_reports import DataReport
from ..reports.month_changes import TenantChanges
from ..reports.rollups import Rollup

PDF_MAX_ROWS = 2_000  # per table

STATIC_DIR = Path(__file__).parent / "static"


def pdf_response(template: str, filename: str, **context) -> Response:
    """``filename`` without ``.pdf``. ``context`` is what the template needs besides the PDF flags."""
    if isinstance(context.get("report"), DataReport):
        context["report"] = cap_rows(context["report"])
    elif isinstance(context.get("report"), TenantChanges):
        context["report"] = cap_changes(context["report"])
    if isinstance(context.get("rollup"), Rollup):
        context["rollup"] = cap_rollup(context["rollup"])
    if isinstance(context.get("summaries"), list) and len(context["summaries"]) > PDF_MAX_ROWS:
        context["cut"] = len(context["summaries"])
        context["summaries"] = context["summaries"][:PDF_MAX_ROWS]
    if export.pdf_available():
        html = render_template(
            template,
            pdf=True,
            print_view=False,
            pdf_available=True,
            css_href=(STATIC_DIR / "report.css").as_uri(),
            **context,
        )
        return Response(
            export.to_pdf(html, STATIC_DIR.as_uri() + "/"),
            mimetype="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}.pdf"'},
        )
    # The browser names the saved PDF after the page title, so the title is the file name.
    return Response(
        render_template(
            template,
            pdf=False,
            print_view=True,
            print_name=filename,
            pdf_available=False,
            css_href=url_for("static", filename="report.css"),
            **context,
        )
    )


def cap_rows(report: DataReport, limit: int | None = None) -> DataReport:
    """The report with at most ``limit`` (``PDF_MAX_ROWS``) rows per table, each cut table saying where the
    rest are."""
    limit = limit or PDF_MAX_ROWS
    if all(len(s.rows) <= limit for s in report.sections):
        return report
    sections = []
    for s in report.sections:
        if len(s.rows) <= limit:
            sections.append(s)
            continue
        note = (
            f"This PDF shows the first {limit:,} of {len(s.rows):,} rows. "
            "Download the spreadsheet (XLSX) for all of them."
        )
        sections.append(
            dataclasses.replace(s, rows=s.rows[:limit], flags=s.flags[:limit], intro=f"{s.intro} {note}".strip())
        )
    return dataclasses.replace(report, sections=sections)


def _cut_note(limit: int, total: int, what: str, rest: str) -> str:
    return f"This PDF shows the first {limit:,} of {total:,} {what}. {rest}"


def cap_rollup(rollup: Rollup, limit: int | None = None) -> Rollup:
    """The rollup with at most ``limit`` tenants, saying so in its footnote."""
    limit = limit or PDF_MAX_ROWS
    if len(rollup.rows) <= limit:
        return rollup
    note = _cut_note(limit, len(rollup.rows), "clients", "Download the spreadsheet (XLSX) for all of them.")
    return dataclasses.replace(rollup, rows=rollup.rows[:limit], footnote=f"{rollup.footnote} {note}".strip())


def cap_changes(tc: TenantChanges, limit: int | None = None) -> TenantChanges:
    """A tenant's change report with at most ``limit`` detail changes, new findings and fixed findings each,
    each cut list named in the report's notes."""
    limit = limit or PDF_MAX_ROWS
    notes = list(tc.notes)
    more = "Download the spreadsheet (XLSX) for all of them."
    new, fixed = tc.new_findings, tc.fixed_findings
    if len(new) > limit:
        notes.append(_cut_note(limit, len(new), "new findings", more))
        new = new[:limit]
    if len(fixed) > limit:
        notes.append(_cut_note(limit, len(fixed), "fixed findings", more))
        fixed = fixed[:limit]
    reports = tc.reports
    if tc.report_changes > limit:
        notes.append(_cut_note(limit, tc.report_changes, "changes in detail reports", more))
        reports, room = [], limit
        for r in tc.reports:
            sections = []
            for s in r.sections:
                kept = s.changes[: max(room, 0)]
                room -= len(kept)
                sections.append(dataclasses.replace(s, changes=kept))
            reports.append(dataclasses.replace(r, sections=sections))
    if notes == tc.notes:
        return tc
    return dataclasses.replace(tc, new_findings=new, fixed_findings=fixed, reports=reports, notes=notes)
