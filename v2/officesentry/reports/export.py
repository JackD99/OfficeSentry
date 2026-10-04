"""Report exports: XLSX (always) and PDF (when WeasyPrint is installed)."""

from __future__ import annotations

from ..text import subject_label
from .monthly_review import Report
from .workbook import Book, Cover


def pdf_available() -> bool:
    try:
        import weasyprint  # noqa: F401
    except (ImportError, OSError):  # OSError: missing pango/cairo system libraries
        return False
    return True


def to_pdf(html: str, base_url: str) -> bytes:
    import weasyprint

    return weasyprint.HTML(string=html, base_url=base_url).write_pdf()


RESULTS = {"pass": "Passed", "fail": "Needs attention", "unavailable": "Not checked", "accepted": "Accepted risk"}


def _kpi_value(k):
    if k is None or k.value is None:
        return None
    return f"{k.value:g}%" if k.unit == "%" else k.value


def to_xlsx(report: Report, brand=None) -> bytes:
    book = Book(brand.accent if brand else "")
    book.table("Checks", ["Category", "Check", "Result", "Severity", "What was found", "Recommendation"], [
        (r.category, r.title, RESULTS.get(r.status, r.status), (r.severity or "").capitalize() or None, r.summary,
         r.recommendation) for r in report.results], description="Every check, passed or not")
    book.table("Findings", ["Severity", "Check", "Account or item", "Detail", "New this period"], [
        (f.severity.capitalize(), r.summary, subject_label(f.subject), f.detail, "Yes" if f.new else None)
        for r in report.failing for f in r.findings], description="One row per account or setting to fix")
    book.table("Licences", ["Licence", "Part number", "Purchased", "Assigned", "Unassigned", "On disabled users",
                            "On inactive users", f"Monthly price ({report.currency})",
                            f"Monthly waste ({report.currency})", "Free or trial"], [
        (ln.name, ln.part_number, ln.purchased, ln.assigned, ln.unassigned, ln.on_disabled_users,
         ln.on_stale_users, ln.monthly_price, ln.monthly_waste, "Yes" if ln.free else None)
        for ln in report.licences.lines], description="Each subscription and what's unused")
    book.table("Data freshness", ["Data", "Last collected (UTC)", "Status"], [
        (f.title, f.finished_at, "Out of date" if f.stale and f.finished_at else
         ("Not collected" if not f.finished_at else "Up to date")) for f in report.freshness],
        description="When each part of the data was read from Microsoft 365")

    since = f"{report.period_start.day} {report.period_start:%b %Y}" if report.period_start else "4 weeks ago"
    facts = []
    for k in report.kpis:
        before = "" if k.previous is None else (
            f"{k.previous:,.2f}" if k.unit == "currency" else f"{k.previous:g}{k.unit}")
        note = k.note or (f"{before} on {since}" if before else "")
        facts.append((k.label + (f" ({report.currency})" if k.unit == "currency" else ""), _kpi_value(k), note))
    by_sev = [f"{n} {s}" for s, n in report.counts.items() if n]
    facts += [("Checks needing attention", len(report.failing), ", ".join(by_sev) + " findings" if by_sev else ""),
              ("Checks passed", len(report.passing), ""), ("Not checked", len(report.unavailable), "")]
    cover = Cover(report.title, report.tenant["name"], report.generated_at, brand.display_name if brand else "",
                  report.tenant["tenant_ref"], brand.accent if brand else "",
                  f"Security checks, key figures and licences for {report.tenant['name']}.", facts)
    return book.save(cover)
