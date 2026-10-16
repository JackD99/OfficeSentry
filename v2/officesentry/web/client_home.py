"""The home page for client users: where they stand first (the verdict and their MSP's note), then what to do
next, then the figures, their other reports and who to contact.

Built from the client's copy of this month's review, as the report page shows it (``reports.tenant_report``):
less what the month-end reviewer left out, and with the MSP's commentary and recommendations once the month is
approved. So a client never sees anything here that their report doesn't already say.
"""

from __future__ import annotations

from flask import g, render_template

from ..reports import summary
from ..reports.data_reports import grouped, hidden
from ..reports.monthly_review import build, leave_out
from .reporting import report_extras

MAX_RECOMMENDATIONS = 8
# The MSP's recommendations by status: the pill's tone ("Waiting on you" is the client's to do).
ACTION_TONES = {"planned": "info", "in_progress": "accent", "waiting": "warn"}


def _from_checks(report) -> list[dict]:
    """Failing checks, worst first, then accepted risks: what was found and what to do about it."""
    recs = [
        {
            "title": r.summary.rstrip("."),
            "action": r.action,
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
                "severity": "",
                "status": "Accepted risk",
                "tone": "info",
            }
    return recs + list(accepted.values())


def _from_msp(actions: list) -> list[dict]:
    """The MSP's own recommendations for the month (``month_end.report_context``) that aren't done yet."""
    return [
        {
            "title": a["title"],
            "action": a["detail"] or "",
            "severity": "",
            "status": a["status_label"],
            "tone": ACTION_TONES.get(a["status"], "info"),
            "target": a["target_label"],
        }
        for a in actions
        if a["status"] != "done"
    ]


def _row(tenant) -> dict:
    extras = report_extras(tenant, page=True)
    me = extras["month_end"]
    report = leave_out(build(g.db, tenant), me["hidden"])
    collected = [f.finished_at for f in report.freshness if f.finished_at]
    planned = _from_msp(me["actions"])
    recs = planned or _from_checks(report)
    return {
        "tenant": report.tenant,
        "verdict": summary.monthly(report).verdict,
        "commentary": [p.strip() for p in (me["commentary"] or "").split("\n\n") if p.strip()],
        "from_msp": bool(planned),
        "kpis": {k.key: k for k in report.kpis},
        # By check, as the report counts them: "46 checks, 15 critical or high", not the accounts they affect.
        "counts": {s: sum(1 for r in report.failing if r.severity == s) for s in summary.SEVERITIES},
        "failing": len(report.failing),
        "resolved": len(report.resolved),
        "stale_data": any(f.stale for f in report.freshness),
        "data_as_of": max(collected) if collected else None,
        "recommendations": recs[:MAX_RECOMMENDATIONS],
        "more_recommendations": max(0, len(recs) - MAX_RECOMMENDATIONS),
    }


def render(tenants):
    return render_template("client_home.html", rows=[_row(t) for t in tenants], report_groups=grouped(hidden(g.db)))
