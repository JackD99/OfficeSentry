"""Who holds which permission level on each SharePoint site, through which SharePoint group.

Graph can't say who is in a SharePoint group, which permission level each person or group holds, or which
lists and subsites have permissions of their own. This collector reads those from each site's own REST API
(``sharepoint_api.py``), signing in as the app with its certificate. It needs the application permission
Sites.Read.All on the "Office 365 SharePoint Online" resource, which comes with the bundled consent batch;
until the client approves it the collector stores that it's waiting, without making the run partial, and the
report says so.

For every team site (OneDrives are the File and folder permissions report's): the site's permission levels
(role assignments) with hidden ones such as Limited Access left out, its SharePoint groups and their members,
which of them are the site's owners, members and visitors groups, the lists and libraries that don't inherit
the site's permissions and what they grant, and the subsites that don't. Everything here is GET only.
"""

from __future__ import annotations

import time
from datetime import timedelta

from ..graph import PermissionMissing
from ..sharepoint_api import SharePointAccessDenied, SharePointError, beside, host_of
from ..text import plural
from .base import Collector, RunContext, register
from .sharepoint_lookup import DRIVE_FIELDS, SITE_FIELDS, Lookups
from .site_access import classify_login, guest_address

ROLE_PARAMS = {"$expand": "Member,RoleDefinitionBindings"}
WEB_PARAMS = {
    "$select": "Id,Title,Url,HasUniqueRoleAssignments,AssociatedOwnerGroup/Id,AssociatedMemberGroup/Id,"
    "AssociatedVisitorGroup/Id",
    "$expand": "AssociatedOwnerGroup,AssociatedMemberGroup,AssociatedVisitorGroup",
}
GROUP_PARAMS = {
    "$select": "Id,Title,LoginName,OwnerTitle,Users/Id,Users/Title,Users/LoginName,Users/Email,Users/PrincipalType",
    "$expand": "Users",
}
LIST_PARAMS = {
    "$select": "Id,Title,BaseType,Hidden,IsSystemList,HasUniqueRoleAssignments,ItemCount,RootFolder/ServerRelativeUrl",
    "$expand": "RootFolder",
    "$filter": "Hidden eq false",
    "$top": "5000",
}
WEB_LIST_PARAMS = {"$select": "Title,Url,HasUniqueRoleAssignments", "$top": "5000"}

MAX_SITES = 3000
MAX_REQUESTS = 12_000  # per run, across every site
UNIQUE_LISTS_READ = 50  # lists with their own permissions whose grants are read, per site
SUBSITES_READ = 25  # subsites with their own permissions whose grants are read, per site
GRANTS_KEPT = 200
MEMBERS_KEPT = 200
GROUPS_KEPT = 100
TIME_BUDGET = timedelta(hours=1)
clock = time.monotonic

# SharePoint principal types (SP.Utilities.PrincipalType).
SHAREPOINT_GROUP = 8
# Permission levels SharePoint grants by itself so people can reach something shared inside the site; they give
# nothing on their own and are reported as file and folder sharing instead.
HIDDEN_LEVELS = {"limited access", "web-only limited access"}


def principal(member: dict, sp_groups: dict[int, dict] | None = None) -> dict | None:
    """A role assignment's member or a SharePoint group's user, as ``{name, kind, id, outside}``.

    Kinds: ``person``, ``guest``, ``group_members`` and ``group_owners`` (a Microsoft 365 group, by id),
    ``security_group`` (id), ``everyone``, ``everyone_but_guests`` and ``sp_group`` (the group's SharePoint
    id). System accounts give None.
    """
    if not isinstance(member, dict):
        return None
    title = member.get("Title") if isinstance(member.get("Title"), str) else None
    if member.get("PrincipalType") == SHAREPOINT_GROUP:
        gid = member.get("Id")
        name = title or (sp_groups or {}).get(gid, {}).get("name") or member.get("LoginName") or "?"
        return {"name": name, "kind": "sp_group", "id": gid, "outside": False}
    kind, value = classify_login(member.get("LoginName"))
    if kind in ("system", "unknown"):
        return None
    if kind == "sp_group":  # a group named without a principal type
        return {"name": title or value, "kind": "sp_group", "id": member.get("Id"), "outside": False}
    if kind == "guest":
        address = guest_address(value, member.get("Email"))
        return {"name": title or address, "kind": "guest", "id": address, "outside": True}
    if kind == "person":
        address = (member.get("Email") or value or "").lower()
        return {"name": title or address, "kind": "person", "id": value, "outside": False}
    if kind == "everyone":
        return {"name": "Everyone", "kind": "everyone", "id": value, "outside": True}
    if kind == "everyone_but_guests":
        return {"name": "Everyone except external users", "kind": kind, "id": value, "outside": False}
    return {"name": title or value, "kind": kind, "id": value, "outside": False}


def levels(bindings) -> list[str]:
    """The visible permission level names of one role assignment (Full Control, Edit, Read, a custom one)."""
    out = []
    for b in bindings if isinstance(bindings, list) else []:
        if not isinstance(b, dict) or b.get("Hidden") is True:
            continue
        name = b.get("Name")
        if isinstance(name, str) and name and name.lower() not in HIDDEN_LEVELS and name not in out:
            out.append(name)
    return out


