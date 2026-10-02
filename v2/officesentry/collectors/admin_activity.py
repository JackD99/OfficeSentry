"""Admin activity: security-relevant changes from the Entra ID directory audit log."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..adminactivity import FREE_RETENTION_DAYS, WINDOW_DAYS, kind_of, normalise
from ..graph import LicenseRequired, PermissionMissing
from .base import Collector, RunContext, register

MAX_EVENTS = 10_000  # audit events read per run, newest first
PAGE_SIZE = 999


@register
class AdminActivity(Collector):
    key = "admin_activity"
    title = "Admin activity"
    description = ("Admin role, Conditional Access, app credential, consent, domain and account changes from "
                   "the directory audit log (30 days with Entra ID P1, otherwise 7).")
    permissions = ("AuditLog.Read.All",)

    def collect(self, ctx: RunContext) -> None:
        now = datetime.now(UTC)
        since = (now - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        p1 = ctx.capabilities.get("entra_p1")
        window = {"requested_days": WINDOW_DAYS, "since": since,
                  "retention_days": None if p1 is None else (WINDOW_DAYS if p1 else FREE_RETENTION_DAYS)}
        try:
            result = ctx.graph.get_all("/auditLogs/directoryAudits",
                                       params={"$filter": f"activityDateTime ge {since}", "$top": str(PAGE_SIZE)},
                                       max_pages=-(-MAX_EVENTS // PAGE_SIZE))
        except (LicenseRequired, PermissionMissing) as exc:
            why = "the tenant's licence doesn't include it" if isinstance(exc, LicenseRequired) \
                else "AuditLog.Read.All consent missing"
            ctx.warn(f"The directory audit log wasn't readable ({why}): {exc.message}")
            ctx.objects("audit_window", [("window", {**window, "available": False, "reason": exc.message})])
            return

        events = result.items
        capped = result.truncated or len(events) > MAX_EVENTS
        events = events[:MAX_EVENTS]
        if capped:
            ctx.warn(f"Only the newest {MAX_EVENTS:,} directory audit events were read; "
                     "older changes in the window are left out")
        times = sorted(e["activityDateTime"] for e in events if e.get("activityDateTime"))
        kept = [(e["id"], normalise(e)) for e in events if e.get("id") and kind_of(e)]
        ctx.objects("audit_window", [("window", {
            **window, "available": True, "capped": capped, "read": len(events), "kept": len(kept),
            "oldest": times[0] if times else None, "newest": times[-1] if times else None})])
        ctx.objects("admin_event", kept)

        done = [e for _, e in kept if e["result"] == "success"]
        ctx.metric("admin_activity.events", len(kept))
        ctx.metric("admin_activity.role_changes", sum(e["kind"] in ("role_grant", "role_removal") for e in done))
        ctx.metric("admin_activity.ca_changes", sum(e["kind"] == "ca" for e in done))
        ctx.metric("admin_activity.app_credential_changes", sum(e["kind"] == "app_credential" for e in done))
