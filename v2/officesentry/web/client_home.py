"""The home page for client users: their latest report first, then what's still open, then who to contact.

Built from the same monthly review the report page shows (``monthly_review.build``),
so a client never sees anything here that their report doesn't already say.
"""

from __future__ import annotations

from flask import g, render_template

from ..reports.data_reports import grouped
from ..reports.monthly_review import build

MAX_RECOMMENDATIONS = 8


def _recommendations(report) -> list[dict]:
    """Failing checks, worst first, then accepted risks: what the MSP is working on with the client."""
    recs = [
        {
            "title": r.title,
            "action": r.action,
            "summary": r.summary,
            "severity": r.severity or "info",
            "status": "New" if any(getattr(f, "new", False) for f in r.findings) else "Open",
            "tone": "bad" if r.severity in ("critical", "high") else "warn",
        }
        for r in report.failing
    ]
    accepted: dict[str, dict] = {}
    for r, _f, _life in report.accepted:
        if r.key not in accepted and r.status == "accepted":
            accepted[r.key] = {
                "title": r.title,
                "action": "",
                "summary": r.summary,
                "severity": "",
                "status": "Accepted risk",
                "tone": "info",
            }
    return recs + list(accepted.values())


def _row(tenant) -> dict:
    report = build(g.db, tenant)
    collected = [f.finished_at for f in report.freshness if f.finished_at]
    recs = _recommendations(report)
    return {
        "tenant": report.tenant,
        "kpis": {k.key: k for k in report.kpis},
        "counts": report.counts,
        "failing": len(report.failing),
        "resolved": len(report.resolved),
        "stale_data": any(f.stale for f in report.freshness),
        "data_as_of": max(collected) if collected else None,
        "recommendations": recs[:MAX_RECOMMENDATIONS],
        "more_recommendations": max(0, len(recs) - MAX_RECOMMENDATIONS),
    }


def render(tenants):
    return render_template("client_home.html", rows=[_row(t) for t in tenants], report_groups=grouped())
