"""Mail flow: how much mail each of the tenant's domains sent and received, how much was delivered, sent to Junk,
quarantined or failed, and who gets the most spam and phishing.

Counts come from the message trace (Get-MessageTraceV2), one row per message and recipient. Microsoft
answers at most 10 days a query, so, as for distribution lists, each run reads only what's new since the
last one, a day at a time, and adds it to per-domain, per-day counts kept with the snapshot
(``mail_flow``). A message counts against one of the tenant's accepted domains: the recipient's domain for
mail coming in or between colleagues, the sender's for mail going out. Only counts are kept: never
subjects, senders or recipients.

The top spam and phishing recipients come from Microsoft's own summary (Get-MailTrafficSummaryReport, the
Defender portal's "Top senders and recipients"), which every tenant with Exchange Online has and Global
Reader can read: the top 20 addresses and how many such messages each got in the last 30 days.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .. import store
from ..devices import parse_ts
from ..exchange import ExchangeError
from .base import Collector, RunContext, register

TRACE_DAYS = 10  # the most Microsoft answers in one message trace query
TRACE_SETTLE = timedelta(hours=1)  # trace data can take a while to appear, so stop reading an hour short of now
TRACE_PAGE = 5000  # Microsoft's largest ResultSize
TRACE_QUERIES = 30  # per run; Microsoft accepts 100 every 5 minutes per tenant, shared with distribution lists
KEEP_DAYS = 30  # per-day counts kept, the longest window the report shows
TOP_DAYS = 30
TOP = {"spam": "TopSpamRecipient", "phish": "TopPhishRecipient"}

# The message trace's Status, in the report's words. "Expanded" is a distribution list handing the message on
# to its members, who each get a row of their own, so it isn't counted.
STATUSES = {
    "Delivered": "delivered",
    "FilteredAsSpam": "junk",
    "Quarantined": "quarantined",
    "Failed": "failed",
    "Pending": "pending",
    "GettingStatus": "pending",
}
DIRECTIONS = ("in", "out", "internal")


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _domain(address) -> str:
    text = address if isinstance(address, str) else ""
    return text.rpartition("@")[2].strip().lower() if "@" in text else ""


def classify(row: dict, accepted: set[str]) -> tuple[str, str, str] | None:
    """(domain, direction, status) a trace row counts as, or None (a list expansion, or mail that passes through
    without touching one of the tenant's domains)."""
    status = STATUSES.get(row.get("Status"))
    if status is None:
        return None
    sender, recipient = _domain(row.get("SenderAddress")), _domain(row.get("RecipientAddress"))
    ours_from, ours_to = sender in accepted, recipient in accepted
    if ours_to:
        return recipient, "internal" if ours_from else "in", status
    if ours_from:
        return sender, "out", status
    return None


def _day_slices(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """[start, end) cut at each midnight UTC, oldest first, so a run that stops early has whole slices read."""
    slices = []
    while start < end:
        midnight = (start + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        slices.append((start, min(midnight, end)))
        start = slices[-1][1]
    return slices


@register
class MailFlow(Collector):
    key = "mail_flow"
    title = "Mail flow"
    description = (
        "How much mail each domain sends and receives, how much is delivered, sent to Junk, quarantined or "
        "failed, and who gets the most spam and phishing (needs Global Reader)."
    )
    permissions = ("Exchange.ManageAsApp",)

    def collect(self, ctx: RunContext) -> None:
        if ctx.capabilities.get("exchange") is False:
            ctx.log("Tenant has no Exchange Online licence; nothing to collect")
            ctx.objects("mail_flow", [])
            ctx.objects("mail_flow_status", [("status", {"available": False, "reason": "no_exchange"})])
            return
        now = datetime.now(UTC).replace(microsecond=0)
        accepted = {
            str(d["DomainName"]).lower() for d in ctx.exchange.invoke("Get-AcceptedDomain") if d.get("DomainName")
        }
        self._flow(ctx, now, accepted)
        self._top(ctx, now)

    @staticmethod
    def _flow(ctx: RunContext, now: datetime, accepted: set[str]) -> None:
        """Add the message trace since the last run to the per-domain, per-day counts."""
        end = now - TRACE_SETTLE
        earliest = end - timedelta(days=TRACE_DAYS)
        previous = store.latest_run(ctx.conn, ctx.tenant["id"], MailFlow.key)
        kept_rows = store.run_objects(ctx.conn, previous["id"], "mail_flow") if previous else []
        kept_status = store.run_objects(ctx.conn, previous["id"], "mail_flow_status") if previous else []
        old = kept_status[0] if kept_status else {}
        counts = {d: {"days": {}} for d in accepted}  # a domain with no mail yet is listed too
        counts.update({r["_id"]: {"days": dict(r.get("days") or {})} for r in kept_rows})
        read_to, watched = parse_ts(old.get("readTo")), old.get("watchedFrom")
        if read_to is None or read_to < earliest:
            # First run, or the last one was longer ago than a query reaches: watching starts again here.
            read_to, watched = earliest, _iso(earliest)
        queries, reason = 0, None
        try:
            for start, stop in _day_slices(read_to, end):
                rows, queries = MailFlow._trace(ctx, start, stop, queries)
                if rows is None:  # out of queries part-way through the slice: read it whole next run
                    break
                for row in rows:
                    found, when = classify(row, accepted), parse_ts(row.get("Received"))
                    # Slices meet at midnight, so a row on the boundary counts in one of them only.
                    if found is None or when is None or not start <= when < stop:
                        continue
                    domain, direction, status = found
                    day = counts.setdefault(domain, {"days": {}})["days"].setdefault(when.date().isoformat(), {})
                    bucket = day.setdefault(direction, {})
                    bucket[status] = bucket.get(status, 0) + 1
                read_to = stop
        except ExchangeError as exc:  # access denied included: the summary below may still read
            reason = exc.message[:300]
            ctx.warn(f"The message trace isn't available, so mail flow per domain wasn't read: {exc.message}")
        if read_to < end and reason is None:
            ctx.warn(
                f"Read the message trace up to {_iso(read_to)} only (Microsoft allows {TRACE_QUERIES} queries a "
                "run here); the rest is read next run"
            )
        cutoff = (now - timedelta(days=KEEP_DAYS)).date().isoformat()
        for entry in counts.values():
            entry["days"] = {d: v for d, v in sorted(entry["days"].items()) if d >= cutoff}
        ctx.objects("mail_flow", sorted((d, e) for d, e in counts.items() if e["days"] or d in accepted))
        ctx.objects(
            "mail_flow_status",
            [
                (
                    "status",
                    {
                        "available": reason is None,
                        "reason": reason,
                        "checkedAt": _iso(now),
                        "readTo": _iso(read_to),
                        "watchedFrom": watched,
                        "domains": sorted(accepted),
                    },
                )
            ],
        )

    @staticmethod
    def _trace(ctx: RunContext, start: datetime, end: datetime, queries: int) -> tuple[list[dict] | None, int]:
        """Every trace row received from ``start`` to ``end``, following Microsoft's StartingRecipientAddress
        paging; None if the run's query budget runs out first."""
        rows: list[dict] = []
        upper, starting = end, None
        while True:
            if queries >= TRACE_QUERIES:
                return None, queries
            params = {"StartDate": _iso(start), "EndDate": _iso(upper), "ResultSize": TRACE_PAGE}
            if starting:
                params["StartingRecipientAddress"] = starting
            found = ctx.exchange.invoke("Get-MessageTraceV2", params)
            queries += 1
            rows.extend(found)
            if len(found) < TRACE_PAGE:
                return rows, queries
            last = found[-1]
            upper = parse_ts(last.get("Received")) or upper
            starting = last.get("RecipientAddress")

    @staticmethod
    def _top(ctx: RunContext, now: datetime) -> None:
        """Microsoft's top 20 spam and phishing recipients for the last 30 days."""
        start = (now - timedelta(days=TOP_DAYS)).replace(hour=0, minute=0, second=0)
        rows, read = [], {}
        for kind, category in TOP.items():
            try:
                found = ctx.exchange.invoke(
                    "Get-MailTrafficSummaryReport",
                    {"Category": category, "StartDate": _iso(start), "EndDate": _iso(now)},
                )
            except ExchangeError as exc:
                read[kind] = False
                ctx.warn(f"Microsoft's top {kind} recipients weren't available: {exc.message}")
                continue
            read[kind] = True
            for rank, r in enumerate(found, 1):
                address = r.get("C1")
                if isinstance(address, str) and address:
                    rows.append(
                        (
                            f"{kind}:{address.lower()}",
                            {"kind": kind, "address": address, "count": _int(r.get("C2")), "rank": rank},
                        )
                    )
        ctx.objects("top_recipient", rows)
        ctx.objects("top_recipient_status", [("status", {"read": read, "days": TOP_DAYS, "from": _iso(start)})])
