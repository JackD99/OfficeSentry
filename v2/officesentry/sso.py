"""Sign in with Microsoft (Microsoft Entra ID, OpenID Connect), for staff and for clients' own people.

This is about who signs in to the portal, not about client data. It uses a *separate* app registration in
the MSP's own tenant (the "sign-in app"), never the multi-tenant read-only app that reads client tenants, so
nothing about client access or its permissions changes. The sign-in app has no API permissions at all:
Office Sentry only ever asks Microsoft who the person is (an ID token).

How it works:

* The admin turns it on under Settings → Microsoft sign-in. The setup wizard signs them in to Microsoft with a
  device code (``app_setup.py``, as for the read-only app) and creates the sign-in app
  (``create_sign_in_app``): single tenant, a certificate Office Sentry made (the private key stays
  encrypted here), an app role for each built-in role (``APP_ROLE_DEFINITIONS``), "assignment required" on its service
  principal, the admin who ran the wizard assigned Admin, and admin consent for the sign-in scopes so staff
  never see a consent prompt. The manual path makes the same app by hand (``docs/staff-sign-in.md``).
* "Sign in with Microsoft" sends the browser to the tenant's authorize endpoint (``authorize_url``) with a
  state, a nonce and a PKCE verifier kept in the signed cookie. On return, the code is exchanged at the
  token endpoint (``exchange_code``) with a client assertion signed by the certificate, and the ID token is
  verified (``verify_id_token``): signature against the tenant's published keys, audience, issuer, tenant,
  lifetime and nonce.
* The person's Office Sentry role (``roles.py``) is the app role Entra put in the token (``role_for``): one
  per built-in role, Analyst for Second line, and a value an admin gives a custom role (``sso_role_values``).
  It's read on every sign-in, so a change in Entra applies at the next sign-in. Which clients someone sees is
  still granted in Office Sentry (``web/access.py``): tenant isolation doesn't change.
* Accounts are matched by Entra object ID (``users.entra_oid``), or linked on first sign-in when the
  username is the person's sign-in name, or linked by the person from their Account page. New staff get an
  account at their first sign-in. Client accounts keep passwords.
* Passwords stay as the break-glass route for staff until an admin turns them off here, which only an admin
  signed in with Microsoft can do. ``python -m officesentry staff-sign-in --passwords on`` turns them back
  on from the server if Microsoft sign-in ever breaks.
* An account with no password (``NO_PASSWORD``) confirms a security change, where others type their
  password again, by signing in to Microsoft again (``Flow.confirm_user_id``, ``prompt=login``); the
  session notes when (``sessions.confirmed_at``) and that counts for ``CONFIRM_MINUTES``.

Client sign-in (``clients_enabled``): a client's people sign in with their own work account, in their own
tenant, so their organisation's MFA and Conditional Access apply and a leaver is locked out when their
account is disabled there. The sign-in app is single tenant until the admin lets other organisations use it
(``allow_other_organisations``, from the wizard's device sign-in, or by hand; ``sso_multi_tenant``); then
"Sign in with Microsoft" goes to ``organizations`` and the token's tenant decides the path. The MSP's own
tenant is the staff path above, unchanged. Any other tenant is the client path (``client_account_for``):

* Only an account the MSP made for that person: staff find them in the client's directory as Office Sentry
  last collected it (``directory_person``), and the account is matched on that object ID and tenant ID only,
  never on a sign-in name or email address. Staff accounts are never reached from a client's tenant, and no
  account is created.
* The token's tenant must be the Directory ID of a client the account is granted (``directory_id``). App
  roles in the token are ignored: a client account is always the client role it was given here, and only
  ever sees clients in that organisation (``access.visible_tenants``), whatever else it is granted.
* Each client's own administrator approves the sign-in app once (``approval_url``, Microsoft's admin consent
  for the sign-in scopes, nothing else), unless their people may approve such apps themselves.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import jwt
import requests

from . import clouds, store
from . import permissions as p
from .certs import GeneratedCert, generate_certificate, key_credential
from .db import utcnow

DEFAULT_NAME = "Office Sentry sign-in"
CALLBACK_PATH = "/login/microsoft/callback"
SCOPES = "openid profile email"
# Where "Sign in with Microsoft" goes once client sign-in is on: any work or school account, whose tenant then
# decides the path (the MSP's own: staff; a client's: that client's people).
ORGANIZATIONS = "organizations"
# A confirmation with Microsoft (accounts with no password) counts for this long.
CONFIRM_MINUTES = 10
GUID_RE = re.compile(r"^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$")
# Microsoft's app ID for Microsoft Graph, whose delegated "openid profile email" the sign-in app uses.
GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
# What the setup wizard's device sign-in asks for: create the app, assign the first admin to it, and grant
# the sign-in scopes for everyone so staff never see a consent prompt. All delegated, used once.
SETUP_PERMISSIONS = (
    "Application.ReadWrite.All",
    "AppRoleAssignment.ReadWrite.All",
    "DelegatedPermissionGrant.ReadWrite.All",
)
# What the client sign-in wizard's device sign-in asks for: change the app's supported account types. Delegated.
UPDATE_PERMISSIONS = ("Application.ReadWrite.All",)
# A sign-in that took longer than this (state in the cookie) starts again.
FLOW_SECONDS = 10 * 60
# Published signing keys are kept this long; an unknown key ID fetches them again (at most once a minute).
KEYS_TTL = timedelta(hours=24)
KEYS_RETRY = timedelta(minutes=1)
NO_PASSWORD = "!"  # noqa: S105 (password_hash of an account that only signs in with Microsoft; never matches)

# The sign-in app's roles (assigned to people or groups in Entra) -> the built-in Office Sentry role
# (``permissions.TEMPLATES`` key), the first a token carries winning, in this order. Second line's value is
# Analyst, what staff were called before roles. A custom role is reached through a value an admin gives it
# (``sso_role_values``). Which clients someone sees is still granted in Office Sentry
# (users.all_tenants, user_tenants, groups), never from Entra.
APP_ROLES: dict[str, str] = {
    "Admin": "administrator",
    "TeamLeader": "team_leader",
    "Analyst": "second_line",
    "FirstLine": "first_line",
    "AccountManager": "account_manager",
    "Auditor": "auditor",
}
# The app roles as the app registration declares them (ids are fixed so a hand-made app can copy them).
_ROLE_IDS = {
    "Admin": "8f1b3f0e-6f8a-4a4e-9c3e-1d4a2b6c7e01",
    "Analyst": "8f1b3f0e-6f8a-4a4e-9c3e-1d4a2b6c7e02",
    "TeamLeader": "8f1b3f0e-6f8a-4a4e-9c3e-1d4a2b6c7e03",
    "FirstLine": "8f1b3f0e-6f8a-4a4e-9c3e-1d4a2b6c7e04",
    "AccountManager": "8f1b3f0e-6f8a-4a4e-9c3e-1d4a2b6c7e05",
    "Auditor": "8f1b3f0e-6f8a-4a4e-9c3e-1d4a2b6c7e06",
}


def _definition(value: str) -> dict:
    t = p.TEMPLATE_BY_KEY[APP_ROLES[value]]
    return {
        "id": _ROLE_IDS[value],
        "value": value,
        "displayName": t.name,
        "description": t.description,
        "allowedMemberTypes": ["User"],
        "isEnabled": True,
    }


APP_ROLE_DEFINITIONS = [_definition(value) for value in APP_ROLES]
# An app role value a custom role can be given: what Entra accepts, without spaces.
VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,59}$")
DEFAULT_ROLES = {"": "Can't sign in", "analyst": "Second line, with no clients until an admin grants some"}

KEYS = (
    "sso_enabled",  # '1' when staff may sign in with Microsoft
    "sso_tenant",  # the MSP's Directory (tenant) ID
    "sso_client_id",
    "sso_cloud",
    "sso_app_name",
    "sso_app_object_id",  # the app registration's object id (for the admin centre link); wizard only
    "sso_sp_object_id",  # its service principal (enterprise app); wizard only
    "sso_cert_pem",
    "sso_key_enc",
    "sso_thumbprint",
    "sso_cert_not_after",
    "sso_next_cert_pem",  # a replacement certificate, until the admin switches to it
    "sso_next_key_enc",
    "sso_next_thumbprint",
    "sso_next_not_after",
    "sso_cert_warned",
    "sso_passwords",  # 'on' (default) or 'off': may staff still sign in with a password?
    "sso_default_role",  # '' (refuse) or 'analyst': people whose token carries no app role
    "sso_role_values",  # JSON {app role value: custom role id}: custom roles reached from Entra
    "sso_notes",  # setup steps the wizard couldn't do, for the page to say
    "sso_set_up_at",
    "sso_set_up_by",
    "sso_clients",  # '1' when client accounts may sign in with Microsoft (their own tenant's account)
    "sso_multi_tenant",  # '1' once the sign-in app accepts accounts from other organisations
    "sso_multi_tenant_by",  # who made that change, and how ("wizard" or "by hand")
)
ENCRYPTED_KEYS = ("sso_key_enc", "sso_next_key_enc")


class SignInError(Exception):
    """Sign-in can't go on; the message is for the person on the page."""


