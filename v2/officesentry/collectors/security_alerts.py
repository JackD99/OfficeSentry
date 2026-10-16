"""Security alerts: what Microsoft Defender has flagged in the tenant, and its incidents, which group related
alerts together.

Reads ``/security/alerts_v2`` and ``/security/incidents``, which need SecurityAlert.Read.All and
SecurityIncident.Read.All, added in Office Sentry 2.4 (``permission_batch``): until the client approves one, its
part is skipped and stored as waiting, so the run doesn't fail. A tenant that has approved the permission but
that Defender refuses (no product that reports to it) is stored as unavailable, not as a failure.

Keeps what's still open, however old (Defender keeps alerts for 180 days), and what was resolved in the last
``defender.DAYS`` days. An alert's evidence is cut to the accounts and devices it names: nothing from an email or
a file, and never a command line or a process.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..defender import ALERTS, DAYS, INCIDENTS, OPEN_INCIDENT
from ..graph import GraphError, LicenseRequired, PermissionMissing
from ..text import plural
from .base import Collector, RunContext, register
from .connection import awaiting, granted

# Pages of Microsoft's own size (it documents none, and no $top maximum); newest first, so a cut keeps the newest.
MAX_PAGES = 200
NAMED = 10  # accounts or devices kept per alert

USER_EVIDENCE = ("userEvidence", "mailboxEvidence")
DEVICE_EVIDENCE = ("deviceEvidence",)


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _kind(evidence: dict) -> str:
    return (evidence.get("@odata.type") or "").rsplit(".", 1)[-1]


def named(evidence: list | None) -> tuple[list[str], list[str]]:
    """The accounts and devices an alert's evidence names, without repeats."""
    users: dict[str, None] = {}
    devices: dict[str, None] = {}
    for e in evidence if isinstance(evidence, list) else []:
        if not isinstance(e, dict):
            continue
        kind = _kind(e)
        if kind in USER_EVIDENCE:
            account = e.get("userAccount") or {}
            local = "\\".join(x for x in (_text(account.get("domainName")), _text(account.get("accountName"))) if x)
            user = (
                _text(account.get("userPrincipalName"))
                or _text(e.get("primaryAddress"))
                or _text(account.get("displayName"))
                or local
            )
            if user:
                users[user.lower() if "@" in user else user] = None
        elif kind in DEVICE_EVIDENCE:
            device = _text(e.get("hostName")) or _text(e.get("deviceDnsName"))
            if device:
                devices[device] = None
    return list(users)[:NAMED], list(devices)[:NAMED]


def alert(a: dict) -> dict:
    users, devices = named(a.get("evidence"))
    return {
        "title": _text(a.get("title")),
        "severity": _text(a.get("severity")),
        "status": _text(a.get("status")),
        "classification": _text(a.get("classification")),
        "determination": _text(a.get("determination")),
        "category": _text(a.get("category")),
        "source": _text(a.get("serviceSource")),
        "incident": _text(a.get("incidentId")),
        "assignedTo": _text(a.get("assignedTo")),
        "created": _text(a.get("createdDateTime")),
        "updated": _text(a.get("lastUpdateDateTime")),
        "resolved": _text(a.get("resolvedDateTime")),
        "url": _text(a.get("alertWebUrl")),
        "users": users,
        "devices": devices,
    }


def incident(i: dict) -> dict:
    tags = i.get("customTags")
    return {
        "name": _text(i.get("displayName")),
        "severity": _text(i.get("severity")),
        "status": _text(i.get("status")),
        "classification": _text(i.get("classification")),
        "determination": _text(i.get("determination")),
        "assignedTo": _text(i.get("assignedTo")),
        "created": _text(i.get("createdDateTime")),
        "updated": _text(i.get("lastUpdateDateTime")),
        "url": _text(i.get("incidentWebUrl")),
        "tags": [t for t in tags if isinstance(t, str)][:NAMED] if isinstance(tags, list) else [],
    }


@register
class SecurityAlerts(Collector):
    key = "security_alerts"
    title = "Security alerts"
    description = (
        f"Microsoft Defender's alerts and incidents: those still open, and those resolved in the last {DAYS} days."
    )
    permissions = (ALERTS, INCIDENTS)

    def collect(self, ctx: RunContext) -> None:
        since = (datetime.now(UTC) - timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        waiting = awaiting(ctx, ALERTS, INCIDENTS)
        if waiting:
            ctx.log(f"Waiting for the client to approve {', '.join(waiting)}; those Defender lists are skipped")
        parts = {"alerts": "waiting" if ALERTS in waiting else "read"}
        parts["incidents"] = "waiting" if INCIDENTS in waiting else "read"
        status: dict = {"days": DAYS, "since": since}

        if parts["alerts"] == "read":
            found = self._read(ctx, parts, "alerts", ALERTS, "/security/alerts_v2", status)
            if found is not None:
                rows = [(a["id"], alert(a)) for a in found if isinstance(a, dict) and _text(a.get("id"))]
                kept = [(k, r) for k, r in rows if r["status"] != "resolved" or (r["updated"] or "") >= since]
                ctx.objects("security_alert", kept)
                live = [r for _, r in kept if r["status"] != "resolved"]
                ctx.metric("defender.alerts_open", len(live))
                ctx.metric("defender.alerts_open_high", sum(r["severity"] == "high" for r in live))
                ctx.log(f"{plural(len(live), 'open alert')}; read {plural(len(rows), 'alert')}")
        if parts["incidents"] == "read":
            found = self._read(ctx, parts, "incidents", INCIDENTS, "/security/incidents", status)
            if found is not None:
                rows = [(str(i["id"]), incident(i)) for i in found if isinstance(i, dict) and i.get("id")]
                kept = [
                    (k, r)
                    for k, r in rows
                    if r["status"] in OPEN_INCIDENT or (r["status"] == "resolved" and (r["updated"] or "") >= since)
                ]
                ctx.objects("security_incident", kept)
                ctx.metric("defender.incidents_open", sum(r["status"] in OPEN_INCIDENT for _, r in kept))
        ctx.objects("security_alert_status", [("status", {**status, "parts": parts})])

    @staticmethod
    def _read(
        ctx: RunContext,
        parts: dict,
        part: str,
        permission: str,
        path: str,
        status: dict,
    ) -> list[dict] | None:
        try:
            result = ctx.graph.get_all(path, max_pages=MAX_PAGES)
        except (LicenseRequired, PermissionMissing) as exc:
            if isinstance(exc, PermissionMissing) and not granted(ctx, permission):
                # A new permission (BATCH_OF), so a refusal before it's granted means not approved yet.
                parts[part] = "waiting"
                ctx.log(f"Defender {part} skipped: {permission} isn't approved yet ({exc.message})")
            else:
                # Granted, but Defender has nothing for this tenant: no product that reports to it.
                parts[part] = "unavailable"
                ctx.log(f"Defender {part} aren't available in this tenant: {exc.message}")
            return None
        except GraphError as exc:
            parts[part] = "failed"
            ctx.warn(f"Defender {part} weren't readable: {exc.message}")
            return None
        status[f"{part}_read"] = len(result.items)
        status[f"{part}_truncated"] = result.truncated
        return ctx.paged(result)
