"""Who can reach each SharePoint site, and how.

One collector over every site (``Sites.Read.All``) that answers what clients ask about site access
with the permissions Office Sentry already has:

* who owns the site: the owners of its Microsoft 365 group, else its site collection administrators and
  the members of its owners group
* its site collection administrators, and everyone SharePoint has recorded on the site, read from the
  site's hidden user information list (people, groups and guests)
* what the default document library grants and to whom: site groups, the group, people, everyone
* whether everyone in the organisation can open the site, from what it grants
* guests who have been given access on the site, and the group's guests

Who is in a SharePoint group needs SharePoint's own API (Sites.Read.All on the SharePoint resource, in the
2.2 consent batch), which the Site permissions collector reads; this one uses its latest snapshot. Until a
client approves that, a site whose owners or "Everyone" may be in a SharePoint group says it doesn't know
rather than guessing. SharePoint records both "Everyone" claims in every site's user information list by
default, so finding them there says nothing about access. Everything here is GET only.
"""

from __future__ import annotations

from ..graph import GraphError, PermissionMissing
from ..snapshot import TenantSnapshot
from ..text import plural
from .base import Collector, RunContext, register
from .sharepoint import PERMISSION_FIELDS
from .sharepoint_logins import classify_login, guest_address
from .sharepoint_lookup import DRIVE_FIELDS, SITE_FIELDS, Lookups, is_system_site

# The hidden list SharePoint keeps on every site of everyone it has resolved there: people given access
# (or who opened something), SharePoint groups, Microsoft 365 group claims and the "Everyone" claims.
USER_LIST = "User Information List"
USER_LIST_PARAMS = {
    "$select": "id,contentType",
    "$expand": "fields($select=Title,EMail,Name,UserName,IsSiteAdmin,Deleted)",
    "$top": "999",
}
MAX_SITES = 3000
USER_LIST_PAGES = 20  # pages of 999 rows per site
MAX_PAGES = 4000  # per run, across every site
OWNERS_KEPT = 20
ADMINS_KEPT = 20
GUESTS_KEPT = 50
ACCESS_KEPT = 60

ROLE_WORDS = {"owner": "Full control", "write": "Edit", "read": "View"}


def site_group_kind(name: str) -> str:
    low = (name or "").lower()
    if low.endswith(" owners"):
        return "Site owners group"
    if low.endswith(" members"):
        return "Site members group"
    if low.endswith(" visitors"):
        return "Site visitors group"
    return "SharePoint group"


def describe_grant(p: dict) -> dict | None:
    """One default-library permission as a row: who, how they hold it, what they can do, outside or not."""
    roles = [r for r in (p.get("roles") if isinstance(p.get("roles"), list) else []) if isinstance(r, str)]
    can = ", ".join(ROLE_WORDS.get(r, r.capitalize()) for r in roles) or "Unknown"
    link = p.get("link")
    if isinstance(link, dict):
        scope = link.get("scope") or "users"
        who = {"anonymous": "Anyone with the link", "organization": "Everyone in the organisation"}.get(
            scope, "People with a sharing link"
        )
        return {"who": who, "how": "Sharing link", "can": can, "outside": scope == "anonymous"}
    identity = p.get("grantedToV2")
    if not isinstance(identity, dict):
        return None
    if isinstance(identity.get("siteGroup"), dict):
        name = identity["siteGroup"].get("displayName") or identity["siteGroup"].get("loginName") or "?"
        return {"who": name, "how": site_group_kind(name), "can": can, "outside": False}
    if isinstance(identity.get("group"), dict):
        name = identity["group"].get("displayName") or identity["group"].get("id") or "?"
        return {"who": name, "how": "Microsoft 365 or security group", "can": can, "outside": False}
    for part in ("user", "siteUser"):
        who = identity.get(part)
        if not isinstance(who, dict):
            continue
        kind, value = classify_login(who.get("loginName"))
        if kind == "system":
            return None
        if kind == "everyone":
            return {"who": "Everyone, including guests", "how": "Everyone", "can": can, "outside": True}
        if kind == "everyone_but_guests":
            return {"who": "Everyone in the organisation", "how": "Everyone", "can": can, "outside": False}
        if kind == "guest":
            return {"who": guest_address(value, who.get("email")), "how": "Guest", "can": can, "outside": True}
        name = (who.get("email") or who.get("displayName") or value or "?").lower()
        return {"who": name, "how": "Person", "can": can, "outside": False}
    return None


