"""Report exports: XLSX (always) and PDF (when WeasyPrint is installed)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from ..text import subject_label
from .monthly_review import Report
from .workbook import Book, Cover


def pdf_available() -> bool:
    try:
        import weasyprint  # noqa: F401
    except (ImportError, OSError):  # OSError: missing pango/cairo system libraries
        return False
    return True


# The only files a PDF may load: the report stylesheet, fonts and icons shipped with the app.
STATIC_DIR = (Path(__file__).resolve().parent.parent / "web" / "static").resolve()


def allowed_url(url: str) -> bool:
    """True for ``data:`` URIs and files inside STATIC_DIR. Everything else (other files, http(s),
    ftp, other hosts' file shares) is refused, so a slip that lets tenant data into PDF HTML can't
    read /keys/secrets.json or reach the network from the server."""
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    if scheme == "data":
        return True
    if scheme != "file":
        return False
    parts = urlsplit(url)
    if parts.netloc not in ("", "localhost"):
        return False
    try:
        path = Path(url2pathname(unquote(parts.path))).resolve()
    except (OSError, ValueError):
        return False
    return path.is_relative_to(STATIC_DIR) and path.is_file()


def url_fetcher():
    """A WeasyPrint URL fetcher that only fetches what ``allowed_url`` allows."""
    from weasyprint import urls

    if hasattr(urls, "URLFetcher"):  # WeasyPrint 65 and later

        class StaticOnlyFetcher(urls.URLFetcher):
            def fetch(self, url, headers=None):
                if not allowed_url(url):
                    raise ValueError(f"Office Sentry doesn't load {url[:100]} into PDFs")
                return super().fetch(url, headers)

        return StaticOnlyFetcher(allowed_protocols=("data", "file"), allow_redirects=False)

    def fetch(url, *args, **kwargs):  # older WeasyPrint: a plain function
        if not allowed_url(url):
            raise ValueError(f"Office Sentry doesn't load {url[:100]} into PDFs")
        return urls.default_url_fetcher(url, *args, **kwargs)

    return fetch


# Tagged PDF/UA: headings, tables and reading order are marked up for screen readers, and the
# document's language and title are set, which insurers and public-sector clients increasingly ask for.
PDF_VARIANT = "pdf/ua-1"


def to_pdf(html: str, base_url: str) -> bytes:
    """Every PDF the app makes goes through here (web downloads and emailed reports)."""
    import weasyprint

    return weasyprint.HTML(string=html, base_url=base_url, url_fetcher=url_fetcher()).write_pdf(pdf_variant=PDF_VARIANT)


RESULTS = {"pass": "Passed", "fail": "Needs attention", "unavailable": "Not checked", "accepted": "Accepted risk"}


def _kpi_value(k):
    if k is None or k.value is None:
        return None
    return f"{k.value:g}%" if k.unit == "%" else k.value


def to_xlsx(report: Report, brand=None) -> bytes:
    book = Book(brand.accent if brand else "")
    book.table(
        "Checks",
        ["Category", "Check", "Result", "Severity", "What was found", "Recommendation"],
        [
            (
                r.category,
                r.title,
                RESULTS.get(r.status, r.status),
                (r.severity or "").capitalize() or None,
                r.summary,
                r.recommendation,
            )
            for r in report.results
        ],
        description="Every check, passed or not",
    )
    book.table(
        "Findings",
        ["Severity", "Check", "Account or item", "Detail", "New this period"],
        [
            (f.severity.capitalize(), r.summary, subject_label(f.subject), f.detail, "Yes" if f.new else None)
            for r in report.failing
            for f in r.findings
        ],
        description="One row per account or setting to fix",
    )
    book.table(
        "Licences",
        [
            "Licence",
            "Part number",
            "Purchased",
            "Assigned",
            "Unassigned",
            "On disabled users",
            "On inactive users",
            f"Monthly price ({report.currency})",
            f"Monthly waste ({report.currency})",
            "Free or trial",
        ],
        [
            (
                ln.name,
                ln.part_number,
                ln.purchased,
                ln.assigned,
                ln.unassigned,
                ln.on_disabled_users,
                ln.on_stale_users,
                ln.monthly_price,
                ln.monthly_waste,
                "Yes" if ln.free else None,
            )
            for ln in report.licences.lines
        ],
        description="Each subscription and what's unused",
    )
    book.table(
        "Data freshness",
        ["Data", "Last collected (UTC)", "Status"],
        [
            (
                f.title,
                f.finished_at,
                "Out of date"
                if f.stale and f.finished_at
                else ("Not collected" if not f.finished_at else "Up to date"),
            )
            for f in report.freshness
        ],
        description="When each part of the data was read from Microsoft 365",
    )

    since = f"{report.period_start.day} {report.period_start:%b %Y}" if report.period_start else "4 weeks ago"
    facts = []
    for k in report.kpis:
        before = (
            "" if k.previous is None else (f"{k.previous:,.2f}" if k.unit == "currency" else f"{k.previous:g}{k.unit}")
        )
        note = k.note or (f"{before} on {since}" if before else "")
        facts.append((k.label + (f" ({report.currency})" if k.unit == "currency" else ""), _kpi_value(k), note))
    by_sev = [f"{n} {s}" for s, n in report.counts.items() if n]
    facts += [
        ("Checks needing attention", len(report.failing), ", ".join(by_sev) + " findings" if by_sev else ""),
        ("Checks passed", len(report.passing), ""),
        ("Not checked", len(report.unavailable), ""),
    ]
    cover = Cover(
        report.title,
        report.tenant["name"],
        report.generated_at,
        brand.display_name if brand else "",
        report.tenant["tenant_ref"],
        brand.accent if brand else "",
        f"Security checks, key figures and licences for {report.tenant['name']}.",
        facts,
    )
    return book.save(cover)