# -- Settings -------------------------------------------------------------------------------------------


def settings(conn) -> dict:
    rows = conn.execute(f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(KEYS))})", KEYS)
    return {r["key"]: r["value"] for r in rows}


def save(conn, values: dict) -> None:
    for key, value in values.items():
        if key not in KEYS:
            raise KeyError(key)
        if value is None or value == "":
            conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, str(value)),
            )


def forget(conn) -> None:
    """Remove the sign-in app and every setting (the "Remove Microsoft sign-in" button)."""
    conn.execute(f"DELETE FROM settings WHERE key IN ({','.join('?' * len(KEYS))})", KEYS)


def is_configured(s: dict) -> bool:
    return all(s.get(k) for k in ("sso_tenant", "sso_client_id", "sso_key_enc", "sso_thumbprint"))


def enabled(s: dict) -> bool:
    """May staff sign in with Microsoft?"""
    return s.get("sso_enabled") == "1" and is_configured(s)


def multi_tenant(s: dict) -> bool:
    """Does the sign-in app accept accounts from other organisations (needed for client sign-in)?"""
    return s.get("sso_multi_tenant") == "1" and is_configured(s)


def clients_enabled(s: dict) -> bool:
    """May client accounts sign in with Microsoft (with an account in their own tenant)?"""
    return s.get("sso_clients") == "1" and multi_tenant(s)


def shown(s: dict) -> bool:
    """Does the sign-in page offer "Sign in with Microsoft"?"""
    return enabled(s) or clients_enabled(s)


def break_glass(s: dict) -> bool:
    """Whether staff may still sign in with a password (the break-glass way in while Microsoft sign-in is on)."""
    return s.get("sso_passwords", "on") != "off"


def passwords_allowed(s: dict, role: str) -> bool:
    """May someone with this role sign in with a password? Clients always can; staff until it's turned off."""
    return role == "client" or not enabled(s) or s.get("sso_passwords", "on") != "off"


def cloud(s: dict) -> clouds.Cloud:
    return clouds.get(s.get("sso_cloud") or clouds.DEFAULT)


def has_password(user) -> bool:
    return bool(user["password_hash"]) and user["password_hash"] != NO_PASSWORD


def redirect_uri(base_url: str) -> str:
    return base_url.rstrip("/") + CALLBACK_PATH


def issuer(s: dict, tenant: str | None = None) -> str:
    """Who signs the ID token: the MSP's tenant, or ``tenant`` (a client's) for a sign-in through
    ``organizations``."""
    return f"{cloud(s).login}/{tenant or s['sso_tenant']}/v2.0"


def admin_centre_url(s: dict) -> str:
    """Where people and groups are assigned to the app's roles: its enterprise app in the Entra admin centre."""
    c = cloud(s)
    if s.get("sso_sp_object_id") and s.get("sso_client_id"):
        return (
            f"{c.admin_centre}/#view/Microsoft_AAD_IAM/ManagedAppMenuBlade/~/Users"
            f"/objectId/{s['sso_sp_object_id']}/appId/{s['sso_client_id']}"
        )
    return f"{c.admin_centre}/#view/Microsoft_AAD_IAM/StartboardApplicationsMenuBlade/~/AppAppsPreview"


