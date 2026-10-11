"""Administrative units the person is in, and the ones they administer."""

from __future__ import annotations

from ..admin_units import scoped_assignments
from ..data_reports import Section
from .base import Part, Person, part


@part(35)
def admin_units_part(p: Person) -> Part:
    result = Part("admin_units", "Administrative units")
    units = p.snap.objects("admin_units", "admin_unit")
    if units is None:
        result.unavailable = (
            "Administrative units haven't been collected yet. Refresh Administrative units on the client page."
        )
        return result
    units = [u for u in units if isinstance(u.get("_id"), str)]
    if not units:
        result.summary, result.empty = "None", "The tenant has no administrative units."
        return result
    scoped = scoped_assignments(p.snap) or {}
    names = {u["_id"]: u.get("displayName") or "Unnamed unit" for u in units}
    member_of = [u for u in units if p.id in (u.get("userIds") or [])]
    administers = {
        uid: sorted({r.get("roleName") or "Unknown role" for r in rows if r.get("principalId") == p.id})
        for uid, rows in scoped.items()
        if uid in names and any(r.get("principalId") == p.id for r in rows)
    }
    if not member_of and not administers:
        result.summary, result.empty = "None", "In no administrative unit, and administers none."
        return result
    table = Section("Administrative units", ["Unit", "Relationship", "Membership", "Restricted"])
    for uid, roles in sorted(administers.items(), key=lambda kv: names[kv[0]].lower()):
        unit = next(u for u in units if u["_id"] == uid)
        table.add([names[uid], f"Administers as {', '.join(roles)}", _membership(unit), bool(unit.get("restricted"))])
    for unit in sorted(member_of, key=lambda u: names[u["_id"]].lower()):
        table.add([names[unit["_id"]], "Member", _membership(unit), bool(unit.get("restricted"))])
    result.tables = [table]
    parts = []
    if administers:
        parts.append(f"Administers {len(administers)}")
    if member_of:
        parts.append(f"In {len(member_of)}")
    result.summary = ", ".join(parts)
    if administers and not p.enabled:
        held = ", ".join(sorted(names[uid] for uid in administers))
        p.flag(
            "medium", f"Sign-in is blocked but the account still administers {held}. Remove the roles.", "admin_units"
        )
    return result


def _membership(unit: dict) -> str:
    if not unit.get("dynamic"):
        return "Assigned"
    return "Dynamic (paused)" if unit.get("ruleState") == "Paused" else "Dynamic"
