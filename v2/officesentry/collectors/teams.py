"""Teams: each team's channels, who is in its private and shared channels (guests and people from other
organisations among them), the apps added to each team, and when each team was last used.

Teams are the Microsoft 365 groups with a team, read with the same request as ``groups``. Channels,
channel members and apps need permissions added in Office Sentry 2.2 (``permission_batch``): until the
client approves them, those parts are skipped and stored as waiting, so the run doesn't fail and the
report says what it's waiting for. Activity comes from Microsoft's Teams activity report
(Reports.Read.All), which runs a couple of days behind and may conceal team names, as in ``usage``.
"""

from __future__ import annotations

from ..devices import parse_day
from ..graph import GraphError, PermissionMissing
from ..permission_batch import BATCH_OF
from ..usage import concealed
from .base import Collector, RunContext, register
from .connection import awaiting
from .groups import GROUP_FIELDS
from .usage import JSON

CHANNELS = "Channel.ReadBasic.All"
MEMBERS = "ChannelMember.Read.All"
APPS = "TeamsAppInstallation.ReadForTeam.All"
PARTS = {"channels": CHANNELS, "members": MEMBERS, "apps": APPS}

CHANNEL_FIELDS = "id,displayName,membershipType,createdDateTime,tenantId"
ACTIVITY_DAYS = 90
ACTIVITY = f"/reports/getTeamsTeamActivityDetail(period='D{ACTIVITY_DAYS}')"
TEAM_LIMIT = 500  # teams whose channels and apps are read (a few calls each)
MEMBERS_KEPT = 200  # people stored per private or shared channel; the counts are always complete
MEMBER_PAGES = 20
# Channels whose members are listed: a standard channel's members are the team's, in the Groups report.
LISTED = ("private", "shared")
# Microsoft's distribution methods for an app, in words.
DISTRIBUTION = {"store": "Teams store", "organization": "Your organisation", "sideloaded": "Custom upload"}


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _day(value) -> str | None:
    day = parse_day(value) if value else None
    return day.isoformat() if day else None


def _member(m: dict, home: str | None) -> dict:
    roles = [r for r in m.get("roles") or [] if isinstance(r, str)]
    tenant = m.get("tenantId")
    return {
        "name": m.get("displayName") or m.get("email") or m.get("userId"),
        "email": m.get("email"),
        "role": "owner" if "owner" in roles else "guest" if "guest" in roles else "member",
        # Someone from another organisation, in a shared channel (a guest has an account in this tenant).
        "external": bool(home and tenant and tenant.lower() != home.lower()),
    }


def _app(installed: dict) -> dict:
    app = installed.get("teamsApp") or {}
    definition = installed.get("teamsAppDefinition") or {}
    method = app.get("distributionMethod")
    return {
        "name": definition.get("displayName") or app.get("displayName") or app.get("id"),
        "appId": app.get("id"),
        "distribution": method,
        "source": DISTRIBUTION.get(method, method),
        "version": definition.get("version"),
    }


def activity_rows(items: list[dict]) -> dict[str, dict]:
    """Each team's last activity and use in the period, by team ID, from getTeamsTeamActivityDetail."""
    rows = {}
    for r in items:
        tid = r.get("teamId")
        if not tid or r.get("isDeleted"):
            continue
        details = next(
            (d for d in r.get("details") or [] if _int(d.get("reportPeriod")) == ACTIVITY_DAYS),
            (r.get("details") or [r])[0],
        )
        rows[tid.lower()] = {
            "lastActivity": _day(r.get("lastActivityDate")),
            "reportDate": _day(r.get("reportRefreshDate")),
            "activeUsers": _int(details.get("activeUsers")),
            "activeChannels": _int(details.get("activeChannels")),
            "messages": _int(details.get("channelMessages"))
            + _int(details.get("postMessages"))
            + _int(details.get("replyMessages")),
            "meetings": _int(details.get("meetingsOrganized")),
            "concealed": concealed(r.get("teamName")),
        }
    return rows


