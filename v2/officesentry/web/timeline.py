"""A client's Timeline (``reports/timeline.py``): the Changes tab's third page, staff only, with its downloads.

The page shows each person only what they open on this client (``access.opens_on``, ``collect`` for failed
collections, ``month_end.review`` for staff-only notes), as What changed does.
"""

from __future__ import annotations

from datetime import UTC, datetime

from flask import Blueprint, Response, g, render_template, request, url_for

from ..db import audit
from ..reports import branding, tables, timeline, workbook
from .access import can, needs, opens_on, tenant_or_404
from .printing import pdf_response

bp = Blueprint("timeline", __name__)

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PERIOD_LABELS = {12: "Last 12 months", 24: "Last 24 months", 0: "Everything kept"}


def _months() -> int:
    raw = request.args.get("months", "")
    if raw == "all":
        return 0
    try:
        n = int(raw)
    except ValueError:
        return timeline.DEFAULT_MONTHS
    return n if n in timeline.PERIODS and n else timeline.DEFAULT_MONTHS


def _show() -> str:
    show = request.args.get("show", "")
    return show if show in timeline.GROUP_TITLES else ""


def _href(target: str, tenant_id: int) -> str:
    """Where an entry's title opens. The template only links it for someone who opens that page."""
    if target == "findings":
        return url_for("reports.tenant_findings", tenant_id=tenant_id)
    if target == "resolved":
        return url_for("reports.resolved_findings", tenant_id=tenant_id)
    if target == timeline.ADMIN_REPORT:
        return url_for("reports.data_report", tenant_id=tenant_id, key=timeline.ADMIN_REPORT)
    if target == "report-history":
        return url_for("email.report_history", tenant_id=tenant_id)
    if target == "manage":
        return url_for("tenant_admin.manage", tenant_id=tenant_id)
    if target.startswith("run:"):
        return url_for("tenants.run", run_id=int(target[4:]))
    if target.startswith("review:"):
        return url_for("reporting.review", period=target[7:], tenant_id=tenant_id)
    return ""


def _url(tenant_id: int, months: int, show: str) -> str:
    args = {}
    if months != timeline.DEFAULT_MONTHS:
        args["months"] = "all" if months == 0 else months
    if show:
        args["show"] = show
    return url_for("timeline.page", tenant_id=tenant_id, **args)


def _page(t) -> dict:
    months, show = _months(), _show()
    tl = timeline.build(
        g.db,
        t,
        months=months,
        show=show,
        opens=opens_on(t["id"]),
        collects=can("collect", t["id"]),
        reviews=can("month_end.review", t["id"]),
    )
    for m in tl.months:
        for e in m.entries:
            e.href = _href(e.target, t["id"])
    query = {}
    if months != timeline.DEFAULT_MONTHS:
        query["months"] = "all" if months == 0 else months
    if show:
        query["show"] = show
    period = PERIOD_LABELS[months]
    title = (
        f"{timeline.GROUP_TITLES[show]} at {t['name']}, {period.lower()}" if show else f"{t['name']}, {period.lower()}"
    )
    today = datetime.now(UTC).date()
    return {
        "tl": tl,
        "months": months,
        "show": show,
        "query": query,
        "slug": ("all" if months == 0 else f"{months}-months") + (f"-{show}" if show else ""),
        "title": title,
        "period": period,
        "groups": timeline.GROUPS,
        "show_links": [
            ("Everything", _url(t["id"], months, ""), tl.total, not show),
            *((label, _url(t["id"], months, key), tl.counts[key], show == key) for key, label in timeline.GROUPS),
        ],
        "period_links": [(PERIOD_LABELS[n], _url(t["id"], n, show), n == months) for n in timeline.PERIODS],
        # Each month's heading opens What changed between its first and last day (or today).
        "changed": {
            m.key: url_for(
                "across.tenant_changes",
                tenant_id=t["id"],
                **{"from": m.start.isoformat(), "to": min(m.end, today).isoformat()},
            )
            for m in tl.months
        },
    }


@bp.get("/tenants/<int:tenant_id>/timeline")
@needs("reports.changes", kind="staff")
def page(tenant_id: int):
    t = tenant_or_404(tenant_id)
    return render_template("timeline.html", tenant=t, **_page(t))


@bp.get("/tenants/<int:tenant_id>/timeline.pdf")
@needs("reports.changes", "export", kind="staff")
def pdf(tenant_id: int):
    t = tenant_or_404(tenant_id)
    page = _page(t)
    audit(g.db, g.user["username"], "report_downloaded", t["id"], f"timeline-{page['slug']}.pdf")
    return pdf_response("reports/timeline.html", f"timeline-{page['slug']}-{tables.slug(t['name'])}", tenant=t, **page)


@bp.get("/tenants/<int:tenant_id>/timeline.csv", defaults={"ext": "csv"})
@bp.get("/tenants/<int:tenant_id>/timeline.xlsx", defaults={"ext": "xlsx"})
@needs("reports.changes", "export", kind="staff")
def download(tenant_id: int, ext: str):
    t = tenant_or_404(tenant_id)
    page = _page(t)
    tl = page["tl"]
    audit(g.db, g.user["username"], "report_downloaded", t["id"], f"timeline-{page['slug']}.{ext}")
    sheets = timeline.to_tables(tl)
    if ext == "csv":
        body = tables.to_csv(sheets[0])
    else:
        brand = branding.load(g.db)
        body = tables.to_xlsx(
            sheets,
            cover=workbook.Cover(
                f"Timeline: {page['period'].lower()}",
                t["name"],
                datetime.now(UTC),
                brand.display_name,
                t["tenant_ref"],
                brand.accent,
                "What happened at this client, month by month, from what Office Sentry keeps.",
                [("Entries", tl.shown, "newest first"), ("Months", len(tl.months), "")],
            ),
        )
    filename = f"timeline-{page['slug']}-{tables.slug(t['name'])}.{ext}"
    return Response(
        body,
        mimetype=XLSX if ext == "xlsx" else "text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
