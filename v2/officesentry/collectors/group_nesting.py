"""Group nesting: the members of security groups and distribution lists, and dynamic membership rules.

Groups and Teams reads the owners and members of teams and Microsoft 365 groups, which can't contain other
groups. This reads the other groups' members: how many there are and which of them are groups, so the
report can show empty groups and groups inside groups. Members are object IDs only; nothing about the
people is kept.
"""

from __future__ import annotations

from ..graph import GraphError, PermissionMissing
from .base import Collector, RunContext, register
from .groups import group_kind

GROUP_FIELDS = (
    "id,displayName,groupTypes,mailEnabled,securityEnabled,membershipRule,membershipRuleProcessingState,"
    "onPremisesSyncEnabled,isAssignableToRole,resourceProvisioningOptions,createdDateTime,mail"
)
GROUP_LIMIT = 2000  # security groups and distribution lists whose members are read (one call each, mostly)
MEMBER_PAGES = 20  # 999 members a page
CHILDREN_KEPT = 200  # groups inside a group, by ID
USERS_KEPT = 5000  # people in a group, by ID, so the report can name blocked accounts still in it


def _summary(group: dict) -> dict:
    rule = group.get("membershipRule")
    state = group.get("membershipRuleProcessingState")
    return {
        "displayName": group.get("displayName"),
        "kind": group_kind(group),
        "isTeam": "Team" in (group.get("resourceProvisioningOptions") or []),
        "mail": group.get("mail"),
        "createdDateTime": group.get("createdDateTime"),
        "dynamic": "DynamicMembership" in (group.get("groupTypes") or []) or bool(rule),
        "rule": rule if isinstance(rule, str) and rule else None,
        "ruleState": state if isinstance(state, str) and state else None,
        "synced": bool(group.get("onPremisesSyncEnabled")),
        "roleAssignable": group.get("isAssignableToRole") is True,
        "examined": False,
    }


@register
class GroupNesting(Collector):
    key = "group_nesting"
    title = "Group nesting"
    description = (
        "How many members each security group and distribution list has, the groups inside each, and "
        "dynamic membership rules."
    )
    permissions = ("Directory.Read.All",)

    def collect(self, ctx: RunContext) -> None:
        groups = ctx.paged(ctx.graph.get_all("/groups", params={"$select": GROUP_FIELDS, "$top": "999"}))
        rows = {g["id"]: _summary(g) for g in groups if g.get("id")}
        others = sorted(
            (gid for gid, g in rows.items() if g["kind"] != "microsoft365"),
            key=lambda gid: (rows[gid]["displayName"] or "").lower(),
        )
        if len(others) > GROUP_LIMIT:
            ctx.warn(f"Read members of the first {GROUP_LIMIT} of {len(others)} security groups and distribution lists")
        for gid in others[:GROUP_LIMIT]:
            try:
                rows[gid].update(self._members(ctx, gid))
            except PermissionMissing as exc:
                ctx.warn(f"Group members weren't readable: {exc.message}")
                break
            except GraphError as exc:  # e.g. deleted since the list was read
                ctx.log(f"Could not read the members of {rows[gid]['displayName']}: {exc.message}", "warning")
        ctx.objects("group", list(rows.items()))

        examined = [g for g in rows.values() if g["examined"]]
        ctx.metric("group_nesting.examined", len(examined))
        ctx.metric("group_nesting.empty", sum(g["memberCount"] == 0 for g in examined))
        ctx.metric("group_nesting.containing_groups", sum(bool(g["groupIds"]) for g in examined))
        ctx.metric("group_nesting.dynamic", sum(g["dynamic"] for g in rows.values()))

    @staticmethod
    def _members(ctx: RunContext, gid: str) -> dict:
        result = ctx.graph.get_all(
            f"/groups/{gid}/members", params={"$select": "id", "$top": "999"}, max_pages=MEMBER_PAGES
        )
        by_type: dict[str, list[str]] = {"group": [], "user": []}
        for m in result.items:
            kind = (m.get("@odata.type") or "").rsplit(".", 1)[-1]
            if kind in by_type and isinstance(m.get("id"), str):
                by_type[kind].append(m["id"])
        groups, users = by_type["group"], by_type["user"]
        return {
            "examined": True,
            "memberCount": len(result.items),
            "membersComplete": not result.truncated,
            "groupIds": groups[:CHILDREN_KEPT],
            "groupCount": len(groups),
            "userIds": users[:USERS_KEPT],
            "userIdsComplete": len(users) <= USERS_KEPT,
        }
