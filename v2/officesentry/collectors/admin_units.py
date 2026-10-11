"""Administrative units: each unit, what it holds and how it is kept up to date.

An administrative unit scopes admin roles to part of a tenant: a branch, a region, a school. This reads every
unit (assigned or dynamic, and whether its members are protected from tenant-wide admins) and how many users,
groups and devices are in each, keeping member IDs so the report and each person's page can name them. The
admins scoped to a unit come from Admin roles, whose role assignments carry the unit as their scope.
"""

from __future__ import annotations

from ..graph import GraphError, PermissionMissing
from .base import Collector, RunContext, register

UNIT_FIELDS = (
    "id,displayName,description,membershipType,membershipRule,membershipRuleProcessingState,visibility,"
    "isMemberManagementRestricted"
)
MEMBER_FIELDS = "id"
UNIT_LIMIT = 500  # units whose members are read (one call each, mostly)
MEMBER_PAGES = 20  # 999 members a page
MEMBER_IDS_KEPT = 5000  # user and group object IDs stored per unit; the counts are always complete


def _kind(member: dict) -> str:
    return (member.get("@odata.type") or "").rsplit(".", 1)[-1] or "unknown"


def _summary(unit: dict) -> dict:
    rule = unit.get("membershipRule")
    state = unit.get("membershipRuleProcessingState")
    return {
        "displayName": unit.get("displayName"),
        "description": (unit.get("description") or "")[:300] or None,
        "dynamic": unit.get("membershipType") == "Dynamic" or bool(rule),
        "rule": rule if isinstance(rule, str) and rule else None,
        "ruleState": state if isinstance(state, str) and state else None,
        "hidden": unit.get("visibility") == "HiddenMembership",
        "restricted": unit.get("isMemberManagementRestricted") is True,
        "examined": False,
    }


@register
class AdminUnits(Collector):
    key = "admin_units"
    title = "Administrative units"
    description = (
        "Every administrative unit, whether it is assigned or dynamic and restricted, and the users, groups and "
        "devices in each. The admins scoped to each unit come from Admin roles."
    )
    permissions = ("Directory.Read.All",)

    def collect(self, ctx: RunContext) -> None:
        try:
            units = ctx.paged(ctx.graph.get_all("/directory/administrativeUnits", params={"$select": UNIT_FIELDS}))
        except PermissionMissing as exc:
            ctx.warn(f"Administrative units weren't readable: {exc.message}")
            ctx.objects("admin_unit_status", [("status", {"available": False, "reason": exc.message})])
            return
        rows = {u["id"]: _summary(u) for u in units if isinstance(u.get("id"), str)}
        order = sorted(rows, key=lambda uid: (rows[uid]["displayName"] or "").lower())
        if len(order) > UNIT_LIMIT:
            ctx.warn(f"Read the members of the first {UNIT_LIMIT} of {len(order)} administrative units")
        for uid in order[:UNIT_LIMIT]:
            try:
                rows[uid].update(self._members(ctx, uid))
            except PermissionMissing as exc:
                ctx.warn(f"Members of administrative units weren't readable: {exc.message}")
                break
            except GraphError as exc:  # e.g. deleted since the list was read
                ctx.log(f"Could not read the members of {rows[uid]['displayName']}: {exc.message}", "warning")
        ctx.objects("admin_unit", list(rows.items()))
        ctx.objects("admin_unit_status", [("status", {"available": True, "units": len(rows)})])

        examined = [u for u in rows.values() if u["examined"]]
        ctx.metric("admin_units.total", len(rows))
        ctx.metric("admin_units.restricted", sum(u["restricted"] for u in rows.values()))
        ctx.metric("admin_units.dynamic", sum(u["dynamic"] for u in rows.values()))
        ctx.metric("admin_units.empty", sum(u["memberCount"] == 0 for u in examined))

    @staticmethod
    def _members(ctx: RunContext, uid: str) -> dict:
        result = ctx.graph.get_all(
            f"/directory/administrativeUnits/{uid}/members",
            params={"$select": MEMBER_FIELDS, "$top": "999"},
            max_pages=MEMBER_PAGES,
        )
        kinds = [_kind(m) for m in result.items]
        users = [m["id"] for m in result.items if _kind(m) == "user" and isinstance(m.get("id"), str)]
        groups = [m["id"] for m in result.items if _kind(m) == "group" and isinstance(m.get("id"), str)]
        return {
            "examined": True,
            "memberCount": len(result.items),
            "membersComplete": not result.truncated,
            "userCount": kinds.count("user"),
            "groupCount": kinds.count("group"),
            "deviceCount": kinds.count("device"),
            "userIds": users[:MEMBER_IDS_KEPT],
            "groupIds": groups[:MEMBER_IDS_KEPT],
        }
