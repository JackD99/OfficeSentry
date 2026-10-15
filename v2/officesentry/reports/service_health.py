"""Service health: whether Microsoft 365 is working for the tenant now, Microsoft's outages and advisories, and
the message centre's upcoming changes, those needing action by a date first.

Reads what ``collectors/service_health.py`` stores. A problem on Microsoft's side shows here before anyone spends
time troubleshooting it, and a change with a deadline shows before the deadline passes.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from ..permission_batch import waiting_text
from ..snapshot import TenantSnapshot
from .data_reports import DataReport, Rows, Section, Tile
from .registry import report

COLLECTOR = "service_health"
PARTS = {"health": "ServiceHealth.Read.All", "issues": "ServiceHealth.Read.All", "messages": "ServiceMessage.Read.All"}
NOT_COLLECTED = 'Service health hasn\'t been collected yet. Refresh "Service health" on the client page.'
SOON_DAYS = 30  # a change due within this many days is flagged

STATUS = "Status now"
OPEN = "Open issues"
RESOLVED = "Recently resolved"
ACTION = "Changes needing action"
MAJOR = "Major changes"
RECENT = "Other recent posts"

# Statuses that mean the service works (compared lower-cased: Microsoft writes them both ways).
WORKING = (
    "serviceoperational",
    "servicerestored",
    "postincidentreviewpublished",
    "falsepositive",
    "resolved",
    "resolvedexternal",
    "mitigated",
    "mitigatedexternal",
)
CATEGORIES = {
    "preventOrFixIssue": "Prevent or fix an issue",
    "planForChange": "Plan for change",
    "stayInformed": "Stay informed",
}


def _words(value) -> str:
    """Microsoft's camelCase or PascalCase values in words: ``serviceDegradation`` -> "Service degradation"."""
    if not isinstance(value, str) or not value:
        return ""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value).lower()
    return spaced[0].upper() + spaced[1:]


def _utc(value) -> str:
    return value[:16].replace("T", " ") if isinstance(value, str) and len(value) >= 16 else ""


def _day(value) -> str:
    return value[:10] if isinstance(value, str) else ""


def _rows(snap: TenantSnapshot, kind: str) -> list[dict]:
    return [r for r in snap.objects("service_health", kind) or [] if isinstance(r, dict)]


def _kind(issue: dict) -> str:
    return _words(issue.get("classification")) or "Issue"


