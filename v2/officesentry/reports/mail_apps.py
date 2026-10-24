"""Outlook and mail apps: which Outlook each person connects with, who is on a version Microsoft no longer supports,
and who uses POP, IMAP or SMTP apps.

Reads the per-person usage rows (``collectors/usage.py``, from Microsoft's email apps report for the
``ACTIVE_DAYS`` days to its refresh date), so nothing here calls Microsoft. Support is told by the year in an
Outlook version's name (``usage.OUTLOOK_SUPPORT_ENDS``), on the day the report is built, so a version moves to
unsupported on its own when its day comes.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime

from .. import usage
from ..devices import parse_day
from ..snapshot import TenantSnapshot
from ..usage import ACTIVE_DAYS, MAIL_APPS, OUTLOOK_SUPPORT_ENDS, PROTOCOLS
from .data_reports import DataReport, Rows, Section, Tile
from .identity_reports import _day
from .registry import report
from .usage_reports import NO_PERMISSION, NOT_COLLECTED

VERSIONS = "Outlook versions"
UNSUPPORTED = "People on an unsupported Outlook"
PROTOCOL_USERS = "POP, IMAP and SMTP apps"
EVERYONE = "Everyone's mail apps"
LAST_EMAIL = "Last used email"
NOT_READ = (
    "Microsoft's email apps report couldn't be read at the last collection, so the mail apps people use aren't "
    'known. See the "Microsoft 365 usage" collection\'s log.'
)
NOT_YET = 'Mail apps haven\'t been collected yet. Refresh "Microsoft 365 usage" on the client page.'
UNKNOWN = "Undetermined"  # Microsoft's word for a version it couldn't tell
NAMES = dict(MAIL_APPS)
PLATFORM = {"windows": "Windows", "mac": "Mac"}


def _version(name: str) -> str:
    return "Version not known" if name == UNKNOWN else name


def _support(name: str, today) -> tuple[str, str | None]:
    """How an Outlook version stands with Microsoft, and the row's flag."""
    ends = usage.outlook_support_ends(name)
    if ends is not None:
        if ends <= today:
            return f"Ended {_day(ends)}", "high"
        return f"Supported until {_day(ends)}", None
    if name == UNKNOWN:
        return "Not known", None
    return ("Supported, kept up to date" if "M365" in name else "Not known"), None


def _used(apps: dict, keys) -> list[str]:
    return [NAMES[k] for k in keys if apps.get(k)]


def _lifecycle(today: date) -> str:
    """The Outlook releases whose support ended last, and the next to end, as of ``today``."""
    by_day: dict[date, list[int]] = {}
    for year, ends in sorted(OUTLOOK_SUPPORT_ENDS.items()):
        by_day.setdefault(ends, []).append(year)

    def names(day: date) -> str:
        return "Outlook " + " and ".join(str(y) for y in by_day[day])

    ended = [d for d in by_day if d <= today]
    coming = [d for d in by_day if d > today]
    words = []
    if ended:
        words.append(f"{names(max(ended))} reached the end of support on {_day(max(ended))}")
    if coming:
        nxt = min(coming)
        words.append(f"{names(nxt)} {'reach' if len(by_day[nxt]) > 1 else 'reaches'} it on {_day(nxt)}")
    return ", and ".join(words)


