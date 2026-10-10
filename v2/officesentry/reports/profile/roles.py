"""Admin roles."""

from __future__ import annotations

from ..data_reports import Section
from .base import Part, Person, part


@part(30)
def roles_part(p: Person) -> Part:
    result = Part("roles", "Admin roles")
    rows = p.snap.objects("admin_roles", "role_assignment")
    if rows is None:
        result.unavailable = "Admin roles haven't been collected yet. Refresh Admin roles on the tenant page."
        return result
    mine = [r for r in rows if r.get("principalId") == p.id]
    if not mine:
        result.summary, result.empty = "None", "Holds no admin roles."
        return result
    table = Section("Admin roles", ["Role", "How assigned", "Scope", "Privileged"])
    for r in sorted(mine, key=lambda r: (not r.get("privileged"), r.get("assignment") != "active",
                                         r.get("roleName") or "")):
        table.add([r.get("roleName") or "Unknown role",
                   "Active" if r.get("assignment") == "active" else "Eligible (PIM)",
                   "Whole directory" if r.get("directoryScopeId") in (None, "/") else "Limited scope",
                   bool(r.get("privileged"))])
    result.tables = [table]
    names = list(dict.fromkeys(r.get("roleName") or "Unknown role" for r in mine))
    result.summary = names[0] if len(names) == 1 else f"{names[0]} and {len(names) - 1} more"
    result.tone = "warn" if any(r.get("roleName") == "Global Administrator" for r in mine) else None
    privileged = sorted({r.get("roleName") for r in mine if r.get("privileged") and r.get("roleName")})
    if not p.enabled:
        p.flag("high", f"Sign-in is blocked but the account still holds {', '.join(names)}. Remove the roles.",
               "roles")
    elif p.guest and privileged:
        p.flag("high", f"A guest from another organisation holds {', '.join(privileged)}.", "roles")
    return result
