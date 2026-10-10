"""Distribution lists: their settings, owners and members, and how much mail each one gets.

Lists and their owners (ManagedBy) come from Get-DistributionGroup, members from
Get-DistributionGroupMember (one call per list). Room lists are left out: they group meeting rooms
for the room finder and never carry mail.

Activity comes from the message trace (Get-MessageTraceV2): a message sent to a list shows once with
the status "Expanded" and the list as recipient. Microsoft keeps 90 days of trace but answers at most
10 days per query, so each run reads only what's new since the last one and adds it to a per-list,
per-day count kept with the snapshot (``dl_activity``). Over time that gives "no mail in 90 days"
without ever asking for more than 10 days, and the report can say since when lists have been watched.
Only counts and the time of the last message are kept: never subjects or senders.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from .. import store
from ..devices import parse_ts
from ..exchange import ExchangeAccessDenied, ExchangeError
from ..text import plural
from .base import Collector, RunContext, register

GROUP_FIELDS = (
    "ExternalDirectoryObjectId",
    "Guid",
    "Name",
    "DisplayName",
    "PrimarySmtpAddress",
    "RecipientTypeDetails",
    "ManagedBy",
    "MemberJoinRestriction",
    "MemberDepartRestriction",
    "RequireSenderAuthenticationEnabled",
    "HiddenFromAddressListsEnabled",
    "ModerationEnabled",
    "IsDirSynced",
    "WhenCreatedUTC",
    "WhenChangedUTC",
)
MEMBER_FIELDS = (
    "Name",
    "DisplayName",
    "PrimarySmtpAddress",
    "ExternalEmailAddress",
    "RecipientType",
    "RecipientTypeDetails",
    "ExternalDirectoryObjectId",
)
LIST_LIMIT = 1000  # lists whose members are read each run
MEMBERS_KEPT = 200  # members stored per list; counts are always complete
OWNERS_KEPT = 20

TRACE_DAYS = 10  # the most Microsoft answers in one message trace query
TRACE_SETTLE = timedelta(hours=1)  # trace data can take a while to appear, so stop reading an hour short of now
TRACE_PAGE = 5000  # Microsoft's largest ResultSize
TRACE_ADDRESSES = 20  # lists per query
TRACE_QUERIES = 60  # per run; Microsoft accepts 100 every 5 minutes per tenant
KEEP_DAYS = 90  # per-day counts kept, the longest window the report shows

GROUP_TYPES = {"MailUniversalSecurityGroup", "MailUniversalDistributionGroup", "MailNonUniversalGroup"}
EXTERNAL_TYPES = {"MailContact", "MailUser", "GuestMailUser"}
NESTED_TYPES = {
    "MailUniversalDistributionGroup",
    "MailUniversalSecurityGroup",
    "MailNonUniversalGroup",
    "DynamicDistributionGroup",
    "GroupMailbox",
}


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _list_id(g: dict) -> str:
    return str(g.get("ExternalDirectoryObjectId") or g.get("Guid") or g.get("PrimarySmtpAddress") or "?")


def _member(m: dict) -> dict:
    kind = m.get("RecipientTypeDetails") or m.get("RecipientType") or ""
    external = str(m.get("ExternalEmailAddress") or "").removeprefix("SMTP:").removeprefix("smtp:")
    return {
        "name": m.get("DisplayName") or m.get("Name"),
        "address": m.get("PrimarySmtpAddress") or external or None,
        "type": kind,
        "id": m.get("ExternalDirectoryObjectId"),
    }


@register
class DistributionLists(Collector):
    key = "distribution_lists"
    title = "Distribution lists"
    description = "Every distribution list, its owners and members, and how much mail it gets (needs Global Reader)."
    permissions = ("Exchange.ManageAsApp",)

    def collect(self, ctx: RunContext) -> None:
        if ctx.capabilities.get("exchange") is False:
            ctx.log("Tenant has no Exchange Online licence; nothing to collect")
            ctx.objects("distribution_list", [])
            return
        groups = [
            g
            for g in ctx.exchange.invoke("Get-DistributionGroup", {"ResultSize": "Unlimited"})
            if g.get("RecipientTypeDetails") != "RoomList"
        ]
        groups.sort(key=lambda g: str(g.get("DisplayName") or g.get("Name") or "").lower())
        if len(groups) > LIST_LIMIT:
            ctx.warn(f"Read members for the first {LIST_LIMIT} of {len(groups)} distribution lists")
        rows, failed = [], 0
        for i, g in enumerate(groups):
            row = {f: g.get(f) for f in GROUP_FIELDS}
            owners = [o for o in (g.get("ManagedBy") or []) if isinstance(o, str)]
            row.update({"ManagedBy": owners[:OWNERS_KEPT], "ownerCount": len(owners), "membersRead": False})
            if i < LIST_LIMIT:
                try:
                    members = ctx.exchange.invoke(
                        "Get-DistributionGroupMember",
                        {"Identity": g.get("Guid") or g.get("PrimarySmtpAddress"), "ResultSize": "Unlimited"},
                    )
                except ExchangeAccessDenied:
                    raise
                except ExchangeError:
                    failed += 1
                    members = None
                if members is not None:
                    found = [_member(m) for m in members]
                    row.update(
                        {
                            "membersRead": True,
                            "memberCount": len(found),
                            "externalCount": sum(m["type"] in EXTERNAL_TYPES for m in found),
                            "nestedCount": sum(m["type"] in NESTED_TYPES for m in found),
                            "members": found[:MEMBERS_KEPT],
                        }
                    )
            rows.append((_list_id(g), row))
        if failed:
            ctx.warn(f"Could not read the members of {plural(failed, 'distribution list')}")
        ctx.objects("distribution_list", rows)
        self._activity(ctx, [r for _, r in rows])

        data = [r for _, r in rows]
        ctx.metric("mail.distribution_lists", len(data))
        ctx.metric(
            "mail.distribution_lists_empty", sum(r.get("membersRead") and not r.get("memberCount") for r in data)
        )

    @staticmethod
    def _activity(ctx: RunContext, lists: list[dict]) -> None:
        """Add the message trace since each list was last read to its per-day counts."""
        now = datetime.now(UTC).replace(microsecond=0)
        end = now - TRACE_SETTLE
        earliest = end - timedelta(days=TRACE_DAYS)
        previous = store.latest_run(ctx.conn, ctx.tenant["id"], DistributionLists.key)
        kept = {r["_id"]: r for r in store.run_objects(ctx.conn, previous["id"], "dl_activity")} if previous else {}
        counts: dict[str, dict] = {}
        for g in lists:
            address = g.get("PrimarySmtpAddress")
            if not isinstance(address, str) or not address:
                continue
            old = kept.get(address.lower()) or {}
            read_to = parse_ts(old.get("readTo"))
            entry = {
                "days": dict(old.get("days") or {}),
                "last": old.get("last"),
                "readTo": old.get("readTo"),
                "watchedFrom": old.get("watchedFrom"),
            }
            if read_to is None or read_to < earliest:
                # A new list, or one last read longer ago than a query reaches: watching starts again here,
                # and the date of an earlier last message is still kept.
                entry["readTo"] = entry["watchedFrom"] = _iso(earliest)
            counts[address.lower()] = entry

        # Lists read up to the same moment are asked about together.
        by_start: dict[str, list[str]] = {}
        for address, entry in sorted(counts.items()):
            if parse_ts(entry["readTo"]) < end:
                by_start.setdefault(entry["readTo"], []).append(address)
        queries, behind = 0, 0
        try:
            for start, addresses in sorted(by_start.items()):
                for i in range(0, len(addresses), TRACE_ADDRESSES):
                    chunk = addresses[i : i + TRACE_ADDRESSES]
                    if queries >= TRACE_QUERIES:
                        behind += len(chunk)
                        continue
                    found, queries = DistributionLists._trace(ctx, chunk, start, end, queries)
                    if found is None:  # ran out of queries part-way: read the whole chunk again next run
                        behind += len(chunk)
                        continue
                    for m in found:
                        address, when = str(m.get("RecipientAddress") or "").lower(), parse_ts(m.get("Received"))
                        if address not in chunk or when is None:
                            continue
                        entry = counts[address]
                        day = when.date().isoformat()
                        entry["days"][day] = entry["days"].get(day, 0) + 1
                        if not entry["last"] or _iso(when) > entry["last"]:
                            entry["last"] = _iso(when)
                    for address in chunk:
                        counts[address]["readTo"] = _iso(end)
        except ExchangeError as exc:  # access denied included: lists are still worth showing without activity
            ctx.log(f"The message trace isn't available, so list activity wasn't read: {exc.message}", "warning")
            ctx.objects(
                "dl_activity_status",
                [("status", {"available": False, "reason": exc.message[:300], "checkedAt": _iso(now)})],
            )
            ctx.objects(
                "dl_activity", [(a, {k: v for k, v in kept[a].items() if not k.startswith("_")}) for a in sorted(kept)]
            )
            return
        if behind:
            ctx.warn(
                f"Read the message trace for some distribution lists only (Microsoft allows "
                f"{TRACE_QUERIES} queries a run here); {plural(behind, 'list')} carry on next run"
            )
        cutoff = (now - timedelta(days=KEEP_DAYS)).date().isoformat()
        for entry in counts.values():
            entry["days"] = {d: n for d, n in sorted(entry["days"].items()) if d >= cutoff}
        ctx.objects("dl_activity", sorted(counts.items()))
        ctx.objects(
            "dl_activity_status", [("status", {"available": True, "checkedAt": _iso(now), "readTo": _iso(end)})]
        )

    @staticmethod
    def _trace(
        ctx: RunContext, chunk: list[str], start: str, end: datetime, queries: int
    ) -> tuple[list[dict] | None, int]:
        """Every "Expanded" trace row for the lists in ``chunk`` from ``start`` to ``end``, following Microsoft's
        StartingRecipientAddress paging; None if the run's query budget runs out first."""
        rows: list[dict] = []
        upper, starting = end, None
        while True:
            if queries >= TRACE_QUERIES:
                return None, queries
            params = {
                "RecipientAddress": chunk,
                "Status": ["Expanded"],
                "StartDate": start,
                "EndDate": _iso(upper),
                "ResultSize": TRACE_PAGE,
            }
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
