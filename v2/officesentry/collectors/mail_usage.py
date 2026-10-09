"""Mailbox size and legacy sign-ins.

Sizes come from Exchange's Get-MailboxStatistics, one call per mailbox, rather
than Graph's mailbox usage report: Microsoft hides names in usage reports by
default, so that report usually can't be matched to a mailbox.

Legacy sign-ins (SMTP AUTH, IMAP, POP and other basic-auth clients) come from
the Entra sign-in log, which needs Entra ID P1 and keeps 30 days. Once the
activity history (``events.py``, filled by ``sign_ins.read_log``, which this
collector also calls) holds every sign-in of those 30 days, they come from
there with no query of their own. Until then they are
asked for in one query filtered on every legacy client; if Microsoft times out
on that, the 30 days are halved (newest first) until the queries succeed, then
the client list for a day that still fails, and the results are merged into the
same newest-first list.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from .. import events, store
from ..exchange import ExchangeAccessDenied, ExchangeError
from ..graph import GraphError, LicenseRequired, PermissionMissing, is_transient
from ..mailflow import size_bytes
from ..text import plural
from . import mailbox_window, sign_ins
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
MIN_RANGE = timedelta(days=1)  # a timed-out query shorter than this splits its clients, not its time range


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
        now = events.clock().replace(microsecond=0)
        start = now - timedelta(days=SIGNIN_DAYS)
        since = _iso(start)
        try:
            got = sign_ins.read_log(ctx)
        except (LicenseRequired, PermissionMissing) as exc:
            ctx.warn(f"The sign-in log isn't available: {exc.message}")
            ctx.objects("legacy_signin_status", [("status", {"available": False})])
            return
        except GraphError as exc:
            if not is_transient(exc):
                raise
            got = None
            ctx.log(f"Couldn't bring the stored sign-ins up to date ({exc}); asking for legacy sign-ins directly")
        if got and not got.coverage.missing(int(start.timestamp()), int(now.timestamp())):
            # The activity history holds every sign-in of the 30 days: no query of its own needed.
            items = [s for s in events.signins_between(ctx.conn, ctx.tenant["id"], start, now)
                     if s.get("clientAppUsed") in LEGACY_CLIENTS]
            ctx.log(f"Found {plural(len(items), 'legacy sign-in')} in the stored sign-in log")
            truncated, skipped = False, []
        else:
            items, truncated, skipped = self._query(ctx, since)
            if items is None:
                return
        if skipped:
            ctx.warn("Microsoft's sign-in log timed out for these clients, so some or all of their sign-ins are "
                     "missing: " + ", ".join(skipped))
        if truncated:
            ctx.truncated = True
            ctx.warn(f"More than {SIGNIN_PAGES * 1000} legacy sign-ins; summarised the most recent ones")
        ctx.objects("legacy_signin_status", [("status", {"available": True, "since": since})])
        rows = summarise_signins(items)
        ctx.objects("legacy_signin", rows)
        ctx.metric("mail.legacy_signins", len(items))
        ctx.metric("mail.legacy_signins_succeeded", sum(r["succeeded"] for _, r in rows))

    @staticmethod
    def _query(ctx: RunContext, since: str) -> tuple[list[dict] | None, bool, list[str]]:
        """Legacy sign-ins asked for directly, while the stored history doesn't cover the 30 days yet."""
        skipped: list[str] = []
        try:
            items, truncated, split = fetch_legacy_signins(ctx, since, LEGACY_CLIENTS, skipped)
        except (LicenseRequired, PermissionMissing) as exc:
            ctx.warn(f"The sign-in log isn't available: {exc.message}")
            ctx.objects("legacy_signin_status", [("status", {"available": False})])
            return None, False, []
        if split:
            # Each smaller query is newest first; merge them into one list as the big query would have.
            items.sort(key=lambda s: str(s.get("createdDateTime") or ""), reverse=True)
            if len(items) > SIGNIN_PAGES * 1000:
                del items[SIGNIN_PAGES * 1000:]
                truncated = True
        return items, truncated, skipped


def fetch_legacy_signins(ctx: RunContext, since: str, clients: tuple[str, ...] | list[str],
                         skipped: list[str]) -> tuple[list[dict], bool, bool]:
    """Sign-ins for ``clients`` since ``since``: (items, truncated, whether the query was split).

    A timeout or server error splits the time range in half and tries each half,
    newest first, down to ``MIN_RANGE``; a range that short that still fails
    splits the client list in half instead. A single client that still fails is
    added to ``skipped``, as is everything not yet read once the splitting has
    taken ``sign_ins.FALLBACK_SECONDS`` (a log that is down for the whole tenant
    would otherwise take hours). If no query succeeded the first failure is
    raised, so a broken log still fails.
    """
    def skip(chunk) -> None:
        skipped.extend(c for c in chunk if c not in skipped)

    def fetch(start: datetime, end: datetime | None, chunk) -> tuple[list[dict], bool]:
        nonlocal expired, answered
        if expired is not None and expired():
            ctx.log(f"Gave up on legacy sign-ins for {', '.join(chunk)} before {_iso(end or now)}: "
                    "the sign-in log is too slow")
            skip(chunk)
            return [], False
        flt = " or ".join(f"clientAppUsed eq '{c}'" for c in chunk)
        upper = f" and createdDateTime lt {_iso(end)}" if end else ""
        try:
            result = ctx.graph.get_all("/auditLogs/signIns", params={
                "$filter": f"createdDateTime ge {_iso(start)}{upper} and ({flt})", "$top": "1000",
            }, max_pages=SIGNIN_PAGES, transient_retries=sign_ins.QUERY_RETRIES)
            answered += 1
            return result.items, result.truncated
        except GraphError as exc:
            if not is_transient(exc):
                raise
            errors.append(exc)
            expired = expired or sign_ins.deadline()
        stop = end or now
        if stop - start > MIN_RANGE:
            # A long range is what times out (seen on the test tenant: 30 days 504s, a week answers).
            ctx.log(f"Legacy sign-ins from {_iso(start)} failed; splitting the time range")
            mid = start + (stop - start) / 2
            newer, a_trunc = fetch(mid, end, chunk)
            older, b_trunc = fetch(start, mid, chunk)
            return newer + older, a_trunc or b_trunc
        if len(chunk) == 1:
            ctx.log(f"Legacy sign-ins for {chunk[0]} from {_iso(start)} failed")
            skip(chunk)
            return [], False
        ctx.log(f"Legacy sign-in query for {len(chunk)} clients from {_iso(start)} failed; splitting the clients")
        half = len(chunk) // 2
        a_items, a_trunc = fetch(start, end, chunk[:half])
        b_items, b_trunc = fetch(start, end, chunk[half:])
        return a_items + b_items, a_trunc or b_trunc

    errors: list[GraphError] = []
    expired = None  # starts when the first query fails
    answered = 0
    now = datetime.now(UTC).replace(microsecond=0)
    start = datetime.fromisoformat(since.replace("Z", "+00:00"))
    items, truncated = fetch(start, None, list(clients))
    if errors and not answered:
        raise errors[0]
    return items, truncated, bool(errors)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


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
