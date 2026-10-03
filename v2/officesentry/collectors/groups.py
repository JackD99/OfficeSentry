"""Groups and Teams: every group, the owners and guests of each team and Microsoft 365 group, who can create
them and whether unused ones expire."""

from __future__ import annotations

from ..graph import GraphError, PermissionMissing
from .base import Collector, RunContext, register

GROUP_FIELDS = (
    "id,displayName,description,groupTypes,mailEnabled,securityEnabled,visibility,resourceProvisioningOptions,"
    "createdDateTime,mail,membershipRule,onPremisesSyncEnabled,expirationDateTime"
)
OWNER_FIELDS = "id,displayName,userPrincipalName,accountEnabled,userType"
MEMBER_FIELDS = "id,userType,accountEnabled,userPrincipalName,mail"
GROUP_LIMIT = 1000     # Microsoft 365 groups whose owners and members are read (two or more calls each)
MEMBER_PAGES = 100     # 999 members a page
OWNERS_KEPT = 20       # owner names stored per group; the counts are always complete
GUESTS_KEPT = 50       # guest addresses stored per group

# Microsoft's defaults when the tenant has never saved a Group.Unified setting.
DEFAULT_SETTINGS = {"EnableGroupCreation": True, "GroupCreationAllowedGroupId": "", "AllowGuestsToAccessGroups": True,
                    "AllowToAddGuests": True, "UsageGuidelinesUrl": ""}


def group_kind(group: dict) -> str:
    if "Unified" in (group.get("groupTypes") or []):
        return "microsoft365"
    if group.get("mailEnabled"):
        return "mail_security" if group.get("securityEnabled") else "distribution"
    return "security"


def _summary(group: dict) -> dict:
    return {
        "displayName": group.get("displayName"),
        "description": (group.get("description") or "")[:200] or None,
        "kind": group_kind(group),
        "isTeam": "Team" in (group.get("resourceProvisioningOptions") or []),
        "visibility": group.get("visibility"),
        "mail": group.get("mail"),
        "createdDateTime": group.get("createdDateTime"),
        "expirationDateTime": group.get("expirationDateTime"),
        "dynamic": "DynamicMembership" in (group.get("groupTypes") or []) or bool(group.get("membershipRule")),
        "synced": bool(group.get("onPremisesSyncEnabled")),
        "examined": False,
    }


def _owner(o: dict) -> dict:
    kind = (o.get("@odata.type") or "").rsplit(".", 1)[-1] or "unknown"
    return {"name": o.get("displayName") or o.get("userPrincipalName") or o.get("id"),
            "upn": o.get("userPrincipalName"), "type": kind,
            # An enabled user account: an app or a disabled leaver can't look after a group.
            "enabled": kind == "user" and o.get("accountEnabled") is not False}


def _setting_value(value) -> bool | str:
    text = str(value if value is not None else "")
    return text.lower() == "true" if text.lower() in ("true", "false") else text