class _Site:
    """What one site's user list and default library say, gathered before it's stored."""

    def __init__(self, site: dict):
        self.site = site
        self.group_id: str | None = None
        self.admins: list[dict] = []
        self.people = 0
        self.guests: list[str] = []
        self.sp_groups: list[str] = []
        self.security_groups = 0
        self.access: list[dict] = []
        self.user_list = False
        self.library = False

    def take_user(self, row: dict) -> None:
        f = row.get("fields") if isinstance(row.get("fields"), dict) else {}
        if f.get("Deleted") is True:
            return
        content = (row.get("contentType") or {}).get("name") if isinstance(row.get("contentType"), dict) else None
        kind, value = classify_login(f.get("Name") or f.get("UserName"))
        if content == "SharePointGroup" or kind == "sp_group":
            name = f.get("Title") or value
            if name and name not in self.sp_groups:
                self.sp_groups.append(name)
            return
        # Every site's list has both "Everyone" claims whether or not they're given anything (see the top).
        if kind in ("system", "unknown", "everyone", "everyone_but_guests"):
            return
        if kind == "group_members":
            self.group_id = self.group_id or value
        elif kind == "group_owners":
            self.group_id = self.group_id or value
        elif kind == "security_group":
            self.security_groups += 1
        elif kind == "guest":
            address = guest_address(value, f.get("EMail"))
            if address not in self.guests:
                self.guests.append(address)
        else:
            self.people += 1
        if f.get("IsSiteAdmin") is True and kind in ("person", "guest"):
            self.admins.append({"name": f.get("Title") or value, "upn": value, "email": (f.get("EMail") or "").lower()})


def owners_of(
    found: _Site, groups: dict[str, dict], users: dict[str, dict], sp_site: dict | None = None
) -> tuple[list[dict], str | None]:
    """The site's owners and where they come from: the group's owners ("group"), else its site administrators
    ("admins") and, once SharePoint's own API has been read (``sp_site``, the Site permissions snapshot), the
    members of its owners group ("sharepoint"). None when that group may hold owners nobody can count yet."""
    group = groups.get(found.group_id or "")
    if group is not None and group.get("examined"):
        owners = [
            {"name": o.get("name"), "upn": o.get("upn"), "enabled": bool(o.get("enabled"))}
            for o in group.get("owners") or []
            if isinstance(o, dict) and o.get("type", "user") == "user"
        ]
        return owners, "group"
    if not found.user_list:
        return [], None
    people = [(a["name"], a["upn"], a["email"]) for a in found.admins]
    source = "admins"
    if sp_site is not None:
        source = "sharepoint"
        for g in sp_site.get("groups") or []:
            if not isinstance(g, dict) or g.get("role") != "owners":
                continue
            for m in g.get("members") or []:
                if not isinstance(m, dict) or m.get("kind") not in ("person", "guest"):
                    continue
                login = str(m.get("id") or "").lower()
                if login and login not in {p[1] for p in people}:
                    people.append((m.get("name") or login, login, login))
    elif found.sp_groups and len(people) < 2:
        source = None  # its owners group may hold more, and only SharePoint's own API says who
    owners = []
    for name, upn, email in people:
        u = users.get(upn) or users.get(email)
        enabled = True if (u is None and not users) else bool(u and u.get("accountEnabled") is not False)
        owners.append({"name": name, "upn": upn, "enabled": enabled})
    return owners, source


