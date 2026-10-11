"""Every call Office Sentry makes to Microsoft: path, version, permission, throttle limits and fallback.

One list (``GRAPH`` and ``EXCHANGE``) says, for each Graph path and Exchange
cmdlet the collectors use, which permission it needs, which of Microsoft's
throttling limits (``LIMITS``) it counts against and how many units it costs,
and what the collector does when Microsoft can't answer. The shared limiter
(``ratelimit.py``) reads it before each request, and run telemetry groups
requests by its paths. ``tests/test_ratelimit.py`` fails when a collector
calls something that isn't listed here.

Limits are Microsoft's documented defaults, and Microsoft changes them:
https://learn.microsoft.com/graph/throttling-limits and, for SharePoint and
OneDrive, https://learn.microsoft.com/sharepoint/dev/general-development/how-to-avoid-getting-throttled-or-blocked-in-sharepoint-online.
Where a limit depends on tenant size, the smallest tier is used.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlsplit

GRAPH_DOCS = "https://learn.microsoft.com/graph/throttling-limits"
SHAREPOINT_DOCS = (
    "https://learn.microsoft.com/sharepoint/dev/general-development/"
    "how-to-avoid-getting-throttled-or-blocked-in-sharepoint-online"
)


@dataclass(frozen=True)
class Limit:
    """One of Microsoft's throttling limits: ``units`` per ``seconds``.

    ``scope`` says who shares it: ``app`` is one bucket for the app across every
    tenant, ``tenant`` one per app per tenant, ``api`` one per app per tenant per
    endpoint. ``units`` None means Microsoft documents no rate: requests aren't
    paced, but a 429 still pauses the other threads. ``resource_units``: a request
    costs its endpoint's ``cost`` here, not 1.
    """

    name: str
    units: int | None
    seconds: float
    scope: str
    title: str
    source: str = GRAPH_DOCS
    resource_units: bool = False


LIMITS: dict[str, Limit] = {
    limit.name: limit
    for limit in (
        Limit("graph", 130_000, 10, "app", "Graph, any request"),
        # Resource units; this is the tier for tenants under 50 users (larger tenants get 5,000 and 8,000).
        Limit("directory", 3_500, 10, "tenant", "Users, groups and directory", resource_units=True),
        Limit("signin_activity", 10, 60, "app", "Last sign-in times"),
        Limit("identity_reports", 122, 10, "app", "Sign-in and audit logs"),
        Limit("identity_reports_tenant", 5, 10, "tenant", "Sign-in and audit logs"),
        Limit("identity_protection", 1, 1, "tenant", "Identity Protection"),
        Limit("usage_reports", 100, 600, "api", "Microsoft 365 usage reports"),  # JSON (beta); CSV allows 14
        Limit("secure_score", 150, 60, "tenant", "Secure Score"),
        Limit("intune", 2_000, 20, "tenant", "Intune devices"),
        Limit("outlook", 10_000, 600, "tenant", "Outlook mail settings"),  # Microsoft's is per mailbox
        # Resource units; this is the tier for up to 1,000 licences (more licences allow up to 5 times as many).
        Limit("sharepoint", 1_250, 60, "tenant", "SharePoint and OneDrive", SHAREPOINT_DOCS, resource_units=True),
        # Microsoft's lowest per-app-per-tenant rate for the Teams calls made (installed apps; channels and
        # members allow 60), in requests per second.
        Limit("teams", 30, 1, "tenant", "Microsoft Teams"),
        Limit("exchange", None, 0, "tenant", "Exchange admin API"),  # no documented rate
        Limit(
            "message_trace",
            100,
            300,
            "tenant",
            "Exchange message trace",
            "https://learn.microsoft.com/powershell/module/exchangepowershell/get-messagetracev2",
        ),
    )
}


@dataclass(frozen=True)
class Endpoint:
    """One Microsoft call. ``path`` is version-relative with ``{placeholders}`` for IDs (Graph),
    or the cmdlet name (Exchange). ``limits`` are names in ``LIMITS``; every Graph call also
    counts against ``graph``. ``cost`` is the resource units it uses where its limits count
    units (SharePoint, directory), else 1. ``select`` makes the entry match only when the
    request's ``$select`` includes that field. ``beta`` says why a Graph call uses beta, what v1.0 lacks;
    ``tests/test_endpoints.py`` fails on a beta entry without one, and the run telemetry tests on a beta call
    that isn't listed as beta."""

    path: str
    versions: tuple[str, ...]
    permission: str
    limits: tuple[str, ...] = ()
    cost: int = 1
    fallback: str = ""
    select: str = ""
    api: str = "graph"
    beta: str = ""
    pattern: re.Pattern = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        parts = re.split(r"(\{[^}]+\})", self.path)
        regex = "".join("[^/]+" if p.startswith("{") else re.escape(p) for p in parts)
        object.__setattr__(self, "pattern", re.compile(regex + r"\Z"))

    def key(self, version: str = "") -> str:
        """How telemetry names it: ``"v1.0 /users/{id}/drive"``, ``"Exchange Get-Mailbox"``."""
        if self.api == "exchange":
            return f"Exchange {self.path}"
        return f"{version or self.versions[0]} {self.path}" + (f" ({self.select})" if self.select else "")