def grants(assignments: list, sp_groups: dict[int, dict] | None = None) -> list[dict]:
    """Role assignments as ``[{principal, levels}]``, leaving out system accounts and Limited Access only."""
    out = []
    for a in assignments:
        if not isinstance(a, dict):
            continue
        who = principal(a.get("Member"), sp_groups)
        names = levels(a.get("RoleDefinitionBindings"))
        if who is not None and names:
            out.append({"principal": who, "levels": names})
    return out[:GRANTS_KEPT]


class _Budget:
    def __init__(self):
        self.started = clock()
        self.requests = 0

    @property
    def spent(self) -> str | None:
        if self.requests >= MAX_REQUESTS:
            return f"the limit of {MAX_REQUESTS:,} requests a night"
        if clock() - self.started >= TIME_BUDGET.total_seconds():
            return f"the {int(TIME_BUDGET.total_seconds() // 60)}-minute limit"
        return None


class _NotApproved(Exception):
    """SharePoint refused the app on the first site: the client hasn't approved the permission yet."""


@register
class SitePermissions(Collector):
    key = "site_permissions"
    title = "Site permissions"
    description = (
        "Permission levels on each SharePoint site, SharePoint groups and their members, and lists, libraries "
        "and subsites with permissions of their own. Needs Sites.Read.All on SharePoint (not Graph)."
    )
    permissions = ("Sites.Read.All (SharePoint)",)

    def collect(self, ctx: RunContext) -> None:
        if ctx.capabilities.get("sharepoint") is False:
            ctx.log("Tenant has no SharePoint licence; nothing to read")
            ctx.objects("permissions_status", [("status", {"available": False, "reason": "no_sharepoint"})])
            return
        catalog = Lookups(ctx, self.key, SITE_FIELDS, DRIVE_FIELDS)
        try:
            sites = catalog.sites()
        except PermissionMissing as exc:  # Graph's Sites.Read.All: Site access says the same
            ctx.warn(
                "Reading site permissions needs the Sites.Read.All permission. Re-send the consent link from the "
                f"client page to add it. ({exc.message})"
            )
            ctx.objects("permissions_status", [("status", {"available": False, "reason": "permission"})])
            return
        team_sites = sorted(
            (
                s
                for s in sites
                if isinstance(s, dict)
                and not s.get("isPersonalSite")
                and isinstance(s.get("webUrl"), str)
                and ctx.graph.cloud.is_sharepoint(host_of(s["webUrl"]))
                and not host_of(s["webUrl"]).split(".", 1)[0].endswith("-my")
            ),
            key=lambda s: s["webUrl"].lower(),
        )
        clients: dict[str, object] = {}
        budget = _Budget()
        stored: list[tuple[str, dict]] = []
        unreadable = 0
        stopped = None
        try:
            for n, site in enumerate(team_sites):
                if n >= MAX_SITES:
                    stopped = f"the limit of {MAX_SITES:,} sites"
                    break
                if stopped := budget.spent:
                    break
                host = host_of(site["webUrl"])
                if host not in clients:
                    clients[host] = beside(ctx.graph, host)
                client = clients[host]
                if client is None:
                    ctx.log("This connection can't sign in to SharePoint with a certificate; nothing to read")
                    ctx.objects("permissions_status", [("status", {"available": False, "reason": "no_certificate"})])
                    return
                try:
                    stored.append((site["id"], self._site(ctx, client, site, budget)))
                except SharePointAccessDenied as exc:
                    if not stored and not unreadable:
                        raise _NotApproved(exc.message) from exc
                    unreadable += 1
                    ctx.log(f"SharePoint refused access to {site['webUrl']}: {exc.message}", "warning")
                except SharePointError as exc:
                    unreadable += 1
                    ctx.log(f"Couldn't read the permissions of {site['webUrl']}: {exc.message}", "warning")
        except _NotApproved as exc:
            ctx.log(
                "SharePoint refused the app: the client hasn't approved the SharePoint permission yet "
                f"(Sites.Read.All on Office 365 SharePoint Online). ({exc})"
            )
            catalog.save()
            ctx.objects("permissions_status", [("status", {"available": False, "reason": "consent"})])
            return
        if stopped:
            ctx.warn(
                f"Read the permissions of {plural(len(stored), 'site')} of {len(team_sites)}; stopped at {stopped}"
            )
            ctx.truncated = True
        if unreadable:
            ctx.warn(f"The permissions of {plural(unreadable, 'site')} couldn't be read")
        catalog.save()
        ctx.objects("site", stored)
        ctx.objects(
            "permissions_status",
            [
                (
                    "status",
                    {
                        "available": True,
                        "sites": len(stored),
                        "unreadable": unreadable,
                        "requests": budget.requests,
                        "complete": not ctx.truncated,
                    },
                )
            ],
        )
        rows = [r for _, r in stored]
        direct = [
            g for r in rows for g in r["grants"] if g["principal"]["kind"] in ("person", "guest", "security_group")
        ]
        ctx.metric("site_permissions.sites", len(rows))
        ctx.metric("site_permissions.direct_grants", len(direct))
        ctx.metric("site_permissions.unique_lists", sum(r["uniqueListCount"] for r in rows))
        ctx.metric("site_permissions.groups", sum(len(r["groups"]) for r in rows))

    @staticmethod
    def _site(ctx: RunContext, client, site: dict, budget: _Budget) -> dict:
        url = site["webUrl"]

        def get(path, params, site_url=url):
            budget.requests += 1
            return client.get(site_url, path, params)

        def get_all(path, params, site_url=url):
            result = client.get_all(site_url, path, params)
            budget.requests += max(result.pages, 1)
            return result

        web = get("/web", WEB_PARAMS)
        roles = {
            "owners": (web.get("AssociatedOwnerGroup") or {}).get("Id"),
            "members": (web.get("AssociatedMemberGroup") or {}).get("Id"),
            "visitors": (web.get("AssociatedVisitorGroup") or {}).get("Id"),
        }
        groups: dict[int, dict] = {}
        result = get_all("/web/sitegroups", GROUP_PARAMS)
        for g in result.items:
            if not isinstance(g, dict) or not isinstance(g.get("Id"), int):
                continue
            users = g.get("Users") if isinstance(g.get("Users"), list) else []
            members = [p for p in (principal(u) for u in users) if p is not None]
            groups[g["Id"]] = {
                "id": g["Id"],
                "name": g.get("Title") or g.get("LoginName") or "?",
                "owner": g.get("OwnerTitle"),
                "role": next((k for k, v in roles.items() if v == g["Id"]), None),
                "members": members[:MEMBERS_KEPT],
                "memberCount": len(members),
            }
        site_grants = grants(get_all("/web/roleassignments", ROLE_PARAMS).items, groups)
        granted_groups = {g["principal"]["id"] for g in site_grants if g["principal"]["kind"] == "sp_group"}

        unique_lists: list[dict] = []
        lists = [x for x in get_all("/web/lists", LIST_PARAMS).items if isinstance(x, dict)]
        shown = [x for x in lists if not x.get("IsSystemList")]
        for x in shown:
            if x.get("HasUniqueRoleAssignments") is not True:
                continue
            entry = {
                "id": x.get("Id"),
                "title": x.get("Title") or "?",
                "url": _absolute(url, (x.get("RootFolder") or {}).get("ServerRelativeUrl")),
                "library": x.get("BaseType") == 1,
                "items": x.get("ItemCount") if isinstance(x.get("ItemCount"), int) else None,
                "grants": None,
            }
            if len(unique_lists) < UNIQUE_LISTS_READ and not budget.spent and x.get("Id"):
                try:
                    assigned = get_all(f"/web/lists(guid'{x['Id']}')/roleassignments", ROLE_PARAMS).items
                    entry["grants"] = grants(assigned, groups)
                    granted_groups |= {
                        g["principal"]["id"] for g in entry["grants"] if g["principal"]["kind"] == "sp_group"
                    }
                except SharePointError as exc:
                    ctx.log(f"Couldn't read who can open {entry['title']} on {url}: {exc.message}", "warning")
            unique_lists.append(entry)

        subsites: list[dict] = []
        for w in get_all("/web/webs", WEB_LIST_PARAMS).items:
            if not isinstance(w, dict) or not isinstance(w.get("Url"), str):
                continue
            entry = {
                "title": w.get("Title") or w["Url"],
                "url": w["Url"],
                "unique": w.get("HasUniqueRoleAssignments") is True,
                "grants": None,
            }
            reading = sum(s["grants"] is not None for s in subsites) < SUBSITES_READ
            if entry["unique"] and reading and not budget.spent and host_of(w["Url"]) == client.host:
                try:
                    entry["grants"] = grants(
                        get_all("/web/roleassignments", ROLE_PARAMS, site_url=w["Url"]).items, groups
                    )
                except SharePointError as exc:
                    ctx.log(f"Couldn't read who can open the subsite {w['Url']}: {exc.message}", "warning")
            subsites.append(entry)

        for gid, g in groups.items():
            g["levels"] = next(
                (
                    x["levels"]
                    for x in site_grants
                    if x["principal"]["id"] == gid and x["principal"]["kind"] == "sp_group"
                ),
                [],
            )
            g["used"] = gid in granted_groups
        order = {"owners": 0, "members": 1, "visitors": 2}
        kept = sorted(groups.values(), key=lambda g: (order.get(g["role"], 3), not g["used"], g["name"].lower()))
        return {
            "name": site.get("displayName") or web.get("Title") or url,
            "url": url,
            "grants": site_grants,
            "groups": kept[:GROUPS_KEPT],
            "groupCount": len(groups),
            "lists": unique_lists,
            "listCount": len(shown),
            "uniqueListCount": len(unique_lists),
            "subsites": subsites[: SUBSITES_READ * 4],
            "subsiteCount": len(subsites),
        }


def _absolute(site_url: str, server_relative: str | None) -> str | None:
    if not isinstance(server_relative, str) or not server_relative.startswith("/"):
        return None
    return host_of(site_url) + server_relative
