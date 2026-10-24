"""Microsoft 365 usage report payloads, in the shape the beta JSON reports return (recorded from a real tenant)."""

from datetime import UTC, datetime, timedelta

from officesentry.graph import GRAPH_ROOT

BETA = f"{GRAPH_ROOT}/beta"
PLATFORMS = ("Windows", "Mac", "Mobile", "Web")
APPS = ("outlook", "word", "excel", "powerPoint", "oneNote", "teams")


def days_ago(n: int | None) -> str | None:
    return None if n is None else (datetime.now(UTC) - timedelta(days=n)).date().isoformat()


REFRESHED = days_ago(2)


def active_user(
    upn, products, *, name=None, exchange=None, teams=None, onedrive=None, sharepoint=None, deleted=False, licensed=True
):
    row = {
        "reportRefreshDate": REFRESHED,
        "userPrincipalName": upn,
        "displayName": name or upn.split("@")[0].title(),
        "isDeleted": deleted,
        "deletedDate": None,
        "assignedProducts": products,
        "hasSkypeForBusinessLicense": False,
        "hasYammerLicense": False,
        "skypeForBusinessLastActivityDate": None,
        "yammerLastActivityDate": None,
    }
    for prefix, last in (("exchange", exchange), ("teams", teams), ("oneDrive", onedrive), ("sharePoint", sharepoint)):
        row[f"has{prefix[0].upper()}{prefix[1:]}License"] = licensed
        row[f"{prefix}LastActivityDate"] = days_ago(last)
        row[f"{prefix}LicenseAssignDate"] = days_ago(400) if licensed else None
    return row


def app_user(upn, last=None, **used):
    """``used``: app name -> platforms, e.g. word=("Windows", "Web")."""
    details = {"reportPeriod": 30, **{p.lower(): False for p in PLATFORMS}, **{a: False for a in APPS}}
    for app in APPS:
        for p in PLATFORMS:
            details[f"{app}{p}"] = p in used.get(app, ())
        details[app] = bool(used.get(app))
    for p in PLATFORMS:
        details[p.lower()] = any(p in v for v in used.values())
    return {
        "reportRefreshDate": REFRESHED,
        "userPrincipalName": upn,
        "lastActivationDate": "",
        "lastActivityDate": days_ago(last) or "",
        "details": [details],
    }


ACTIVE_USERS = [
    # Email only on Business Premium: could move to Exchange Online.
    active_user("ann@x.com", ["MICROSOFT 365 BUSINESS PREMIUM", "MICROSOFT POWER AUTOMATE FREE"], exchange=1),
    # Disabled account: Licence waste covers it.
    active_user("bob@x.com", ["MICROSOFT 365 BUSINESS PREMIUM"]),
    # Business Standard, web apps only: could move to Business Basic.
    active_user("carol@x.com", ["MICROSOFT 365 BUSINESS STANDARD"], exchange=2, teams=3, onedrive=10),
    # Nothing used at all.
    active_user("dan@x.com", ["MICROSOFT 365 BUSINESS STANDARD"], exchange=120),
    # Uses everything but SharePoint.
    active_user("erin@x.com", ["OFFICE 365 E3"], exchange=1, teams=1, onedrive=4),
    active_user("left@x.com", ["OFFICE 365 E3"], deleted=True),
    active_user("free@x.com", ["MICROSOFT POWER AUTOMATE FREE"], licensed=False),
]
APP_USERS = [
    app_user("ann@x.com", 1, outlook=("Web",)),
    app_user("carol@x.com", 3, outlook=("Web",), word=("Web",), teams=("Web", "Mobile")),
    app_user("dan@x.com"),
    app_user("erin@x.com", 1, outlook=("Windows",), word=("Windows",), excel=("Windows", "Mac")),
]
ACTIVATIONS = [
    {
        "reportRefreshDate": REFRESHED,
        "userPrincipalName": "erin@x.com",
        "displayName": "Erin",
        "userActivationCounts": [
            {
                "productType": "MICROSOFT 365 APPS FOR ENTERPRISE",
                "lastActivatedDate": days_ago(200),
                "windows": 1,
                "mac": 1,
                "windows10Mobile": 0,
                "ios": 1,
                "android": 0,
                "activatedOnSharedComputer": False,
            }
        ],
    },
]
EMAIL = [
    {
        "reportRefreshDate": REFRESHED,
        "userPrincipalName": u,
        "displayName": u.split("@")[0].title(),
        "isDeleted": False,
        "lastActivityDate": days_ago(1),
        "sendCount": sent,
        "receiveCount": received,
        "readCount": received,
        "meetingCreatedCount": 0,
        "meetingInteractedCount": 0,
        "assignedProducts": [],
        "reportPeriod": "30",
    }
    for u, sent, received in (("ann@x.com", 40, 300), ("carol@x.com", 12, 80), ("erin@x.com", 70, 500))
]
TEAMS = [
    {
        "reportRefreshDate": REFRESHED,
        "userId": "00000000-0000-0000-0000-000000000000",
        "userPrincipalName": u,
        "lastActivityDate": days_ago(1),
        "isDeleted": False,
        "isExternal": False,
        "assignedProducts": [],
        "teamChatMessageCount": chats,
        "privateChatMessageCount": chats * 2,
        "callCount": 1,
        "meetingCount": meetings,
        "isLicensed": True,
        "reportPeriod": "30",
    }
    for u, chats, meetings in (("carol@x.com", 5, 2), ("erin@x.com", 20, 9))
]