V1, BETA, BOTH = ("v1.0",), ("beta",), ("v1.0", "beta")
SP_ONE, SP_MANY, SP_PERMISSIONS = 1, 2, 5  # SharePoint resource units per request

GRAPH: tuple[Endpoint, ...] = (
    # Users and directory. Entra ID P1 is needed for signInActivity: without it users are read without.
    Endpoint(
        "/users",
        V1,
        "User.Read.All, AuditLog.Read.All",
        ("signin_activity", "directory"),
        cost=2,
        select="signInActivity",
        fallback="Without Entra ID P1, users are read without sign-in activity",
    ),
    Endpoint("/users", V1, "User.Read.All", ("directory",), cost=2),
    Endpoint(
        "/users/{id}/authentication/methods",
        V1,
        "UserAuthenticationMethod.Read.All",
        ("directory",),
        fallback="Only used when the registration report is empty",
    ),
    Endpoint(
        "/users/{id}/authentication/requirements",
        BETA,
        "Policy.Read.All",
        ("directory",),
        fallback="Per-user MFA shows as unknown if it fails",
        beta="Per-user MFA state is only in beta",
    ),
    Endpoint("/groups", V1, "Directory.Read.All", ("directory",)),
    Endpoint("/groups/{id}", V1, "Directory.Read.All", ("directory",)),
    Endpoint("/groups/{id}/members", V1, "Directory.Read.All", ("directory",), cost=3),
    Endpoint("/groups/{id}/owners", V1, "Directory.Read.All", ("directory",)),
    Endpoint("/groups/{id}/transitiveMembers", V1, "Directory.Read.All", ("directory",), cost=5),
    Endpoint("/groupSettings", V1, "Directory.Read.All", ("directory",)),
    Endpoint("/groupLifecyclePolicies", V1, "Directory.Read.All", ("directory",)),
    Endpoint("/devices", V1, "Directory.Read.All", ("directory",)),
    Endpoint("/domains", V1, "Directory.Read.All", ("directory",)),
    Endpoint(
        "/organization",
        BOTH,
        "Organization.Read.All",
        ("directory",),
        beta="Only for onPremisesLastPasswordSyncDateTime, which v1.0 doesn't have",
    ),
    Endpoint("/subscribedSkus", V1, "Organization.Read.All", ("directory",)),
    Endpoint("/directory/subscriptions", V1, "Organization.Read.All", ("directory",)),
    Endpoint("/directory/deletedItems/microsoft.graph.user", V1, "Directory.Read.All", ("directory",)),
    Endpoint("/directory/deletedItems/microsoft.graph.group", V1, "Directory.Read.All", ("directory",)),
    Endpoint("/directory/administrativeUnits", V1, "Directory.Read.All", ("directory",)),
    Endpoint("/directory/administrativeUnits/{id}/members", V1, "Directory.Read.All", ("directory",), cost=3),
    Endpoint("/applications", V1, "Application.Read.All", ("directory",)),
    Endpoint("/servicePrincipals", V1, "Application.Read.All", ("directory",)),
    Endpoint("/servicePrincipals/{id}", V1, "Application.Read.All", ("directory",)),
    Endpoint("/servicePrincipals/{id}/appRoleAssignments", V1, "Application.Read.All", ("directory",)),
    Endpoint("/oauth2PermissionGrants", V1, "Directory.Read.All", ("directory",)),
    Endpoint("/roleManagement/directory/roleDefinitions", V1, "RoleManagement.Read.Directory", ("directory",)),
    Endpoint("/roleManagement/directory/roleAssignments", V1, "RoleManagement.Read.Directory", ("directory",)),
    Endpoint(
        "/roleManagement/directory/roleEligibilitySchedules",
        V1,
        "RoleManagement.Read.Directory",
        ("directory",),
        fallback="Needs Entra ID P2; skipped without it",
    ),
    # Policies.
    Endpoint("/policies/authorizationPolicy", V1, "Policy.Read.All", ("directory",)),
    Endpoint("/policies/adminConsentRequestPolicy", V1, "Policy.Read.All", ("directory",)),
    Endpoint("/policies/identitySecurityDefaultsEnforcementPolicy", V1, "Policy.Read.All", ("directory",)),
    Endpoint("/policies/authenticationMethodsPolicy", V1, "Policy.Read.All", ("directory",)),
    Endpoint(
        "/identity/conditionalAccess/policies",
        V1,
        "Policy.Read.All",
        ("directory",),
        fallback="Needs Entra ID P1; security defaults are read instead",
    ),
    Endpoint("/admin/sharepoint/settings", V1, "SharePointTenantSettings.Read.All"),
    # Sign-in and audit logs, and the MFA registration report: the identity and access reports limits.
    Endpoint(
        "/auditLogs/signIns",
        BOTH,
        "AuditLog.Read.All",
        ("identity_reports_tenant", "identity_reports"),
        fallback="Timeouts split into one-day windows",
        beta="authenticationRequirement (MFA or password only) is only in beta; asks for events.SIGNIN_FIELDS "
        "only, since returning every field brings Microsoft's Deprecation and Sunset headers",
    ),
    Endpoint("/auditLogs/directoryAudits", V1, "AuditLog.Read.All", ("identity_reports_tenant", "identity_reports")),
    Endpoint(
        "/reports/authenticationMethods/userRegistrationDetails",
        V1,
        "AuditLog.Read.All",
        ("identity_reports_tenant", "identity_reports"),
        fallback="Needs Entra ID P1; per-user methods are read instead",
    ),
    Endpoint(
        "/reports/servicePrincipalSignInActivities",
        BETA,
        "AuditLog.Read.All",
        ("identity_reports_tenant", "identity_reports"),
        fallback="Unused apps shows why when the tenant's licence or consent doesn't allow it",
        beta="App sign-in activity is only in beta",
    ),
    Endpoint(
        "/identityProtection/riskyUsers",
        V1,
        "IdentityRiskyUser.Read.All",
        ("identity_protection",),
        fallback="Needs Entra ID P2; shown as unavailable without it",
    ),
    # Teams (permissions added in 2.2; skipped while the client hasn't approved them).
    Endpoint(
        "/teams/{id}/channels",
        V1,
        "Channel.ReadBasic.All",
        ("teams",),
        fallback="Skipped, and the Teams report says so, until the client approves the permission",
    ),
    Endpoint(
        "/teams/{id}/channels/{id}/members",
        V1,
        "ChannelMember.Read.All",
        ("teams",),
        fallback="Only private and shared channels; skipped until the client approves the permission",
    ),
    Endpoint(
        "/teams/{id}/installedApps",
        V1,
        "TeamsAppInstallation.ReadForTeam.All",
        ("teams",),
        fallback="Skipped, and the Teams report says so, until the client approves the permission",
    ),
    # Microsoft 365 usage reports. v1.0 returns CSV only, so these use beta's JSON.
    *(
        Endpoint(
            f"/reports/{name}",
            BETA,
            "Reports.Read.All",
            ("usage_reports",),
            beta="v1.0 returns CSV only; beta returns JSON",
        )
        for name in (
            "getOffice365ActiveUserDetail(period={period})",
            "getM365AppUserDetail(period={period})",
            "getOffice365ActivationsUserDetail",
            "getEmailActivityUserDetail(period={period})",
            "getTeamsUserActivityUserDetail(period={period})",
            "getTeamsTeamActivityDetail(period={period})",
            "getSharePointSiteUsageDetail(period={period})",
            "getOneDriveUsageAccountDetail(period={period})",
            "getSharePointSiteUsageStorage(period={period})",
            "getOneDriveUsageStorage(period={period})",
        )
    ),
    Endpoint(
        "/admin/reportSettings",
        V1,
        "ReportSettings.Read.All",
        fallback="Concealed names are told from the codes in the reports until the client approves the permission",
    ),
    Endpoint(
        "/copilot/reports/getMicrosoft365CopilotUsageUserDetail(period={period})",
        BETA,
        "Reports.Read.All",
        ("usage_reports",),
        fallback="Skipped when the tenant has no Copilot licences",
        beta="Copilot usage is only in beta",
    ),
    # Security and devices.
    Endpoint("/security/secureScores", V1, "SecurityEvents.Read.All", ("secure_score",)),
    Endpoint("/security/secureScoreControlProfiles", V1, "SecurityEvents.Read.All", ("secure_score",)),
    Endpoint(
        "/deviceManagement/managedDevices",
        V1,
        "DeviceManagementManagedDevices.Read.All",
        ("intune",),
        fallback="Needs Intune; shown as unavailable without it",
    ),
    # Mailbox settings.
    Endpoint("/users/{id}/mailFolders/inbox/messageRules", V1, "MailboxSettings.Read", ("outlook",)),
    Endpoint("/users/{id}/mailboxSettings/automaticRepliesSetting", V1, "MailboxSettings.Read", ("outlook",)),
    # SharePoint and OneDrive, in SharePoint resource units.
    Endpoint(
        "/sites/getAllSites",
        V1,
        "Sites.Read.All",
        ("sharepoint",),
        cost=SP_MANY,
        fallback="Listed once a night; the second SharePoint collector reuses it (sharepoint_lookup)",
    ),
    Endpoint("/sites/{site}/drive", V1, "Sites.Read.All", ("sharepoint",), cost=SP_ONE),
    Endpoint("/sites/{site}/drives", V1, "Sites.Read.All", ("sharepoint",), cost=SP_MANY),
    Endpoint("/sites/{site}/drive/root/permissions", V1, "Sites.Read.All", ("sharepoint",), cost=SP_PERMISSIONS),
    Endpoint("/sites/{site}/lists", V1, "Sites.Read.All", ("sharepoint",), cost=SP_MANY),
    Endpoint("/sites/{site}/lists/{list}/items", V1, "Sites.Read.All", ("sharepoint",), cost=SP_MANY),
    Endpoint(
        "/sites/{site}/recycleBin/items",
        BETA,
        "Sites.Read.All",
        ("sharepoint",),
        cost=SP_MANY,
        fallback="The recycle bin's size still comes from the site's quota",
        beta="Listing a site's recycle bin is only in beta",
    ),
    Endpoint(
        "/users/{id}/drive",
        V1,
        "Sites.Read.All",
        ("sharepoint",),
        cost=SP_ONE,
        fallback="Asked once a night for both SharePoint collectors; 'no OneDrive' is kept for 7 days",
    ),
    Endpoint("/drives/{drive}/list", V1, "Sites.Read.All", ("sharepoint",), cost=SP_ONE),
    Endpoint("/drives/{drive}/list/items", V1, "Sites.Read.All", ("sharepoint",), cost=SP_MANY),
    Endpoint(
        "/drives/{drive}/root/delta",
        V1,
        "Sites.Read.All",
        ("sharepoint",),
        cost=SP_MANY,
        fallback="Read without a delta token, so each page costs 2 units instead of 1",
    ),
    Endpoint("/drives/{drive}/items/{item}/permissions", V1, "Sites.Read.All", ("sharepoint",), cost=SP_PERMISSIONS),
)

