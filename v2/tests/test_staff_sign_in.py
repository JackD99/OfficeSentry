"""Staff sign-in with Microsoft (sso.py): the setup wizard, the OpenID Connect flow, accounts and roles,
the break-glass password switch and the settings page.

Microsoft is simulated at the HTTP level: the authorize endpoint (what the browser is sent to), the token
endpoint, the tenant's published signing keys, and Graph for the wizard (the app, its service principal, the
admin's role assignment and the consent grant).
"""

from __future__ import annotations

import html
import json
import re
import time
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
import responses
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509 import load_pem_x509_certificate
from responses import matchers

from officesentry import roles as office_roles
from officesentry import sso
from officesentry.__main__ import main
from tests.conftest import PASSWORD, login, make_user

LOGIN = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
TENANT = "c0ffee00-1111-2222-3333-444455556666"
CLIENT_ID = "aaaa1111-2222-3333-4444-555566667777"
TOKEN_URL = f"{LOGIN}/{TENANT}/oauth2/v2.0/token"
KEYS_URL = f"{LOGIN}/{TENANT}/discovery/v2.0/keys"
CALLBACK = "http://localhost:8000/login/microsoft/callback"
JSON = {"Accept": "application/json"}

# Microsoft's signing key for this tenant (made once: RSA generation is slow).
_MS_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
MS_KID = "ms-key-1"
MS_JWK = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_MS_KEY.public_key())), "kid": MS_KID, "use": "sig"}
_MS_KEY_PEM = _MS_KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()

JACK = {"oid": "11111111-aaaa-bbbb-cccc-000000000001", "upn": "jack@msp.example", "name": "Jack Doe"}
AMY = {"oid": "11111111-aaaa-bbbb-cccc-000000000002", "upn": "amy@msp.example", "name": "Amy Lee"}


def id_token(person: dict, nonce: str, roles=("Admin",), **overrides) -> str:
    now = int(time.time())
    claims = {
        "aud": CLIENT_ID,
        "iss": f"{LOGIN}/{TENANT}/v2.0",
        "iat": now - 5,
        "nbf": now - 5,
        "exp": now + 3600,
        "sub": "sub-" + person["oid"],
        "oid": person["oid"],
        "tid": TENANT,
        "preferred_username": person["upn"],
        "name": person["name"],
        "email": person["upn"],
        "nonce": nonce,
        "ver": "2.0",
    }
    if roles is not None:
        claims["roles"] = list(roles)
    claims.update(overrides)
    kid = claims.pop("_kid", MS_KID)
    return jwt.encode(claims, _MS_KEY_PEM, algorithm="RS256", headers={"kid": kid})


def mock_keys(keys=None):
    return responses.get(KEYS_URL, json={"keys": keys if keys is not None else [MS_JWK]})


@pytest.fixture
def configured(db, vault):
    """Microsoft sign-in set up by hand and turned on, by the install's first admin ("owner")."""
    sso.forget_keys()
    make_user(db, "owner")
    sso.new_certificate(db, vault)
    sso.store_manual(db, vault, TENANT, CLIENT_ID, "commercial", "Office Sentry sign-in", "admin")
    sso.save(db, {"sso_enabled": "1"})
    db.commit()
    return sso.settings(db)


def start(client, next_url=None) -> dict:
    """Press "Sign in with Microsoft": what the browser is sent to Microsoft with."""
    resp = client.get("/login/microsoft" + (f"?next={next_url}" if next_url else ""))
    assert resp.status_code == 302, resp.status_code
    url = urlsplit(resp.headers["Location"])
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"{LOGIN}/{TENANT}/oauth2/v2.0/authorize"
    return {k: v[0] for k, v in parse_qs(url.query).items()}


def come_back(client, sent: dict, token: str | None, status=200, body=None, code="auth-code-1"):
    """Microsoft sends the browser back with a code; the token endpoint answers with ``token``."""
    mock_keys()
    call = responses.post(TOKEN_URL, status=status, json=body if body is not None else {"id_token": token})
    resp = client.get(f"/login/microsoft/callback?code={code}&state={sent['state']}")
    return resp, call


def page(client, url: str) -> str:
    """A page's text, with HTML entities (the apostrophes in messages) put back."""
    return html.unescape(client.get(url).get_data(as_text=True))


def accounts(db) -> list[str]:
    """Every account but the first admin's, by name."""
    return [r[0] for r in db.execute("SELECT username FROM users WHERE username <> 'owner' ORDER BY id")]


def _flash(resp) -> list[str]:
    """The messages a response shows."""
    return re.findall(
        r'<div class="flash [^"]*" role="status"><svg .*?</svg><span>(.*?)</span></div>', resp.get_data(as_text=True)
    )


def signed_in_as(client) -> str | None:
    page = client.get("/account")
    if page.status_code != 200:
        return None
    text = page.get_data(as_text=True)
    return text.split("Signed in as ", 1)[1].split(" ", 1)[0] if "Signed in as " in text else None


# -- The flow ---------------------------------------------------------------------------------------------


