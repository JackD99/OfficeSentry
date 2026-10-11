"""Teams fixtures (channels, channel members, installed apps, the team activity report), shaped like Graph's.

The teams are ``groupdata``'s: Sales (g1), Project Phoenix (g2) and Board (g4).
"""

import json
import re
from datetime import UTC, datetime, timedelta

import responses

from officesentry.graph import GRAPH_ROOT

V1 = f"{GRAPH_ROOT}/v1.0"
BETA = f"{GRAPH_ROOT}/beta"
TODAY = datetime.now(UTC).date()
HOME = "home-tenant-id"
PARTNER = "partner-tenant-id"


def _channel(cid, name, kind="standard", created="2025-02-01T09:00:00Z"):
    return {"id": cid, "displayName": name, "membershipType": kind, "createdDateTime": created, "tenantId": HOME}


def _member(name, email, roles=(), tenant=HOME):
    return {
        "@odata.type": "#microsoft.graph.aadUserConversationMember",
        "id": f"m-{email}",
        "roles": list(roles),
        "displayName": name,
        "userId": f"u-{email}",
        "email": email,
        "tenantId": tenant,
    }


CHANNELS = {
    "g1": [_channel("c1", "General"), _channel("c2", "Deals", "private")],
    "g2": [
        _channel("c3", "General"),
        _channel("c4", "Design"),
        _channel("c5", "Supplier", "shared", "2026-03-01T09:00:00Z"),
    ],
    "g4": [_channel("c6", "General")],
}
MEMBERS = {
    "c2": [_member("Alice", "alice@x.com", ["owner"]), _member("Bob", "bob@x.com")],
    "c5": [
        _member("Carol", "carol@x.com", ["owner"]),
        _member("Gail Guest", "gail@partner.com", ["guest"]),
        _member("Pat Partner", "pat@partner.com", tenant=PARTNER),
    ],
}


def _app(name, method, version="1.0.0"):
    return {
        "id": f"i-{name}",
        "teamsApp": {"id": f"a-{name}", "displayName": name, "distributionMethod": method},
        "teamsAppDefinition": {"id": f"d-{name}", "displayName": name, "version": version},
    }


APPS = {
    "g1": [_app("Planner", "store"), _app("Expenses bot", "sideloaded", "0.3")],
    "g2": [_app("Planner", "store"), _app("Contoso CRM", "organization")],
    "g4": [],
}


def _ago(days: int) -> str:
    return (TODAY - timedelta(days=days)).isoformat()


def _activity(tid, name, last, users, messages=0):
    return {
        "reportRefreshDate": _ago(2),
        "teamId": tid,
        "teamName": name,
        "teamType": "Private",
        "lastActivityDate": last,
        "isDeleted": False,
        "details": [
            {
                "reportPeriod": 90,
                "activeUsers": users,
                "activeChannels": 1 if users else 0,
                "guests": 0,
                "channelMessages": messages,
                "postMessages": 0,
                "replyMessages": 0,
                "meetingsOrganized": 0,
            }
        ],
    }


ACTIVITY = [
    _activity("g1", "Sales", _ago(3), 6, 120),
    _activity("g2", "Project Phoenix", _ago(20), 3, 14),
    _activity("g4", "Board", None, 0),
]


def mock_teams(rsps, *, channels=None, members=None, apps=None, activity=None, status=200):
    """Graph's Teams calls for the seeded teams. ``status`` answers every per-team call (403: not approved)."""
    channels = CHANNELS if channels is None else channels
    members = MEMBERS if members is None else members
    apps = APPS if apps is None else apps
    denied = {"error": {"code": "Forbidden", "message": "Missing role permissions on the request."}}

    def serve(table):
        def callback(request):
            key = request.path_url.split("?")[0].split("/")[-2]
            if status != 200:
                return status, {}, json.dumps(denied)
            return 200, {}, json.dumps({"value": table.get(key, [])})

        return callback

    for pattern, table in (
        (r"/teams/[^/]+/channels(\?.*)?$", channels),
        (r"/teams/[^/]+/channels/[^/]+/members(\?.*)?$", members),
        (r"/teams/[^/]+/installedApps(\?.*)?$", apps),
    ):
        rsps.add_callback(
            responses.GET, re.compile(re.escape(V1) + pattern), callback=serve(table), content_type="application/json"
        )
    rsps.get(
        f"{BETA}/reports/getTeamsTeamActivityDetail(period='D90')",
        json={"value": ACTIVITY if activity is None else activity},
    )
