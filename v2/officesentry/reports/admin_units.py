"""Administrative units: what each unit holds, who administers it, and the units nobody looks after.

For an MSP checking how a larger client delegates administration: a branch or a region as a unit, with
helpdesk or user admins scoped to it instead of the whole tenant. Every unit with its membership (assigned
or dynamic, and whether the rule is paused), whether it is a restricted management unit, how many users,
groups and devices are in it and which admins are scoped to it; every scoped admin, flagged when the account
is blocked or a guest; and the members of each unit by name. Built from what Administrative units, Admin
roles, Users and Groups and Teams store. Nothing here calls Microsoft.
"""

from __future__ import annotations

from datetime import datetime

from ..collectors.admin_units import UNIT_LIMIT
from ..snapshot import TenantSnapshot
from ..text import plural
from .data_reports import DataReport, Rows, Section, Tile
from .registry import report

UNITS = "Every unit"
ADMINS = "Scoped admins"
MEMBERS = "Members"
SCOPE_PREFIX = "/administrativeUnits/"
ALLOWED, BLOCKED = "Allowed", "Blocked"
ADMINS_SHOWN = 20  # admin names listed per unit; the count is always complete
MEMBER_ROWS = 5000  # member rows shown; a tenant with more sees a note
PROCESSING = {"On": "Dynamic", "Paused": "Dynamic (paused)"}

NOT_COLLECTED = "Administrative units haven't been collected yet. Refresh Administrative units on the client page."
NO_UNITS = "The tenant has no administrative units: every admin role applies to the whole tenant."
NO_ROLES = "Scoped admins show after the next collection of Admin roles."


def _label(u: dict | None, fallback: str = "") -> str:
    u = u or {}
    return u.get("userPrincipalName") or u.get("mail") or u.get("displayName") or fallback


def _sign_in(account: dict | None) -> str:
    if account is None:
        return "Not known"
    return ALLOWED if account.get("accountEnabled") is True else BLOCKED


def _membership(unit: dict) -> str:
    if unit.get("dynamic"):
        return PROCESSING.get(unit.get("ruleState"), "Dynamic")
    return "Assigned"


def scoped_assignments(snap: TenantSnapshot) -> dict[str, list[dict]] | None:
    """Role assignments scoped to an administrative unit, by unit ID. None before Admin roles was collected."""
    rows = snap.objects("admin_roles", "role_assignment")
    if rows is None:
        return None
    out: dict[str, list[dict]] = {}
    for r in rows:
        scope = r.get("directoryScopeId")
        if isinstance(scope, str) and scope.startswith(SCOPE_PREFIX) and len(scope) > len(SCOPE_PREFIX):
            out.setdefault(scope[len(SCOPE_PREFIX) :], []).append(r)
    return out


