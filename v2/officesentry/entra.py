"""Creating the Office Sentry app registration (one-time, interactive).

The app is multi-tenant and read-only. Each client tenant is added by an admin
of that tenant opening the admin-consent link; no per-tenant app is created.

Creation signs in with the device-code flow using Microsoft's public
"Microsoft Graph Command Line Tools" client, so no extra app is needed for it.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlencode

import requests

from .certs import key_credential

GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
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
    "MailboxSettings.Read": "Forwarding and inbox rules",
}


def admin_consent_url(client_id: str, tenant_ref: str, redirect_uri: str, state: str) -> str:
    query = urlencode({"client_id": client_id, "redirect_uri": redirect_uri, "state": state})
    return f"https://login.microsoftonline.com/{tenant_ref}/adminconsent?{query}"


def required_resource_access(graph_sp: dict) -> list[dict]:
    """Build ``requiredResourceAccess`` from the Graph service principal's app roles."""
    roles = {r["value"]: r["id"] for r in graph_sp.get("appRoles", [])
             if "Application" in r.get("allowedMemberTypes", [])}
    missing = sorted(set(GRAPH_PERMISSIONS) - set(roles))
    if missing:
        raise ValueError(f"Graph does not offer these permissions: {', '.join(missing)}")
    return [{
        "resourceAppId": GRAPH_APP_ID,
        "resourceAccess": [{"id": roles[name], "type": "Role"} for name in sorted(GRAPH_PERMISSIONS)],
    }]


def device_code_token(tenant: str, say: Callable[[str], None]) -> str:
    import msal

    app = msal.PublicClientApplication(
        GRAPH_CLI_CLIENT_ID, authority=f"https://login.microsoftonline.com/{tenant}",
    )
    flow = app.initiate_device_flow(scopes=["https://graph.microsoft.com/Application.ReadWrite.All"])
    if "user_code" not in flow:
        raise RuntimeError(flow.get("error_description", "could not start device sign-in"))
    say(flow["message"])
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(result.get("error_description", "sign-in failed"))
    return result["access_token"]


def create_app_registration(token: str, cert_pem: str, redirect_uri: str,
                            display_name: str = "Office Sentry (read-only)",
                            session: requests.Session | None = None) -> dict:
    """Create the multi-tenant app in the MSP's own tenant. Returns the application."""
    http = session or requests.Session()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = "https://graph.microsoft.com/v1.0"

    resp = http.get(f"{base}/servicePrincipals", headers=headers,
                    params={"$filter": f"appId eq '{GRAPH_APP_ID}'", "$select": "id,appRoles"},
                    timeout=60)
    resp.raise_for_status()
    graph_sp = resp.json()["value"][0]

    body = {
        "displayName": display_name,
        "signInAudience": "AzureADMultipleOrgs",
        "web": {"redirectUris": [redirect_uri]},
        "requiredResourceAccess": required_resource_access(graph_sp),
        "keyCredentials": [key_credential(cert_pem)],
    }
    resp = http.post(f"{base}/applications", headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    return resp.json()