@responses.activate
def test_new_admin_signs_in_with_microsoft(client, db, vault, configured, cfg):
    text = page(client, "/login")
    assert "Sign in with Microsoft" in text and "or with a password" in text

    sent = start(client, "/admin/users")
    assert sent["client_id"] == CLIENT_ID and sent["redirect_uri"] == CALLBACK
    assert sent["response_type"] == "code" and sent["scope"] == "openid profile email"
    assert sent["code_challenge_method"] == "S256" and len(sent["code_challenge"]) == 43
    assert sent["state"] and sent["nonce"]

    resp, call = come_back(client, sent, id_token(JACK, sent["nonce"]))
    assert resp.status_code == 302 and resp.headers["Location"] == "/admin/users"
    # The token request: the code, our PKCE verifier (matching the challenge sent), and a client assertion
    # signed with the sign-in app's certificate, proving the request is Office Sentry's.
    form = {k: v[0] for k, v in parse_qs(call.calls[0].request.body).items()}
    assert form["grant_type"] == "authorization_code" and form["code"] == "auth-code-1"
    assert form["redirect_uri"] == CALLBACK and form["client_id"] == CLIENT_ID
    assert form["client_assertion_type"] == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    import base64
    import hashlib

    challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest()).rstrip(b"=")
    assert challenge.decode() == sent["code_challenge"]
    public_key = load_pem_x509_certificate(configured["sso_cert_pem"].encode()).public_key()
    assertion = jwt.decode(form["client_assertion"], public_key, algorithms=["RS256"], audience=TOKEN_URL)
    assert assertion["iss"] == CLIENT_ID and assertion["sub"] == CLIENT_ID
    assert (
        jwt.get_unverified_header(form["client_assertion"])["x5t"]
        == base64.urlsafe_b64encode(bytes.fromhex(configured["sso_thumbprint"])).rstrip(b"=").decode()
    )

    user = db.execute("SELECT * FROM users WHERE entra_oid = ?", (JACK["oid"],)).fetchone()
    assert user["username"] == "jack@msp.example" and user["role"] == "admin" and user["all_tenants"] == 0
    assert user["password_hash"] == sso.NO_PASSWORD and user["entra_upn"] == JACK["upn"] and user["last_login_at"]
    assert db.execute("SELECT method FROM sessions").fetchone()["method"] == "microsoft"
    assert signed_in_as(client) == "jack@msp.example"
    actions = [(r["action"], r["detail"]) for r in db.execute("SELECT action, detail FROM audit_log ORDER BY id")]
    assert ("user_created", "jack@msp.example (Administrator), Microsoft sign-in") in actions
    assert ("login", "Microsoft") in actions
    # The account page: no password to change, no authenticator; Microsoft is the way in.
    text = page(client, "/account")
    assert "Microsoft is the only way to sign in" in text and "Change password" not in text


@responses.activate
def test_role_follows_entra_and_client_grants_stay(client, db, vault, configured, two_tenants):
    a, _b = two_tenants
    sent = start(client)
    come_back(client, sent, id_token(AMY, sent["nonce"], roles=["Analyst"]))
    user = db.execute("SELECT * FROM users WHERE entra_oid = ?", (AMY["oid"],)).fetchone()
    assert user["role"] == "analyst"
    assert client.get(f"/tenants/{a}").status_code == 404  # no clients granted yet: isolation holds
    db.execute("INSERT INTO user_tenants (user_id, tenant_id) VALUES (?, ?)", (user["id"], a))
    db.commit()
    client.post("/logout")

    # Promoted in Entra: admin at the next sign-in. Demoted again: analyst, and the grant is still there.
    for roles, expected in ((["Admin"], "admin"), (["Analyst", "Admin"], "admin"), (["Analyst"], "analyst")):
        sent = start(client)
        resp, _ = come_back(client, sent, id_token(AMY, sent["nonce"], roles=roles), code=f"code-{expected}")
        assert resp.status_code == 302
        user = db.execute("SELECT * FROM users WHERE entra_oid = ?", (AMY["oid"],)).fetchone()
        assert user["role"] == expected
        client.post("/logout")
    assert client.get(f"/tenants/{a}").status_code == 302  # signed out
    assert [
        r["tenant_id"] for r in db.execute("SELECT tenant_id FROM user_tenants WHERE user_id = ?", (user["id"],))
    ] == [a]
    assert db.execute("SELECT COUNT(*) FROM users WHERE entra_oid = ?", (AMY["oid"],)).fetchone()[0] == 1


def _role_key(db, oid: str) -> str | None:
    user = db.execute("SELECT * FROM users WHERE entra_oid = ?", (oid,)).fetchone()
    return office_roles.role_of(db, user).key


@responses.activate
def test_each_app_role_in_entra_gives_its_office_sentry_role(client, db, vault, configured):
    # The app the wizard makes declares one app role per built-in role, with ids a hand-made app can copy.
    assert [r["value"] for r in sso.APP_ROLE_DEFINITIONS] == list(sso.APP_ROLES)
    assert len({r["id"] for r in sso.APP_ROLE_DEFINITIONS}) == len(sso.APP_ROLES)
    by_value = {r["value"]: r for r in sso.APP_ROLE_DEFINITIONS}
    assert by_value["Analyst"]["displayName"] == "Second line" and by_value["Admin"]["displayName"] == "Administrator"

    # The first role in APP_ROLES order wins; the role (and users.role, the kind) follow Entra every time.
    for entra_roles, expected, column in (
        (["FirstLine"], "first_line", "analyst"),
        (["Auditor", "TeamLeader"], "team_leader", "analyst"),
        (["FirstLine", "Admin"], "administrator", "admin"),
        (["Analyst"], "second_line", "analyst"),
        (["AccountManager"], "account_manager", "analyst"),
    ):
        sent = start(client)
        resp, _ = come_back(client, sent, id_token(AMY, sent["nonce"], roles=entra_roles), code=f"code-{expected}")
        assert resp.status_code == 302 and signed_in_as(client) == "amy@msp.example"
        assert _role_key(db, AMY["oid"]) == expected
        assert db.execute("SELECT role FROM users WHERE entra_oid = ?", (AMY["oid"],)).fetchone()[0] == column
        client.post("/logout")
    assert "role from Entra: Account manager" in [
        r[0] for r in db.execute("SELECT detail FROM audit_log WHERE action = 'user_updated'")
    ]

    # Her user page says the role is set in Entra, and picking another one there is refused.
    amy = db.execute("SELECT id FROM users WHERE entra_oid = ?", (AMY["oid"],)).fetchone()["id"]
    login(client, "owner")
    assert "their role is the app role assigned to them in Entra" in page(client, f"/admin/users/{amy}")
    client.post(f"/admin/users/{amy}", data={"role": str(office_roles.by_key(db, "auditor").id)})
    assert _role_key(db, AMY["oid"]) == "account_manager"
    assert "Change it there" in page(client, f"/admin/users/{amy}")