# Copilot's per-person report names each app's last activity date "<prefix>LastActivityDate".
COPILOT_PREFIXES = {
    "chat": "copilotChat",
    "teams": "microsoftTeamsCopilot",
    "word": "wordCopilot",
    "excel": "excelCopilot",
    "powerPoint": "powerPointCopilot",
    "outlook": "outlookCopilot",
    "oneNote": "oneNoteCopilot",
    "loop": "loopCopilot",
}


def copilot_user(upn, last=None, **apps):
    """A row of Copilot's per-person report (Microsoft's sample): ``last`` days since any use, and days since
    each app's (``word=3``, ``chat=1``)."""
    row = {
        "reportRefreshDate": REFRESHED,
        "userPrincipalName": upn,
        "displayName": upn.split("@")[0].title(),
        "lastActivityDate": days_ago(last) or "",
        "copilotActivityUserDetailsByPeriod": [{"reportPeriod": 90}],
    }
    for app, prefix in COPILOT_PREFIXES.items():
        row[f"{prefix}LastActivityDate"] = days_ago(apps.get(app)) or ""
    return row


def copilot_trend(active_by_day, enabled=3):
    """Copilot's daily counts, as Microsoft returns them: ``active_by_day`` people per day, oldest first, ending on
    the report's refresh date."""
    days = len(active_by_day)
    return [
        {
            "reportRefreshDate": REFRESHED,
            "reportPeriod": 90,
            "adoptionByDate": [
                {
                    "reportDate": days_ago(2 + days - 1 - i),
                    "anyAppEnabledUsers": enabled,
                    "anyAppActiveUsers": active,
                    "microsoftTeamsEnabledUsers": enabled,
                    "microsoftTeamsActiveUsers": min(active, 1),
                    "wordEnabledUsers": enabled,
                    "wordActiveUsers": active,
                    "copilotChatEnabledUsers": enabled,
                    "copilotChatActiveUsers": active,
                }
                for i, active in enumerate(active_by_day)
            ],
        }
    ]


def mock_usage(
    rsps,
    *,
    active=None,
    apps=None,
    status=200,
    copilot_status=200,
    concealed=False,
    copilot=None,
    trend=None,
    trend_status=200,
):
    def get(path, value, code=200):
        body = {"value": value} if code == 200 else {"error": {"code": "Forbidden", "message": "Denied"}}
        rsps.get(f"{BETA}/reports/{path}", json=body, status=code)

    get("getOffice365ActiveUserDetail(period='D30')", ACTIVE_USERS if active is None else active, status)
    get("getM365AppUserDetail(period='D30')", APP_USERS if apps is None else apps)
    get("getOffice365ActivationsUserDetail", ACTIVATIONS)
    get("getEmailActivityUserDetail(period='D30')", EMAIL)
    get("getTeamsUserActivityUserDetail(period='D30')", TEAMS)
    rsps.get(
        f"{GRAPH_ROOT}/v1.0/admin/reportSettings",
        json={"id": "reportSettings", "displayConcealedNames": concealed},
    )
    error = {"error": {"code": "BadRequest", "message": "Bad period"}}
    body = {"value": copilot or []} if copilot_status == 200 else error
    rsps.get(
        f"{BETA}/copilot/reports/getMicrosoft365CopilotUsageUserDetail(period='D90')", json=body, status=copilot_status
    )
    rsps.get(
        f"{BETA}/copilot/reports/getMicrosoft365CopilotUserCountTrend(period='D90')",
        json={"value": trend or []} if trend_status == 200 else error,
        status=trend_status,
    )
