"""Admin activity: security-relevant changes from the Entra ID directory audit log.

Every directory audit event is kept in the activity history (``events.py``): each run reads only what is
new since the last one, from a stored cursor, and the first run reads back as far as Microsoft keeps the
log (30 days with Entra ID P1, otherwise 7). The snapshot holds the last 30 days of the changes worth
knowing about (``adminactivity.ACTIVITIES``), for the federation check and the account page; the Admin
activity report reads further back from the history itself.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .. import events
from ..adminactivity import FREE_RETENTION_DAYS, WINDOW_DAYS, kind_of, normalise
from ..graph import LicenseRequired, PermissionMissing
from ..text import plural
from .base import Collector, RunContext, register

PAGE_BUDGET = 50  # pages read per run (about 50,000 events), newest first; the next run carries on from there


def _at(ts: int) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@register
class AdminActivity(Collector):
    key = "admin_activity"
    title = "Admin activity"
    description = (
        "Admin role, Conditional Access, app credential, consent, domain and account changes from "
        "the directory audit log, kept in the activity history (Microsoft keeps 30 days with Entra ID "
        "P1, otherwise 7)."
    )
    permissions = ("AuditLog.Read.All",)

    def collect(self, ctx: RunContext) -> None:
        now = events.clock().replace(microsecond=0)
        start = now - timedelta(days=WINDOW_DAYS)
        since = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        p1 = ctx.capabilities.get("entra_p1")
        window = {
            "requested_days": WINDOW_DAYS,
            "since": since,
            "retention_days": None if p1 is None else (WINDOW_DAYS if p1 else FREE_RETENTION_DAYS),
        }
        try:
            got = events.ingest(ctx, events.AUDIT_LOG, FREE_RETENTION_DAYS if p1 is False else WINDOW_DAYS, PAGE_BUDGET)
        except (LicenseRequired, PermissionMissing) as exc:
            why = (
                "the tenant's licence doesn't include it"
                if isinstance(exc, LicenseRequired)
                else "AuditLog.Read.All consent missing"
            )
            ctx.warn(f"The directory audit log wasn't readable ({why}): {exc.message}")
            ctx.objects("audit_window", [("window", {**window, "available": False, "reason": exc.message})])
            return

        # Without P1 Microsoft only has the last 7 days, so the rest of the 30 isn't missing, just not there.
        floor = now - timedelta(days=FREE_RETENTION_DAYS) if p1 is False else start
        missing = got.coverage.missing(int(floor.timestamp()), int(now.timestamp()))
        if missing:
            ranges = ", ".join(f"{_at(lo)} to {_at(hi)}" for lo, hi in missing[:3])
            ctx.truncated = got.budget_hit
            ctx.warn(
                f"Directory audit events from {ranges} haven't been read yet"
                + (
                    f" (more than {PAGE_BUDGET * events.AUDIT_LOG.page_size:,} events in one run)"
                    if got.budget_hit
                    else " (Microsoft timed out)"
                )
                + "; the next run carries on from there"
            )
        ctx.log(f"Read {plural(got.read, 'directory audit event')} from Microsoft ({got.new:,} new)")

        stored = list(events.audits_between(ctx.conn, ctx.tenant["id"], start, now))
        times = sorted(e["activityDateTime"] for e in stored if isinstance(e.get("activityDateTime"), str))
        kept = [(e["id"], normalise(e)) for e in stored if e.get("id") and kind_of(e)]
        history = got.coverage.detailed_from
        ctx.objects(
            "audit_window",
            [
                (
                    "window",
                    {
                        **window,
                        "available": True,
                        "capped": bool(missing),
                        "read": len(stored),
                        "kept": len(kept),
                        "oldest": times[0] if times else None,
                        "newest": times[-1] if times else None,
                        "historyFrom": _at(history) if history is not None else None,
                    },
                )
            ],
        )
        ctx.objects("admin_event", kept)

        done = [e for _, e in kept if e["result"] == "success"]
        ctx.metric("admin_activity.events", len(kept))
        ctx.metric("admin_activity.role_changes", sum(e["kind"] in ("role_grant", "role_removal") for e in done))
        ctx.metric("admin_activity.ca_changes", sum(e["kind"] == "ca" for e in done))
        ctx.metric("admin_activity.app_credential_changes", sum(e["kind"] == "app_credential" for e in done))