@responses.activate
def test_a_custom_role_is_reached_through_the_value_an_admin_gives_it(client, db, vault, configured):
    desk = office_roles.create(db, "owner", "Service desk", "", "staff", "granted", ["reports.people"])
    other = office_roles.create(db, "owner", "Finance", "", "staff", "granted", ["reports.email"])
    db.commit()
    login(client, "owner")
    text = page(client, "/admin/sign-in")
    assert "Custom roles from Entra" in text and f'name="value-{desk}" value=""' in text

    # Values Entra wouldn't accept, or that another role already has, are refused and nothing is saved.
    for value, problem in (("Service desk", "no spaces"), ("analyst", "already an app role")):
        resp = client.post("/admin/sign-in/roles", data={f"value-{desk}": value})
        assert problem in html.unescape(client.get(resp.headers["Location"]).get_data(as_text=True))
        assert sso.custom_values(sso.settings(db)) == {}
    client.post("/admin/sign-in/roles", data={f"value-{desk}": "Desk", f"value-{other}": "desk"})
    assert sso.custom_values(sso.settings(db)) == {}
    client.post("/admin/sign-in/roles", data={f"value-{desk}": "ServiceDesk", f"value-{other}": ""})
    assert sso.custom_values(sso.settings(db)) == {"ServiceDesk": desk}
    assert "roles from Entra: ServiceDesk=Service desk" in [
        r[0] for r in db.execute("SELECT detail FROM audit_log WHERE action = 'staff_sign_in_changed'")
    ]
    text = page(client, "/admin/sign-in")
    assert 'value="ServiceDesk"' in text
    client.post("/logout")

    sent = start(client)
    come_back(client, sent, id_token(AMY, sent["nonce"], roles=["ServiceDesk"]))
    assert _role_key(db, AMY["oid"]) is None  # a custom role has no key
    user = db.execute("SELECT * FROM users WHERE entra_oid = ?", (AMY["oid"],)).fetchone()
    assert office_roles.role_of(db, user).id == desk and user["role"] == "analyst"
    client.post("/logout")
    # A built-in role in the token comes first.
    sent = start(client)
    come_back(client, sent, id_token(AMY, sent["nonce"], roles=["ServiceDesk", "Auditor"]), code="code-2")
    assert _role_key(db, AMY["oid"]) == "auditor"
    client.post("/logout")

    # Deleting the role takes its value away, so a role made later with its id isn't reached from Entra.
    office_roles.remove(db, "owner", office_roles.get(db, other))
    db.execute("UPDATE users SET role_id = ? WHERE entra_oid = ?", (office_roles.by_key(db, "auditor").id, AMY["oid"]))
    office_roles.remove(db, "owner", office_roles.get(db, desk))
    db.commit()
    assert sso.custom_values(sso.settings(db)) == {}
    sent = start(client)
    resp, _ = come_back(client, sent, id_token(AMY, sent["nonce"], roles=["ServiceDesk"]), code="code-3")
    assert resp.headers["Location"].endswith("/login") and signed_in_as(client) is None


@responses.activate
def test_a_value_left_for_a_role_that_is_gone_refuses_rather_than_guessing(client, db, vault, configured):
    sso.save(db, {"sso_role_values": '{"Ghost": 9999}'})
    db.commit()
    sent = start(client)
    resp, _ = come_back(client, sent, id_token(AMY, sent["nonce"], roles=["Ghost"]))
    assert resp.headers["Location"].endswith("/login") and signed_in_as(client) is None
    assert "doesn't match a role in Office Sentry any more" in page(client, "/login")
    assert db.execute("SELECT COUNT(*) FROM users WHERE entra_oid = ?", (AMY["oid"],)).fetchone()[0] == 0


@responses.activate
def test_no_role_is_refused_unless_the_install_allows_analysts(client, db, vault, configured):
    sent = start(client)
    resp, _ = come_back(client, sent, id_token(AMY, sent["nonce"], roles=[]))
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")
    text = page(client, "/login")
    assert "isn't assigned an Office Sentry role yet" in text
    assert accounts(db) == []
    assert (
        db.execute("SELECT detail FROM audit_log WHERE action = 'login_failed'")
        .fetchone()["detail"]
        .startswith("Microsoft: Your Microsoft account isn't assigned")
    )

    sso.save(db, {"sso_default_role": "analyst"})
    db.commit()
    sent = start(client)
    resp, _ = come_back(client, sent, id_token(AMY, sent["nonce"], roles=None), code="code-2")
    assert resp.status_code == 302 and resp.headers["Location"] == "/today"
    user = db.execute("SELECT * FROM users WHERE entra_oid = ?", (AMY["oid"],)).fetchone()
    assert user["role"] == "analyst" and user["all_tenants"] == 0


@responses.activate
def test_existing_account_is_linked_by_its_username(client, db, vault, configured, two_tenants):
    a, _b = two_tenants
    uid = make_user(db, "Amy@MSP.example", role="analyst", tenants=[a])
    sent = start(client)
    resp, _ = come_back(client, sent, id_token(AMY, sent["nonce"], roles=["Analyst"]))
    assert resp.status_code == 302
    user = db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    assert user["entra_oid"] == AMY["oid"] and user["entra_linked_at"]
    assert user["password_hash"] != sso.NO_PASSWORD  # the password still works as the break-glass route
    assert accounts(db) == ["Amy@MSP.example"]
    assert client.get(f"/tenants/{a}").status_code == 200
    detail = db.execute("SELECT detail FROM audit_log WHERE action = 'microsoft_connected'").fetchone()["detail"]
    assert detail == "amy@msp.example (matched the username)"