@report(
    "service-health",
    "Service health",
    "Whether Microsoft 365 is working for this client: Microsoft's open outages and advisories, those resolved "
    "recently, and the message centre's upcoming changes, those needing action by a date first.",
    (COLLECTOR,),
    group="tenant",
    order=80,
    keywords="service health status outage incident advisory down degraded microsoft 365 message center message "
    "centre upcoming changes retirement deprecation roadmap deadline act by major change",
)
def service_health_report(snap: TenantSnapshot, now: datetime, out: DataReport) -> None:
    rows = snap.objects("service_health", "service_health_status")
    if rows is None:
        out.unavailable = NOT_COLLECTED
        return
    status = rows[0] if rows and isinstance(rows[0], dict) else {}
    parts = status.get("parts") if isinstance(status.get("parts"), dict) else {}
    out.parts = {k: v for k, v in parts.items() if isinstance(v, str)}
    waiting = list(dict.fromkeys(PARTS[p] for p, state in parts.items() if state == "waiting" and p in PARTS))
    if waiting:
        out.notice = waiting_text(snap.tenant["name"], waiting)
    days = status.get("days") if isinstance(status.get("days"), int) else 30
    today = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    soon = (now + timedelta(days=SOON_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def missing(part: str, what: str) -> str:
        if parts.get(part) == "waiting":
            return f"{what} will show once the client approves the new permission."
        return f"{what} couldn't be read; see the Service health collection's log."

    services = Section(
        STATUS,
        ["Service", "Status"],
        intro="Each Microsoft 365 service the tenant uses, as Microsoft reports it now.",
        empty="Microsoft listed no services." if parts.get("health") == "read" else missing("health", "Services"),
    )
    for s in sorted(_rows(snap, "service_status"), key=lambda s: (s.get("service") or "").lower()):
        state = s.get("status") or ""
        working = state.lower() in WORKING
        services.add([s.get("service"), _words(state) or "Unknown"], None if working or not state else "medium")

    issues = _rows(snap, "service_issue")
    newest = lambda r: r.get("updated") or r.get("start") or ""  # noqa: E731
    open_issues = Section(
        OPEN,
        ["ID", "Title", "Service", "Kind", "Status", "Impact", "Started (UTC)", "Last updated (UTC)"],
        intro="Problems Microsoft is working on that affect this tenant. An incident is an outage or a "
        "noticeable one; an advisory affects some people or features. Check here before troubleshooting "
        "something that may be Microsoft's. The ID finds the issue in the Microsoft 365 admin centre, under "
        "Health > Service health.",
        empty="Microsoft reports no open issues." if parts.get("issues") == "read" else missing("issues", "Issues"),
        optional=("Impact",),
    )
    for i in sorted((i for i in issues if not i.get("resolved")), key=newest, reverse=True):
        open_issues.add(
            [
                i.get("_id"),
                i.get("title"),
                i.get("service"),
                _kind(i),
                _words(i.get("status")),
                i.get("impact") or "",
                _utc(i.get("start")),
                _utc(i.get("updated")),
            ],
            "medium" if i.get("classification") == "incident" else "low",
        )
    resolved = Section(
        RESOLVED,
        ["ID", "Title", "Service", "Kind", "Started (UTC)", "Ended (UTC)"],
        compare=False,
        intro=f"Issues Microsoft resolved in the last {days} days, for explaining a problem someone had.",
        empty=f"No issues were resolved in the last {days} days."
        if parts.get("issues") == "read"
        else missing("issues", "Issues"),
    )
    for i in sorted((i for i in issues if i.get("resolved")), key=lambda i: i.get("end") or newest(i), reverse=True):
        resolved.add(
            [i.get("_id"), i.get("title"), i.get("service"), _kind(i), _utc(i.get("start")), _utc(i.get("end"))]
        )

    messages = _rows(snap, "service_message")
    due = sorted((m for m in messages if (m.get("actBy") or "") >= today), key=lambda m: m["actBy"])
    action = Section(
        ACTION,
        ["ID", "Title", "Services", "Act by", "Category", "Major change"],
        intro="Message centre posts that need something done by a date: a feature retiring, a setting to "
        f"change. Those due within {SOON_DAYS} days are flagged. The ID finds the post in the Microsoft 365 "
        "admin centre, under Health > Message center.",
        empty="No changes need action." if parts.get("messages") == "read" else missing("messages", "Posts"),
    )
    for m in due:
        action.add(
            [
                m.get("_id"),
                m.get("title"),
                m.get("services") or [],
                _day(m.get("actBy")),
                CATEGORIES.get(m.get("category") or "", _words(m.get("category"))),
                m.get("major") is True,
            ],
            "medium" if m["actBy"] <= soon else "low",
        )
    shown = {m.get("_id") for m in due}
    majors = sorted(
        (m for m in messages if m.get("major") and m.get("_id") not in shown),
        key=lambda m: m.get("start") or "",
        reverse=True,
    )
    major = Section(
        MAJOR,
        ["ID", "Title", "Services", "Category", "Published", "Last updated"],
        intro="Changes Microsoft calls major (people will notice them, or an admin may need to prepare) with no "
        "date to act by.",
        empty="No major changes are announced." if parts.get("messages") == "read" else missing("messages", "Posts"),
    )
    for m in majors:
        major.add(
            [
                m.get("_id"),
                m.get("title"),
                m.get("services") or [],
                CATEGORIES.get(m.get("category") or "", _words(m.get("category"))),
                _day(m.get("start")),
                _day(m.get("updated")),
            ]
        )
    shown |= {m.get("_id") for m in majors}
    others = Section(
        RECENT,
        ["ID", "Title", "Services", "Category", "Published"],
        compare=False,
        intro=f"Everything else the message centre posted in the last {days} days.",
        empty=f"Nothing else was posted in the last {days} days."
        if parts.get("messages") == "read"
        else missing("messages", "Posts"),
        optional=("Category",),
    )
    for m in sorted(messages, key=lambda m: m.get("start") or "", reverse=True):
        if m.get("_id") in shown or (m.get("start") or "") < recent:
            continue
        others.add(
            [
                m.get("_id"),
                m.get("title"),
                m.get("services") or [],
                CATEGORIES.get(m.get("category") or "", _words(m.get("category"))),
                _day(m.get("start")),
            ]
        )
    out.sections = [services, open_issues, resolved, action, major, others]

    if parts.get("issues") == "read":
        incidents = sum(r[3] == "Incident" for r in open_issues.rows)
        advisories = len(open_issues.rows) - incidents
        out.tiles += [
            Tile(
                "Open incidents",
                incidents,
                "Outages Microsoft is working on",
                "bad" if incidents else "ok",
                Rows(OPEN, {"Kind": "Incident"}),
            ),
            Tile(
                "Open advisories",
                advisories,
                "Problems affecting some people or features",
                "warn" if advisories else "ok",
                Rows(OPEN, {"Kind": "Advisory"}),
            ),
        ]
    if parts.get("messages") == "read":
        urgent = sum(f == "medium" for f in action.flags)
        out.tiles += [
            Tile(
                "Action needed",
                len(action.rows),
                f"{urgent} due within {SOON_DAYS} days" if urgent else "Changes with a date to act by",
                "warn" if urgent else "ok",
                Rows(ACTION),
            ),
            Tile("Major changes", len(major.rows), "Announced, with no date to act by", rows=Rows(MAJOR)),
        ]
    out.footnote = (
        "From Microsoft 365 service health and the message centre, as Microsoft shows them in this tenant's admin "
        "centre. Office Sentry keeps each post's title and dates, not its text. Times are UTC."
    )
