"""The audit log add-on's data source: each opted-in client's file, sharing and mailbox events, read from
Microsoft's audit log feed (``auditlog/feed.py``) and kept in the activity history (``events.py``, store
``activity``).

Only collected for clients the add-on is on for (``addon.active``); for everyone else it's never queued. Each
run reads only GETs, signed in as the add-on's own app:

1. The feed's subscriptions. A content type that's off at Microsoft (the client's admin turned it off) marks
   the client Off and queues a task that stops the rest of the feed (``addon.check_running``); a client that
   removed the add-on app (``addon.removed``) is marked Off too. Neither is started again without an admin.
2. Per content type, the batches Microsoft made available since the last run, newest first, six hours at a
   time, from the cursor the sign-in and directory audit logs use (``event_cursors``, source
   ``activity:<content type>``). Each batch is downloaded, the records ``auditlog/operations.py`` keeps are
   stored and the rest dropped. The cursor is saved after every six hours, so a crash or the batch budget
   loses nothing and the next run carries on. Microsoft lists batches for 7 days: anything older that wasn't
   read (the worker was down) is ``lost``, and the report says so.
3. The last 30 days' figures as metrics, for the report's trends.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

from .. import events
from ..auditlog import addon, feed, operations
from ..devices import parse_ts
from ..text import plural
from .base import Collector, RunContext, register

SLICE = 6 * 3600  # the feed is listed six hours at a time, and the cursor saved after each
OVERLAP = 15 * 60  # each run lists again from 15 minutes before where the last one stopped
MARGIN = 15 * 60  # Microsoft lists 7 days back; the oldest 15 minutes of that are left alone
BATCH_BUDGET = 5000  # batches downloaded per run; the next run carries on from there
FIGURE_DAYS = 30


def _at(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


def _stamp(ts: int) -> str:
    return _at(ts).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Run:
    """What one run read, across content types."""

    def __init__(self, ctx: RunContext, base, key: int):
        self.ctx, self.base, self.key = ctx, base, key
        self.batches = self.kept = self.new = self.dropped = self.expired = 0
        self.budget_hit = False

    def batch(self, client: feed.FeedClient, listed: dict) -> None:
        """Download one batch and store what's kept of it."""
        uri = listed["contentUri"]
        try:
            records = client.blob(uri)
        except feed.FeedExpired:
            self.expired += 1
            return
        self.batches += 1
        kept, counted = [], Counter()
        for record in records:
            op = operations.operation(record)
            if op is None:
                self.dropped += 1
                continue
            ts = parse_ts(record.get("CreationTime"))
            rid = record.get("Id")
            if ts is None or not isinstance(rid, str) or not rid:
                continue
            who = operations.actor(record)
            if op.counted:
                counted[(ts.strftime("%Y-%m-%d"), who, op.name)] += 1
                continue
            owner, about = operations.subject(record, op)
            kept.append(
                events.Activity(
                    rid,
                    int(ts.timestamp()),
                    op.name,
                    who,
                    owner,
                    about,
                    str(record.get("ClientIP") or "")[:64],
                    operations.summary(record, op),
                    operations.attention(record, op),
                    operations.stored(record),
                )
            )
        batch = str(listed.get("contentId") or uri)
        added = events.add_activity(self.base, self.key, kept, [(*k, n) for k, n in counted.items()], batch)
        self.kept += added.rows
        self.new += added.new

    def content_type(self, client: feed.FeedClient, content_type: str, now: int, floor: int) -> events.Coverage:
        """Read one content type's batches from where the last run stopped."""
        conn, tenant_id = self.ctx.conn, self.ctx.tenant["id"]
        cov = events.coverage(conn, tenant_id, f"activity:{content_type}")
        events.extend(cov, now, floor, overlap=OVERLAP)
        events.save_coverage(conn, tenant_id, cov)
        while cov.gaps:
            if self.batches >= BATCH_BUDGET:
                self.budget_hit = True
                break
            lo, hi = cov.gaps[0]
            start = max(lo, hi - SLICE)
            listed, cut = client.content(content_type, _at(start), _at(hi))
            for item in sorted(listed, key=lambda b: str(b.get("contentCreated") or "")):
                self.batch(client, item)
            done_to = start
            if cut:  # more batches in six hours than one listing follows: the rest are read next
                newest = max((parse_ts(b.get("contentCreated")) for b in listed), default=None, key=_key)
                if newest is not None and int(newest.timestamp()) > start:
                    self.ctx.log(f"More {content_type} batches than one listing holds; the rest are read next")
                    cov.gaps[0] = [int(newest.timestamp()), hi]
                    if start > lo:
                        cov.gaps.insert(1, [lo, start])
                    events.save_coverage(conn, tenant_id, cov)
                    continue
            if done_to > lo:
                cov.gaps[0] = [lo, done_to]
            else:
                del cov.gaps[0]
            events.save_coverage(conn, tenant_id, cov)
        return cov