@responses.activate
def test_a_client_account_with_that_name_is_never_linked(client, db, vault, configured):
    make_user(db, "amy@msp.example", role="client")
    sent = start(client)
    resp, _ = come_back(client, sent, id_token(AMY, sent["nonce"], roles=["Analyst"]))
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")
    assert "already exists and isn't yours" in page(client, "/login")
    assert db.execute("SELECT entra_oid, role FROM users WHERE username <> 'owner'").fetchone()[:] == (None, "client")


@responses.activate
def test_disabled_accounts_stay_out(client, db, vault, configured):
    uid = make_user(db, "amy@msp.example", role="analyst")
    db.execute("UPDATE users SET disabled = 1, entra_oid = ? WHERE id = ?", (AMY["oid"], uid))
    db.commit()
    sent = start(client)
    resp, _ = come_back(client, sent, id_token(AMY, sent["nonce"], roles=["Admin"]))
    assert resp.headers["Location"].endswith("/login")
    assert "disabled" in page(client, "/login")
    assert signed_in_as(client) is None


@responses.activate
@pytest.mark.parametrize(
    "case",
    [
        "wrong state",
        "stale flow",
        "wrong nonce",
        "wrong tenant",
        "wrong audience",
        "expired",
        "unknown key",
        "bad signature",
    ],
)
def test_bad_tokens_and_bad_states_are_refused(client, db, vault, configured, case):
    sent = start(client)
    token = id_token(JACK, sent["nonce"])
    if case == "wrong nonce":
        token = id_token(JACK, "someone-elses-nonce")
    elif case == "wrong tenant":
        other = "deadbeef-1111-2222-3333-444455556666"
        token = id_token(JACK, sent["nonce"], tid=other, iss=f"{LOGIN}/{other}/v2.0")
    elif case == "wrong audience":
        token = id_token(JACK, sent["nonce"], aud="another-app")
    elif case == "expired":
        token = id_token(JACK, sent["nonce"], exp=int(time.time()) - 600)
    elif case == "unknown key":
        token = id_token(JACK, sent["nonce"], _kid="not-published")
    elif case == "bad signature":
        head, body, _sig = id_token(JACK, sent["nonce"]).split(".")
        token = f"{head}.{body}.AAAA"
    if case == "wrong state":
        sent = {**sent, "state": "forged"}
    if case == "stale flow":
        with client.session_transaction() as sess:
            sess["microsoft"] = {**sess["microsoft"], "started": sess["microsoft"]["started"] - sso.FLOW_SECONDS - 1}
    resp, call = come_back(client, sent, token)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login"), case
    assert signed_in_as(client) is None, case
    assert accounts(db) == [], case
    if case in ("wrong state", "stale flow"):
        assert not call.calls  # nothing was asked of Microsoft
    # A sign-in that got as far as Microsoft is written down (never the token itself).
    assert "secret" not in "".join(r["detail"] for r in db.execute("SELECT detail FROM audit_log"))


@responses.activate
def test_microsofts_errors_are_explained(client, db, vault, configured):
    sent = start(client)
    resp = client.get(
        f"/login/microsoft/callback?state={sent['state']}&error=access_denied&error_description="
        "AADSTS50105:+The+signed+in+user+is+not+assigned+to+a+role+for+the+application."
    )
    assert resp.status_code == 302
    text = page(client, "/login")
    assert "isn't assigned to Office Sentry yet" in text and "Users and groups" in text

    sent = start(client)
    resp, _ = come_back(
        client,
        sent,
        None,
        status=400,
        body={
            "error": "invalid_client",
            "error_description": "AADSTS700027: Client assertion failed signature validation.",
        },
    )
    assert "doesn't recognise Office Sentry's certificate" in page(client, "/login")


@responses.activate
def test_signing_keys_are_cached_and_refetched_for_a_new_key(client, db, vault, configured):
    sent = start(client)
    keys = mock_keys()
    responses.post(TOKEN_URL, json={"id_token": id_token(JACK, sent["nonce"])})
    client.get(f"/login/microsoft/callback?code=c1&state={sent['state']}")
    client.post("/logout")
    sent = start(client)
    responses.post(TOKEN_URL, json={"id_token": id_token(JACK, sent["nonce"])})
    client.get(f"/login/microsoft/callback?code=c2&state={sent['state']}")
    assert len(keys.calls) == 1  # the second sign-in used the cached keys
    assert signed_in_as(client) == "jack@msp.example"
    client.post("/logout")
    # Microsoft rotated to a key we haven't seen: fetched again (after the retry interval), and it works.
    responses.replace(responses.GET, KEYS_URL, json={"keys": [{**MS_JWK, "kid": "ms-key-2"}]})
    sso._KEYS[KEYS_URL] = (sso._KEYS[KEYS_URL][0] - sso.KEYS_RETRY, sso._KEYS[KEYS_URL][1])
    sent = start(client)
    responses.post(TOKEN_URL, json={"id_token": id_token(JACK, sent["nonce"], _kid="ms-key-2")})
    client.get(f"/login/microsoft/callback?code=c3&state={sent['state']}")
    assert signed_in_as(client) == "jack@msp.example"


# -- Account page: connecting and disconnecting ------------------------------------------------------