@report(
    "mail-apps",
    "Outlook and mail apps",
    "Which Outlook versions people connect with, who is on one Microsoft no longer supports, and who uses POP, IMAP "
    "or SMTP apps.",
    ("m365_usage",),
    group="email",
    order=56,
    keywords="outlook 2016 2019 2021 versions old unsupported end of support desktop mac mobile web clients pop imap "
    "smtp auth thunderbird apple mail printers scanners",
)
def mail_apps(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    status = usage.status(snap)
    if status is None:
        report.unavailable = NOT_COLLECTED
        return
    if not status.get("available"):
        report.unavailable = NO_PERMISSION
        return
    read = (status.get("read") or {}).get("mail_apps")
    if read is None:  # collected before Office Sentry read the email apps report
        report.unavailable = NOT_YET
        return
    if read is False:
        report.unavailable = NOT_READ
        return
    if status.get("concealed"):
        report.notice = usage.CONCEALED_NOTICE
    today = now.date()
    people = sorted(
        ((usage.label(r), r) for r in usage.people(snap).values() if any((r.get("mailApps") or {}).values())),
        key=lambda p: p[0].lower(),
    )

    versions: Counter[tuple[str, str]] = Counter()
    for _, r in people:
        for key in PLATFORM:
            for v in r["mailApps"].get(key) or []:
                versions[(key, v)] += 1
    table = Section(
        VERSIONS,
        ["Outlook", "On", "People", "Support"],
        intro=f"Each Outlook for Windows and Mac release people connected with in the last {ACTIVE_DAYS} days. "
        "Microsoft no longer fixes security problems in an Outlook past its end of support. A version Microsoft "
        "couldn't tell shows as not known.",
        empty=f"Nobody connected with Outlook for Windows or Mac in the last {ACTIVE_DAYS} days.",
    )
    ordered = sorted(
        versions.items(),
        key=lambda kv: (usage.outlook_support_ends(kv[0][1]) is None, -kv[1], kv[0][1], kv[0][0]),
    )
    for (key, v), n in ordered:
        words, flag = _support(v, today)
        table.add([_version(v), PLATFORM[key], n, words], flag)

    old = Section(
        UNSUPPORTED,
        ["Person", "Outlook", "Support ended", "Licence includes desktop Outlook", LAST_EMAIL],
        intro="People who connected with an Outlook Microsoft no longer supports. Those whose licence includes the "
        "desktop apps can install Microsoft 365's Outlook at no extra cost; the others need a licence with them, "
        "or can use Outlook on the web.",
        empty="Nobody is on an unsupported Outlook.",
        drifts=(LAST_EMAIL,),
    )
    protocols = Section(
        PROTOCOL_USERS,
        ["Person", "Uses", "Also uses", LAST_EMAIL],
        intro="People whose mail is fetched with POP or IMAP, or sent with SMTP, rather than through Outlook: often "
        "a phone's own mail app, Thunderbird, or a printer, scanner or app sending as that person. Ask what each "
        "is; Legacy sign-ins shows whether any sign in with a password alone, which can't do MFA.",
        empty=f"Nobody used POP, IMAP or SMTP in the last {ACTIVE_DAYS} days.",
        drifts=(LAST_EMAIL,),
    )
    everyone = Section(
        EVERYONE,
        [
            "Person",
            "Outlook for Windows",
            "Outlook for Mac",
            "Outlook mobile",
            "Outlook on the web",
            "Other apps",
            LAST_EMAIL,
        ],
        intro=f"Every mail app each person connected with in the last {ACTIVE_DAYS} days, with the Outlook "
        "version on Windows and Mac.",
        empty=f"Nobody connected with a mail app in the last {ACTIVE_DAYS} days.",
        drifts=(LAST_EMAIL,),
    )
    desktop = current = 0
    for name, r in people:
        apps = r["mailApps"]
        last = (r.get("lastActive") or {}).get("exchange")
        last_text = _day(parse_day(last)) if last else ""
        unsupported = usage.old_outlook(r, today)
        if unsupported:
            ends = min(e for v in unsupported if (e := usage.outlook_support_ends(v)))
            old.add(
                [name, unsupported, _day(ends), "Yes" if usage.desktop_licensed(r) else "No", last_text],
                "high",
            )
        used = [p for p in PROTOCOLS if apps.get(p)]
        if used:
            others = [k for k, _ in MAIL_APPS if k not in PROTOCOLS and apps.get(k)]
            protocols.add([name, _used(apps, used), _used(apps, others), last_text], "medium")
        if apps.get("windows") or apps.get("mac"):
            desktop += 1
            current += any("M365" in v for k in PLATFORM for v in apps.get(k) or [])
        everyone.add(
            [
                name,
                [_version(v) for v in apps.get("windows") or []],
                [_version(v) for v in apps.get("mac") or []],
                "Yes" if apps.get("mobile") else "",
                "Yes" if apps.get("web") else "",
                _used(apps, ("macMail", "otherMobile", *PROTOCOLS)),
                last_text,
            ],
            "high" if unsupported else None,
        )
    report.sections = [table, old, protocols, everyone]

    report.tiles = [
        Tile(
            "Unsupported Outlook",
            len(old.rows),
            "People on an Outlook Microsoft no longer supports",
            "bad" if old.rows else "ok",
            Rows(UNSUPPORTED),
        ),
        Tile(
            "POP, IMAP or SMTP",
            len(protocols.rows),
            f"People, last {ACTIVE_DAYS} days",
            "warn" if protocols.rows else "ok",
            Rows(PROTOCOL_USERS),
        ),
        Tile(
            "Desktop Outlook",
            desktop,
            f"{current} on Microsoft 365's, kept up to date" if desktop else "Nobody on Windows or Mac",
            rows=Rows(EVERYONE),
        ),
        Tile("Mail app users", len(people), f"Connected in the last {ACTIVE_DAYS} days", rows=Rows(EVERYONE)),
    ]
    if status.get("reportDate"):
        report.footnote = (
            f"From Microsoft's email apps report: the {ACTIVE_DAYS} days to {_day(status['reportDate'])}, the latest "
            "date Microsoft had processed. Support dates are Microsoft's lifecycle dates for each Outlook release: "
            f"{_lifecycle(today)}. People connecting from a shared or service account show under that account."
        )
