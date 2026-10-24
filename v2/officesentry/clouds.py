"""Microsoft 365 national clouds: every Microsoft endpoint Office Sentry signs in to or reads from.

Each tenant lives in exactly one cloud (``tenants.cloud``), and so does each app
registration (``app_connections.cloud``). The clouds are separate: an app
registered in commercial Entra ID can't sign in to a GCC High, DoD or China
tenant, so every cloud needs its own app registration (its own client ID and
certificate), created in that cloud's Entra admin centre.

GCC (Government Community Cloud, "moderate") is not listed separately: GCC
tenants sign in and use Graph and Exchange through the commercial endpoints.

Hosts per cloud (Microsoft's "national cloud deployments" documentation):

=============  ==========================  ===================================  ======================
Cloud          Sign-in                     Microsoft Graph                      Exchange admin API
=============  ==========================  ===================================  ======================
commercial     login.microsoftonline.com   graph.microsoft.com                  outlook.office365.com
gcc_high       login.microsoftonline.us    graph.microsoft.us                   outlook.office365.us
dod            login.microsoftonline.us    dod-graph.microsoft.us               webmail.apps.mil
china          login.chinacloudapi.cn      microsoftgraph.chinacloudapi.cn      partner.outlook.cn
=============  ==========================  ===================================  ======================

The audit log add-on reads the Office 365 Management Activity feed (``auditlog/feed.py``), which has a host of
its own per cloud (``management``): ``manage.office.com``, ``manage.office365.us`` (GCC High) and
``manage.protection.apps.mil`` (DoD). Microsoft documents none for China, so the add-on is ``missing`` there.
GCC (moderate) tenants read theirs from ``manage-gcc.office.com``, which the commercial cloud can't tell
apart yet, so the add-on doesn't serve them.

SharePoint hostnames differ too (``*.sharepoint.com``, ``*.sharepoint.us``,
``*.sharepoint-mil.us``, ``*.sharepoint.cn``). Office Sentry never builds SharePoint
URLs itself: it reads sites through Graph and uses the ``webUrl`` Microsoft returns.
The site permissions collector signs in to that site's host (``sharepoint_api.py``),
and ``is_sharepoint`` says whether a host belongs to the cloud before a token is
asked for it or sent to it.

Every endpoint lookup goes through ``get()``; nothing else in the code should
spell out a Microsoft host.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

DEFAULT = "commercial"


@dataclass(frozen=True)
class Cloud:
    key: str
    label: str  # shown in forms and on the tenant page
    login: str  # sign-in authority host, without a trailing slash
    graph: str  # Microsoft Graph root (GraphClient adds /v1.0 or /beta)
    exchange: str  # Exchange Online admin API root (also the token resource)
    sharepoint_suffix: str
    admin_centre: str  # where the MSP registers this cloud's app
    management: str = ""  # Office 365 Management Activity feed root (also the token resource), "" where none
    # Collectors or parts of one that Microsoft doesn't offer in this cloud -> the reason, for ctx.warn
    # and a report's "unavailable". Keys are a collector key or "<collector>.<part>".
    missing: dict[str, str] = field(default_factory=dict)
    # Permissions Microsoft doesn't offer in this cloud: an app registered here doesn't ask for them, and the
    # connection check doesn't count them as missing.
    no_permissions: tuple[str, ...] = ()

    def scope(self, resource: str) -> str:
        """The app-only token scope for one of this cloud's resources (``<resource>/.default``)."""
        return f"{resource}/.default"

    def is_sharepoint(self, host: str) -> bool:
        """True for a tenant's SharePoint host in this cloud (``https://contoso.sharepoint.com``), only."""
        return re.fullmatch(rf"https://[a-z0-9-]+\.{re.escape(self.sharepoint_suffix)}", host or "") is not None

    @property
    def is_default(self) -> bool:
        return self.key == DEFAULT


_CHINA = "Microsoft 365 operated by 21Vianet"
_DOD = "Microsoft 365 US Government DoD"
# Entra Agent ID's permission (added in 2.6). Microsoft doesn't say whether the US Government clouds or 21Vianet offer
# agent identities yet, so an app registered there asks for it only where Graph has it.
AGENT_ID = "AgentIdentity.Read.All"