@register
class Teams(Collector):
    key = "teams"
    title = "Teams"
    description = (
        "Each team's channels, the guests and people from other organisations in private and shared "
        "channels, the apps added to each team, and when each team was last used."
    )
    permissions = ("Directory.Read.All", "Reports.Read.All", CHANNELS, MEMBERS, APPS)

    def collect(self, ctx: RunContext) -> None:
        groups = ctx.paged(ctx.graph.get_all("/groups", params={"$select": GROUP_FIELDS, "$top": "999"}))
        teams = sorted(
            (g for g in groups if "Team" in (g.get("resourceProvisioningOptions") or [])),
            key=lambda g: (g.get("displayName") or "").lower(),
        )
        if len(teams) > TEAM_LIMIT:
            ctx.warn(f"Read channels and apps for the first {TEAM_LIMIT} of {len(teams)} teams")
        waiting = awaiting(ctx, *PARTS.values())
        # Each part: "read", "waiting" (a new permission the client hasn't approved) or "failed".
        parts = {part: "waiting" if perm in waiting else "read" for part, perm in PARTS.items()}
        if waiting:
            ctx.log(f"Waiting for the client to approve {', '.join(waiting)}; those parts of Teams are skipped")
        activity, read_activity = self._activity(ctx)

        rows = {}
        for i, team in enumerate(teams):
            tid = team["id"]
            seen = activity.get(tid.lower()) or {}
            row = {
                "displayName": team.get("displayName"),
                "visibility": team.get("visibility"),
                "createdDateTime": team.get("createdDateTime"),
                "mail": team.get("mail"),
                "lastActivity": seen.get("lastActivity"),
                "activeUsers": seen.get("activeUsers"),
                "messages": seen.get("messages"),
                "meetings": seen.get("meetings"),
                "inActivityReport": bool(seen),
                "channels": None,
                "apps": None,
            }
            if i < TEAM_LIMIT:
                if parts["channels"] == "read":
                    row["channels"] = self._channels(ctx, tid, row["displayName"], parts)
                if parts["apps"] == "read":
                    row["apps"] = self._apps(ctx, tid, row["displayName"], parts)
            rows[tid] = row
        ctx.objects("team", list(rows.items()))
        ctx.objects(
            "teams_status",
            [
                (
                    "status",
                    {
                        "parts": parts,
                        "waiting": [PARTS[p] for p, state in parts.items() if state == "waiting"],
                        "activity": read_activity,
                        "activityDays": ACTIVITY_DAYS,
                        "reportDate": max(
                            (a["reportDate"] for a in activity.values() if a["reportDate"]), default=None
                        ),
                        "concealed": any(a["concealed"] for a in activity.values()),
                        "limit": TEAM_LIMIT if len(teams) > TEAM_LIMIT else None,
                    },
                )
            ],
        )

        channels = [c for r in rows.values() for c in r["channels"] or []]
        ctx.metric("teams.total", len(rows))
        if parts["channels"] == "read":
            ctx.metric("teams.private_channels", sum(c["type"] == "private" for c in channels))
            ctx.metric("teams.shared_channels", sum(c["type"] == "shared" for c in channels))
        if parts["members"] == "read":
            ctx.metric("teams.channel_guests", sum(c.get("guestCount") or 0 for c in channels))
            ctx.metric("teams.channel_external", sum(c.get("externalCount") or 0 for c in channels))

    @staticmethod
    def _activity(ctx: RunContext) -> tuple[dict[str, dict], bool]:
        try:
            return activity_rows(ctx.paged(ctx.graph.get_all(ACTIVITY, JSON, version="beta"))), True
        except GraphError as exc:
            ctx.warn(f"The Teams activity report wasn't available: {exc.message}")
            return {}, False

    @staticmethod
    def _missing(ctx: RunContext, parts: dict, part: str, exc: GraphError) -> None:
        """A part's permission turned out to be missing: a new one is waiting, an original one a warning."""
        permission = PARTS[part]
        if isinstance(exc, PermissionMissing) and permission in BATCH_OF:
            parts[part] = "waiting"
            ctx.log(f"Teams {part} skipped: {permission} isn't approved yet ({exc.message})")
        else:
            parts[part] = "failed"
            ctx.warn(f"Teams {part} weren't readable: {exc.message}")

    def _channels(self, ctx: RunContext, tid: str, team: str | None, parts: dict) -> list[dict] | None:
        try:
            found = ctx.paged(ctx.graph.get_all(f"/teams/{tid}/channels", params={"$select": CHANNEL_FIELDS}))
        except PermissionMissing as exc:
            self._missing(ctx, parts, "channels", exc)
            return None
        except GraphError as exc:  # a team being deleted, or one Teams can't open yet
            ctx.log(f"Could not read the channels of {team}: {exc.message}", "warning")
            return None
        channels = []
        for c in found:
            kind = c.get("membershipType") or "standard"
            channel = {
                "id": c.get("id"),
                "name": c.get("displayName"),
                "type": kind,
                "createdDateTime": c.get("createdDateTime"),
                "membersRead": False,
            }
            if kind in LISTED and parts["members"] == "read":
                channel.update(self._members(ctx, tid, c, team, parts))
            channels.append(channel)
        return channels

    def _members(self, ctx: RunContext, tid: str, channel: dict, team: str | None, parts: dict) -> dict:
        try:
            found = ctx.paged(
                ctx.graph.get_all(f"/teams/{tid}/channels/{channel['id']}/members", max_pages=MEMBER_PAGES)
            )
        except PermissionMissing as exc:
            self._missing(ctx, parts, "members", exc)
            return {}
        except GraphError as exc:
            ctx.log(f"Could not read the members of {team} / {channel.get('displayName')}: {exc.message}", "warning")
            return {}
        people = [_member(m, channel.get("tenantId")) for m in found]
        return {
            "membersRead": True,
            "memberCount": len(people),
            "ownerCount": sum(p["role"] == "owner" for p in people),
            "guestCount": sum(p["role"] == "guest" for p in people),
            "externalCount": sum(p["external"] for p in people),
            "members": people[:MEMBERS_KEPT],
        }

    def _apps(self, ctx: RunContext, tid: str, team: str | None, parts: dict) -> list[dict] | None:
        try:
            found = ctx.paged(
                ctx.graph.get_all(f"/teams/{tid}/installedApps", params={"$expand": "teamsApp,teamsAppDefinition"})
            )
        except PermissionMissing as exc:
            self._missing(ctx, parts, "apps", exc)
            return None
        except GraphError as exc:
            ctx.log(f"Could not read the apps of {team}: {exc.message}", "warning")
            return None
        return sorted((_app(a) for a in found), key=lambda a: (a["name"] or "").lower())