@register
class Groups(Collector):
    key = "groups"
    title = "Groups and Teams"
    description = ("Every group, the owners, members and guests of each team and Microsoft 365 group, "
                   "who can create them and whether unused ones expire.")
    permissions = ("Directory.Read.All",)

    def collect(self, ctx: RunContext) -> None:
        groups = ctx.paged(ctx.graph.get_all("/groups", params={"$select": GROUP_FIELDS, "$top": "999"}))
        rows = {g["id"]: _summary(g) for g in groups}
        m365 = sorted((gid for gid, g in rows.items() if g["kind"] == "microsoft365"),
                      key=lambda gid: (not rows[gid]["isTeam"], (rows[gid]["displayName"] or "").lower()))
        if len(m365) > GROUP_LIMIT:
            ctx.warn(f"Read owners and members for the first {GROUP_LIMIT} of {len(m365)} teams and Microsoft 365 "
                     "groups (teams first)")
        for gid in m365[:GROUP_LIMIT]:
            try:
                rows[gid].update(self._people(ctx, gid))
            except PermissionMissing as exc:
                ctx.warn(f"Group owners and members weren't readable: {exc.message}")
                break
            except GraphError as exc:  # e.g. deleted since the list was read
                ctx.log(f"Could not read owners or members of {rows[gid]['displayName']}: {exc.message}", "warning")
        ctx.objects("group", list(rows.items()))
        self._settings(ctx, rows)
        self._expiration(ctx)

        checked = [g for g in rows.values() if g["examined"]]
        ctx.metric("groups.total", len(rows))
        ctx.metric("groups.m365", len(m365))
        ctx.metric("groups.teams", sum(rows[gid]["isTeam"] for gid in m365))
        ctx.metric("groups.ownerless", sum(not g["enabledOwnerCount"] for g in checked))
        ctx.metric("groups.with_guests", sum(bool(g["guestCount"]) for g in checked))
        ctx.metric("groups.guest_memberships", sum(g["guestCount"] for g in checked))

    @staticmethod
    def _people(ctx: RunContext, gid: str) -> dict:
        owners = [_owner(o) for o in ctx.paged(ctx.graph.get_all(
            f"/groups/{gid}/owners", params={"$select": OWNER_FIELDS, "$top": "999"}))]
        members = ctx.paged(ctx.graph.get_all(f"/groups/{gid}/members", params={
            "$select": MEMBER_FIELDS, "$top": "999"}, max_pages=MEMBER_PAGES))
        guests = sorted(m.get("mail") or m.get("userPrincipalName") or m["id"] for m in members
                        if m.get("userType") == "Guest")
        return {
            "examined": True,
            "ownerCount": len(owners),
            "enabledOwnerCount": sum(o["enabled"] for o in owners),
            "owners": owners[:OWNERS_KEPT],
            "memberCount": len(members),
            "guestCount": len(guests),
            "guests": guests[:GUESTS_KEPT],
        }

    @staticmethod
    def _settings(ctx: RunContext, rows: dict) -> None:
        try:
            settings = ctx.paged(ctx.graph.get_all("/groupSettings"))
        except GraphError as exc:
            ctx.warn(f"Group settings weren't readable: {exc.message}")
            ctx.objects("group_settings", [("Group.Unified", {"available": False})])
            return
        unified = next((s for s in settings if s.get("displayName") == "Group.Unified"), None)
        values = dict(DEFAULT_SETTINGS)
        if unified is not None:
            values.update({v["name"]: _setting_value(v.get("value")) for v in unified.get("values") or []
                           if v.get("name") in DEFAULT_SETTINGS})
        allowed = values["GroupCreationAllowedGroupId"] or None
        ctx.objects("group_settings", [("Group.Unified", {
            "available": True,
            "source": "tenant" if unified is not None else "default",
            "enableGroupCreation": values["EnableGroupCreation"] is not False,
            "allowedGroupId": allowed,
            "allowedGroupName": (rows.get(allowed) or {}).get("displayName") if allowed else None,
            "allowGuestsToAccessGroups": values["AllowGuestsToAccessGroups"] is not False,
            "allowToAddGuests": values["AllowToAddGuests"] is not False,
            "usageGuidelinesUrl": values["UsageGuidelinesUrl"] or None,
        })])

    @staticmethod
    def _expiration(ctx: RunContext) -> None:
        try:
            policies = ctx.paged(ctx.graph.get_all("/groupLifecyclePolicies"))
        except GraphError as exc:  # LicenseRequired without Entra ID P1, PermissionMissing without consent
            ctx.warn(f"The group expiration policy wasn't readable: {exc.message}")
            ctx.objects("expiration_policy", [("expiration", {"available": False})])
            return
        policy = policies[0] if policies else {}
        ctx.objects("expiration_policy", [("expiration", {
            "available": True,
            "managedGroupTypes": policy.get("managedGroupTypes") or "None",
            "groupLifetimeInDays": policy.get("groupLifetimeInDays"),
            "alternateNotificationEmails": policy.get("alternateNotificationEmails") or None,
        })])