@responses.activate
def test_connect_from_the_account_page_then_sign_in_with_microsoft(client, db, vault, configured):
    make_user(db, "jack")
    login(client, "jack")
    text = page(client, "/account")
    assert "Connect my Microsoft account" in text
    resp = client.post("/account/microsoft/connect", data={"password": PASSWORD})
    sent = {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["Location"]).query).items()}
    assert sent["prompt"] == "select_account"
    resp, _ = come_back(client, sent, id_token(JACK, sent["nonce"]))
    assert resp.headers["Location"].endswith("/account")
    text = page(client, "/account")
    assert "Connected" in text and "jack@msp.example" in text and "You can sign in with Microsoft from now on" in text
    user = db.execute("SELECT * FROM users WHERE username = 'jack'").fetchone()
    assert user["entra_oid"] == JACK["oid"] and user["role"] == "admin"

    client.post("/logout")
    sent = start(client)
    resp, _ = come_back(client, sent, id_token(JACK, sent["nonce"]), code="code-2")
    assert signed_in_as(client) == "jack"  # the same account, not a second one
    assert accounts(db) == ["jack"]

    # Disconnecting goes back to the password (allowed: this account has one).
    assert client.post("/account/microsoft/disconnect", data={"password": PASSWORD}).status_code == 302
    assert db.execute("SELECT entra_oid FROM users WHERE username = 'jack'").fetchone()[0] is None
    assert "disconnected" in page(client, "/account")


@responses.activate
def test_connecting_warns_when_entra_gives_no_role(client, db, vault, configured):
    make_user(db, "jack")
    login(client, "jack")
    resp = client.post("/account/microsoft/connect", data={"password": PASSWORD})
    sent = {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["Location"]).query).items()}
    come_back(client, sent, id_token(JACK, sent["nonce"], roles=[]))
    text = page(client, "/account")
    assert "no Office Sentry role in Entra yet" in text and "your password still does" in text
    assert db.execute("SELECT entra_oid FROM users WHERE username = 'jack'").fetchone()[0] == JACK["oid"]


@responses.activate
def test_a_microsoft_account_connects_to_one_account_only(client, db, vault, configured):
    make_user(db, "jack")
    other = make_user(db, "someone-else")
    db.execute("UPDATE users SET entra_oid = ? WHERE id = ?", (JACK["oid"], other))
    db.commit()
    login(client, "jack")
    resp = client.post("/account/microsoft/connect", data={"password": PASSWORD})
    sent = {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["Location"]).query).items()}
    come_back(client, sent, id_token(JACK, sent["nonce"]))
    assert "already connected to another" in page(client, "/account")
    assert db.execute("SELECT entra_oid FROM users WHERE username = 'jack'").fetchone()[0] is None


@responses.activate
def test_the_connect_callback_needs_the_same_signed_in_person(client, db, vault, configured):
    make_user(db, "jack")
    login(client, "jack")
    resp = client.post("/account/microsoft/connect", data={"password": PASSWORD})
    sent = {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["Location"]).query).items()}
    client.post("/logout")
    resp, call = come_back(client, sent, id_token(JACK, sent["nonce"]))
    assert resp.headers["Location"].endswith("/login") and not call.calls
    assert db.execute("SELECT entra_oid FROM users WHERE username = 'jack'").fetchone()[0] is None


def test_clients_cannot_connect(client, db, configured):
    make_user(db, "client1", role="client")
    login(client, "client1")
    assert client.post("/account/microsoft/connect", data={"password": PASSWORD}).status_code == 404
    assert "Microsoft account" not in page(client, "/account")


def test_an_account_without_a_password_cannot_be_disconnected(client, db, vault, configured):
    uid = make_user(db, "jack@msp.example")
    db.execute("UPDATE users SET password_hash = ?, entra_oid = ? WHERE id = ?", (sso.NO_PASSWORD, JACK["oid"], uid))
    db.commit()
    admin = make_user(db, "admin")
    login(client, "admin")
    resp = client.post(f"/admin/users/{uid}/microsoft/disconnect")
    assert resp.status_code == 302
    assert "has no password, so can't be disconnected" in page(client, f"/admin/users/{uid}")
    assert db.execute("SELECT entra_oid FROM users WHERE id = ?", (uid,)).fetchone()[0] == JACK["oid"]
    # Set a password on their page: now it can be, and they sign in with that password.
    client.post(
        f"/admin/users/{uid}", data={"role": "admin", "password": "a brand new password", "admin_password": PASSWORD}
    )
    assert client.post(f"/admin/users/{uid}/microsoft/disconnect").status_code == 302
    assert db.execute("SELECT entra_oid FROM users WHERE id = ?", (uid,)).fetchone()[0] is None
    assert admin


def test_disconnecting_someone_else_takes_managing_users_and_reach(client, db, configured):
    owner = db.execute("SELECT id FROM users WHERE username = 'owner'").fetchone()[0]
    db.execute("UPDATE users SET entra_oid = ?, entra_tid = ? WHERE id = ?", (JACK["oid"], TENANT, owner))
    for name, caps in (("settings-only", ["settings.manage"]), ("helpdesk", ["users.manage"])):
        role = office_roles.create(db, "owner", name, "", "staff", "granted", caps)
        db.execute("UPDATE users SET role_id = ? WHERE id = ?", (role, make_user(db, name, role="analyst")))
    db.commit()
    before = db.execute("SELECT session_version FROM users WHERE id = ?", (owner,)).fetchone()[0]
    # Settings alone isn't managing users.
    login(client, "settings-only")
    assert client.post(f"/admin/users/{owner}/microsoft/disconnect").status_code == 403
    client.post("/logout")
    # Managing users, but not an Administrator's account.
    login(client, "helpdesk")
    resp = client.post(f"/admin/users/{owner}/microsoft/disconnect")
    assert resp.status_code == 302
    assert "Only an Administrator can change an Administrator's account" in page(client, resp.headers["Location"])
    row = db.execute("SELECT entra_oid, session_version FROM users WHERE id = ?", (owner,)).fetchone()
    assert tuple(row) == (JACK["oid"], before)
    assert client.post("/admin/users/999999/microsoft/disconnect").status_code == 404


