"""Per-mailbox checks on big tenants: a rotating window with earlier results kept.

Reading something for each mailbox (permissions, inbox rules, size) is one
call per mailbox, so a run checks at most ``PER_MAILBOX_LIMIT`` of them. Rather
than always the same first ones, each run checks the mailboxes checked longest
ago (never-checked first, ties by ID), and the rows of the rest are copied from
the collector's previous run, so reports still show every mailbox. When each
mailbox was last checked is stored with the run as one ``<kind>_coverage``
object, so the next run can pick up where this one left off.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .. import store
from ..text import plural
from .base import RunContext

PER_MAILBOX_LIMIT = 2000


@dataclass
class MailboxWindow:
    kind: str
    mailboxes: list  # the ones to check this run, in their original order
    carried: list[tuple[str, dict]] = field(default_factory=list)  # earlier rows for the others
    carried_mailboxes: int = 0  # mailboxes not checked this run that have earlier results
    checked_at: dict[str, str] = field(default_factory=dict)
    collector: str = ""

    def save(self, ctx: RunContext) -> None:
        ctx.objects(f"{self.kind}_coverage", [("checked", {"checkedAt": self.checked_at})])

    def keep_unread(self, ctx: RunContext, unread: set[str]) -> int:
        """Carry the previous run's rows for mailboxes this run tried and couldn't read (a timeout, a refusal),
        as for those outside the window, so a rule found before doesn't vanish, and its finding look fixed,
        because one request failed. They keep their earlier check time, so the next run reads them first.
        Returns how many of them had earlier results."""
        previous = store.latest_run(ctx.conn, ctx.tenant["id"], self.collector) if unread else None
        earlier: dict[str, str] = {}
        rows = []
        if previous is not None:
            for c in store.run_objects(ctx.conn, previous["id"], f"{self.kind}_coverage"):
                earlier = c.get("checkedAt") or {}
            rows = [
                (r["_id"], {k: v for k, v in r.items() if k not in ("_id", "_kind")})
                for r in store.run_objects(ctx.conn, previous["id"], self.kind)
                if r.get("mailboxId") in unread
            ]
        self.carried += rows
        kept = {i for i in unread if i in earlier} | {r["mailboxId"] for _, r in rows}
        for i in unread:
            if i in earlier:
                self.checked_at[i] = earlier[i]
            else:
                self.checked_at.pop(i, None)
        self.carried_mailboxes += len(kept)
        return len(kept)


def kept_note(kept: int) -> str:
    """The end of a "Could not read ..." warning when ``MailboxWindow.keep_unread`` kept earlier results."""
    return f"; showing their earlier results for {plural(kept, 'mailbox')}" if kept else ""


def plan(
    ctx: RunContext, collector: str, kind: str, mailboxes: list, mailbox_id: Callable[[object], str], what: str
) -> MailboxWindow:
    """Choose this run's mailboxes for ``kind`` rows (which carry a ``mailboxId``); warn if that isn't all."""
    limit = PER_MAILBOX_LIMIT
    now = datetime.now(UTC).isoformat(timespec="microseconds")
    if len(mailboxes) <= limit:
        return MailboxWindow(kind, mailboxes, checked_at={mailbox_id(m): now for m in mailboxes}, collector=collector)

    previous = store.latest_run(ctx.conn, ctx.tenant["id"], collector)
    earlier: dict[str, str] = {}
    if previous:
        for c in store.run_objects(ctx.conn, previous["id"], f"{kind}_coverage"):
            earlier = c.get("checkedAt") or {}
    ids = {mailbox_id(m) for m in mailboxes}
    chosen = set(sorted(ids, key=lambda i: (earlier.get(i, ""), i))[:limit])
    rest = {i: earlier[i] for i in ids - chosen if i in earlier}
    carried = []
    if previous and rest:
        carried = [
            (r["_id"], {k: v for k, v in r.items() if k not in ("_id", "_kind")})
            for r in store.run_objects(ctx.conn, previous["id"], kind)
            if r.get("mailboxId") in rest
        ]

    never = len(ids) - len(chosen) - len(rest)
    message = (
        f"Checked {what} for {len(chosen)} of {plural(len(ids), 'mailbox')} this run ({limit} per run, "
        f"so each mailbox is checked about every {math.ceil(len(ids) / limit)} runs)"
    )
    if rest:
        oldest, newest = min(rest.values())[:10], max(rest.values())[:10]
        message += f"; showing earlier results for {plural(len(rest), 'mailbox')}, last checked " + (
            f"on {oldest}" if oldest == newest else f"between {oldest} and {newest}"
        )
    if never:
        message += f"; {plural(never, 'mailbox')} not checked yet"
    ctx.warn(message)
    return MailboxWindow(
        kind,
        [m for m in mailboxes if mailbox_id(m) in chosen],
        carried,
        len(rest),
        rest | dict.fromkeys(chosen, now),
        collector,
    )
