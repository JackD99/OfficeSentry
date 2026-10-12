"""Counted nouns for summaries people read: "1 admin", "3 admins", never "3 admin(s)"."""

from __future__ import annotations

import unicodedata

# Words whose plural doesn't follow the endings below.
IRREGULAR = {
    "person": "people",
    "mailbox": "mailboxes",
    "policy": "policies",
    "library": "libraries",
    "child": "children",
    "team or group": "teams and groups",
    "file or folder": "files and folders",
}


def nouns(n: int, noun: str, many: str | None = None) -> str:
    """The noun to go with ``n``: ``nouns(2, "policy")`` is "policies". ``many`` overrides the plural."""
    if n == 1:
        return noun
    if many:
        return many
    if noun in IRREGULAR:
        return IRREGULAR[noun]
    # Only the last word changes: "mail domain" -> "mail domains", "app registration" -> "app registrations".
    head, _, last = noun.rpartition(" ")
    lead = head + " " if head else ""
    if last.endswith("y") and last[-2:-1] not in "aeiou":
        return lead + last[:-1] + "ies"
    if last.endswith(("s", "x", "z", "ch", "sh")):
        return lead + last + "es"
    return lead + last + "s"


def plural(n: int, noun: str, many: str | None = None) -> str:
    """``plural(3, "admin")`` is "3 admins"; ``plural(1, "admin")`` is "1 admin"."""
    return f"{n} {nouns(n, noun, many)}"


def verb(n: int, one: str, many: str) -> str:
    """The verb that agrees with ``n``: ``verb(n, "is", "are")``."""
    return one if n == 1 else many


# Finding subjects for settings that apply to the whole tenant rather than one account.
WHOLE_ORGANISATION = ("Tenant", "Organisation")


def subject_label(subject: str) -> str:
    """A finding's subject as a client reads it: "Whole organisation" rather than "Tenant"."""
    return "Whole organisation" if subject in WHOLE_ORGANISATION else subject


def day(iso: str | None) -> str:
    """ "1 Oct 2026" for an ISO date or timestamp, for text people read; "?" when there's none."""
    if not iso or len(iso) < 10 or not iso[:4].isdigit():
        return iso or "?"
    from datetime import date

    try:
        d = date.fromisoformat(iso[:10])
    except ValueError:
        return iso
    return f"{d.day} {d:%b %Y}"


def every(n: int, noun: str, many: str | None = None, start: bool = False) -> str:
    """ "all 4 domains", or "the 1 domain" when there's only one. ``start`` capitalises it to open a sentence."""
    text = f"the 1 {noun}" if n == 1 else f"all {n} {nouns(n, noun, many)}"
    return text[0].upper() + text[1:] if start else text


def duration(seconds: float) -> str:
    """How long, roughly, for text people read: "40 seconds", "12 minutes", "5 hours", "3 days"."""
    seconds = max(0, int(seconds))
    for unit, size, below in (("second", 1, 60), ("minute", 60, 3600), ("hour", 3600, 2 * 86400)):
        if seconds < below:
            return plural(seconds // size, unit)
    return plural(seconds // 86400, "day")


def fold(text: str) -> str:
    """``text`` as it sorts: without case or accents, so "Åsa" comes before "Bea" rather than after "Zoe"."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).casefold()