# -- Passwords as the break-glass route ------------------------------------------------------------------


@responses.activate
def test_passwords_off_for_staff_needs_a_microsoft_session_and_spares_clients(client, db, vault, configured):
    make_user(db, "jack")
    make_user(db, "client1", role="client")
    login(client, "jack")
    # From a password session, turning passwords off is refused (and says why).
    resp = client.post("/admin/sign-in", data={"enabled": "1", "passwords": "off", "default_role": ""})
    assert resp.status_code == 302
    text = page(client, "/admin/sign-in")
    assert "first sign in with Microsoft yourself" in text
    assert sso.settings(db).get("sso_passwords", "on") == "on"

    # Signed in with Microsoft (the account connected by its name), it works.
    client.post("/logout")
    make_user(db, "jack@msp.example")
    sent = start(client)
    come_back(client, sent, id_token(JACK, sent["nonce"]))
    assert signed_in_as(client) == "jack@msp.example"
    client.post("/admin/sign-in", data={"enabled": "1", "passwords": "off", "default_role": ""})
    assert sso.settings(db)["sso_passwords"] == "off"
    assert db.execute("SELECT detail FROM audit_log WHERE action = 'staff_sign_in_changed'").fetchone()["detail"] == (
        "microsoft=on passwords=off default_role=none"
    )
    client.post("/logout")

    # Staff passwords are refused even when right, answered and counted exactly as a wrong one so the page
    # never confirms a guess; clients still sign in with theirs.
    right, wrong = login(client, "jack"), login(client, "jack", "not the password")
    assert right.status_code == wrong.status_code == 200
    assert "Passwords are off" not in right.get_data(as_text=True)
    assert _flash(right) and _flash(right) == _flash(wrong)
    assert signed_in_as(client) is None
    assert [r[0] for r in db.execute("SELECT outcome FROM login_attempts WHERE username = 'jack' ORDER BY id")][
        -2:
    ] == ["failed", "failed"]
    assert db.execute("SELECT detail FROM audit_log WHERE action = 'login_failed'").fetchone()["detail"] == (
        "password sign-in is off for staff"
    )
    assert login(client, "client1").status_code == 302
    assert signed_in_as(client) == "client1"
    client.post("/logout")
    text = page(client, "/login")
    assert "Client account? Sign in with a password" in text and "or with a password" not in text


def test_the_server_command_turns_passwords_back_on(client, db, configured, cfg, capsys, monkeypatch):
    sso.save(db, {"sso_passwords": "off"})
    db.commit()
    make_user(db, "jack")
    assert login(client, "jack").status_code == 200
    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(cfg.data_dir))
    main(["staff-sign-in", "--passwords", "on"])
    assert "Staff signing in with a password: allowed" in capsys.readouterr().out
    assert login(client, "jack").status_code == 302
    client.post("/logout")
    main(["staff-sign-in", "--off"])
    assert "Microsoft sign-in: set up but off" in capsys.readouterr().out
    assert client.get("/login/microsoft").status_code == 404
    assert "Sign in with Microsoft" not in page(client, "/login")


def test_turning_microsoft_off_turns_passwords_on(client, db, configured):
    make_user(db, "jack")
    login(client, "jack")
    db.execute("UPDATE sessions SET method = 'microsoft'")
    db.commit()
    client.post("/admin/sign-in", data={"enabled": "1", "passwords": "off", "default_role": ""})
    assert sso.settings(db)["sso_passwords"] == "off"
    client.post("/admin/sign-in", data={"passwords": "off", "default_role": ""})
    s = sso.settings(db)
    assert not sso.enabled(s) and s["sso_passwords"] == "on"


# -- When it isn't set up -----------------------------------------------------------------------------


def test_nothing_shows_until_it_is_set_up(client, db):
    assert client.get("/login/microsoft").status_code == 404
    assert client.get("/login/microsoft/callback?code=x&state=y").status_code == 404
    make_user(db, "jack")
    text = page(client, "/login")
    assert "Sign in with Microsoft" not in text and 'name="username"' in text
    login(client, "jack")
    assert "Microsoft account" not in page(client, "/account")
    text = page(client, "/admin/sign-in")
    assert "Sign in to Microsoft" in text and "Set it up by hand" in text


# -- The setup wizard --------------------------------------------------------------------------------------

DEVICE_CODE = "DAQABAAEAAAD-secret-device-code"
SETUP_TOKEN = jwt.encode({"tid": TENANT, "oid": JACK["oid"]}, "not-checked-here", algorithm="HS256")
GRAPH_SP = "sp-graph-object-id"


def _device_sign_in():
    responses.post(
        f"{LOGIN}/organizations/oauth2/v2.0/devicecode",
        json={
            "device_code": DEVICE_CODE,
            "user_code": "HXQ7-PL2M",
            "verification_uri": "https://microsoft.com/devicelogin",
            "expires_in": 900,
            "interval": 5,
        },
    )
    return responses.post(f"{LOGIN}/organizations/oauth2/v2.0/token", json={"access_token": SETUP_TOKEN})


def _graph(sp_status=201, assign_status=201, grant_status=201):
    created = responses.post(
        f"{GRAPH}/applications", status=201, json={"id": "app-object-1", "appId": CLIENT_ID.upper()}
    )
    sp = responses.post(
        f"{GRAPH}/servicePrincipals",
        status=sp_status,
        json={"id": "sp-object-1"} if sp_status < 300 else {"error": {"message": "no"}},
    )
    assign = responses.post(
        f"{GRAPH}/servicePrincipals/sp-object-1/appRoleAssignedTo",
        status=assign_status,
        json={} if assign_status < 300 else {"error": {"message": "no"}},
    )
    responses.get(
        f"{GRAPH}/servicePrincipals",
        json={"value": [{"id": GRAPH_SP}]},
        match=[matchers.query_param_matcher({"$filter": f"appId eq '{sso.GRAPH_APP_ID}'", "$select": "id"})],
    )
    grant = responses.post(
        f"{GRAPH}/oauth2PermissionGrants",
        status=grant_status,
        json={} if grant_status < 300 else {"error": {"message": "no"}},
    )
    return created, sp, assign, grant


