"""Mail flow: delivered, Junk, quarantined and failed mail for each of the tenant's domains, and the top spam
and phishing recipients.

Reads ``collectors/mail_flow.py``: per-domain, per-day counts from the message trace, kept since the tenant was
first watched (at most ``WINDOW_DAYS``), and Microsoft's top recipients summary for the last 30 days.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..collectors.mail_flow import DIRECTIONS, KEEP_DAYS, TOP_DAYS
from ..devices import parse_ts
from ..snapshot import TenantSnapshot
from ..text import day, plural
from .data_reports import DataReport, Rows, Section, Tile
from .mail_reports import _no_exchange
from .registry import report

NOT_COLLECTED = "Mail flow hasn't been collected yet. Refresh Mail flow on the client page."
WINDOW_DAYS = KEEP_DAYS
OUTCOMES = (("delivered", "Delivered"), ("junk", "Junk"), ("quarantined", "Quarantined"), ("failed", "Failed"))
DIRECTION_WORDS = {"in": "Received", "out": "Sent", "internal": "Between colleagues"}
FAILED_SHARE = 0.05  # a domain where more than this share of mail fails is worth a look

DOMAINS = "Mail per domain"
DAILY = "Mail per day"
SPAM = "Top spam recipients"
PHISH = "Top phishing recipients"


def _status(snap: TenantSnapshot, kind: str) -> dict:
    rows = snap.objects("mail_flow", kind) or []
    return rows[0] if rows and isinstance(rows[0], dict) else {}


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _share(part: int, whole: int) -> str:
    return f"{part / whole:.0%}" if whole else ""


def _counts(entry) -> dict:
    return entry if isinstance(entry, dict) else {}


@report(
    "mail-flow",
    "Mail flow by domain",
    "How much mail each domain sends and receives, how much was delivered, sent to Junk, quarantined or failed, "
    "and who gets the most spam and phishing.",
    ("mail_flow",),
    group="email",
    order=58,
    keywords="mail flow message trace delivered quarantined quarantine failed bounced junk spam phishing phish "
    "top recipients email volume domains",
    uses=("compromise",),
)
def mail_flow_report(snap: TenantSnapshot, now: datetime, out: DataReport) -> None:
    rows = snap.objects("mail_flow", "mail_flow")
    if rows is None:
        out.unavailable = NOT_COLLECTED
        return
    status = _status(snap, "mail_flow_status")
    if _no_exchange(snap) or status.get("reason") == "no_exchange":
        out.unavailable = "This tenant has no Exchange Online licence."
        return
    since = (now - timedelta(days=WINDOW_DAYS)).date().isoformat()
    read_to = parse_ts(status.get("readTo"))

    domains = Section(
        DOMAINS,
        ["Domain", "Received", "Sent", "Between colleagues", *(w for _, w in OUTCOMES), "Failed share"],
        empty="No mail was counted for this tenant's domains yet.",
        drifts=("Received", "Sent", "Between colleagues", *(w for _, w in OUTCOMES), "Failed share"),
    )
    daily = Section(
        DAILY,
        ["Date", "Domain", "Direction", *(w for _, w in OUTCOMES)],
        empty="No mail was counted yet.",
        compare=False,
    )
    totals = dict.fromkeys((k for k, _ in OUTCOMES), 0)
    messages = 0
    per_day = []
    for row in sorted(rows, key=lambda r: str(r.get("_id") or "")):
        name = str(row.get("_id") or "")
        by_direction = dict.fromkeys(DIRECTIONS, 0)
        by_outcome = dict.fromkeys((k for k, _ in OUTCOMES), 0)
        for date, entry in _counts(row.get("days")).items():
            if not isinstance(date, str) or date < since:
                continue
            for direction in DIRECTIONS:
                counts = _counts(_counts(entry).get(direction))
                if not counts:
                    continue
                by_direction[direction] += sum(_int(n) for n in counts.values())
                for key, _ in OUTCOMES:
                    by_outcome[key] += _int(counts.get(key))
                per_day.append((date, name, direction, counts))
        for key in totals:
            totals[key] += by_outcome[key]
        sent = sum(by_direction.values())  # pending mail too, which has no column of its own
        messages += sent
        failed_share = by_outcome["failed"] / sent if sent else 0
        domains.add(
            [
                name,
                by_direction["in"],
                by_direction["out"],
                by_direction["internal"],
                *(by_outcome[k] for k, _ in OUTCOMES),
                _share(by_outcome["failed"], sent),
            ],
            "medium" if failed_share > FAILED_SHARE else None,
        )
    # From the first day counted: after a gap in watching, Microsoft's trace can reach back before the tenant
    # was last watched from, and those days are in the totals.
    first = min((date for date, *_ in per_day), default=None)
    span = f"since {day(first)}" if first and first > since else f"in the last {WINDOW_DAYS} days"
    for date, name, direction, counts in sorted(
        per_day, key=lambda r: (r[0], r[1], DIRECTIONS.index(r[2])), reverse=True
    ):
        daily.add(
            [date, name, DIRECTION_WORDS[direction], *(_int(counts.get(k)) for k, _ in OUTCOMES)],
            "low" if _int(counts.get("failed")) else None,
        )

    top_status = _status(snap, "top_recipient_status")
    read = top_status.get("read") if isinstance(top_status.get("read"), dict) else {}
    top = [r for r in snap.objects("mail_flow", "top_recipient") or [] if isinstance(r, dict)]
    tops = {}
    for kind, title, column, word in (
        ("spam", SPAM, "Spam messages", "spam"),
        ("phish", PHISH, "Phishing messages", "phishing"),
    ):
        section = Section(
            title,
            ["Rank", "Recipient", column],
            empty=f"Microsoft found no {word} for anyone in the last {TOP_DAYS} days."
            if read.get(kind)
            else "Microsoft's top recipients summary wasn't available; see the Mail flow collection's log.",
            compare=False,
        )
        for r in sorted((r for r in top if r.get("kind") == kind), key=lambda r: _int(r.get("rank"))):
            section.add([_int(r.get("rank")), str(r.get("address") or ""), _int(r.get("count"))])
        tops[kind] = section

    domains.intro = (
        f"Mail counted for each of the tenant's domains {span}, one count per message and recipient: received "
        "from outside, sent outside, and between colleagues. Junk is mail delivered to the Junk Email folder; "
        "quarantined mail waits in the Defender portal's quarantine (Email & collaboration > Review > "
        "Quarantine); failed mail wasn't delivered (the sender gets a bounce). A domain where more than "
        f"{FAILED_SHARE:.0%} of mail fails is flagged: check its MX record and any connectors."
    )
    daily.intro = "The same counts for each day, newest first."
    tops["spam"].intro = (
        f"The people Microsoft caught the most spam for in the last {TOP_DAYS} days (its top 20). Someone near "
        "the top every month may have an address that's been widely published or leaked."
    )
    tops["phish"].intro = (
        f"The people Microsoft caught the most phishing for in the last {TOP_DAYS} days (its top 20). These are "
        "the people attackers target: make sure they have phishing-resistant MFA and know how to report a message."
    )
    traced = status.get("available") is not False
    if not traced:
        out.notice = "The message trace wasn't readable, so mail wasn't counted" + (
            f": {status['reason']}" if status.get("reason") else "."
        )
    if read_to:
        out.footnote = f"Counted up to {day(read_to.isoformat())}; the message trace runs about an hour behind."

    def counted(label: str, value, foot: str, tone: str | None, rows: Rows | None = None) -> Tile:
        """A figure from the message trace, or "Not read" without a tone when the trace wasn't readable."""
        if not traced:
            return Tile(label, "Not read", "The message trace wasn't readable", None, rows)
        return Tile(label, value, foot, tone, rows)

    out.tiles = [
        counted("Messages", messages, f"Once per recipient, {span}", None, Rows(DOMAINS)),
        counted("Delivered", _share(totals["delivered"], messages) or 0, plural(totals["delivered"], "message"), None),
        counted(
            "Quarantined",
            totals["quarantined"],
            "Held in quarantine",
            "warn" if totals["quarantined"] else "ok",
            Rows(DOMAINS),
        ),
        counted("Failed", totals["failed"], "Not delivered", "warn" if totals["failed"] else "ok", Rows(DOMAINS)),
        Tile(
            "Phishing recipients",
            len(tops["phish"].rows) if read.get("phish") else "Not read",
            f"Targeted in the last {TOP_DAYS} days",
            ("warn" if tops["phish"].rows else "ok") if read.get("phish") else None,
            Rows(PHISH),
        ),
    ]
    out.sections = [domains, tops["phish"], tops["spam"], daily]
