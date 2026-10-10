"""Creating the Office Sentry app registration (one-time, interactive).

The app is multi-tenant and read-only. Each client tenant is added by an admin
of that tenant opening the admin-consent link; no per-tenant app is created.

Each national cloud (GCC High, DoD, China; see ``clouds.py``) is a separate
Entra ID: an app registered in one can't sign in to another, so a tenant in one
of them needs Office Sentry's app registered in that cloud too. ``create-app
--cloud gcc_high`` (and so on) creates it there.

Creation signs in with the device-code flow using Microsoft's public
"Microsoft Graph Command Line Tools" client, so no extra app is needed for it.
The CLI runs that flow with MSAL (``device_code_token``); the portal's setup
wizard (``app_setup.py``) runs it one step per page request
(``start_device_sign_in``, ``poll_device_sign_in``), since the page checks back
every few seconds and each check may reach a different portal process. Both ask
for the same single delegated scope (``setup_scope``) and create the same app.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlencode

import jwt
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


def admin_consent_url(client_id: str, tenant_ref: str, redirect_uri: str, state: str, cloud: str | None = None) -> str:
    """The admin-consent link, on the sign-in host of the tenant's cloud."""
    query = urlencode({"client_id": client_id, "redirect_uri": redirect_uri, "state": state})
    return f"{clouds.get(cloud).login}/{tenant_ref}/adminconsent?{query}"


def _resource(sp: dict, app_id: str, wanted: dict[str, str]) -> dict:
    roles = {r["value"]: r["id"] for r in sp.get("appRoles", []) if "Application" in r.get("allowedMemberTypes", [])}
    missing = sorted(set(wanted) - set(roles))
    if missing:
        raise ValueError(f"{app_id} does not offer these permissions: {', '.join(missing)}")
    return {"resourceAppId": app_id, "resourceAccess": [{"id": roles[name], "type": "Role"} for name in sorted(wanted)]}


def required_resource_access(graph_sp: dict, exchange_sp: dict | None = None) -> list[dict]:
    """Build ``requiredResourceAccess`` from the resource service principals' app roles."""
    access = [_resource(graph_sp, GRAPH_APP_ID, GRAPH_PERMISSIONS)]
    if exchange_sp is not None:
        access.append(_resource(exchange_sp, EXCHANGE_APP_ID, EXCHANGE_PERMISSIONS))
    return access


def setup_scope(cloud: str | None = None) -> str:
    """The one delegated permission creating the app needs: making an app registration in the MSP's tenant."""
    return f"{clouds.get(cloud).graph}/Application.ReadWrite.All"


def device_code_token(tenant: str, say: Callable[[str], None], cloud: str | None = None) -> str:
    import msal

    c = clouds.get(cloud)
    app = msal.PublicClientApplication(GRAPH_CLI_CLIENT_ID, authority=f"{c.login}/{tenant}")
    flow = app.initiate_device_flow(scopes=[setup_scope(c.key)])
    if "user_code" not in flow:
        raise RuntimeError(flow.get("error_description", "could not start device sign-in"))
    say(flow["message"])
    result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise RuntimeError(result.get("error_description", "sign-in failed"))
    return result["access_token"]


def create_app_registration(
    token: str,
    cert_pem: str,
    redirect_uri: str,
    display_name: str = "Office Sentry (read-only)",
    session: requests.Session | None = None,
    cloud: str | None = None,
) -> dict:
    """Create the multi-tenant app in the MSP's own tenant (in ``cloud``). Returns the application."""
    http = session or requests.Session()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = f"{clouds.get(cloud).graph}/v1.0"

    def resource_sp(app_id: str) -> dict:
        resp = http.get(
            f"{base}/servicePrincipals",
            headers=headers,
            params={"$filter": f"appId eq '{app_id}'", "$select": "id,appRoles"},
            timeout=60,
        )
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


# -- Device sign-in, one step per request (the portal's setup wizard) ------------------------------------

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
_AADSTS = re.compile(r"AADSTS(\d+)")

# Sign-in failures people hit when creating the app, by AADSTS code or OAuth error -> what to do.
SIGN_IN_PROBLEMS = {
    "authorization_declined": "Sign-in was cancelled on Microsoft's page. Start again when you're ready.",
    "expired_token": "The code expired before sign-in finished. Start again to get a new code.",
    "bad_verification_code": "Microsoft no longer recognises this code. Start again to get a new one.",
    "53003": "Your Conditional Access policies block this kind of sign-in (device code). Ask whoever manages "
    "Conditional Access to let this account use it for a few minutes, or create the app by hand.",
    "50020": "That account isn't in the tenant you named. Sign in with an account of your own (the MSP's) "
    'tenant, or leave the tenant on "Any tenant" under More options.',
    "90002": "Microsoft doesn't know that tenant. Check the domain or Directory (tenant) ID under More options.",
    "50059": "Microsoft couldn't tell which tenant to use. Enter your tenant's domain under More options.",
    "65001": "The sign-in needs consent to create app registrations. Sign in as a Global Administrator, "
    "Application Administrator or Cloud Application Administrator and accept the prompt.",
    "50076": "Your tenant wants a second sign-in step that this sign-in couldn't do. Start again and complete "
    "it on Microsoft's page.",
    "700016": "Microsoft's sign-in app isn't available in this tenant. Create the app by hand instead.",
}