def _everything_stored(db) -> str:
    out = []
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        for row in db.execute(f'SELECT * FROM "{table}"').fetchall():
            out.extend(str(v) for v in row)
    return "\n".join(out)


@responses.activate
def test_the_wizard_creates_the_sign_in_app_and_turns_it_on(client, db, vault):
    make_user(db, "jack")
    login(client, "jack")
    device = _device_sign_in()
    resp = client.post("/admin/sign-in/setup", data={})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/admin/sign-in#setup")
    asked = parse_qs(responses.calls[0].request.body)["scope"][0].split()
    assert asked == [f"https://graph.microsoft.com/{p}" for p in sso.SETUP_PERMISSIONS]
    text = page(client, "/admin/sign-in")
    assert "HXQ7-PL2M" in text and "Waiting for you to sign in" in text and DEVICE_CODE not in text
    row = db.execute("SELECT * FROM setup_sign_ins").fetchone()
    assert row["purpose"] == "sign_in" and row["device_code_enc"]
    # The App connection page's own wizard isn't confused by it.
    assert "Waiting for sign-in" not in page(client, "/admin/connections")

    created, sp, assign, grant = _graph()
    db.execute("UPDATE setup_sign_ins SET next_poll_at = NULL")
    db.commit()
    assert client.post("/admin/sign-in/setup/check", headers=JSON).get_json()["status"] == "done"
    assert parse_qs(device.calls[0].request.body)["device_code"] == [DEVICE_CODE]

    body = json.loads(created.calls[0].request.body)
    s = sso.settings(db)
    assert body["signInAudience"] == "AzureADMyOrg" and body["displayName"] == "Office Sentry sign-in"
    assert body["web"]["redirectUris"] == [CALLBACK]
    assert [r["value"] for r in body["appRoles"]] == [
        "Admin",
        "TeamLeader",
        "Analyst",
        "FirstLine",
        "AccountManager",
        "Auditor",
    ]
    assert body["keyCredentials"][0]["key"] and "requiredResourceAccess" not in body  # no API permissions
    assert json.loads(sp.calls[0].request.body) == {"appId": CLIENT_ID, "appRoleAssignmentRequired": True}
    assert json.loads(assign.calls[0].request.body) == {
        "principalId": JACK["oid"],
        "resourceId": "sp-object-1",
        "appRoleId": sso.APP_ROLE_DEFINITIONS[0]["id"],
    }
    assert json.loads(grant.calls[0].request.body) == {
        "clientId": "sp-object-1",
        "consentType": "AllPrincipals",
        "resourceId": GRAPH_SP,
        "scope": "openid profile email",
    }
    assert sso.enabled(s) and s["sso_tenant"] == TENANT and s["sso_client_id"] == CLIENT_ID
    assert s["sso_sp_object_id"] == "sp-object-1" and s["sso_set_up_by"] == "jack" and not s.get("sso_notes")
    assert vault.decrypt(s["sso_key_enc"]).startswith("-----BEGIN PRIVATE KEY-----")
    stored = _everything_stored(db)
    assert SETUP_TOKEN not in stored and DEVICE_CODE not in stored and "BEGIN PRIVATE KEY" not in stored
    actions = [r["action"] for r in db.execute("SELECT action FROM audit_log ORDER BY id")]
    assert actions[-2:] == ["app_setup_started", "sign_in_app_created"]

    text = page(client, "/admin/sign-in")
    assert "Connect my Microsoft account" in text and CLIENT_ID in text and "Replace the certificate" in text
    client.post("/logout")
    assert "Sign in with Microsoft" in page(client, "/login")


@responses.activate
def test_the_wizard_says_what_it_could_not_do(client, db, vault):
    make_user(db, "jack")
    login(client, "jack")
    _device_sign_in()
    client.post("/admin/sign-in/setup", data={})
    _graph(assign_status=403, grant_status=403)
    db.execute("UPDATE setup_sign_ins SET next_poll_at = NULL")
    db.commit()
    assert client.post("/admin/sign-in/setup/check", headers=JSON).get_json()["status"] == "done"
    text = page(client, "/admin/sign-in")
    assert "Nobody has a role yet" in text and "Consent for the sign-in scopes wasn't granted" in text
    assert sso.enabled(sso.settings(db))


@responses.activate
def test_the_wizard_fails_in_words_when_the_app_cannot_be_made(client, db, vault):
    make_user(db, "jack")
    login(client, "jack")
    _device_sign_in()
    client.post("/admin/sign-in/setup", data={})
    responses.post(f"{GRAPH}/applications", status=403, json={"error": {"message": "Insufficient privileges"}})
    db.execute("UPDATE setup_sign_ins SET next_poll_at = NULL")
    db.commit()
    assert client.post("/admin/sign-in/setup/check", headers=JSON).get_json()["status"] == "failed"
    text = page(client, "/admin/sign-in")
    assert "can't create app registrations" in text and not sso.is_configured(sso.settings(db))


# -- Settings page: by hand, certificates, removal ---------------------------------------------------------


