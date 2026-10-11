"""Automatic replies: which mailboxes answer every message with an out-of-office reply, who hears it, and
replies that look forgotten.

Reads ``collectors/auto_replies.py``. How long a reply has been on comes from Office Sentry's own collections
(the daily history once retention has removed their snapshots): the earliest collection in an unbroken run of
collections that found it on.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..devices import parse_ts
from ..snapshot import TenantSnapshot
from ..text import plural, verb
from .data_reports import DataReport, Rows, Section, Tile, _type
from .mail_reports import _no_exchange
from .registry import report

NOT_COLLECTED = "Automatic replies haven't been collected yet. Refresh Automatic replies on the client page."
FORGOTTEN_DAYS = 30

EVERY = "Mailboxes with an automatic reply"
OUTSIDE = {"all": "Everyone", "contactsOnly": "Contacts only", "none": "No"}
ON, SCHEDULED_ON, UPCOMING, ENDED = "On", "On (scheduled)", "Scheduled", "Schedule ended"


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _at(value) -> datetime | None:
    """A Graph dateTimeTimeZone as a UTC time. Graph gives it in the mailbox's own time zone, usually a Windows
    name Python can't read, so it's taken as UTC: at worst a few hours out, which a day-level report doesn't show."""
    if not isinstance(value, dict):
        return None
    when = parse_ts(value.get("dateTime"))
    return when.replace(tzinfo=UTC) if when else None


def state(row: dict, now: datetime) -> str:
    """On, On (scheduled), Scheduled (not started), Schedule ended, or the status itself if it's unknown."""
    status = _text(row.get("status"))
    if status == "alwaysEnabled":
        return ON
    if status != "scheduled":
        return status or "Unknown"
    start, end = _at(row.get("start")), _at(row.get("end"))
    if start and now < start:
        return UPCOMING
    if end and now >= end:
        return ENDED
    return SCHEDULED_ON


def _sending(value: str) -> bool:
    return value in (ON, SCHEDULED_ON)


def first_seen(snap: TenantSnapshot, now: datetime) -> dict[str, str]:
    """For each mailbox whose reply is sending now, the date of the earliest collection in the unbroken run of
    collections (newest first) that found it sending, read from the daily history once a snapshot has gone."""
    from .. import state_history  # here: the history imports the worker's schedule

    cutoff = snap.as_of or now.strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = snap.conn.execute(
        "SELECT id, finished_at FROM runs WHERE tenant_id = ? AND collector = 'auto_replies'"
        " AND status IN ('succeeded', 'partial') AND finished_at <= ? ORDER BY finished_at DESC, id DESC",
        (snap.tenant["id"], cutoff),
    ).fetchall()
    seen: dict[str, str] = {}
    open_: set[str] | None = None  # mailboxes still sending in every run read so far
    for run in runs:
        rows = state_history.objects_of(snap.conn, run["id"], "auto_reply")
        if not rows:
            break  # no longer held, or nothing on: the run stops here either way
        sending = {_text(r.get("mailboxId")) for r in rows if _sending(state(r, parse_ts(run["finished_at"]) or now))}
        open_ = sending if open_ is None else open_ & sending
        if not open_:
            break
        for mailbox in open_:
            seen[mailbox] = _text(run["finished_at"])[:10]
    return seen


