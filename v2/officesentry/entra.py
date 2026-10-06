"""Creating the Office Sentry app registration (one-time, interactive).

The app is multi-tenant and read-only. Each client tenant is added by an admin
of that tenant opening the admin-consent link; no per-tenant app is created.

Each national cloud (GCC High, DoD, China; see ``clouds.py``) is a separate
Entra ID: an app registered in one can't sign in to another, so a tenant in one
of them needs Office Sentry's app registered in that cloud too. ``create-app
--cloud gcc_high`` (and so on) creates it there.

Creation signs in with the device-code flow using Microsoft's public
"Microsoft Graph Command Line Tools" client, so no extra app is needed for it.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlencode

import requests

from . import clouds
from .certs import key_credential

GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
EXCHANGE_APP_ID = "00000002-0000-0ff1-ce00-000000000000"  # Office 365 Exchange Online
GRAPH_CLI_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"

# The complete set of application permissions Office Sentry requests.
# Every one is read-only. Adding one later means every tenant must re-consent,
# so this list covers the reporting roadmap, not only today's collectors.
GRAPH_PERMISSIONS: dict[str, str] = {
    "Organization.Read.All": "Tenant details and subscriptions",
    "User.Read.All": "Users, licences, password age",
    "Directory.Read.All": "Groups, roles, guests, domains",
    "AuditLog.Read.All": "Sign-in activity and audit logs",
    "Reports.Read.All": "Usage and MFA registration reports",
    "Policy.Read.All": "Conditional Access and authorization policies",
    "RoleManagement.Read.Directory": "Admin role holders and PIM",
    "UserAuthenticationMethod.Read.All": "Registered MFA methods",
    "Application.Read.All": "App registrations, consents, secret expiry",
    "SecurityEvents.Read.All": "Secure Score",
    "IdentityRiskyUser.Read.All": "Risky users (Entra ID P2)",
    "DeviceManagementManagedDevices.Read.All": "Intune device compliance",
    "SharePointTenantSettings.Read.All": "SharePoint sharing settings",
    "Sites.Read.All": "Which files are shared, and with whom (read-only)",
    "MailboxSettings.Read": "Inbox rules",
}

# Exchange Online admin API (read-only cmdlets only; see exchange.py). Each
# tenant must also give the app the Global Reader role for Exchange to answer.
EXCHANGE_PERMISSIONS: dict[str, str] = {
    "Exchange.ManageAsApp": "Mailbox forwarding, permissions and mail settings (read-only cmdlets)",
}


def admin_consent_url(client_id: str, tenant_ref: str, redirect_uri: str, state: str,
                      cloud: str | None = None) -> str:
    """The admin-consent link, on the sign-in host of the tenant's cloud."""
    query = urlencode({"client_id": client_id, "redirect_uri": redirect_uri, "state": state})
    return f"{clouds.get(cloud).login}/{tenant_ref}/adminconsent?{query}"


def _resource(sp: dict, app_id: str, wanted: dict[str, str]) -> dict:
    roles = {r["value"]: r["id"] for r in sp.get("appRoles", [])
             if "Application" in r.get("allowedMemberTypes", [])}
    missing = sorted(set(wanted) - set(roles))
    if missing:
        raise ValueError(f"{app_id} does not offer these permissions: {', '.join(missing)}")
    return {"resourceAppId": app_id,
            "resourceAccess": [{"id": roles[name], "type": "Role"} for name in sorted(wanted)]}


def required_resource_access(graph_sp: dict, exchange_sp: dict | None = None) -> list[dict]:
    """Build ``requiredResourceAccess`` from the resource service principals' app roles."""
    access = [_resource(graph_sp, GRAPH_APP_ID, GRAPH_PERMISSIONS)]
    if exchange_sp is not None:
        access.append(_resource(exchange_sp, EXCHANGE_APP_ID, EXCHANGE_PERMISSIONS))
    return access


def device_code_token(tenant: str, say: Callable[[str], None], cloud: str | None = None) -> str:
    import msal

    c = clouds.get(cloud)
    app = msal.PublicClientApplication(GRAPH_CLI_CLIENT_ID, authority=f"{c.login}/{tenant}")
    flow = app.initiate_device_flow(scopes=[f"{c.graph}/Application.ReadWrite.All"])
    if "user_code" not in flow:
        raise RuntimeError(flow.get("error_description", "could not start device sign-in"))
    say(flow["message"])
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(result.get("error_description", "sign-in failed"))
    return result["access_token"]


def create_app_registration(token: str, cert_pem: str, redirect_uri: str,
                            display_name: str = "Office Sentry (read-only)",
                            session: requests.Session | None = None, cloud: str | None = None) -> dict:
    """Create the multi-tenant app in the MSP's own tenant (in ``cloud``). Returns the application."""
    http = session or requests.Session()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = f"{clouds.get(cloud).graph}/v1.0"

    def resource_sp(app_id: str) -> dict:
        resp = http.get(f"{base}/servicePrincipals", headers=headers,
                        params={"$filter": f"appId eq '{app_id}'", "$select": "id,appRoles"}, timeout=60)
        resp.raise_for_status()
        return resp.json()["value"][0]

    graph_sp = resource_sp(GRAPH_APP_ID)
    exchange_sp = resource_sp(EXCHANGE_APP_ID)

    body = {
        "displayName": display_name,
        "signInAudience": "AzureADMultipleOrgs",
        "web": {"redirectUris": [redirect_uri]},
        "requiredResourceAccess": required_resource_access(graph_sp, exchange_sp),
        "keyCredentials": [key_credential(cert_pem)],
    }
    resp = http.post(f"{base}/applications", headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    return resp.json()