def test_set_up_by_hand_and_replace_the_certificate(client, db, vault):
    make_user(db, "jack")
    login(client, "jack")
    assert client.post("/admin/sign-in/certificate").status_code == 302
    s = sso.settings(db)
    assert s["sso_thumbprint"] and vault.decrypt(s["sso_key_enc"])
    cer = client.get("/admin/sign-in/certificate.cer")
    assert cer.status_code == 200 and cer.mimetype == "application/pkix-cert" and cer.data[0] == 0x30
    assert b"PRIVATE" not in cer.data

    resp = client.post("/admin/sign-in/manual", data={"tenant": "not-a-guid", "client_id": CLIENT_ID})
    assert "tenant ID is the Directory" in page(client, "/admin/sign-in")
    resp = client.post("/admin/sign-in/manual", data={"tenant": TENANT.upper(), "client_id": CLIENT_ID.upper()})
    assert resp.status_code == 302
    s = sso.settings(db)
    assert sso.is_configured(s) and not sso.enabled(s) and s["sso_tenant"] == TENANT
    text = page(client, "/admin/sign-in")
    assert "Set up, but the button is hidden" in text
    client.post("/admin/sign-in", data={"enabled": "1", "passwords": "on", "default_role": ""})
    assert sso.enabled(sso.settings(db))

    # Replacement: made beside the current one, downloadable, then switched to.
    old = s["sso_thumbprint"]
    client.post("/admin/sign-in/certificate")
    s = sso.settings(db)
    assert s["sso_thumbprint"] == old and s["sso_next_thumbprint"] and s["sso_next_thumbprint"] != old
    assert client.get("/admin/sign-in/certificate.cer?which=next").status_code == 200
    assert "Switch to the new certificate" in page(client, "/admin/sign-in")
    client.post("/admin/sign-in/certificate/switch")
    s = sso.settings(db)
    assert s["sso_thumbprint"] != old and not s.get("sso_next_thumbprint") and not s.get("sso_next_key_enc")
    assert client.get("/admin/sign-in/certificate.cer?which=next").status_code == 404


def test_remove(client, db, configured):
    make_user(db, "jack")
    login(client, "jack")
    assert client.post("/admin/sign-in/remove").status_code == 302
    assert not sso.settings(db)
    assert client.get("/login/microsoft").status_code == 404
    assert "Let your team sign in with Microsoft" in page(client, "/admin/sign-in")


def test_certificate_expiry_is_watched(db, configured):
    from officesentry import cert_lifecycle

    db.execute("UPDATE settings SET value = '2026-10-20T00:00:00Z' WHERE key = 'sso_cert_not_after'")
    db.commit()
    from datetime import UTC, datetime

    found = cert_lifecycle.expiring(db, datetime(2026, 10, 1, tzinfo=UTC))
    assert [e.kind for e in found] == ["sign_in"] and found[0].level == "30"
    assert found[0].headline.startswith("The sign-in app's certificate expires in 19 days")
    assert "Microsoft sign-in" in cert_lifecycle.fix_steps(found[0])


def test_admins_only(client, db, configured):
    make_user(db, "ana", role="analyst")
    login(client, "ana")
    assert client.get("/admin/sign-in").status_code == 403
    assert client.post("/admin/sign-in/setup").status_code == 403
    assert client.get("/admin/sign-in/certificate.cer").status_code == 403


# -- Review follow-ups: linking, password checks, a rotated key, long names ---------------------------------


@responses.activate
def test_the_email_claim_never_links_an_account(client, db, vault, configured):
    make_user(db, "amy@msp.example", role="analyst")  # Amy's mail address, not her sign-in name
    sent = start(client)
    token = id_token(
        AMY, sent["nonce"], roles=["Analyst"], preferred_username="amy.lee@msp.example", email="amy@msp.example"
    )
    resp, _ = come_back(client, sent, token)
    assert resp.status_code == 302
    assert db.execute("SELECT entra_oid FROM users WHERE username = 'amy@msp.example'").fetchone()[0] is None
    assert "amy.lee@msp.example" in accounts(db)  # a new account instead


@responses.activate
def test_a_username_without_an_at_sign_is_never_claimed(client, db, vault, configured):
    make_user(db, "amy", role="analyst")
    sent = start(client)
    come_back(client, sent, id_token(AMY, sent["nonce"], roles=["Analyst"], preferred_username="amy"))
    assert db.execute("SELECT entra_oid FROM users WHERE username = 'amy'").fetchone()[0] is None
    assert "already exists and isn't yours" in page(client, "/login")


def test_connecting_and_disconnecting_ask_for_the_password(client, db, vault, configured):
    make_user(db, "jack")
    login(client, "jack")
    resp = client.post("/account/microsoft/connect", data={"password": "wrong"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/account#microsoft")
    assert "isn't right" in page(client, "/account")
    db.execute("UPDATE users SET entra_oid = ? WHERE username = 'jack'", (JACK["oid"],))
    db.commit()
    client.post("/account/microsoft/disconnect", data={"password": "wrong"})
    assert db.execute("SELECT entra_oid FROM users WHERE username = 'jack'").fetchone()[0] == JACK["oid"]
    client.post("/account/microsoft/disconnect", data={"password": PASSWORD})
    assert db.execute("SELECT entra_oid FROM users WHERE username = 'jack'").fetchone()[0] is None


@responses.activate
def test_a_key_that_no_longer_decrypts_gives_a_message_not_an_error(client, db, vault, configured):
    sso.save(db, {"sso_key_enc": "gAAAAABnot-a-token-any-more"})
    db.commit()
    sent = start(client)
    resp, call = come_back(client, sent, id_token(JACK, sent["nonce"]))
    assert resp.status_code == 302 and not call.calls
    assert "can't read the key of its sign-in certificate" in page(client, "/login")


def test_long_sign_in_names_are_cut_to_the_username_limit():
    identity = sso.Identity(oid="o", tid="t", upn="a" * 70 + "@msp.example", name="", email=None)
    assert len(identity.username) == 64
