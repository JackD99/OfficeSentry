"""The Autodesk products a person holds a seat of, and when they last used each (``reports/autodesk.py``).

Left out of the page unless Autodesk licences are read for the person's client. Matched by the person's sign-in
name and address, as the report matches them.
"""

from __future__ import annotations

from ...autodesk import setup
from ..autodesk import DAYS, used_since
from ..data_reports import Section
from .base import Part, Person, part


@part(12)
def autodesk_part(p: Person) -> Part | None:
    snap = p.snap
    if not setup.applies(snap.conn, snap.tenant):
        return None
    seats = snap.objects("autodesk", "autodesk_seat")
    if seats is None:
        return None
    teams = snap.objects("autodesk", "autodesk_team") or []
    usage_read = bool(teams) and all(t.get("usage") == "read" for t in teams if isinstance(t, dict))
    mine = sorted(
        (s for s in seats if isinstance(s, dict) and p.named(s.get("person"))),
        key=lambda s: str(s.get("product") or "").lower(),
    )
    result = Part("autodesk", "Autodesk")
    if not mine:
        result.summary, result.empty = "None", "Holds no Autodesk seat."
        return result
    since = used_since(snap, p.now)
    table = Section("Autodesk", ["Product", "Last used", DAYS, "Assigned on", "Subscription"])
    unused = []
    for s in mine:
        last = str(s.get("lastUsed") or "")
        idle = usage_read and last < since
        if idle:
            unused.append(str(s.get("product") or "a product"))
        table.add(
            [s.get("product") or "", last, s.get("daysUsed"), s.get("assignedOn") or "", s.get("subscription") or ""],
            "medium" if idle else None,
        )
    result.tables = [table]
    result.summary = f"{len(mine)} product" + ("s" if len(mine) != 1 else "")
    if unused:
        result.summary += f", {len(unused)} not used lately"
        result.tone = "warn"
    if not p.enabled:
        held = ", ".join(str(s.get("product") or "") for s in mine)
        p.flag(
            "medium",
            f"Sign-in is blocked but the account still holds Autodesk seats ({held}). Take them back in Autodesk "
            "Account.",
            "autodesk",
        )
    return result
