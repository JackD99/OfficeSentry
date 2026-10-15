"""Service health: Microsoft 365 outages and advisories affecting the tenant, and the message centre's posts about
upcoming changes.

Reads ``/admin/serviceAnnouncement``: ``healthOverviews`` (each service's status now) and ``issues`` (incidents and
advisories) need ServiceHealth.Read.All, and ``messages`` (the message centre) ServiceMessage.Read.All, both added
in Office Sentry 2.4 (``permission_batch``): until the client approves one, its parts are skipped and stored as
waiting, so the run doesn't fail.

Keeps issues still open and those that ended in the last ``DAYS`` days, and message centre posts not past their
end date. Neither keeps Microsoft's text beyond a one-line summary: the report gives each post's ID, which finds it
in the admin centre.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..graph import GraphError, PermissionMissing
from ..text import plural
from .base import Collector, RunContext, register
from .connection import awaiting

HEALTH = "ServiceHealth.Read.All"
MESSAGES = "ServiceMessage.Read.All"
PARTS = {"health": HEALTH, "issues": HEALTH, "messages": MESSAGES}
PATHS = {
    "health": "/admin/serviceAnnouncement/healthOverviews",
    "issues": "/admin/serviceAnnouncement/issues",
    "messages": "/admin/serviceAnnouncement/messages",
}
DAYS = 30  # issues that ended this long ago are kept
PAGE = {"Prefer": "odata.maxpagesize=1000"}  # Microsoft's largest page; 100 without it
MAX_PAGES = 20
SUMMARY = 300  # characters of an issue's impact kept


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def issue(i: dict) -> dict:
    return {
        "title": _text(i.get("title")),
        "service": _text(i.get("service")),
        "feature": _text(i.get("feature")),
        "classification": (_text(i.get("classification")) or "").lower() or None,  # "incident" or "advisory"
        "status": _text(i.get("status")),
        "impact": (_text(i.get("impactDescription")) or "")[:SUMMARY] or None,
        "resolved": i.get("isResolved") is True,
        "origin": _text(i.get("origin")),
        "start": _text(i.get("startDateTime")),
        "end": _text(i.get("endDateTime")),
        "updated": _text(i.get("lastModifiedDateTime")),
    }


def message(m: dict) -> dict:
    services = m.get("services")
    tags = m.get("tags")
    return {
        "title": _text(m.get("title")),
        "category": _text(m.get("category")),
        "severity": _text(m.get("severity")),
        "services": [s for s in services if isinstance(s, str)] if isinstance(services, list) else [],
        "tags": [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else [],
        "major": m.get("isMajorChange") is True,
        "actBy": _text(m.get("actionRequiredByDateTime")),
        "start": _text(m.get("startDateTime")),
        "end": _text(m.get("endDateTime")),
        "updated": _text(m.get("lastModifiedDateTime")),
    }


@register
class ServiceHealth(Collector):
    key = "service_health"
    title = "Service health"
    description = (
        "Microsoft 365 service status, outages and advisories affecting the tenant, and the message centre's posts "
        "about upcoming changes."
    )
    permissions = (HEALTH, MESSAGES)

    def collect(self, ctx: RunContext) -> None:
        now = datetime.now(UTC)
        since = (now - timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        today = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        waiting = awaiting(ctx, HEALTH, MESSAGES)
        if waiting:
            ctx.log(f"Waiting for the client to approve {', '.join(waiting)}; that part of service health is skipped")
        parts = {part: "waiting" if perm in waiting else "read" for part, perm in PARTS.items()}

        found = self._read(ctx, parts, "health")
        if found is not None:
            ctx.objects(
                "service_status",
                [
                    (s.get("id") or s["service"], {"service": s["service"], "status": _text(s.get("status"))})
                    for s in found
                    if isinstance(s, dict) and _text(s.get("service"))
                ],
            )
        found = self._read(ctx, parts, "issues")
        if found is not None:
            rows = [(i["id"], issue(i)) for i in found if isinstance(i, dict) and _text(i.get("id"))]
            kept = [(k, r) for k, r in rows if not r["resolved"] or (r["end"] or r["updated"] or "") >= since]
            ctx.objects("service_issue", kept)
            live = [r for _, r in kept if not r["resolved"]]
            ctx.metric("service_health.incidents_open", sum(r["classification"] == "incident" for r in live))
            ctx.metric("service_health.advisories_open", sum(r["classification"] == "advisory" for r in live))
            ctx.log(f"{plural(len(live), 'open issue')} with Microsoft 365 services")
        found = self._read(ctx, parts, "messages")
        if found is not None:
            rows = [(m["id"], message(m)) for m in found if isinstance(m, dict) and _text(m.get("id"))]
            kept = [(k, r) for k, r in rows if not r["end"] or r["end"] >= today]
            ctx.objects("service_message", kept)
            due = [r for _, r in kept if r["actBy"] and r["actBy"] >= today]
            ctx.metric("service_messages.action_required", len(due))
        ctx.objects("service_health_status", [("status", {"parts": parts, "days": DAYS})])

    @staticmethod
    def _read(ctx: RunContext, parts: dict, part: str) -> list[dict] | None:
        if parts[part] != "read":
            return None
        try:
            return ctx.paged(ctx.graph.get_all(PATHS[part], headers=PAGE, max_pages=MAX_PAGES))
        except PermissionMissing as exc:  # a new permission (BATCH_OF), so a refusal means not approved yet
            parts[part] = "waiting"
            ctx.log(f"Service health {part} skipped: {PARTS[part]} isn't approved yet ({exc.message})")
        except GraphError as exc:
            parts[part] = "failed"
            ctx.warn(f"Service health {part} weren't readable: {exc.message}")
        return None