EXCHANGE: tuple[Endpoint, ...] = tuple(
    Endpoint(
        cmdlet,
        ("beta",),
        "Exchange.ManageAsApp and Global Reader",
        ("exchange",),
        api="exchange",
        fallback="Undocumented InvokeCommand endpoint; the v2.0 admin API doesn't cover this cmdlet yet",
    )
    for cmdlet in (
        "Get-Mailbox",
        "Get-MailboxPermission",
        "Get-MailboxStatistics",
        "Get-CASMailbox",
        "Get-InboxRule",
        "Get-Recipient",
        "Get-RecipientPermission",
        "Get-OrganizationConfig",
        "Get-TransportConfig",
        "Get-RemoteDomain",
        "Get-HostedOutboundSpamFilterPolicy",
        "Get-AcceptedDomain",
        "Get-MobileDevice",
        "Get-MobileDeviceStatistics",
        "Get-DistributionGroup",
        "Get-DistributionGroupMember",
    )
) + (
    Endpoint(
        "Get-MessageTraceV2",
        ("beta",),
        "Exchange.ManageAsApp and Global Reader",
        ("exchange", "message_trace"),
        api="exchange",
        fallback="10 days a query; list activity and mail flow are kept as daily counts so each run reads "
        "only what's new. Without access, lists are shown without activity and mail flow isn't counted",
    ),
    Endpoint(
        "Get-MailTrafficSummaryReport",
        ("beta",),
        "Exchange.ManageAsApp and Global Reader",
        ("exchange",),
        api="exchange",
        fallback="Undocumented InvokeCommand endpoint; the top spam and phishing recipients are left out without it",
    ),
)

