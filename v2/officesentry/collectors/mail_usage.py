"""Mailbox size and legacy sign-ins.

Sizes come from Exchange's Get-MailboxStatistics, one call per mailbox, rather
than Graph's mailbox usage report: Microsoft hides names in usage reports by
default, so that report usually can't be matched to a mailbox.

Legacy sign-ins (SMTP AUTH, IMAP, POP and other basic-auth clients) come from
the Entra sign-in log, which needs Entra ID P1 and keeps 30 days.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from .. import store
from ..exchange import ExchangeAccessDenied, ExchangeError
from ..graph import LicenseRequired, PermissionMissing
from ..mailflow import size_bytes
from ..text import plural
from . import mailbox_window
from .base import Collector, RunContext, register
from .exchange import SYSTEM_MAILBOX_TYPES

# Sign-in log clientAppUsed values for clients that use basic (legacy) authentication.
LEGACY_CLIENTS = (
    "Authenticated SMTP", "AutoDiscover", "Exchange ActiveSync", "Exchange Online PowerShell",
    "Exchange Web Services", "IMAP4", "MAPI Over HTTP", "Offline Address Book", "Other clients",
    "Outlook Anywhere (RPC over HTTP)", "Outlook Service", "POP3", "Reporting Web Services",
)
SIGNIN_DAYS = 30
SIGNIN_PAGES = 50


@register
class MailboxUsage(Collector):
    key = "mailbox_usage"
    title = "Mailbox size"
    description = "Size and item count of every mailbox, against its quota (needs Global Reader)."
    permissions = ("Exchange.ManageAsApp",)

    def collect(self, ctx: RunContext) -> None:
        if ctx.capabilities.get("exchange") is False:
            ctx.log("Tenant has no Exchange Online licence; nothing to collect")
            ctx.objects("mailbox_size", [])
            return
        run = store.latest_run(ctx.conn, ctx.tenant["id"], "mailboxes")
        boxes = [m for m in (store.run_objects(ctx.conn, run["id"], "mailbox") if run else [])
                 if m.get("RecipientTypeDetails") not in SYSTEM_MAILBOX_TYPES]
        if not run:
            ctx.warn("Mailboxes haven't been collected yet, so there's nothing to size")
        window = mailbox_window.plan(ctx, self.key, "mailbox_size", boxes, lambda m: m["_id"], "mailbox sizes")
        rows, failed = [], 0
        for m in window.mailboxes:
            try:
                stats = ctx.exchange.invoke("Get-MailboxStatistics", {"Identity": m.get("Guid") or m["_id"]})
            except ExchangeAccessDenied:
                raise
            except ExchangeError:
                failed += 1
                continue
            s = stats[0] if stats else {}
            rows.append((m["_id"], {
                "mailboxId": m["_id"], "mailbox": m.get("PrimarySmtpAddress"),
                "mailboxType": m.get("RecipientTypeDetails"),
                "totalBytes": size_bytes(s.get("TotalItemSize")),
                "deletedBytes": size_bytes(s.get("TotalDeletedItemSize")),
                "itemCount": s.get("ItemCount"),
                "warningQuotaBytes": size_bytes(m.get("IssueWarningQuota")),
                "sendQuotaBytes": size_bytes(m.get("ProhibitSendQuota")),
                "sendReceiveQuotaBytes": size_bytes(m.get("ProhibitSendReceiveQuota")),
                "archive": m.get("ArchiveStatus") not in (None, "", "None"),
                "litigationHold": m.get("LitigationHoldEnabled"),
            }))
        if failed:
            ctx.warn(f"Could not read the size of {plural(failed, 'mailbox')}")
        rows += window.carried
        ctx.objects("mailbox_size", rows)
        window.save(ctx)
        data = [r for _, r in rows]
        ctx.metric("mail.storage_bytes", sum(r["totalBytes"] or 0 for r in data))
        ctx.metric("mail.mailboxes_near_quota", sum(_near_quota(r) for r in data))


def _near_quota(row: dict) -> bool:
    quota = row.get("sendQuotaBytes")
    return bool(quota and (row.get("totalBytes") or 0) >= 0.9 * quota)


@register
class LegacySignIns(Collector):
    key = "legacy_signins"
    title = "Legacy sign-ins"
    description = (f"Sign-ins over SMTP AUTH, IMAP, POP and other basic-auth clients in the last {SIGNIN_DAYS} "
                   "days (needs Entra ID P1).")
    permissions = ("AuditLog.Read.All",)

    def collect(self, ctx: RunContext) -> None:
        if ctx.capabilities.get("entra_p1") is False:
            ctx.log("Tenant has no Entra ID P1; the sign-in log isn't available")
            ctx.objects("legacy_signin_status", [("status", {"available": False})])
            return
        since = (datetime.now(UTC) - timedelta(days=SIGNIN_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        clients = " or ".join(f"clientAppUsed eq '{c}'" for c in LEGACY_CLIENTS)
        try:
            result = ctx.graph.get_all("/auditLogs/signIns", params={
                "$filter": f"createdDateTime ge {since} and ({clients})", "$top": "1000",
            }, max_pages=SIGNIN_PAGES)
        except (LicenseRequired, PermissionMissing) as exc:
            ctx.warn(f"The sign-in log isn't available: {exc.message}")
            ctx.objects("legacy_signin_status", [("status", {"available": False})])
            return
        if result.truncated:
            ctx.truncated = True
            ctx.warn(f"More than {SIGNIN_PAGES * 1000} legacy sign-ins; summarised the most recent ones")
        ctx.objects("legacy_signin_status", [("status", {"available": True, "since": since})])
        rows = summarise_signins(result.items)
        ctx.objects("legacy_signin", rows)
        ctx.metric("mail.legacy_signins", len(result.items))
        ctx.metric("mail.legacy_signins_succeeded", sum(r["succeeded"] for _, r in rows))


def summarise_signins(signins: list[dict]) -> list[tuple[str, dict]]:
    """One row per account and client: counts, last attempt and where from."""
    groups: dict[tuple[str, str], dict] = {}
    ips: dict[tuple[str, str], Counter] = {}
    for s in signins:
        user = s.get("userPrincipalName") or s.get("userId") or "Unknown"
        client = s.get("clientAppUsed") or "Other clients"
        key = (str(user).lower(), str(client))
        ok = ((s.get("status") or {}).get("errorCode") or 0) == 0
        when = s.get("createdDateTime") or ""
        g = groups.setdefault(key, {"user": user, "userId": s.get("userId"), "client": client, "succeeded": 0,
                                    "failed": 0, "lastAttempt": "", "lastSuccess": None, "lastFailure": None,
                                    "countries": [], "apps": []})
        g["succeeded" if ok else "failed"] += 1
        g["lastAttempt"] = max(g["lastAttempt"], when)
        if ok:
            g["lastSuccess"] = max(g["lastSuccess"] or "", when)
        else:
            reason = (s.get("status") or {}).get("failureReason")
            if when >= (g.get("_failureAt") or ""):
                g["_failureAt"], g["lastFailure"] = when, reason
        country = (s.get("location") or {}).get("countryOrRegion")
        if country and country not in g["countries"]:
            g["countries"].append(country)
        app = s.get("appDisplayName")
        if app and app not in g["apps"]:
            g["apps"].append(app)
        if s.get("ipAddress"):
            ips.setdefault(key, Counter())[s["ipAddress"]] += 1
    rows = []
    for key, g in groups.items():
        g.pop("_failureAt", None)
        g["topIps"] = [ip for ip, _ in ips.get(key, Counter()).most_common(5)]
        rows.append((f"{key[0]}|{key[1]}", g))
    return rows