def _key(dt: datetime | None) -> float:
    return dt.timestamp() if dt else 0.0


@register
class AuditActivity(Collector):
    key = "audit_activity"
    title = "Files and mailbox access"
    description = (
        "Files deleted, downloaded and shared, mailboxes opened by someone other than their owner, inbox rules, "
        "forwarding and mailbox permissions, from the client's Microsoft 365 audit log. Only for clients that "
        "approved the audit log add-on."
    )
    permissions = ()  # not Graph: the add-on's own app, with ActivityFeed.Read on the Office 365 Management APIs
    addon = True

    @classmethod
    def applies(cls, conn, tenant) -> bool:
        return addon.active(conn, tenant)

    def collect(self, ctx: RunContext) -> None:
        conn, tenant = ctx.conn, ctx.tenant
        if not addon.active(conn, tenant):
            ctx.log("The audit log add-on isn't on for this client, so there's nothing to read")
            return
        row = addon.row_for(conn, tenant["id"])
        now = events.clock().replace(microsecond=0)
        try:
            subscriptions = ctx.feed.subscriptions()
        except feed.FeedError as exc:
            if not addon.removed(exc):
                raise
            addon.mark_off(conn, tenant["id"], addon.REMOVED)
            ctx.warn(addon.REMOVED)
            return
        if not addon.check_running(conn, tenant, subscriptions).ok:
            ctx.warn(addon.row_for(conn, tenant["id"])["stop_reason"] or "The audit log feed is off at Microsoft.")
            return

        base = events.root(conn)
        if base is None:
            raise RuntimeError("The activity history needs the database in a file")
        run = _Run(ctx, base, events.tenant_key(conn, tenant["id"], create=True))
        started = parse_ts(row["started_at"]) or now
        floor = max(int((now - feed.KEPT_FOR).timestamp()) + MARGIN, int(started.timestamp()) - MARGIN)
        end = int(now.timestamp())
        covered: dict[str, events.Coverage] = {}
        try:
            for content_type in feed.CONTENT_TYPES:
                covered[content_type] = run.content_type(ctx.feed, content_type, end, floor)
        except feed.FeedError as exc:
            if not addon.removed(exc):
                raise
            addon.mark_off(conn, tenant["id"], addon.REMOVED)
            ctx.warn(addon.REMOVED)
            return
        ctx.log(
            f"Read {plural(run.batches, 'batch', 'batches')} of audit records: kept {run.kept:,} ({run.new:,} new), "
            f"left out {run.dropped:,}" + (f"; {run.expired} had expired" if run.expired else "")
        )

        since = max(int(started.timestamp()), end - FIGURE_DAYS * events.DAY)
        unread, lost = [], []
        for content_type, cov in covered.items():
            for lo, hi in cov.missing(since, end):
                gone = any(a <= lo and hi <= b for a, b in cov.lost)
                (lost if gone else unread).append((content_type, lo, hi))
        if unread:
            ctx.truncated = run.budget_hit
            ranges = ", ".join(f"{c} {_stamp(lo)} to {_stamp(hi)}" for c, lo, hi in unread[:3])
            ctx.warn(
                f"Audit records from {ranges} haven't been read yet"
                + (f" (more than {BATCH_BUDGET:,} batches in one run)" if run.budget_hit else "")
                + "; the next run carries on from there"
            )
        history = [cov.detailed_from for cov in covered.values() if cov.detailed_from is not None]
        ctx.objects(
            "audit_feed",
            [
                (
                    "feed",
                    {
                        "startedAt": row["started_at"],
                        "contentTypes": subscriptions,
                        "historyFrom": _stamp(max(history)) if history else None,
                        "lost": [[c, _stamp(lo), _stamp(hi)] for c, lo, hi in lost][:20],
                        "batches": run.batches,
                        "kept": run.kept,
                        "new": run.new,
                        "budgetHit": run.budget_hit,
                    },
                )
            ],
        )
        recent = list(events.activity_between(conn, tenant["id"], now - timedelta(days=FIGURE_DAYS), now))
        for key, value in operations.figures(recent).items():
            ctx.metric(f"audit_activity.{key}", value)