@report(
    "auto-replies",
    "Automatic replies",
    "Mailboxes answering mail with an out-of-office reply, who outside hears it, and replies "
    "that have been on so long they look forgotten.",
    ("auto_replies", "users"),
    group="email",
    order=62,
    keywords="out of office ooo away holiday vacation automatic reply autoreply",
    uses=("offboarding",),
)
def auto_replies_report(snap: TenantSnapshot, now: datetime, out: DataReport) -> None:
    rows = snap.objects("auto_replies", "auto_reply")
    if rows is None:
        out.unavailable = NOT_COLLECTED
        return
    if _no_exchange(snap):
        out.unavailable = "This tenant has no Exchange Online licence."
        return
    users = {u["_id"]: u for u in snap.objects("users", "user") or []}
    seen = first_seen(snap, now)

    every = Section(
        EVERY,
        [
            "Mailbox",
            "Type",
            "State",
            "From",
            "Until",
            "Replies outside the organisation",
            "First seen on",
            "Account",
            "Reply to colleagues",
            "Reply to outside senders",
        ],
        empty="No mailbox has an automatic reply on or scheduled.",
    )
    sending = outside = forgotten = upcoming = 0
    entries = []
    for r in rows:
        current = state(r, now)
        mailbox_id = _text(r.get("mailboxId"))
        user = users.get(mailbox_id)
        audience = OUTSIDE.get(_text(r.get("externalAudience")), _text(r.get("externalAudience")) or "Unknown")
        since = seen.get(mailbox_id, "") if _sending(current) else ""
        since_at = parse_ts(since)
        long_on = bool(since_at and (now - since_at).days >= FORGOTTEN_DAYS)
        is_on = _sending(current)
        sending += is_on
        outside += is_on and audience != "No"
        upcoming += current == UPCOMING
        # A leaver's reply is meant to stay on; anyone else's on for a month may have been forgotten.
        flag = None
        if long_on and current == ON and (user is None or user.get("accountEnabled") is not False):
            forgotten += 1
            flag = "medium"
        start, end = (_at(r.get(k)) for k in ("start", "end"))
        status = _text(r.get("status"))
        entries.append(
            (
                (flag is None, not is_on, _text(r.get("mailbox")).lower()),
                [
                    r.get("mailbox"),
                    _type(r.get("mailboxType")),
                    current,
                    start.date().isoformat() if start and status == "scheduled" else "",
                    end.date().isoformat() if end and status == "scheduled" else "",
                    audience if is_on or current == UPCOMING else "",
                    since,
                    "Not found in the directory"
                    if user is None
                    else ("Active" if user.get("accountEnabled") is not False else "Sign-in blocked"),
                    _text(r.get("internalText")),
                    _text(r.get("externalText")) if audience != "No" else "",
                ],
                flag,
            )
        )
    for _, row, flag in sorted(entries, key=lambda e: e[0]):
        every.add(row, flag)

    checked = snap.metric("mail.auto_reply_mailboxes_checked")
    every.intro = (
        f"{plural(sending, 'mailbox')} {verb(sending, 'is', 'are')} answering mail with an automatic "
        f"reply now, {outside} of them to people outside the organisation too."
        + (f" Checked {plural(int(checked), 'mailbox')}." if checked else "")
        + f" Replies on for over {FORGOTTEN_DAYS} days on an active account are flagged: they're "
        "often left on after someone is back. Turn one off in Outlook (Settings > Mail > Automatic "
        "replies) or, as an admin, in the Exchange admin centre (Recipients > Mailboxes > the mailbox > "
        "Others > Automatic replies)."
    )
    out.tiles = [
        Tile(
            "Replying now",
            sending,
            "Mailboxes sending an automatic reply",
            rows=Rows(EVERY, {"State": [ON, SCHEDULED_ON]}),
        ),
        Tile(
            "Replying outside the organisation",
            outside,
            "To everyone or to contacts",
            rows=Rows(EVERY, {"Replies outside the organisation": ["Everyone", "Contacts only"]}),
        ),
        Tile(
            f"On for over {FORGOTTEN_DAYS} days",
            forgotten,
            "Active accounts, possibly forgotten",
            "warn" if forgotten else "ok",
            Rows(EVERY, flagged=True),
        ),
        Tile("Scheduled to start", upcoming, "Replies set for later", rows=Rows(EVERY, {"State": UPCOMING})),
    ]
    out.footnote = (
        "First seen on is the earliest of Office Sentry's collections, in an unbroken run, that found "
        "the reply on; it can't go back further than the snapshots kept."
    )
    out.sections = [every]