class SignInError(Exception):
    """Creating the app can't go on; the message says why in words for people."""


@dataclass(frozen=True)
class DeviceCode:
    device_code: str  # secret: whoever holds it can collect the token once the admin signs in
    user_code: str  # what the admin types on Microsoft's page
    verification_uri: str
    expires_in: int  # seconds
    interval: int  # seconds Microsoft wants between checks


@dataclass(frozen=True)
class Poll:
    token: str | None = None  # the access token, once the admin has signed in
    slower: bool = False  # Microsoft asked us to check less often


def _sign_in_problem(body: dict) -> str:
    error = str(body.get("error") or "")
    description = str(body.get("error_description") or "")
    code = _AADSTS.search(description)
    for key in (code.group(1) if code else None, error):
        if key in SIGN_IN_PROBLEMS:
            return SIGN_IN_PROBLEMS[key]
    first = description.split("\r\n", 1)[0].split("\n", 1)[0].strip()
    return f"Microsoft refused the sign-in: {first or error or 'no reason given'}."


def _json(resp: requests.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def start_device_sign_in(
    tenant: str = "organizations", cloud: str | None = None, session: requests.Session | None = None
) -> DeviceCode:
    """Ask Microsoft for a device code. Only ``setup_scope`` is requested: no refresh token, no profile."""
    http = session or requests.Session()
    try:
        resp = http.post(
            f"{clouds.get(cloud).login}/{tenant}/oauth2/v2.0/devicecode",
            data={"client_id": GRAPH_CLI_CLIENT_ID, "scope": setup_scope(cloud)},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise SignInError(
            "Office Sentry couldn't reach Microsoft's sign-in service. Check the server can reach "
            "the internet, then try again."
        ) from exc
    body = _json(resp)
    if not resp.ok or "device_code" not in body or "user_code" not in body:
        raise SignInError(_sign_in_problem(body))
    return DeviceCode(
        body["device_code"],
        body["user_code"],
        body.get("verification_uri") or "",
        int(body.get("expires_in") or 900),
        int(body.get("interval") or 5),
    )


def poll_device_sign_in(
    device_code: str, tenant: str = "organizations", cloud: str | None = None, session: requests.Session | None = None
) -> Poll:
    """Ask Microsoft once whether the admin has signed in. Raises ``SignInError`` when it can't go on, and
    ``requests.RequestException`` when Microsoft didn't answer (worth asking again)."""
    http = session or requests.Session()
    resp = http.post(
        f"{clouds.get(cloud).login}/{tenant}/oauth2/v2.0/token",
        data={"grant_type": DEVICE_GRANT, "client_id": GRAPH_CLI_CLIENT_ID, "device_code": device_code},
        timeout=30,
    )
    body = _json(resp)
    if resp.ok and body.get("access_token"):
        return Poll(token=body["access_token"])
    if body.get("error") == "authorization_pending":
        return Poll()
    if body.get("error") == "slow_down":
        return Poll(slower=True)
    raise SignInError(_sign_in_problem(body))


def token_tenant(token: str) -> str | None:
    """The tenant an access token was issued in, for showing and prefilling. The token isn't verified here;
    nothing trusts this value, and Microsoft checks the token itself when it's used."""
    try:
        tid = jwt.decode(token, options={"verify_signature": False}).get("tid")
    except jwt.PyJWTError:
        return None
    return tid.lower() if isinstance(tid, str) and re.fullmatch(r"[0-9a-fA-F-]{36}", tid) else None


def create_problem(exc: requests.RequestException) -> str:
    """Why creating the app registration failed, in words for people."""
    resp = getattr(exc, "response", None)
    if resp is None:
        return "Office Sentry couldn't reach Microsoft Graph to create the app. Try again."
    error = _json(resp).get("error")
    message = str(error.get("message") or "") if isinstance(error, dict) else ""
    if resp.status_code in (401, 403):
        return (
            "That account can't create app registrations. Sign in as a Global Administrator, Application "
            "Administrator or Cloud Application Administrator of your own tenant and start again."
        )
    return f"Microsoft wouldn't create the app ({resp.status_code}): {message or 'no reason given'}."