_EXCHANGE = {e.path: e for e in EXCHANGE}
_ID = re.compile(
    r"/(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|b![^/]+|[^/]*,[^/]*)(?=/|$)", re.IGNORECASE
)


def _selected(url: str, params: dict | None) -> set[str]:
    query = dict(parse_qsl(urlsplit(url).query))
    query.update(params or {})
    return set(str(query.get("$select", "")).split(","))


def split(url: str) -> tuple[str, str]:
    """``(version, path)`` of a full Graph URL, e.g. ``("v1.0", "/users/abc/drive")``."""
    path = urlsplit(url).path
    _, version, rest = (path.split("/", 2) + ["", ""])[:3]
    return version, "/" + rest


def graph(url: str, params: dict | None = None) -> Endpoint | None:
    """The catalogue entry for a Graph request (full URL, as sent, plus its query parameters)."""
    version, path = split(url)
    for endpoint in GRAPH:
        if (
            version in endpoint.versions
            and endpoint.pattern.match(path)
            and (not endpoint.select or endpoint.select in _selected(url, params))
        ):
            return endpoint
    return None


def graph_key(url: str, params: dict | None = None) -> str:
    """How telemetry groups a Graph request: its catalogue path, or (not catalogued) the path with IDs blanked."""
    endpoint = graph(url, params)
    version, path = split(url)
    if endpoint is not None:
        return endpoint.key(version)
    return f"{version} {_ID.sub('/{id}', path)}"


def exchange(cmdlet: str) -> Endpoint | None:
    return _EXCHANGE.get(cmdlet)
