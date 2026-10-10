"""All users: every account in one plain list, with department, job title, manager and licences.

The directory an MSP is asked for ("send us a list of everyone and their licences"), and the org chart's
loose ends: accounts that report to someone whose sign-in is blocked, and members with no manager. Built
from what Users, Departments and managers, and Tenant profile store. Nothing here calls Microsoft.
"""

from __future__ import annotations

from datetime import datetime

from ..licences import last_sign_in, sku_name
from ..snapshot import TenantSnapshot
from ..text import plural
from .data_reports import DataReport, Rows, Section, Tile, _date
from .registry import report

EVERY = "Every account"
MANAGERS = "Managers"
DEPARTMENTS = "Departments"
NO_DEPARTMENT = "No department"
ALLOWED, BLOCKED = "Allowed", "Blocked"
REPORTS_SHOWN = 50  # names listed per manager; the count is always complete

NOT_COLLECTED = "User data hasn't been collected yet. Refresh Users on the tenant page."
NO_DETAILS = ("Departments, job titles and managers haven't been collected yet. Refresh Departments and managers "
              "on the tenant page.")


def _label(u: dict) -> str:
    return u.get("userPrincipalName") or u.get("mail") or u.get("displayName") or u.get("_id") or ""


def _sign_in(u: dict) -> str:
    if not u.get("signInActivityAvailable"):
        return "Not checked"
    last = last_sign_in(u)
    return last.date().isoformat() if last else "Never"