@report(
    "admin-units",
    "Administrative units",
    "Every administrative unit with its members and the admins scoped to it, restricted units, and units "
    "with nobody in them or nobody administering them.",
    ("admin_units", "admin_roles", "users", "groups"),
    group="identity",
    order=35,
    keywords="administrative units AU delegated administration scoped admins restricted management branch "
    "region department helpdesk user administrator",
)
def build(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    units = snap.objects("admin_units", "admin_unit")
    status = (snap.objects("admin_units", "admin_unit_status") or [None])[0]
    if isinstance(status, dict) and status.get("available") is False:
        reason = status.get("reason")
        report.unavailable = (
            "Administrative units couldn't be read on the last collection"
            + (f": {reason}" if isinstance(reason, str) and reason else "")
            + ". The collection log on the client page has the detail."
        )
        return
    if units is None:
        report.unavailable = NOT_COLLECTED
        return
    units = [u for u in units if isinstance(u.get("_id"), str)]
    scoped = scoped_assignments(snap)
    if scoped is None:
        report.notes.append(NO_ROLES)
    users = snap.objects("users", "user")
    groups = snap.objects("groups", "group")
    if units and users is None:
        report.notes.append("Member names show after the next collection of Users.")
    people = {u["_id"]: u for u in users or [] if isinstance(u.get("_id"), str)}
    by_group = {g["_id"]: g for g in groups or [] if isinstance(g.get("_id"), str)}

    unread = [u for u in units if not u.get("examined")]
    if unread and len(units) > UNIT_LIMIT:
        report.notes.append(
            f"Members were read for the first {UNIT_LIMIT:,} administrative units, not the other {len(unread):,}."
        )
    elif unread:
        report.notes.append(
            f"Members of {plural(len(unread), 'unit')} couldn't be read. The collection log on the client page "
            "says why."
        )

    def count(unit: dict, key: str) -> object:
        value = unit.get(key)
        return value if unit.get("examined") and isinstance(value, int) else "Not read"

    def admin_name(r: dict) -> str:
        return r.get("displayName") or r.get("userPrincipalName") or r.get("principalId") or "Unknown"

    every = Section(
        UNITS,
        ["Unit", "Membership", "Restricted", "Users", "Groups", "Devices", "Scoped admins", "Admins"],
        intro="Every administrative unit. A unit with nobody in it, or with no admin scoped to it, usually "
        "isn't doing anything; a dynamic unit whose rule is paused is no longer kept up to date. A restricted "
        "management unit also keeps tenant-wide admins (other than Global Administrators) from changing its members.",
        empty=NO_UNITS,
        filter_columns=("Membership", "Restricted"),
    )
    rows = []
    for unit in units:
        admins = scoped.get(unit["_id"], []) if scoped is not None else []
        accounts = sorted({admin_name(r) for r in admins}, key=str.lower)
        flag = None
        if unit.get("dynamic") and unit.get("ruleState") == "Paused":
            flag = "medium"
        elif unit.get("examined") and unit.get("memberCount") == 0:
            flag = "low"
        elif scoped is not None and not admins:
            flag = "low"
        name = unit.get("displayName") or "Unnamed unit"
        rows.append(
            (
                str(name).lower(),
                [
                    name,
                    _membership(unit),
                    bool(unit.get("restricted")),
                    count(unit, "userCount"),
                    count(unit, "groupCount"),
                    count(unit, "deviceCount"),
                    len(accounts) if scoped is not None else "Not checked",
                    accounts[:ADMINS_SHOWN]
                    + ([f"and {len(accounts) - ADMINS_SHOWN} more"] if len(accounts) > ADMINS_SHOWN else []),
                ],
                flag,
            )
        )
    for _, row, flag in sorted(rows, key=lambda r: r[0]):
        every.add(row, flag)

    names = {u["_id"]: u.get("displayName") or "Unnamed unit" for u in units}
    scoped_admins = Section(
        ADMINS,
        ["Account", "Name", "Role", "Unit", "Assignment", "Sign-in", "Type"],
        intro="Every admin role scoped to a unit rather than the whole tenant. An account whose sign-in is "
        "blocked has usually left: remove the role. A guest or an app with a scoped role deserves a look.",
        empty=("No admin role is scoped to a unit." if units else NO_UNITS) if scoped is not None else NO_ROLES,
        filter_columns=("Role", "Unit", "Sign-in"),
    )
    rows = []
    for uid, assignments in (scoped or {}).items():
        if uid not in names:
            continue  # a unit deleted since, or outside what was read
        for r in assignments:
            account = people.get(r.get("principalId"))
            kind = r.get("principalType")
            if kind == "servicePrincipal":
                type_, sign_in, flag = "App", "", "low"
            elif kind == "group":  # a role-assignable group: its members hold the role
                type_, sign_in, flag = "Group", "", None
                group = by_group.get(r.get("principalId")) or {}
                account = {"userPrincipalName": group.get("mail"), "displayName": group.get("displayName")}
            else:
                guest = (account or {}).get("userType") == "Guest" or r.get("userType") == "Guest"
                enabled = account.get("accountEnabled") if account else r.get("accountEnabled")
                type_ = "Guest" if guest else "Member"
                sign_in = ALLOWED if enabled is True else (BLOCKED if enabled is False else "Not known")
                flag = "high" if guest else ("medium" if enabled is False else None)
            label = _label(account) or r.get("userPrincipalName") or admin_name(r)
            rows.append(
                (
                    (flag is None, str(label).lower(), names[uid].lower()),
                    [
                        label,
                        (account or {}).get("displayName") or r.get("displayName") or "",
                        r.get("roleName") or "Unknown role",
                        names[uid],
                        "Active" if r.get("assignment") == "active" else "Eligible (PIM)",
                        sign_in,
                        type_,
                    ],
                    flag,
                )
            )
    for _, row, flag in sorted(rows, key=lambda r: r[0]):
        scoped_admins.add(row, flag)

    members = Section(
        MEMBERS,
        ["Unit", "Member", "Name", "Type", "Sign-in"],
        intro="The users and groups in each unit. Devices are counted above but not listed.",
        empty="No unit has any users or groups in it." if units else NO_UNITS,
        filter_columns=("Unit", "Type"),
    )
    rows = []
    for unit in sorted(units, key=lambda u: str(u.get("displayName") or "").lower()):
        unit_name = names[unit["_id"]]
        for uid in unit.get("userIds") or []:
            if not isinstance(uid, str):
                continue
            account = people.get(uid)
            kind = "Guest" if (account or {}).get("userType") == "Guest" else "User"
            rows.append(
                [unit_name, _label(account, uid), (account or {}).get("displayName") or "", kind, _sign_in(account)]
            )
        for gid in unit.get("groupIds") or []:
            if not isinstance(gid, str):
                continue
            g = by_group.get(gid) or {}
            rows.append(
                [unit_name, g.get("mail") or g.get("displayName") or gid, g.get("displayName") or "", "Group", ""]
            )
    if len(rows) > MEMBER_ROWS:
        report.notes.append(
            f"Members lists the first {MEMBER_ROWS:,} of {len(rows):,} members. The counts in Every unit are complete."
        )
    for row in rows[:MEMBER_ROWS]:
        members.add(row)
    report.sections = [every, scoped_admins, members]

    examined = [u for u in units if u.get("examined")]
    empty = sum(1 for u in examined if u.get("memberCount") == 0)
    paused = sum(1 for u in units if u.get("dynamic") and u.get("ruleState") == "Paused")
    restricted = sum(1 for u in units if u.get("restricted"))
    dynamic = sum(1 for u in units if u.get("dynamic"))
    unadministered = sum(1 for u in units if scoped is not None and not scoped.get(u["_id"]))
    admin_accounts = {r.get("principalId") for rows_ in (scoped or {}).values() for r in rows_}
    foot = f"{plural(dynamic, 'dynamic unit')}, {restricted} restricted"
    if paused:
        foot += f", {plural(paused, 'rule')} paused"
    report.tiles = [
        Tile("Units", len(units), foot, "warn" if paused else None, Rows(UNITS)),
        Tile(
            "Scoped admins",
            len(admin_accounts) if scoped is not None else "Not checked",
            f"Across {plural(len({u for u, rows_ in (scoped or {}).items() if rows_ and u in names}), 'unit')}"
            if scoped
            else "Admin roles scoped to a unit",
            rows=Rows(ADMINS) if scoped is not None else None,
        ),
        Tile(
            "Empty units",
            empty if examined or not units else "Not read",
            "No users, groups or devices in them",
            ("warn" if empty else "ok") if units else None,
            Rows(UNITS, {"Users": 0, "Groups": 0, "Devices": 0}) if examined else None,
        ),
        Tile(
            "No scoped admin",
            unadministered if scoped is not None else "Not checked",
            "Units no admin role is scoped to",
            ("warn" if unadministered else "ok") if units and scoped is not None else None,
            Rows(UNITS, {"Scoped admins": 0}) if scoped is not None else None,
        ),
    ]
    report.footnote = (
        "An administrative unit scopes an admin role to the users, groups and devices in it. Scoped admins "
        "come from the role assignments Admin roles reads; roles assigned through PIM show as eligible."
    )
