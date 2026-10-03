"""PDF downloads for client-facing templates (anything extending ``reports/_layout.html``).

With WeasyPrint installed (the Docker image) the server renders the PDF. Without it (a Windows
PC running the portal straight from Python, say) the same template is served as a print-ready
page that opens the browser's print window, where "Save as PDF" gives the same layout. Either
way the PDF button always works.
"""

from __future__ import annotations

from pathlib import Path

from flask import Response, render_template, url_for

from ..reports import export

STATIC_DIR = Path(__file__).parent / "static"


def pdf_response(template: str, filename: str, **context) -> Response:
    """``filename`` without ``.pdf``. ``context`` is what the template needs besides the PDF flags."""
    if export.pdf_available():
        html = render_template(template, pdf=True, print_view=False, pdf_available=True,
                               css_href=(STATIC_DIR / "report.css").as_uri(), **context)
        return Response(export.to_pdf(html, STATIC_DIR.as_uri() + "/"), mimetype="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{filename}.pdf"'})
    # The browser names the saved PDF after the page title, so the title is the file name.
    return Response(render_template(template, pdf=False, print_view=True, print_name=filename, pdf_available=False,
                                    css_href=url_for("static", filename="report.css"), **context))
