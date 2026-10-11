"""Who can reach each SharePoint site, and how.

One collector over every site (``Sites.Read.All``) that answers what clients ask about site access
with the permissions Office Sentry already has:

* who owns the site: the owners of its Microsoft 365 group, else its site collection administrators
* its site collection administrators, and everyone SharePoint has recorded on the site, read from the
  site's hidden user information list (people, groups, guests, and "Everyone" claims)
* what the default document library grants and to whom: site groups, the group, people, everyone
* guests who have been given access on the site, and the group's guests

Membership of SharePoint groups on sites that have no Microsoft 365 group needs SharePoint's own API
(a permission on the SharePoint resource, coming in a later consent batch); those sites say so rather
than guessing. Everything here is GET only.
"""

from __future__ import annotations

import re

from ..graph import GraphError, PermissionMissing
from ..snapshot import TenantSnapshot
from ..text import plural
from .base import Collector, RunContext, register
from .sharepoint import PERMISSION_FIELDS
from .sharepoint_lookup import DRIVE_FIELDS, SITE_FIELDS, Lookups

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

# Login name shapes (claims). Anything else without "|" is a SharePoint group by its name.
_PERSON = re.compile(r"^i:0#\.f\|membership\|(.+)$", re.IGNORECASE)
_M365 = re.compile(r"^c:0o\.c\|federateddirectoryclaimprovider\|([^|]+?)(_o)?$", re.IGNORECASE)
_SECURITY = re.compile(r"^c:0t\.c\|tenant\|([^|]+)$", re.IGNORECASE)
EVERYONE = "c:0(.s|true"
EVERYONE_BUT_GUESTS = "spo-grid-all-users"
SYSTEM_LOGINS = ("app@sharepoint", "sharepoint\\system", "_spocache", "_spocrawler", "_spofrm")
ROLE_WORDS = {"owner": "Full control", "write": "Edit", "read": "View"}


def classify_login(login: str | None) -> tuple[str, str]:
    """``(kind, value)`` for a SharePoint login name.

    Kinds: ``person`` (address), ``guest`` (address), ``group_members`` and ``group_owners`` (Microsoft 365
    group id), ``security_group`` (id), ``everyone``, ``everyone_but_guests``, ``system``, ``sp_group``
    (the group's name) or ``unknown``.
    """
    login = (login or "").strip()
    low = login.lower()
    if not login:
        return ("unknown", "")
    if any(s in low for s in SYSTEM_LOGINS):
        return ("system", login)
    if low == EVERYONE:
        return ("everyone", login)
    if EVERYONE_BUT_GUESTS in low:
        return ("everyone_but_guests", login)
    if m := _PERSON.match(login):
        who = m.group(1)
        return ("guest" if "#ext#" in who.lower() or "urn:spo:guest" in who.lower() else "person", who.lower())
    if "urn:spo:guest" in low:
        return ("guest", login.rsplit("|", 1)[-1].lower())
    if m := _M365.match(login):
        return ("group_owners" if m.group(2) else "group_members", m.group(1).lower())
    if m := _SECURITY.match(login):
        return ("security_group", m.group(1).lower())
    if "|" not in login:
        return ("sp_group", login)
    return ("unknown", login)


def guest_address(login_value: str, email: str | None) -> str:
    """A guest's own address: the invitation email, else the address folded into the login name."""
    if email and "@" in email:
        return email.lower()
    local = login_value.split("#ext#", 1)[0]
    return local.replace("_", "@", 1).lower() if "_" in local and "@" not in local else local.lower()


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
        self.everyone: str | None = None
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
        if kind == "system" or kind == "unknown":
            return
        if kind == "group_members":
            self.group_id = self.group_id or value
        elif kind == "group_owners":
            self.group_id = self.group_id or value
        elif kind == "security_group":
            self.security_groups += 1
        elif kind == "everyone":
            self.everyone = "all"
        elif kind == "everyone_but_guests":
            self.everyone = self.everyone or "members"
        elif kind == "guest":
            address = guest_address(value, f.get("EMail"))
            if address not in self.guests:
                self.guests.append(address)
        else:
            self.people += 1
        if f.get("IsSiteAdmin") is True and kind in ("person", "guest"):
            self.admins.append({"name": f.get("Title") or value, "upn": value, "email": (f.get("EMail") or "").lower()})


def owners_of(found: _Site, groups: dict[str, dict], users: dict[str, dict]) -> tuple[list[dict], str | None]:
    """The site's owners and where they come from: the group's owners, else its site administrators."""
    group = groups.get(found.group_id or "")
    if group is not None and group.get("examined"):
        owners = [
            {"name": o.get("name"), "upn": o.get("upn"), "enabled": bool(o.get("enabled"))}
            for o in group.get("owners") or []
            if isinstance(o, dict) and o.get("type", "user") == "user"
        ]
        return owners, "group"
    if found.user_list:
        owners = []
        for a in found.admins:
            u = users.get(a["upn"]) or users.get(a["email"])
            enabled = True if (u is None and not users) else bool(u and u.get("accountEnabled") is not False)
            owners.append({"name": a["name"], "upn": a["upn"], "enabled": enabled})
        return owners, "admins"
    return [], None


@register
class SiteAccess(Collector):
    key = "site_access"
    title = "Site access"
    description = (
        "Who owns, administers and can reach each SharePoint site: group owners and site admins, guests, "
        "what the default library grants, and sites open to everyone. Needs the Sites.Read.All permission."
    )
    permissions = ("Sites.Read.All",)

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
                f"tenant page to add it. ({exc.message})"
            )
            ctx.objects("access_status", [("status", {"available": False, "reason": "permission"})])
            return
        snap = TenantSnapshot(ctx.conn, ctx.tenant)
        groups = {g["_id"]: g for g in snap.objects("groups", "group") or [] if isinstance(g, dict)}
        group_by_mail = {
            (g.get("mail") or "").lower(): gid for gid, g in groups.items() if isinstance(g.get("mail"), str)
        }
        users: dict[str, dict] = {}
        for u in snap.objects("users", "user") or []:
            for key in ("userPrincipalName", "mail"):
                if isinstance(u.get(key), str) and u[key]:
                    users.setdefault(u[key].lower(), u)
        usage = {
            (s.get("url") or "").lower(): s for s in snap.objects("sharepoint_usage", "site") or [] if s.get("url")
        }

        team_sites = [
            s for s in sites if not s.get("isPersonalSite") and "-my.sharepoint." not in (s.get("webUrl") or "")
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
                found.group_id = group_by_mail.get((use.get("ownerPrincipalName") or "").lower())
            group = groups.get(found.group_id or "")
            owners, source = owners_of(found, groups, users)
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
                        "everyone": found.everyone,
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