def everyone_access(access: list[dict], sp_site: dict | None) -> str | None:
    """ "all" when everyone, guests included, is given access to the site, "members" when everyone in the
    organisation is, else None: from the default library's grants and, once read, the site's own (directly or
    through a SharePoint group that holds a permission level)."""
    kinds = {"everyone" if a.get("outside") else "everyone_but_guests" for a in access if a.get("how") == "Everyone"}
    if sp_site is not None:
        members = {g.get("id"): g.get("members") or [] for g in sp_site.get("groups") or [] if isinstance(g, dict)}
        for grant in sp_site.get("grants") or []:
            who = grant.get("principal") if isinstance(grant, dict) else None
            if not isinstance(who, dict):
                continue
            inside = members.get(who.get("id"), []) if who.get("kind") == "sp_group" else [who]
            kinds |= {m.get("kind") for m in inside if isinstance(m, dict)}
    if "everyone" in kinds:
        return "all"
    return "members" if "everyone_but_guests" in kinds else None


@register
class SiteAccess(Collector):
    key = "site_access"
    title = "Site access"
    description = (
        "Who owns, administers and can reach each SharePoint site: group owners and site admins, guests, "
        "what the default library grants, and sites open to everyone. Needs the Sites.Read.All permission."
    )
    permissions = ("Sites.Read.All",)
    needs = ("sharepoint",)

    def collect(self, ctx: RunContext) -> None:
        if ctx.capabilities.get("sharepoint") is False:
            ctx.log("Tenant has no SharePoint licence; nothing to read")
            ctx.objects("access_status", [("status", {"available": False, "reason": "no_sharepoint"})])
            return
        catalog = Lookups(ctx, self.key, SITE_FIELDS, DRIVE_FIELDS)
        try:
            sites = catalog.sites()
        except PermissionMissing as exc:
            ctx.warn(
                "Reading site access needs the Sites.Read.All permission. Re-send the consent link from the "
                f"client page to add it. ({exc.message})"
            )
            ctx.objects("access_status", [("status", {"available": False, "reason": "permission"})])
            return
        snap = TenantSnapshot(ctx.conn, ctx.tenant)
        groups = {g["_id"]: g for g in snap.objects("groups", "group") or [] if isinstance(g, dict)}
        # Each site's SharePoint groups and grants, from the last time SharePoint's own API was read.
        sp_sites = {s["_id"]: s for s in snap.objects("site_permissions", "site") or [] if isinstance(s, dict)}
        group_by_mail = {
            (g.get("mail") or "").lower(): gid for gid, g in groups.items() if isinstance(g.get("mail"), str)
        }
        users: dict[str, dict] = {}
        for u in snap.objects("users", "user") or []:
            for key in ("userPrincipalName", "mail"):
                if isinstance(u.get(key), str) and u[key]:
                    users.setdefault(u[key].lower(), u)
        usage = {
            s["url"].lower(): s
            for s in snap.objects("sharepoint_usage", "site") or []
            if isinstance(s.get("url"), str) and s["url"]
        }

        team_sites = [
            s
            for s in sites
            if not s.get("isPersonalSite")
            and "-my.sharepoint." not in (s.get("webUrl") or "")
            and not is_system_site(s.get("webUrl"), (usage.get((s.get("webUrl") or "").lower()) or {}).get("template"))
        ]
        pages = 0
        unreadable = 0
        stored: list[tuple[str, dict]] = []
        for site in team_sites[:MAX_SITES]:
            if pages >= MAX_PAGES:
                break
            found = _Site(site)
            url = site.get("webUrl") or ""
            try:
                result = ctx.graph.get_all(
                    f"/sites/{site['id']}/lists/{USER_LIST}/items",
                    USER_LIST_PARAMS,
                    max_pages=min(USER_LIST_PAGES, MAX_PAGES - pages),
                )
                pages += result.pages
                found.user_list = True
                for row in result.items:
                    if isinstance(row, dict):
                        found.take_user(row)
                if result.truncated:
                    ctx.log(f"Read the first {result.pages} pages of people on {url}", "warning")
            except GraphError as exc:
                unreadable += 1
                ctx.log(f"Couldn't read who has access on {url}: {exc.message}", "warning")
            try:
                perms = ctx.graph.get_all(
                    f"/sites/{site['id']}/drive/root/permissions", {"$select": PERMISSION_FIELDS}
                ).items
                found.library = True
                seen: set[tuple] = set()
                for p in perms:
                    row = describe_grant(p) if isinstance(p, dict) else None
                    key = (row["who"], row["how"], row["can"]) if row else None
                    if row and key not in seen and len(found.access) < ACCESS_KEPT:
                        seen.add(key)
                        found.access.append(row)
            except GraphError as exc:
                ctx.log(f"Couldn't read the default library's permissions on {url}: {exc.message}", "warning")
            use = usage.get(url.lower(), {})
            if found.group_id is None:
                owner = use.get("ownerPrincipalName")
                found.group_id = group_by_mail.get(owner.lower()) if isinstance(owner, str) else None
            group = groups.get(found.group_id or "")
            sp_site = sp_sites.get(site["id"])
            owners, source = owners_of(found, groups, users, sp_site)
            everyone = everyone_access(found.access, sp_site)
            guests = list(found.guests)
            for g in (group or {}).get("guests") or []:
                if isinstance(g, str) and g.lower() not in guests:
                    guests.append(g.lower())
            stored.append(
                (
                    site["id"],
                    {
                        "name": site.get("displayName") or url,
                        "url": url,
                        "template": use.get("template"),
                        "group": (
                            {
                                "id": found.group_id,
                                "name": group.get("displayName"),
                                "isTeam": bool(group.get("isTeam")),
                            }
                            if group is not None
                            else ({"id": found.group_id, "name": None, "isTeam": False} if found.group_id else None)
                        ),
                        "owners": owners[:OWNERS_KEPT],
                        "ownerSource": source,
                        "ownerCount": sum(o["enabled"] for o in owners) if source else None,
                        "admins": found.admins[:ADMINS_KEPT],
                        "adminCount": len(found.admins),
                        "people": found.people,
                        "memberCount": group.get("memberCount") if group and group.get("examined") else None,
                        "guests": sorted(guests)[:GUESTS_KEPT],
                        "guestCount": len(guests),
                        "everyone": everyone,
                        # Not known while "Everyone" may be in a SharePoint group nobody has read.
                        "everyoneKnown": everyone is not None or sp_site is not None or not found.sp_groups,
                        "sharePointRead": sp_site is not None,
                        "spGroups": found.sp_groups[:ACCESS_KEPT],
                        "securityGroups": found.security_groups,
                        "access": found.access,
                        "userList": found.user_list,
                        "libraryAccess": found.library,
                        "externalSharing": use.get("externalSharing"),
                        "anyoneLinks": use.get("anyoneLinks") or 0,
                        "guestLinks": use.get("guestLinks") or 0,
                    },
                )
            )
        if len(team_sites) > MAX_SITES:
            ctx.warn(f"Read access for the first {MAX_SITES} of {len(team_sites)} sites")
            ctx.truncated = True
        if pages >= MAX_PAGES:
            ctx.warn(f"Stopped after reading {MAX_PAGES:,} pages of site users; later sites aren't covered")
            ctx.truncated = True
        if unreadable:
            ctx.warn(f"The user list of {plural(unreadable, 'site')} couldn't be read")
        catalog.save()
        ctx.objects("site", stored)
        ctx.objects(
            "access_status",
            [
                (
                    "status",
                    {
                        "available": True,
                        "sites": len(stored),
                        "unreadable": unreadable,
                        "pages": pages,
                        "complete": not ctx.truncated,
                    },
                )
            ],
        )
        rows = [r for _, r in stored]
        ctx.metric("sites.examined", len(rows))
        ctx.metric("sites.ownerless", sum(r["ownerCount"] == 0 for r in rows))
        ctx.metric("sites.single_owner", sum(r["ownerCount"] == 1 for r in rows))
        ctx.metric("sites.with_guests", sum(r["guestCount"] > 0 for r in rows))
        ctx.metric("sites.guests", len({g for r in rows for g in r["guests"]}))
        ctx.metric("sites.open_to_everyone", sum(bool(r["everyone"]) for r in rows))
