"""PDF downloads for client-facing templates (anything extending ``reports/_layout.html``).

With WeasyPrint installed (the Docker image) the server renders the PDF. Without it (a Windows
PC running the portal straight from Python, say) the same template is served as a print-ready
page that opens the browser's print window, where "Save as PDF" gives the same layout. Either
way the PDF button always works.

A table report's PDF keeps the first ``PDF_MAX_ROWS`` rows of each table and says so: WeasyPrint's time
grows faster than the row count (about 5 s for 1,000 rows, 36 s for 5,000), and a request mustn't run
into the server's timeout. The XLSX and CSV downloads always have every row.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from flask import Response, render_template, url_for

from ..reports import export
from ..reports.data_reports import DataReport

PDF_MAX_ROWS = 2_000  # per table

STATIC_DIR = Path(__file__).parent / "static"


def pdf_response(template: str, filename: str, **context) -> Response:
    """``filename`` without ``.pdf``. ``context`` is what the template needs besides the PDF flags."""
    if isinstance(context.get("report"), DataReport):
        context["report"] = cap_rows(context["report"])
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