CLOUDS: dict[str, Cloud] = {
    "commercial": Cloud(
        "commercial",
        "Commercial (includes GCC)",
        login="https://login.microsoftonline.com",
        graph="https://graph.microsoft.com",
        exchange="https://outlook.office365.com",
        sharepoint_suffix="sharepoint.com",
        admin_centre="https://entra.microsoft.com",
        management="https://manage.office.com",
    ),
    "gcc_high": Cloud(
        "gcc_high",
        "US Government GCC High",
        login="https://login.microsoftonline.us",
        graph="https://graph.microsoft.us",
        exchange="https://outlook.office365.us",
        sharepoint_suffix="sharepoint.us",
        admin_centre="https://entra.microsoft.us",
        management="https://manage.office365.us",
        missing={
            "usage.apps": "Microsoft doesn't offer the Microsoft 365 Apps usage report in GCC High.",
            "usage.copilot": "Microsoft doesn't offer the Copilot usage report in GCC High.",
        },
        no_permissions=(AGENT_ID,),
    ),
    "dod": Cloud(
        "dod",
        "US Government DoD",
        login="https://login.microsoftonline.us",
        graph="https://dod-graph.microsoft.us",
        exchange="https://webmail.apps.mil",
        sharepoint_suffix="sharepoint-mil.us",
        admin_centre="https://entra.microsoft.us",
        management="https://manage.protection.apps.mil",
        missing={
            "usage.apps": f"Microsoft doesn't offer the Microsoft 365 Apps usage report in {_DOD}.",
            "usage.copilot": f"Microsoft doesn't offer the Copilot usage report in {_DOD}.",
        },
        no_permissions=(AGENT_ID,),
    ),
    "china": Cloud(
        "china",
        "China (operated by 21Vianet)",
        login="https://login.chinacloudapi.cn",
        graph="https://microsoftgraph.chinacloudapi.cn",
        exchange="https://partner.outlook.cn",
        sharepoint_suffix="sharepoint.cn",
        admin_centre="https://portal.azure.cn",
        missing={
            "secure_score": f"Microsoft doesn't offer the Secure Score API in {_CHINA}.",
            "risky_users": f"Microsoft doesn't offer Entra ID Protection (risky users) in {_CHINA}.",
            "usage.apps": f"Microsoft doesn't offer the Microsoft 365 Apps usage report in {_CHINA}.",
            "usage.copilot": f"Microsoft doesn't offer Copilot in {_CHINA}.",
            "audit_activity": f"Microsoft doesn't document its audit log feed for {_CHINA}, so the audit log "
            "add-on isn't available there.",
            "security_alerts": f"Microsoft doesn't offer Defender alerts and incidents through Graph in {_CHINA}.",
        },
        no_permissions=("SecurityAlert.Read.All", "SecurityIncident.Read.All", AGENT_ID),
    ),
}

CHOICES: list[tuple[str, str]] = [(c.key, c.label) for c in CLOUDS.values()]


def get(key: str | None) -> Cloud:
    """The cloud for ``key``; unknown or empty means commercial, so old rows keep working."""
    return CLOUDS.get(key or DEFAULT, CLOUDS[DEFAULT])


def of(row) -> Cloud:
    """The cloud of a tenant or app connection row (sqlite Row or dict), commercial when it has none."""
    try:
        key = row["cloud"]
    except (KeyError, IndexError, TypeError):
        key = None
    return get(key)


def valid(key: str | None) -> bool:
    return key in CLOUDS


def missing(cloud: Cloud | str | None, feature: str) -> str | None:
    """Why Microsoft doesn't offer ``feature`` in this cloud, or None when it does."""
    c = cloud if isinstance(cloud, Cloud) else get(cloud)
    return c.missing.get(feature)


def app_needed(cloud: Cloud) -> str:
    """The plain-English "set up an app for this cloud" message."""
    return (
        f"This client is in {cloud.label}, and no app connection is set up for that cloud yet. "
        f"An app registered in another cloud can't sign in to it: register Office Sentry's app in "
        f"{cloud.label} ({cloud.admin_centre}), then add it under App connection with the cloud set to "
        f"{cloud.label} and choose it for this client under Manage."
    )