# -- The authorization-code flow --------------------------------------------------------------------------


@dataclass
class Flow:
    """What the browser's cookie keeps between sending someone to Microsoft and their coming back."""

    state: str
    nonce: str
    verifier: str
    started: int
    next: str | None = None
    link_user_id: int | None = None  # set when a signed-in person is connecting their Microsoft account
    confirm_user_id: int | None = None  # set when a signed-in person confirms a change with Microsoft
    clients: bool = False  # sent to ``organizations``: a client's tenant may answer, not only the MSP's

    @classmethod
    def new(
        cls,
        next_url: str | None = None,
        link_user_id: int | None = None,
        confirm_user_id: int | None = None,
        clients: bool = False,
    ) -> Flow:
        return cls(
            secrets.token_urlsafe(24),
            secrets.token_urlsafe(24),
            secrets.token_urlsafe(48),
            int(time.time()),
            next_url,
            link_user_id,
            confirm_user_id,
            clients and link_user_id is None and confirm_user_id is None,
        )

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "nonce": self.nonce,
            "verifier": self.verifier,
            "started": self.started,
            "next": self.next,
            "link": self.link_user_id,
            "confirm": self.confirm_user_id,
            "clients": self.clients,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> Flow | None:
        try:
            return cls(
                d["state"],
                d["nonce"],
                d["verifier"],
                int(d["started"]),
                d.get("next"),
                d.get("link"),
                d.get("confirm"),
                d.get("clients") is True,
            )
        except (TypeError, KeyError, ValueError):
            return None

    @property
    def expired(self) -> bool:
        return time.time() - self.started > FLOW_SECONDS

    def authority(self, s: dict) -> str:
        """The tenant segment of Microsoft's endpoints for this sign-in."""
        return ORGANIZATIONS if self.clients else s["sso_tenant"]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def authorize_url(s: dict, redirect: str, flow: Flow, login_hint: str | None = None, prompt: str | None = None) -> str:
    challenge = _b64url(hashlib.sha256(flow.verifier.encode("ascii")).digest())
    query = {
        "client_id": s["sso_client_id"],
        "response_type": "code",
        "redirect_uri": redirect,
        "response_mode": "query",
        "scope": SCOPES,
        "state": flow.state,
        "nonce": flow.nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if login_hint:
        query["login_hint"] = login_hint
    if prompt:
        query["prompt"] = prompt
    return f"{cloud(s).login}/{flow.authority(s)}/oauth2/v2.0/authorize?{urlencode(query)}"


def token_endpoint(s: dict, authority: str | None = None) -> str:
    return f"{cloud(s).login}/{authority or s['sso_tenant']}/oauth2/v2.0/token"


def client_assertion(s: dict, key_pem: str, now: datetime | None = None, authority: str | None = None) -> str:
    """A JWT signed with the sign-in app's certificate, proving to Microsoft that this is Office Sentry
    (``private_key_jwt``). ``x5t`` carries the certificate's SHA-1 thumbprint, as Entra expects. Its audience
    is the token endpoint it's sent to."""
    now = now or datetime.now(UTC)
    claims = {
        "aud": token_endpoint(s, authority),
        "iss": s["sso_client_id"],
        "sub": s["sso_client_id"],
        "jti": str(uuid.uuid4()),
        "nbf": int(now.timestamp()) - 60,
        "exp": int(now.timestamp()) + 5 * 60,
    }
    return jwt.encode(claims, key_pem, algorithm="RS256", headers={"x5t": _b64url(bytes.fromhex(s["sso_thumbprint"]))})


# Microsoft's sign-in failures people hit, by AADSTS code or OAuth error -> what to do.
PROBLEMS = {
    "access_denied": "Sign-in was cancelled on Microsoft's page.",
    "50105": "Your Microsoft account isn't assigned to Office Sentry yet. Ask an Office Sentry admin to assign you "
    "a role in the Entra admin centre (Enterprise applications → Office Sentry sign-in → "
    "Users and groups).",
    "50011": "The reply address Office Sentry sent doesn't match the sign-in app in Entra. An admin needs to add it "
    "to the app registration (Authentication → Web → Redirect URIs): see Settings → Microsoft sign-in.",
    "65001": "The sign-in app hasn't been given consent in your tenant. An admin can grant it in the Entra admin "
    "centre (Enterprise applications → Office Sentry sign-in → Permissions → Grant admin consent).",
    "7000215": "Microsoft doesn't recognise Office Sentry's certificate for the sign-in app. An admin should "
    "upload the current .cer from Settings → Microsoft sign-in to the app registration.",
    "700027": "Microsoft doesn't recognise Office Sentry's certificate for the sign-in app. An admin should "
    "upload the current .cer from Settings → Microsoft sign-in to the app registration.",
    "700016": "Microsoft can't find the sign-in app in that tenant. An admin should check the client ID and "
    "tenant ID under Settings → Microsoft sign-in.",
    "90002": "Microsoft doesn't know the tenant Office Sentry is set to sign you in to. An admin should check the "
    "tenant ID under Settings → Microsoft sign-in.",
    "50020": "That account belongs to another tenant. Sign in with your account in your own (the MSP's) tenant.",
    "50076": "Your tenant wants a second sign-in step that wasn't completed. Try again and complete it on "
    "Microsoft's page.",
    "53003": "Your Conditional Access policies blocked this sign-in.",
    "50058": "Microsoft didn't sign you in. Try again.",
    "50126": "That didn't work on Microsoft's page. Try again.",
    "50194": "The sign-in app only accepts accounts from the MSP's own tenant, so client sign-in can't work yet. "
    "Staff can use Sign in with Microsoft as staff below; an admin should finish setting up client sign-in "
    "under Settings → Microsoft sign-in.",
}
# The same failures on a sign-in a client's tenant may answer (``Flow.clients``): said for a client's people,
# who can't change anything at the MSP.
NOT_APPROVED = (
    "Your organisation hasn't approved signing in to these reports yet. Ask your IT provider for the approval "
    "link and pass it to your Microsoft 365 administrator; it takes them a minute."
)
CLIENT_PROBLEMS = {
    "65001": NOT_APPROVED,
    "65004": "You didn't approve the sign-in on Microsoft's page, so you weren't signed in.",
    "90094": NOT_APPROVED,
    "90095": NOT_APPROVED,
    "consent_required": NOT_APPROVED,
    "50105": "Your organisation only lets people it has assigned use this sign-in. Ask your Microsoft 365 "
    "administrator to assign you to it, or your IT provider for help.",
    "53003": "Your organisation's Conditional Access policies blocked this sign-in.",
}
_AADSTS = re.compile(r"AADSTS(\d+)")


def known_problem(error: str | None, description: str | None = None, clients: bool = False) -> str | None:
    """Our explanation of Microsoft's error, or None for one we don't know.
    ``clients``: the sign-in went to ``organizations``, so it may be one of a client's people."""
    code = _AADSTS.search(description or "")
    for key in (code.group(1) if code else None, error):
        if clients and key in CLIENT_PROBLEMS:
            return CLIENT_PROBLEMS[key]
        if key in PROBLEMS:
            return PROBLEMS[key]
    return None


def problem(error: str | None, description: str | None = None, clients: bool = False) -> str:
    """What to tell the person, from Microsoft's error (the query string, or the token endpoint's body)."""
    if known := known_problem(error, description, clients):
        return known
    code = _AADSTS.search(description or "")
    # The first line of Microsoft's text, without its "AADSTS12345: " prefix and trace ids, printable only.
    first = (description or "").splitlines()[:1]
    first = "".join(ch for ch in (first[0] if first else "") if ch.isprintable()).strip()
    if code and first.startswith(code.group(0) + ": "):
        first = first[len(code.group(0)) + 2 :]
    cut = first.find(". Trace ID")
    if cut >= 0:
        first = first[:cut]
    first = first.strip()
    return f"Microsoft refused the sign-in: {first or error or 'no reason given'}."


def _json(resp: requests.Response) -> dict:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def exchange_code(
    s: dict, key_pem: str, code: str, redirect: str, flow: Flow, session: requests.Session | None = None
) -> str:
    """Trade the authorization code for the ID token, at the endpoint the sign-in started at. Returns the raw
    ID token (verified separately)."""
    http = session or requests.Session()
    authority = flow.authority(s)
    try:
        resp = http.post(
            token_endpoint(s, authority),
            data={
                "client_id": s["sso_client_id"],
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect,
                "scope": SCOPES,
                "code_verifier": flow.verifier,
                "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                "client_assertion": client_assertion(s, key_pem, authority=authority),
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise SignInError(
            "Office Sentry couldn't reach Microsoft's sign-in service to finish signing you in. Try again."
        ) from exc
    body = _json(resp)
    if not resp.ok or not body.get("id_token"):
        raise SignInError(problem(body.get("error"), body.get("error_description"), clients=flow.clients))
    return str(body["id_token"])


# -- Verifying the ID token --------------------------------------------------------------------------------

_KEYS: dict[str, tuple[datetime, dict[str, dict]]] = {}
_KEYS_LOCK = threading.Lock()


def keys_url(s: dict, authority: str | None = None) -> str:
    return f"{cloud(s).login}/{authority or s['sso_tenant']}/discovery/v2.0/keys"


def _fetch_keys(s: dict, session: requests.Session | None, authority: str | None = None) -> dict[str, dict]:
    http = session or requests.Session()
    try:
        resp = http.get(keys_url(s, authority), timeout=30)
        resp.raise_for_status()
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise SignInError(
            "Office Sentry couldn't fetch Microsoft's signing keys to check your sign-in. Try again."
        ) from exc
    found = {k["kid"]: k for k in body.get("keys", []) if isinstance(k, dict) and k.get("kid")}
    if not found:
        raise SignInError("Microsoft's signing keys came back empty. Try again in a minute.")
    return found


def signing_key(
    s: dict,
    kid: str,
    session: requests.Session | None = None,
    now: datetime | None = None,
    authority: str | None = None,
) -> dict:
    """The published key with this ID (the MSP tenant's, or ``organizations``' for a sign-in a client's tenant
    may answer), from a per-process cache refreshed when it's stale or the ID is new (Microsoft rotates keys).
    Raises ``SignInError`` when it can't be had."""
    now = now or datetime.now(UTC)
    url = keys_url(s, authority)
    with _KEYS_LOCK:
        fetched, keys = _KEYS.get(url, (None, {}))
        fresh = fetched is not None and now - fetched < KEYS_TTL
        if fresh and kid in keys:
            return keys[kid]
        if fresh and now - fetched < KEYS_RETRY:
            raise SignInError(
                "Microsoft signed this sign-in with a key it hasn't published yet. Try again in a minute."
            )
        keys = _fetch_keys(s, session, authority)
        _KEYS[url] = (now, keys)
    if kid not in keys:
        raise SignInError("Microsoft signed this sign-in with a key it hasn't published. Try again in a minute.")
    return keys[kid]


def forget_keys() -> None:
    with _KEYS_LOCK:
        _KEYS.clear()


@dataclass(frozen=True)
class Identity:
    """Who Microsoft says signed in."""

    oid: str
    tid: str
    upn: str  # preferred_username: what they sign in with (an email address, normally)
    name: str
    email: str
    roles: tuple[str, ...] = field(default_factory=tuple)

    @property
    def username(self) -> str:
        return (self.upn or self.email or self.oid).lower()[:64]  # the same limit as the setup and users pages


def verify_id_token(
    s: dict, id_token: str, flow: Flow, session: requests.Session | None = None, now: datetime | None = None
) -> Identity:
    """Check the ID token's signature, audience, issuer, tenant, lifetime and nonce. Raises ``SignInError``.

    A sign-in that went to the MSP's tenant must come back from it. One that went to ``organizations``
    (``Flow.clients``) may come back from any tenant, but signed by that very tenant (the issuer names the
    token's ``tid``); which tenants get anywhere is ``client_account_for``'s call."""
    try:
        header = jwt.get_unverified_header(id_token)
    except jwt.PyJWTError as exc:
        raise SignInError("Microsoft's answer wasn't a sign-in token. Try again.") from exc
    if header.get("alg") != "RS256" or not header.get("kid"):
        raise SignInError("Microsoft's sign-in token was signed in a way Office Sentry doesn't accept.")
    authority = flow.authority(s)
    key = jwt.PyJWK(signing_key(s, str(header["kid"]), session, now, authority)).key
    try:
        claims = jwt.decode(
            id_token,
            key,
            algorithms=["RS256"],
            audience=s["sso_client_id"],
            issuer=None if flow.clients else issuer(s),  # checked against the token's own tenant below
            leeway=60,
            options={"require": ["exp", "iat", "aud", "iss", "sub", "nonce", "oid", "tid"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise SignInError("Your sign-in took too long and the token expired. Try again.") from exc
    except jwt.PyJWTError as exc:
        raise SignInError("Microsoft's sign-in token didn't check out, so you weren't signed in. Try again.") from exc
    if not secrets.compare_digest(str(claims.get("nonce", "")), flow.nonce):
        raise SignInError("This sign-in didn't start in this browser. Try again.")
    tid = str(claims.get("tid", "")).lower()
    if flow.clients:
        if not GUID_RE.match(tid) or str(claims.get("iss", "")) != issuer(s, tid):
            raise SignInError("Microsoft's sign-in token didn't check out, so you weren't signed in. Try again.")
    elif tid != s["sso_tenant"].lower():
        raise SignInError(PROBLEMS["50020"])
    oid = str(claims.get("oid", "")).lower()
    if not GUID_RE.match(oid):
        raise SignInError("Microsoft's sign-in token didn't say who you are. Try again.")
    roles = claims.get("roles") or []
    if not isinstance(roles, list):
        roles = []
    return Identity(
        oid,
        tid,
        str(claims.get("preferred_username") or "")[:200],
        str(claims.get("name") or "")[:200],
        str(claims.get("email") or "")[:200],
        tuple(str(r) for r in roles if isinstance(r, str)),
    )


@dataclass(frozen=True)
class EntraRole:
    """What a token's app roles make someone: the app role ``value``, then a built-in role's ``key`` or a
    custom role's ``role_id``."""

    value: str
    key: str | None = None
    role_id: int | None = None


def custom_values(s: dict) -> dict[str, int]:
    """App role value -> custom role id, as an admin set them under Settings → Microsoft sign-in."""
    try:
        raw = json.loads(s.get("sso_role_values") or "{}")
    except ValueError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(v): r for v, r in raw.items() if isinstance(r, int) and not isinstance(r, bool)}


def forget_role(conn, role_id: int) -> None:
    """A custom role was deleted: no app role value gives it any more."""
    values = {v: r for v, r in custom_values(settings(conn)).items() if r != role_id}
    save(conn, {"sso_role_values": json.dumps(values, sort_keys=True) if values else ""})


def role_for(identity: Identity, s: dict) -> EntraRole | None:
    """The Office Sentry role from the token's app roles: the built-in roles in ``APP_ROLES`` order (Admin
    first), then custom roles in the order they were made; the install's default for people with none; None
    when they may not sign in."""
    for value, key in APP_ROLES.items():
        if value in identity.roles:
            return EntraRole(value, key=key)
    for value, role_id in sorted(custom_values(s).items(), key=lambda item: item[1]):
        if value in identity.roles:
            return EntraRole(value, role_id=role_id)
    if s.get("sso_default_role") == "analyst":
        return EntraRole("", key="second_line")
    return None


def _role_row(conn, grant: EntraRole):
    """The roles row the grant names. Raises ``Refused`` when a custom role was deleted or is a client role."""
    if grant.key is not None:
        row = conn.execute("SELECT id, kind FROM roles WHERE key = ?", (grant.key,)).fetchone()
    else:
        row = conn.execute("SELECT id, kind FROM roles WHERE id = ?", (grant.role_id,)).fetchone()
    if row is None or row["kind"] not in (p.ADMIN, p.STAFF):
        raise Refused(
            f"Your role in Entra ({grant.value}) doesn't match a role in Office Sentry any more. Ask an Office "
            "Sentry admin to sort it out."
        )
    return row


# -- Accounts -------------------------------------------------------------------------------------------


class Refused(Exception):
    """Microsoft signed them in, but Office Sentry won't: the message says why, for the person."""


NO_ROLE = (
    "Your Microsoft account isn't assigned an Office Sentry role yet. Ask an Office Sentry admin to assign "
    "you one in the Entra admin centre, then sign in again."
)


def account_for(conn, identity: Identity, s: dict, now: str | None = None):
    """The user row for this Microsoft identity: linked by object ID, linked now by matching username, or
    created. The role follows the token every time; client grants stay as the admin set them. Raises ``Refused``."""
    now = now or utcnow()
    grant = role_for(identity, s)
    user = conn.execute("SELECT * FROM users WHERE entra_oid = ?", (identity.oid,)).fetchone()
    if user is not None and (user["role"] == "client" or (user["entra_tid"] or identity.tid) != identity.tid):
        # A client's account never becomes staff, whatever Entra says.
        raise Refused(NOT_STAFF)
    if user is None and identity.upn and "@" in identity.upn:
        # Only the sign-in name (the UPN): Microsoft's "email" claim is neither verified nor unique.
        user = conn.execute(
            "SELECT * FROM users WHERE username = ? AND entra_oid IS NULL AND role <> 'client'", (identity.upn,)
        ).fetchone()
    if user is not None and user["disabled"]:
        raise Refused("Your Office Sentry account is disabled. Ask an admin if you need it back.")
    if grant is None:
        raise Refused(NO_ROLE)
    row = _role_row(conn, grant)
    role, role_id = ("admin" if row["kind"] == p.ADMIN else "analyst"), row["id"]
    if user is None:
        username = identity.username
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            # A client account with that name, or a staff account someone else already linked.
            raise Refused(
                f"An account named {username} already exists and isn't yours to link. Ask an Office Sentry "
                "admin to sort it out."
            )
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, role_id, all_tenants, created_at, entra_oid,"
            " entra_tid, entra_upn, entra_linked_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?)",
            (username, NO_PASSWORD, role, role_id, now, identity.oid, identity.tid, identity.upn, now),
        )
        return conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone(), "created"
    outcome = "linked" if not user["entra_oid"] else "signed_in"
    changed = user["role"] != role or user["role_id"] != role_id
    conn.execute(
        "UPDATE users SET entra_oid = ?, entra_tid = ?, entra_upn = ?, entra_linked_at = COALESCE(entra_linked_at, ?),"
        " role = ?, role_id = ?, session_version = session_version + ? WHERE id = ?",
        (identity.oid, identity.tid, identity.upn, now, role, role_id, int(changed), user["id"]),
    )
    return conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone(), (
        "role_changed" if changed and outcome == "signed_in" else outcome
    )


# -- Clients' own people -----------------------------------------------------------------------------------

NOT_STAFF = (
    "That Microsoft account belongs to a client's account here, so it can't sign in as staff. Ask an Office "
    "Sentry admin to sort it out."
)
NOT_SET_UP = (
    "Your Microsoft account isn't set up to sign in here. Ask your IT provider to add you, using the address "
    "you sign in to Microsoft with."
)
OTHER_ORGANISATION = (
    "That account belongs to another organisation. Sign in with your account in your own (the MSP's) tenant."
)


def directory_id(conn, tenant) -> str | None:
    """A client's Directory (tenant) ID: its tenant ID when it was added by ID (or Office Sentry swapped the
    domain for it), else the organisation its latest collection read. None until either is known."""
    ref = str(tenant["tenant_ref"] or "").lower()
    if GUID_RE.match(ref):
        return ref
    run = store.latest_run(conn, tenant["id"], "tenant_profile")
    if run is None:
        return None
    ids = {str(o.get("id") or "").lower() for o in store.run_objects(conn, run["id"], "organization")}
    ids = {i for i in ids if GUID_RE.match(i)}
    return ids.pop() if len(ids) == 1 else None


def client_tenants(conn, user_id: int, tid: str, s: dict) -> list:
    """The clients granted to this account whose Directory ID is ``tid``, in the sign-in app's cloud and not
    archived. A client account is never given clients through a group (``access._GRANTED``)."""
    rows = conn.execute(
        "SELECT t.* FROM tenants t JOIN user_tenants ut ON ut.tenant_id = t.id"
        " WHERE ut.user_id = ? AND t.archived_at IS NULL AND t.cloud = ? ORDER BY t.id",
        (user_id, cloud(s).key),
    ).fetchall()
    return [t for t in rows if directory_id(conn, t) == tid]


def directory_person(conn, tenant, address: str) -> dict:
    """The member of a client's own directory whose sign-in address (else email address) is ``address``, from
    its latest Users collection: ``oid``, ``upn`` and ``name``. Their object ID is what their client account is
    matched on when they sign in (``client_account_for``); the address only finds them here, once. Raises
    ``Refused`` saying why nobody was found."""
    run = store.latest_run(conn, tenant["id"], "users")
    if run is None:
        raise Refused(
            f"Office Sentry hasn't collected {tenant['name']}'s users yet, so it can't find {address} in their "
            "directory. Collect their data first."
        )
    wanted = address.strip().lower()
    people = store.run_objects(conn, run["id"], "user")
    found = [u for u in people if str(u.get("userPrincipalName") or "").lower() == wanted]
    if not found:
        found = [u for u in people if str(u.get("mail") or "").lower() == wanted]
    if len(found) != 1:
        raise Refused(
            f"{address} isn't a sign-in address in {tenant['name']}'s directory as of its last collection. Check "
            "the address; someone new shows up after the next collection."
        )
    person = found[0]
    oid = str(person.get("_id") or "").lower()
    if str(person.get("userType") or "Member") != "Member" or not GUID_RE.match(oid):
        raise Refused(
            f"{address} is a guest in {tenant['name']}'s directory, so they sign in through their own organisation. "
            "Give them a password account instead."
        )
    return {"oid": oid, "upn": str(person.get("userPrincipalName") or address), "name": person.get("displayName")}


def client_account_for(conn, identity: Identity, s: dict, now: str | None = None):
    """The client account for a Microsoft identity from a client's tenant: the one whose object ID and tenant
    are the token's, as staff added it from the client's directory (``directory_person``). Never by sign-in
    name or email address, which a tenant's administrator can set to anything. It must be granted a client
    whose Directory ID is the token's tenant. Never a staff account, never a new account, and the token's app
    roles count for nothing. Returns (user row, the clients it matched). Raises ``Refused``."""
    now = now or utcnow()
    if not clients_enabled(s):
        raise Refused(OTHER_ORGANISATION)
    user = conn.execute(
        "SELECT * FROM users WHERE entra_oid = ? AND entra_tid = ? AND role = 'client'", (identity.oid, identity.tid)
    ).fetchone()
    if user is None:
        raise Refused(NOT_SET_UP)
    matched = client_tenants(conn, user["id"], identity.tid, s)
    if not matched:
        raise Refused(NOT_SET_UP)
    if user["disabled"]:
        raise Refused("Your account here is disabled. Ask your IT provider if you need it back.")
    conn.execute("UPDATE users SET entra_upn = ? WHERE id = ?", (identity.upn, user["id"]))
    conn.executemany(
        "UPDATE tenants SET sign_in_approved_at = COALESCE(sign_in_approved_at, ?) WHERE id = ?",
        [(now, t["id"]) for t in matched],
    )
    return conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone(), matched


def client_people(conn, tenant_id: int) -> list:
    """The client accounts granted this client (its own people), by name."""
    return conn.execute(
        "SELECT u.* FROM users u JOIN user_tenants ut ON ut.user_id = u.id"
        " WHERE ut.tenant_id = ? AND u.role = 'client' ORDER BY u.username COLLATE NOCASE",
        (tenant_id,),
    ).fetchall()


def signs_in_with_microsoft(user) -> bool:
    """Does this account sign in with Microsoft (connected already, or made without a password for it)? A
    client's person is connected when they're added."""
    return bool(user["entra_oid"]) or not has_password(user)


def approval_url(s: dict, tenant: str, redirect: str, state: str) -> str:
    """The link a client's administrator opens to approve the sign-in app for their organisation: Microsoft's
    admin consent for the sign-in scopes only (no API permissions), back to ``redirect`` with ``state``."""
    query = {"client_id": s["sso_client_id"], "scope": SCOPES, "redirect_uri": redirect, "state": state}
    return f"{cloud(s).login}/{tenant}/v2.0/adminconsent?{urlencode(query)}"


def link(conn, user_id: int, identity: Identity, now: str | None = None) -> None:
    """Connect a signed-in person's Microsoft account to their Office Sentry account (Account page)."""
    other = conn.execute("SELECT id FROM users WHERE entra_oid = ? AND id <> ?", (identity.oid, user_id)).fetchone()
    if other:
        raise Refused("That Microsoft account is already connected to another Office Sentry account.")
    conn.execute(
        "UPDATE users SET entra_oid = ?, entra_tid = ?, entra_upn = ?, entra_linked_at = ? WHERE id = ?",
        (identity.oid, identity.tid, identity.upn, now or utcnow(), user_id),
    )


def unlink(conn, user_id: int) -> bool:
    """Disconnect. Refused (False) for an account with no password: it would have no way to sign in."""
    user = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None or not has_password(user):
        return False
    conn.execute(
        "UPDATE users SET entra_oid = NULL, entra_tid = NULL, entra_upn = NULL, entra_linked_at = NULL WHERE id = ?",
        (user_id,),
    )
    return True


def staff_counts(conn) -> tuple[int, int]:
    """(staff accounts connected to a Microsoft account, staff accounts in all), ignoring disabled ones."""
    row = conn.execute(
        "SELECT SUM(entra_oid IS NOT NULL), COUNT(*) FROM users WHERE role <> 'client' AND disabled = 0"
    ).fetchone()
    return int(row[0] or 0), int(row[1] or 0)


# -- The sign-in app in Entra (setup wizard) -----------------------------------------------------------


def setup_scope(cloud_key: str | None = None, permissions: tuple[str, ...] = SETUP_PERMISSIONS) -> str:
    graph = clouds.get(cloud_key).graph
    return " ".join(f"{graph}/{p}" for p in permissions)


def _token_claim(token: str, claim: str) -> str | None:
    """A claim from the wizard's access token, for choosing what to do (never trusted for access: Microsoft
    checks the token itself when it's used).

    The token came straight from Microsoft's token endpoint over TLS and is for Graph, whose tokens can't be
    verified by anyone else, so the payload is read as it is rather than checked as a signed token.
    """
    try:
        payload = token.split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get(claim)
    except (IndexError, ValueError, AttributeError):
        return None
    return value.lower() if isinstance(value, str) and GUID_RE.match(value.lower()) else None


@dataclass
class CreatedApp:
    client_id: str
    object_id: str
    sp_object_id: str | None
    tenant: str
    notes: list[str]  # steps that didn't work and must be done by hand


def create_sign_in_app(
    token: str,
    cert_pem: str,
    redirect: str,
    name: str = DEFAULT_NAME,
    cloud_key: str | None = None,
    session: requests.Session | None = None,
) -> CreatedApp:
    """Create the single-tenant sign-in app with ``token`` (from the wizard's device sign-in).

    The app, its service principal with "assignment required" and the admin's own Admin assignment, and
    admin consent for the sign-in scopes. Only the app itself is essential: the rest is tried and, when
    the account can't do it, written down for the admin to do by hand (``CreatedApp.notes``).
    Raises ``SignInError`` when the app can't be created.
    """
    http = session or requests.Session()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = f"{clouds.get(cloud_key).graph}/v1.0"
    tenant = _token_claim(token, "tid") or ""
    body = {
        "displayName": name,
        "signInAudience": "AzureADMyOrg",
        "web": {"redirectUris": [redirect]},
        "appRoles": APP_ROLE_DEFINITIONS,
        "keyCredentials": [key_credential(cert_pem, "Office Sentry sign-in")],
        "optionalClaims": {"idToken": [{"name": "email", "essential": False}]},
    }
    try:
        resp = http.post(f"{base}/applications", headers=headers, json=body, timeout=60)
        resp.raise_for_status()
        app = resp.json()
    except requests.RequestException as exc:
        raise SignInError(create_problem(exc)) from exc
    created = CreatedApp(str(app["appId"]).lower(), str(app["id"]), None, tenant, [])

    try:
        resp = http.post(
            f"{base}/servicePrincipals",
            headers=headers,
            json={"appId": created.client_id, "appRoleAssignmentRequired": True},
            timeout=60,
        )
        resp.raise_for_status()
        sp = resp.json()
        created.sp_object_id = str(sp["id"])
    except requests.RequestException:
        created.notes.append(
            "The enterprise app (service principal) couldn't be created, so people and groups can't be assigned "
            "roles yet. In the Entra admin centre open App registrations → the app → Overview and choose "
            "Create service principal, then under Enterprise applications set Assignment required to Yes."
        )
        return created

    me = _token_claim(token, "oid")
    admin_role = next(r["id"] for r in APP_ROLE_DEFINITIONS if r["value"] == "Admin")
    assigned = False
    if me:
        try:
            resp = http.post(
                f"{base}/servicePrincipals/{created.sp_object_id}/appRoleAssignedTo",
                headers=headers,
                json={"principalId": me, "resourceId": created.sp_object_id, "appRoleId": admin_role},
                timeout=60,
            )
            resp.raise_for_status()
            assigned = True
        except requests.RequestException:
            pass
    if not assigned:
        created.notes.append(
            "Nobody has a role yet. Assign yourself Admin in the Entra admin centre: Enterprise applications → "
            f"{name} → Users and groups → Add user/group."
        )

    try:
        resp = http.get(
            f"{base}/servicePrincipals",
            headers=headers,
            params={"$filter": f"appId eq '{GRAPH_APP_ID}'", "$select": "id"},
            timeout=60,
        )
        resp.raise_for_status()
        graph_sp = resp.json()["value"][0]["id"]
        resp = http.post(
            f"{base}/oauth2PermissionGrants",
            headers=headers,
            json={
                "clientId": created.sp_object_id,
                "consentType": "AllPrincipals",
                "resourceId": graph_sp,
                "scope": SCOPES,
            },
            timeout=60,
        )
        resp.raise_for_status()
    except (requests.RequestException, LookupError, ValueError):
        created.notes.append(
            "Consent for the sign-in scopes wasn't granted, so each person may be asked to accept a consent "
            "prompt (or told they need admin approval) the first time. Grant it in the Entra admin centre: "
            f"Enterprise applications → {name} → Permissions → Grant admin consent."
        )
    return created


def create_problem(exc: requests.RequestException) -> str:
    resp = getattr(exc, "response", None)
    if resp is None:
        return "Office Sentry couldn't reach Microsoft Graph to create the sign-in app. Try again."
    error = _json(resp).get("error")
    message = str(error.get("message") or "") if isinstance(error, dict) else ""
    if resp.status_code in (401, 403):
        return (
            "That account can't create app registrations. Sign in as a Global Administrator, Application "
            "Administrator or Cloud Application Administrator of your own tenant and start again."
        )
    return f"Microsoft wouldn't create the sign-in app ({resp.status_code}): {message or 'no reason given'}."


def allow_other_organisations(
    token: str, s: dict, cloud_key: str | None = None, session: requests.Session | None = None
) -> str:
    """Let the sign-in app accept work accounts from other organisations (client sign-in), with ``token`` from
    the wizard's device sign-in: its "supported account types" become any organisation's directory. Nothing
    else about the app changes; it still has no permissions. Returns the app's object id. Raises
    ``SignInError`` in words for people."""
    http = session or requests.Session()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = f"{clouds.get(cloud_key).graph}/v1.0"
    tenant = _token_claim(token, "tid")
    if tenant and tenant != s["sso_tenant"].lower():
        raise SignInError(
            "You signed in to a different tenant from the one the sign-in app is in. Sign in to your own (the "
            "MSP's) tenant and start again."
        )
    try:
        resp = http.get(
            f"{base}/applications",
            headers=headers,
            params={"$filter": f"appId eq '{s['sso_client_id']}'", "$select": "id,signInAudience"},
            timeout=60,
        )
        resp.raise_for_status()
        found = resp.json().get("value") or []
        if not found:
            raise SignInError(
                f"Your tenant has no app registration with client ID {s['sso_client_id']}. Check the IDs under "
                "The sign-in app on this page."
            )
        object_id = str(found[0]["id"])
        if found[0].get("signInAudience") != "AzureADMultipleOrgs":
            resp = http.patch(
                f"{base}/applications/{object_id}",
                headers=headers,
                json={"signInAudience": "AzureADMultipleOrgs"},
                timeout=60,
            )
            resp.raise_for_status()
    except requests.RequestException as exc:
        bad = getattr(exc, "response", None)
        if bad is not None and bad.status_code in (401, 403):
            raise SignInError(
                "That account can't change app registrations. Sign in as a Global Administrator, Application "
                "Administrator or Cloud Application Administrator of your own tenant and start again."
            ) from exc
        raise SignInError("Microsoft wouldn't change the sign-in app just now. Try again.") from exc
    except (LookupError, ValueError, AttributeError) as exc:
        raise SignInError("Microsoft's answer about the sign-in app didn't make sense. Try again.") from exc
    return object_id


def store_app(conn, vault, created: CreatedApp, cert: GeneratedCert, name: str, cloud_key: str, actor: str) -> None:
    """Save the app the wizard created, as the sign-in app, and turn Microsoft sign-in on."""
    save(
        conn,
        {
            "sso_enabled": "1",
            "sso_tenant": created.tenant,
            "sso_client_id": created.client_id,
            "sso_cloud": cloud_key,
            "sso_app_name": name,
            "sso_app_object_id": created.object_id,
            "sso_sp_object_id": created.sp_object_id or "",
            "sso_cert_pem": cert.cert_pem,
            "sso_key_enc": vault.encrypt(cert.key_pem),
            "sso_thumbprint": cert.thumbprint,
            "sso_cert_not_after": cert.not_after.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "sso_next_cert_pem": "",
            "sso_next_key_enc": "",
            "sso_next_thumbprint": "",
            "sso_next_not_after": "",
            "sso_cert_warned": "",
            "sso_notes": "\n".join(created.notes),
            "sso_set_up_at": utcnow(),
            "sso_set_up_by": actor,
        },
    )


def store_manual(conn, vault, tenant: str, client_id: str, cloud_key: str, name: str, actor: str) -> None:
    """The hand-made path: the admin created the app in Entra and pasted its IDs; the certificate was made
    here first (``new_certificate``)."""
    save(
        conn,
        {
            "sso_tenant": tenant.lower(),
            "sso_client_id": client_id.lower(),
            "sso_cloud": cloud_key,
            "sso_app_name": name or DEFAULT_NAME,
            "sso_app_object_id": "",
            "sso_sp_object_id": "",
            "sso_notes": "",
            "sso_set_up_at": utcnow(),
            "sso_set_up_by": actor,
        },
    )


def new_certificate(conn, vault, replace: bool = False) -> GeneratedCert:
    """Make the sign-in app's certificate (``replace``: a replacement kept beside the current one until
    ``switch_certificate``). The private key is stored encrypted and never leaves."""
    cert = generate_certificate("Office Sentry sign-in", years=2)
    prefix = "sso_next_" if replace else "sso_"
    save(
        conn,
        {
            f"{prefix}cert_pem": cert.cert_pem,
            f"{prefix}key_enc": vault.encrypt(cert.key_pem),
            f"{prefix}thumbprint": cert.thumbprint,
            f"{prefix}{'not_after' if replace else 'cert_not_after'}": cert.not_after.strftime("%Y-%m-%dT%H:%M:%SZ"),
            **({} if replace else {"sso_cert_warned": ""}),
        },
    )
    return cert


def switch_certificate(conn) -> bool:
    """Make the replacement certificate the one sign-ins use (after its .cer was uploaded to the app)."""
    s = settings(conn)
    if not s.get("sso_next_key_enc"):
        return False
    save(
        conn,
        {
            "sso_cert_pem": s["sso_next_cert_pem"],
            "sso_key_enc": s["sso_next_key_enc"],
            "sso_thumbprint": s["sso_next_thumbprint"],
            "sso_cert_not_after": s["sso_next_not_after"],
            "sso_next_cert_pem": "",
            "sso_next_key_enc": "",
            "sso_next_thumbprint": "",
            "sso_next_not_after": "",
            "sso_cert_warned": "",
        },
    )
    return True