@report(
    "all-users",
    "All users",
    "Every account with its department, job title, manager, licences and last sign-in, its managers and "
    "departments, and accounts reporting to a blocked manager.",
    ("users", "user_details", "tenant_profile"),
    group="identity",
    order=5,
    uses=("offboarding",),
    keywords="directory everyone staff list employees export department job title manager org chart reports to "
             "direct reports licences",
)
def build(snap: TenantSnapshot, now: datetime, report: DataReport) -> None:
    users = snap.objects("users", "user")
    if users is None:
        report.unavailable = NOT_COLLECTED
        return
    details = snap.objects("user_details", "person")
    if details is None:
        report.notes.append(NO_DETAILS)
    people = {d["_id"]: d for d in details or [] if isinstance(d.get("_id"), str)}
    skus = snap.objects("tenant_profile", "subscribed_sku")
    names = {s["skuId"]: sku_name(s.get("skuPartNumber")) for s in skus or [] if isinstance(s.get("skuId"), str)}
    if skus is None:
        report.notes.append("Licence names show after the next collection of Tenant profile and licences.")
    by_id = {u["_id"]: u for u in users if isinstance(u.get("_id"), str)}

    def manager_of(uid: str) -> dict | None:
        m = (people.get(uid) or {}).get("manager")
        return m if isinstance(m, dict) and m.get("id") else None

    def manager_blocked(m: dict) -> bool:
        account = by_id.get(m["id"])
        enabled = account.get("accountEnabled") if account else m.get("enabled")
        return enabled is False

    every = Section(EVERY, ["Account", "Name", "Type", "Sign-in", "Department", "Job title", "Manager", "Licences",
                            "Source", "Created", "Last sign-in"],
                    intro="Every member and guest account. Accounts that report to someone whose sign-in is blocked "
                          "are flagged: the manager has usually left, so approvals and HR records go nowhere.",
                    empty="The tenant has no accounts.")
    reports: dict[str, list[str]] = {}
    known: dict[str, dict] = {}  # each manager as the accounts reporting to them see it
    rows = []
    for u in users:
        uid = u.get("_id")
        d = people.get(uid) or {}
        enabled = u.get("accountEnabled") is True
        m = manager_of(uid)
        manager = ""
        flag = None
        if m:
            target = by_id.get(m["id"])
            manager = ((target or {}).get("displayName") or m.get("name") or (_label(target) if target else "")
                       or m.get("upn") or m["id"])
            reports.setdefault(m["id"], []).append(u.get("displayName") or _label(u))
            known.setdefault(m["id"], m)
            if enabled and manager_blocked(m):
                flag = "medium"
        licences = [names.get(s, "Unknown licence") for s in u.get("skuIds") or [] if isinstance(s, str)]
        name = u.get("displayName") or ""
        rows.append(((str(name).lower(), _label(u).lower()), [
            _label(u), name, "Guest" if u.get("userType") == "Guest" else "Member", ALLOWED if enabled else BLOCKED,
            d.get("department"), d.get("jobTitle"), manager, licences,
            "Synced from AD" if u.get("onPremisesSyncEnabled") else "Cloud", _date(u.get("createdDateTime")),
            _sign_in(u)], flag))
    for _, row, flag in sorted(rows, key=lambda r: r[0]):
        every.add(row, flag)

    managers = Section(MANAGERS, ["Manager", "Name", "Sign-in", "Department", "Direct reports", "Reports"],
                       intro="Everyone who is someone's manager, with the accounts that report to them. A manager "
                             "whose sign-in is blocked still has people reporting to them: update the manager in "
                             "Entra ID (or on-premises AD, for synced accounts).",
                       empty="No account has a manager set." if details is not None else NO_DETAILS)
    rows = []
    for mid, team in reports.items():
        account = by_id.get(mid)
        m = known[mid]
        blocked = manager_blocked(m)
        label = _label(account) if account else (m.get("upn") or m.get("name") or mid)
        name = (account or {}).get("displayName") or m.get("name") or ""
        status = (BLOCKED if blocked else ALLOWED) if account or m.get("enabled") is not None else "Not known"
        rows.append(((not blocked, str(name).lower()),
                     [label, name, status, (people.get(mid) or {}).get("department"), len(team),
                      sorted(team)[:REPORTS_SHOWN] + ([f"and {len(team) - REPORTS_SHOWN} more"]
                                                      if len(team) > REPORTS_SHOWN else [])],
                     "medium" if blocked else None))
    for _, row, flag in sorted(rows, key=lambda r: r[0]):
        managers.add(row, flag)

    departments = Section(DEPARTMENTS, ["Department", "Members", "Sign-in allowed", "Licensed", "No manager"],
                          intro="Member accounts by department, as set on each account. Guests are left out.",
                          empty="No member accounts." if details is not None else NO_DETAILS)
    counts: dict[str, list[int]] = {}
    for u in users:
        if u.get("userType") == "Guest":
            continue
        dept = (people.get(u.get("_id")) or {}).get("department")
        c = counts.setdefault(dept if isinstance(dept, str) and dept else NO_DEPARTMENT, [0, 0, 0, 0])
        c[0] += 1
        c[1] += u.get("accountEnabled") is True
        c[2] += bool(u.get("skuIds"))
        c[3] += u.get("accountEnabled") is True and manager_of(u.get("_id")) is None
    if details is not None:
        for dept, c in sorted(counts.items(), key=lambda kv: (kv[0] == NO_DEPARTMENT, kv[0].lower())):
            departments.add([dept, *c])
    report.sections = [every, managers, departments]

    members = [u for u in users if u.get("userType") != "Guest"]
    blocked = [u for u in users if u.get("accountEnabled") is not True]
    licensed_blocked = sum(bool(u.get("skuIds")) for u in blocked)
    no_manager = sum(u.get("accountEnabled") is True and manager_of(u.get("_id")) is None for u in members)
    report.tiles = [
        Tile("Accounts", len(users), f"{plural(len(members), 'member')}, {plural(len(users) - len(members), 'guest')}",
             rows=Rows(EVERY)),
        Tile("Sign-in blocked", len(blocked), f"{licensed_blocked} still licensed" if blocked else "",
             "warn" if licensed_blocked else None, Rows(EVERY, {"Sign-in": BLOCKED})),
        Tile("Report to a blocked manager", every.flagged, "Enabled accounts whose manager can't sign in",
             "warn" if every.flagged else "ok", Rows(EVERY, flagged=True)),
        Tile("Members with no manager", no_manager if details is not None else "Not checked", "Sign-in allowed",
             rows=Rows(EVERY, {"Type": "Member", "Sign-in": ALLOWED, "Manager": ""}) if details is not None else None),
    ]
    report.footnote = ("Departments, job titles and managers are whatever is set on each account in Entra ID (or in "
                       "on-premises AD, for synced accounts). Last sign-in is the latest of interactive and "
                       "background sign-ins, and needs Entra ID P1.")
