"""Client sign-in with Microsoft (sso.py, web/sso.py, web/client_access.py): a client's people sign in with their
own work account, from their own company's tenant only, into their own client account, and nothing more.

Most of these tests try to cross the boundary: a token from another client's tenant, from the MSP's tenant, with
app roles in it, signed for one tenant and claiming another, naming someone else's address, for an account that's
staff, disabled, archived, in another cloud or granted another company's client. Accounts are matched on the
token's tenant and object ID only. Microsoft is simulated at the HTTP level, as in test_staff_sign_in.py.
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

from officesentry import permissions, sso, store
from officesentry import roles as office_roles
from tests.conftest import PASSWORD, PASSWORD_HASH, login, make_user

LOGIN = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
MSP = "c0ffee00-1111-2222-3333-444455556666"  # the MSP's own tenant, where the sign-in app lives
ALPHA = "a1a1a1a1-1111-2222-3333-444455556666"  # Alpha Ltd's tenant (added by ID)
BRAVO = "b2b2b2b2-1111-2222-3333-444455556666"  # Bravo Inc's tenant (added by domain; learnt from collection)
CLIENT_ID = "aaaa1111-2222-3333-4444-555566667777"
CALLBACK = "http://localhost:8000/login/microsoft/callback"
ORGANIZATIONS = "organizations"
JSON = {"Accept": "application/json"}

_MS_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
MS_KID = "ms-key-1"
MS_JWK = {**json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(_MS_KEY.public_key())), "kid": MS_KID, "use": "sig"}
_MS_KEY_PEM = _MS_KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
).decode()

JO = {"oid": "22222222-aaaa-bbbb-cccc-000000000001", "upn": "jo@alpha.example", "name": "Jo Alpha", "tid": ALPHA}
BO = {"oid": "22222222-aaaa-bbbb-cccc-000000000002", "upn": "bo@bravo.example", "name": "Bo Bravo", "tid": BRAVO}
JACK = {"oid": "11111111-aaaa-bbbb-cccc-000000000001", "upn": "jack@msp.example", "name": "Jack Doe", "tid": MSP}
AMY = {"oid": "11111111-aaaa-bbbb-cccc-000000000002", "upn": "amy@msp.example", "name": "Amy Lee", "tid": MSP}


def id_token(person: dict, nonce: str, roles=None, **overrides) -> str:
    """An ID token as Microsoft signs it: issued by the person's own tenant (``tid`` and ``iss`` agree)."""
    now = int(time.time())
    tid = overrides.pop("tid", person["tid"])
    claims = {
        "aud": CLIENT_ID,
        "iss": f"{LOGIN}/{tid}/v2.0",
        "iat": now - 5,
        "nbf": now - 5,
        "exp": now + 3600,
        "sub": "sub-" + person["oid"],
        "oid": person["oid"],
        "tid": tid,
        "preferred_username": person["upn"],
        "name": person["name"],
        "email": person["upn"],
        "nonce": nonce,
        "ver": "2.0",
    }
    if roles is not None:
        claims["roles"] = list(roles)
    claims.update(overrides)
    return jwt.encode(claims, _MS_KEY_PEM, algorithm="RS256", headers={"kid": MS_KID})


PAT = {"oid": "22222222-aaaa-bbbb-cccc-000000000003", "upn": "pat@alpha.example", "name": "Pat Alpha", "tid": ALPHA}
# Kim's sign-in name isn't her email address; Lee is a guest in Alpha's directory from another company.
KIM = {"oid": "22222222-aaaa-bbbb-cccc-000000000004", "upn": "kim@alpha.onmicrosoft.com", "name": "Kim", "tid": ALPHA}
LEE = {"oid": "22222222-aaaa-bbbb-cccc-000000000005", "upn": "lee_partner.example#EXT#@alpha.onmicrosoft.com"}


def directory(db, tenant_id: int, *people: dict, extra=None) -> None:
    """A Users collection of the client's directory holding exactly ``people``."""
    run = store.enqueue_run(db, tenant_id, "users")
    rows = [
        (
            p["oid"],
            {"userPrincipalName": p["upn"], "mail": p["upn"], "displayName": p.get("name"), "userType": "Member"},
        )
        for p in people
    ]
    store.add_objects(db, run, "user", rows + list(extra or []))
    store.finish_run(db, run, "succeeded")
    db.commit()


@pytest.fixture
def world(db, vault, two_tenants):
    """Microsoft sign-in set up and turned on for staff and clients; Alpha (added by its tenant ID) and Bravo
    (added by domain, its ID read by its last collection), each with its directory collected."""
    alpha, bravo = two_tenants
    sso.forget_keys()
    make_user(db, "owner")
    sso.new_certificate(db, vault)
    sso.store_manual(db, vault, MSP, CLIENT_ID, "commercial", "Office Sentry sign-in", "admin")
    sso.save(db, {"sso_enabled": "1", "sso_multi_tenant": "1", "sso_clients": "1"})
    db.execute("UPDATE tenants SET tenant_ref = ? WHERE id = ?", (ALPHA, alpha))
    run = store.enqueue_run(db, bravo, "tenant_profile")
    store.add_objects(db, run, "organization", [(BRAVO, {"id": BRAVO, "displayName": "Bravo Inc"})])
    store.finish_run(db, run, "succeeded")
    kim = (KIM["oid"], {"userPrincipalName": KIM["upn"], "mail": "kim@alpha.example", "userType": "Member"})
    lee = (LEE["oid"], {"userPrincipalName": LEE["upn"], "mail": "lee@partner.example", "userType": "Guest"})
    directory(db, alpha, JO, PAT, extra=[kim, lee])
    directory(db, bravo, BO)
    return alpha, bravo


def client_account(db, username: str, tenants=(), password: bool = False, person: dict | None = None) -> int:
    """A Client viewer account granted ``tenants``, with no password by default, connected to ``person``'s
    Microsoft account as the Client sign-in page connects it."""
    role = office_roles.by_key(db, "client_viewer")
    uid = make_user(db, username, role="client", tenants=tenants)
    db.execute(
        "UPDATE users SET role_id = ?, password_hash = ? WHERE id = ?",
        (role.id, PASSWORD_HASH if password else sso.NO_PASSWORD, uid),
    )
    if person:
        db.execute(
            "UPDATE users SET entra_oid = ?, entra_tid = ?, entra_upn = ?, entra_linked_at = ? WHERE id = ?",
            (person["oid"], person["tid"], person["upn"], "2026-01-01T00:00:00Z", uid),
        )
    db.commit()
    return uid


def start(client, query: str = "") -> tuple[str, dict]:
    """Press "Sign in with Microsoft": the tenant (or ``organizations``) the browser is sent to, and the query."""
    resp = client.get("/login/microsoft" + query)
    assert resp.status_code == 302, resp.status_code
    url = urlsplit(resp.headers["Location"])
    assert url.path.endswith("/oauth2/v2.0/authorize"), url.path
    return url.path.split("/")[1], {k: v[0] for k, v in parse_qs(url.query).items()}


def come_back(client, sent: dict, token: str, authority: str = ORGANIZATIONS, code: str = "auth-code-1"):
    """Microsoft sends the browser back; the authority's token endpoint answers with ``token``."""
    responses.get(f"{LOGIN}/{authority}/discovery/v2.0/keys", json={"keys": [MS_JWK]})
    call = responses.post(f"{LOGIN}/{authority}/oauth2/v2.0/token", json={"id_token": token})
    resp = client.get(f"/login/microsoft/callback?code={code}&state={sent['state']}")
    return resp, call


def sign_in(client, person: dict, roles=None, **overrides):
    authority, sent = start(client)
    resp, _ = come_back(client, sent, id_token(person, sent["nonce"], roles=roles, **overrides), authority)
    return resp


def page(client, url: str) -> str:
    return html.unescape(client.get(url).get_data(as_text=True))


def signed_in_as(client) -> str | None:
    resp = client.get("/account")
    if resp.status_code != 200:
        return None
    text = resp.get_data(as_text=True)
    return text.split("Signed in as ", 1)[1].split(" ", 1)[0] if "Signed in as " in text else None


def user(db, username: str):
    return db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def flashed(client) -> str:
    return page(client, "/login")


# -- Signing in --------------------------------------------------------------------------------------------


@responses.activate
def test_client_signs_in_with_their_own_work_account(client, db, world, cfg):
    alpha, bravo = world
    client_account(db, "jo@alpha.example", [alpha], person=JO)
    assert "Sign in with Microsoft" in page(client, "/login")

    authority, sent = start(client)
    assert authority == ORGANIZATIONS  # any organisation's account; the token's tenant decides who it is
    assert sent["client_id"] == CLIENT_ID and sent["redirect_uri"] == CALLBACK and "prompt" not in sent
    resp, call = come_back(client, sent, id_token(JO, sent["nonce"]))
    assert resp.status_code == 302 and "/login" not in resp.headers["Location"]
    # The code was redeemed at the same authority, with an assertion made out to it.
    form = {k: v[0] for k, v in parse_qs(call.calls[0].request.body).items()}
    public_key = load_pem_x509_certificate(sso.settings(db)["sso_cert_pem"].encode()).public_key()
    jwt.decode(
        form["client_assertion"],
        public_key,
        algorithms=["RS256"],
        audience=f"{LOGIN}/{ORGANIZATIONS}/oauth2/v2.0/token",
    )

    jo = user(db, "jo@alpha.example")
    assert jo["entra_oid"] == JO["oid"] and jo["entra_tid"] == ALPHA and jo["entra_upn"] == JO["upn"]
    assert jo["role"] == "client" and jo["password_hash"] == sso.NO_PASSWORD and jo["last_login_at"]
    assert store.get_tenant(db, alpha)["sign_in_approved_at"]  # someone from Alpha got in, so Alpha approved
    assert store.get_tenant(db, bravo)["sign_in_approved_at"] is None
    assert db.execute("SELECT method FROM sessions").fetchone()["method"] == "microsoft"
    assert signed_in_as(client) == "jo@alpha.example"
    audit = db.execute("SELECT * FROM audit_log WHERE action = 'login'").fetchone()
    assert audit["username"] == "jo@alpha.example" and audit["detail"] == "Microsoft"

    # Alpha's reports, and nothing else: not Bravo, not staff pages, not settings, not the sign-in setup.
    home = page(client, "/")
    assert "Alpha Ltd" in home and "Bravo Inc" not in home
    assert client.get(f"/tenants/{bravo}").status_code == 404
    assert client.get(f"/tenants/{bravo}/findings").status_code == 404
    for url in (
        "/admin/users",
        "/admin/sign-in",
        "/admin/connections",
        f"/tenants/{alpha}/sign-in",
        "/across",
        "/admin/sign-in/certificate.cer",
    ):
        assert client.get(url).status_code in (403, 404), url
    assert client.post("/account/microsoft/confirm").status_code == 404
    assert client.post("/admin/sign-in/clients", data={"clients": ""}).status_code == 403
    assert sso.clients_enabled(sso.settings(db))

    # Next time it's the same account, found by its object ID.
    client.post("/logout")
    assert sign_in(client, JO).status_code == 302
    assert signed_in_as(client) == "jo@alpha.example"
    assert db.execute("SELECT COUNT(*) FROM users WHERE entra_oid = ?", (JO["oid"],)).fetchone()[0] == 1


@responses.activate
def test_a_client_added_by_domain_signs_in_once_its_id_is_known(client, db, world):
    _, bravo = world
    client_account(db, "bo@bravo.example", [bravo], person=BO)
    assert sign_in(client, BO).status_code == 302
    assert signed_in_as(client) == "bo@bravo.example"


@responses.activate
def test_accounts_are_matched_on_tenant_and_object_id_never_on_a_name(client, db, world):
    alpha, _ = world
    # An account named exactly as Jo's sign-in name, but never connected to her Microsoft account.
    client_account(db, "jo@alpha.example", [alpha])
    sign_in(client, JO)
    assert signed_in_as(client) is None and user(db, "jo@alpha.example")["entra_oid"] is None
    assert sso.NOT_SET_UP in flashed(client)

    # Pat is connected; Alpha's administrator renames someone else to Pat's name and address.
    client_account(db, "pat@alpha.example", [alpha], person=PAT)
    impostor = {**PAT, "oid": "22222222-aaaa-bbbb-cccc-0000000000ff"}
    sign_in(client, impostor)
    assert signed_in_as(client) is None
    sign_in(client, JO, preferred_username=PAT["upn"], email=PAT["upn"])
    assert signed_in_as(client) is None

    # Pat, renamed in Alpha since: still Pat, by their object ID. Staff pages keep showing the address from
    # Alpha's directory, not whatever the token says.
    assert sign_in(client, PAT, preferred_username="jack@msp.example", email="").status_code == 302
    assert signed_in_as(client) == "pat@alpha.example"
    assert user(db, "pat@alpha.example")["entra_upn"] == PAT["upn"]


@responses.activate
def test_a_token_from_another_clients_tenant_gets_nowhere(client, db, world):
    alpha, bravo = world
    jo = client_account(db, "jo@alpha.example", [alpha], person=JO)
    # Jo's object ID and sign-in name, but issued by Bravo's tenant.
    resp = sign_in(client, JO, tid=BRAVO)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")
    assert sso.NOT_SET_UP in flashed(client)
    assert signed_in_as(client) is None
    # Never says whether an account exists, and never stores a stranger's name.
    rows = db.execute("SELECT * FROM audit_log WHERE action LIKE 'login_failed%'").fetchall()
    assert rows and all("jo@alpha.example" not in (r["detail"] or "") for r in rows)
    assert all(r["username"] is None for r in rows)
    # Granted Bravo too: Bravo's tenant still can't sign in as Jo.
    db.execute("INSERT INTO user_tenants (user_id, tenant_id) VALUES (?, ?)", (jo, bravo))
    db.commit()
    sign_in(client, JO, tid=BRAVO)
    assert signed_in_as(client) is None
    assert user(db, "jo@alpha.example")["entra_tid"] == ALPHA and user(db, "jo@alpha.example")["last_login_at"] is None


@responses.activate
def test_an_account_granted_another_client_only_is_refused(client, db, world):
    _, bravo = world
    client_account(db, "jo@alpha.example", [bravo], person=JO)  # granted Bravo, but signs in from Alpha
    sign_in(client, JO)
    assert signed_in_as(client) is None


@responses.activate
def test_a_client_account_only_ever_sees_its_own_organisation(client, db, world):
    alpha, bravo = world
    # Granted Bravo as well, by mistake (or by SQL): it still only ever sees Alpha, signed in either way.
    jo = client_account(db, "jo@alpha.example", [alpha, bravo], password=True, person=JO)
    for signed_in in (lambda: sign_in(client, JO), lambda: login(client, "jo@alpha.example")):
        signed_in()
        assert signed_in_as(client) == "jo@alpha.example"
        home = page(client, "/")
        assert "Alpha Ltd" in home and "Bravo Inc" not in home
        assert client.get(f"/tenants/{alpha}/findings").status_code == 200
        for url in (f"/tenants/{bravo}", f"/tenants/{bravo}/findings", f"/tenants/{bravo}/report-history"):
            assert client.get(url).status_code == 404, url
        client.post("/logout")
    # The users page won't grant it another organisation's client either.
    login(client, "owner")
    db.execute("DELETE FROM user_tenants WHERE user_id = ? AND tenant_id = ?", (jo, bravo))
    db.commit()
    viewer = office_roles.by_key(db, "client_viewer").id
    client.post(f"/admin/users/{jo}", data={"role": viewer, "tenants": [str(alpha), str(bravo)]})
    assert "can't be given Bravo Inc" in page(client, f"/admin/users/{jo}")
    grants = [r[0] for r in db.execute("SELECT tenant_id FROM user_tenants WHERE user_id = ?", (jo,))]
    assert grants == [alpha]


def _grants(db, user_id: int) -> list[int]:
    return [r[0] for r in db.execute("SELECT tenant_id FROM user_tenants WHERE user_id = ? ORDER BY 1", (user_id,))]


@responses.activate
def test_disconnecting_never_makes_another_organisations_client_count(client, db, world):
    alpha, bravo = world
    jo = client_account(db, "jo@alpha.example", [alpha, bravo], password=True, person=JO)
    sign_in(client, JO)
    assert client.get(f"/tenants/{bravo}/findings").status_code == 404
    client.post("/account/microsoft/disconnect", data={"password": PASSWORD})
    assert user(db, "jo@alpha.example")["entra_oid"] is None and signed_in_as(client) == "jo@alpha.example"
    # Bravo was never theirs to see, so it goes rather than starting to count; Alpha stays.
    assert _grants(db, jo) == [alpha]
    assert client.get(f"/tenants/{bravo}/findings").status_code == 404
    assert client.get(f"/tenants/{alpha}/findings").status_code == 200


def test_staff_limited_to_one_client_cant_change_an_account_holding_another(client, db, world):
    """A grant the account can't see now still puts it out of reach of staff without that client."""
    alpha, bravo = world
    jo = client_account(db, "jo@alpha.example", [alpha, bravo], password=True, person=JO)
    role = office_roles.create(db, "owner", "Alpha's manager", "", "staff", "granted", permissions.KEYS)
    manager = make_user(db, "manager", role="analyst", tenants=[alpha])
    db.execute("UPDATE users SET role_id = ? WHERE id = ?", (role, manager))
    db.commit()
    login(client, "manager")
    before = tuple(user(db, "jo@alpha.example"))
    new = {"tenants": [str(alpha)], "password": "Kettle-Drum-Orchard-42", "admin_password": PASSWORD}
    client.post(f"/admin/users/{jo}", data=new)
    resp = client.post(f"/admin/users/{jo}/microsoft/disconnect")
    assert "only an Administrator can change their account" in page(client, resp.headers["Location"])
    assert tuple(user(db, "jo@alpha.example")) == before and _grants(db, jo) == [alpha, bravo]


@responses.activate
def test_app_roles_in_a_clients_token_count_for_nothing(client, db, world):
    alpha, _ = world
    client_account(db, "jo@alpha.example", [alpha], person=JO)
    # Alpha's own admin assigned Jo the "Admin" app role on the app in Alpha's tenant.
    assert sign_in(client, JO, roles=["Admin"]).status_code == 302
    jo = user(db, "jo@alpha.example")
    assert jo["role"] == "client" and office_roles.role_of(db, jo).key == "client_viewer"
    assert client.get("/admin/users").status_code == 403
    client.post("/logout")

    # Someone at Alpha with no account here and an Admin app role: refused, and no account is made.
    before = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    stranger = {"oid": "22222222-aaaa-bbbb-cccc-000000000009", "upn": "boss@alpha.example", "name": "B", "tid": ALPHA}
    sign_in(client, stranger, roles=["Admin"])
    assert signed_in_as(client) is None
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == before


@responses.activate
def test_a_clients_tenant_never_reaches_a_staff_account(client, db, world):
    alpha, _ = world
    # A staff account named like an address at a client (a contractor, or a mistake).
    make_user(db, "jo@alpha.example", role="analyst", tenants=[alpha])
    db.commit()
    sign_in(client, JO, roles=["Admin"])
    assert signed_in_as(client) is None
    staff = user(db, "jo@alpha.example")
    assert staff["entra_oid"] is None and staff["role"] == "analyst"


@responses.activate
def test_the_msps_tenant_never_links_a_client_account(client, db, world):
    alpha, _ = world
    client_account(db, "jack@msp.example", [alpha])
    sign_in(client, JACK, roles=["Admin"])  # staff path: Entra's Admin role, a client's username
    assert signed_in_as(client) is None
    acct = user(db, "jack@msp.example")
    assert acct["entra_oid"] is None and acct["role"] == "client"

    # A client account linked at Alpha; the same object ID presented by the MSP's tenant is not staff.
    jo = client_account(db, "jo@alpha.example", [alpha])
    db.execute("UPDATE users SET entra_oid = ?, entra_tid = ? WHERE id = ?", (JO["oid"], ALPHA, jo))
    db.commit()
    sign_in(client, JO, roles=["Admin"], tid=MSP)
    assert signed_in_as(client) is None
    assert user(db, "jo@alpha.example")["role"] == "client"
    assert sso.NOT_STAFF in flashed(client)
    # Even made staff behind the users page's back, an account linked at a client stays out of the staff path.
    db.execute("UPDATE users SET role = 'analyst' WHERE id = ?", (jo,))
    db.commit()
    sign_in(client, JO, roles=["Admin"], tid=MSP)
    assert signed_in_as(client) is None and user(db, "jo@alpha.example")["role"] == "analyst"


@pytest.mark.parametrize("change", ["disabled", "archived", "other_cloud", "ungranted"])
@responses.activate
def test_client_sign_in_is_refused_when_the_account_or_client_is_out(client, db, world, change):
    alpha, _ = world
    jo = client_account(db, "jo@alpha.example", [alpha], person=JO)
    if change == "disabled":
        db.execute("UPDATE users SET disabled = 1 WHERE id = ?", (jo,))
    elif change == "archived":
        db.execute("UPDATE tenants SET archived_at = '2026-01-01T00:00:00Z' WHERE id = ?", (alpha,))
    elif change == "other_cloud":
        db.execute("UPDATE tenants SET cloud = 'gcc_high' WHERE id = ?", (alpha,))
    else:
        db.execute("DELETE FROM user_tenants WHERE user_id = ?", (jo,))
    db.commit()
    resp = sign_in(client, JO)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")
    assert signed_in_as(client) is None


@responses.activate
def test_with_client_sign_in_off_only_the_msps_tenant_gets_in(client, db, world):
    alpha, _ = world
    client_account(db, "jo@alpha.example", [alpha], person=JO)
    # Turned off while someone was at Microsoft's page: their answer no longer counts.
    authority, sent = start(client)
    assert authority == ORGANIZATIONS
    sso.save(db, {"sso_clients": ""})
    db.commit()
    come_back(client, sent, id_token(JO, sent["nonce"]))
    assert signed_in_as(client) is None and user(db, "jo@alpha.example")["last_login_at"] is None

    authority, sent = start(client)
    assert authority == MSP  # back to the MSP's own tenant
    come_back(client, sent, id_token(JO, sent["nonce"]), MSP)
    assert signed_in_as(client) is None and user(db, "jo@alpha.example")["last_login_at"] is None

    # On, but the sign-in app was never made to accept other organisations: still off.
    sso.save(db, {"sso_clients": "1", "sso_multi_tenant": ""})
    db.commit()
    assert not sso.clients_enabled(sso.settings(db))
    assert start(client)[0] == MSP


@responses.activate
def test_a_token_must_be_issued_by_its_own_tenant(client, db, world):
    alpha, _ = world
    client_account(db, "jo@alpha.example", [alpha], person=JO)
    for iss in (
        f"{LOGIN}/{BRAVO}/v2.0",
        f"{LOGIN}/{MSP}/v2.0",
        f"{LOGIN}/common/v2.0",
        f"{LOGIN}/organizations/v2.0",
        f"{LOGIN}/{{tenantid}}/v2.0",  # the issuer template in the organizations metadata
        "https://evil.example/v2.0",
    ):
        resp = sign_in(client, JO, iss=iss)
        assert resp.status_code == 302 and signed_in_as(client) is None, iss
    sign_in(client, JO, tid="not-a-guid", iss=f"{LOGIN}/not-a-guid/v2.0")
    assert signed_in_as(client) is None
    # Signed by a tenant that is no client's (issuer and tid agree), with Jo's object ID.
    other = "c3c3c3c3-1111-2222-3333-444455556666"
    sign_in(client, JO, tid=other)
    assert signed_in_as(client) is None
    assert user(db, "jo@alpha.example")["last_login_at"] is None
    # A token for another app.
    sign_in(client, JO, aud="bbbb1111-2222-3333-4444-555566667777")
    assert signed_in_as(client) is None


@responses.activate
def test_staff_still_sign_in_through_organizations(client, db, world):
    assert sign_in(client, JACK, roles=["Admin"]).status_code == 302
    jack = user(db, "jack@msp.example")
    assert jack["role"] == "admin" and jack["entra_tid"] == MSP
    client.post("/logout")
    # "?as=staff" keeps to the MSP's tenant, for staff the organizations sign-in can't place (guests).
    authority, _ = start(client, "?as=staff")
    assert authority == MSP


@responses.activate
def test_staff_are_offered_their_own_tenant_when_microsoft_or_the_client_path_turns_them_away(client, db, world):
    assert "as staff" not in page(client, "/login")
    _, sent = start(client)
    resp = client.get(
        f"/login/microsoft/callback?error=invalid_request&error_description=AADSTS50194%3A+Application+is+not+"
        f"configured+as+a+multi-tenant+application.&state={sent['state']}"
    )
    assert resp.status_code == 302
    text = page(client, "/login")
    assert "only accepts" in text and "Sign in with Microsoft as staff" in text
    assert "?as=staff" in text or "as=staff" in text

    # A guest of the MSP's tenant signs in through organizations with their home tenant: not a client, so
    # the same way through is offered.
    with client.session_transaction() as s:
        s.clear()
    guest = {"oid": "33333333-aaaa-bbbb-cccc-000000000001", "upn": "pat@partner.example", "name": "Pat", "tid": BRAVO}
    sign_in(client, guest)
    assert "Sign in with Microsoft as staff" in page(client, "/login")


# -- A client's administrator approving the app ------------------------------------------------------------


def _approval_link(client, tenant_id: int) -> str:
    text = client.get(f"/tenants/{tenant_id}/sign-in").get_data(as_text=True)
    found = re.search(r'data-copy-text="(https://login\.microsoftonline\.com/[^"]+/adminconsent\?[^"]+)"', text)
    assert found, "no approval link on the page"
    return html.unescape(found.group(1))


def test_the_approval_link_asks_only_for_sign_in(client, db, world):
    alpha, bravo = world
    login(client, "owner")
    link = urlsplit(_approval_link(client, alpha))
    assert link.path == f"/{ALPHA}/v2.0/adminconsent"
    query = {k: v[0] for k, v in parse_qs(link.query).items()}
    assert query["client_id"] == CLIENT_ID and query["scope"] == "openid profile email"
    assert query["redirect_uri"] == CALLBACK and query["state"]
    assert urlsplit(_approval_link(client, bravo)).path == f"/{BRAVO}/v2.0/adminconsent"


def test_the_approval_return_page_records_nothing(client, db, world):
    """Anyone holding the link can write the return address, so it only thanks or explains; a client counts
    as approved once someone from it signs in (test_client_signs_in_with_their_own_work_account)."""
    alpha, _ = world
    login(client, "owner")
    state = parse_qs(urlsplit(_approval_link(client, alpha)).query)["state"][0]
    client.post("/logout")

    # Approved, but from another organisation.
    text = page(client, f"/login/microsoft/callback?admin_consent=True&tenant={BRAVO}&state={state}")
    assert "another organisation" in text and "Alpha Ltd" not in text
    assert store.get_tenant(db, alpha)["sign_in_approved_at"] is None
    # Declined: Microsoft's own words are never shown back.
    text = page(
        client,
        f"/login/microsoft/callback?error=access_denied&error_description=AADSTS65004%3A+<b>declined</b>&state={state}",
    )
    assert "wasn't approved" in text and "<b>declined</b>" not in text and "AADSTS" not in text
    assert store.get_tenant(db, alpha)["sign_in_approved_at"] is None
    # A forged state is just a broken sign-in.
    resp = client.get(f"/login/microsoft/callback?admin_consent=True&tenant={ALPHA}&state={state}x")
    assert resp.status_code == 302 and store.get_tenant(db, alpha)["sign_in_approved_at"] is None

    text = page(client, f"/login/microsoft/callback?admin_consent=True&tenant={ALPHA}&state={state}")
    assert "Thank you" in text and "Alpha Ltd" not in text and ALPHA not in text
    assert store.get_tenant(db, alpha)["sign_in_approved_at"] is None
    actions = [r["action"] for r in db.execute("SELECT action FROM audit_log")]
    assert "client_sign_in_approved" in actions and "client_sign_in_not_approved" in actions


def test_an_approval_link_for_a_deleted_client_does_nothing_for_a_new_one(client, db, world, connection_id):
    alpha, _ = world
    login(client, "owner")
    state = parse_qs(urlsplit(_approval_link(client, alpha)).query)["state"][0]
    db.execute("UPDATE tenants SET created_at = '2030-01-01T00:00:00Z' WHERE id = ?", (alpha,))  # "another" client
    db.commit()
    client.post("/logout")
    resp = client.get(f"/login/microsoft/callback?admin_consent=True&tenant={ALPHA}&state={state}")
    assert resp.status_code == 302 and store.get_tenant(db, alpha)["sign_in_approved_at"] is None


def test_the_approval_link_is_still_offered_once_approval_is_noted(client, db, world):
    """Office Sentry can't see the client remove the sign-in app from their tenant, after which their people
    are asked for approval again, so the page keeps the link to send again, beside when approval was noted."""
    alpha, _ = world
    login(client, "owner")
    before = _approval_link(client, alpha)
    db.execute("UPDATE tenants SET sign_in_approved_at = '2026-09-01T09:00:00Z' WHERE id = ?", (alpha,))
    db.commit()
    text = page(client, f"/tenants/{alpha}/sign-in")
    approval = text.split('id="approval"', 1)[1].split("</section>", 1)[0]
    assert "Approval noted" in approval and "2026-09-01T09:00:00Z" in approval
    assert "removes the sign-in app" in approval and "to approve it once more" in approval
    assert ">Approved<" not in text
    assert _approval_link(client, alpha) == before


# -- The client's Client sign-in page ----------------------------------------------------------------------


@responses.activate
def test_staff_add_a_clients_people_and_they_sign_in(client, db, world):
    alpha, bravo = world
    db.execute(
        "INSERT INTO contacts (tenant_id, name, email, created_at) VALUES (?, 'Jo', ?, '2026-01-01T00:00:00Z')",
        (alpha, "jo@alpha.example"),
    )
    db.commit()
    login(client, "owner")
    text = page(client, f"/tenants/{alpha}/sign-in")
    assert "Who can sign in" in text and "Add someone" in text and "jo@alpha.example" in text

    resp = client.post(f"/tenants/{alpha}/sign-in/people", data={"email": "Jo@Alpha.example"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("#message")
    jo = user(db, "jo@alpha.example")
    assert jo["role"] == "client" and jo["password_hash"] == sso.NO_PASSWORD and jo["all_tenants"] == 0
    # Connected now to Jo's object ID in Alpha's own tenant, from Alpha's collected directory.
    assert (jo["entra_oid"], jo["entra_tid"], jo["entra_upn"]) == (JO["oid"], ALPHA, JO["upn"])
    assert office_roles.role_of(db, jo).key == "client_viewer"
    grants = [r[0] for r in db.execute("SELECT tenant_id FROM user_tenants WHERE user_id = ?", (jo["id"],))]
    assert grants == [alpha]
    audit = db.execute("SELECT * FROM audit_log WHERE action = 'user_created'").fetchone()
    assert audit["tenant_id"] == alpha and "signs in with Microsoft" in audit["detail"]
    text = page(client, f"/tenants/{alpha}/sign-in")
    assert "not signed in yet" in text and "Copy message" in text and "Sign-in link" in text

    # Kim is found by her email address; her account is named as she signs in.
    client.post(f"/tenants/{alpha}/sign-in/people", data={"email": "kim@alpha.example"})
    assert user(db, KIM["upn"])["entra_oid"] == KIM["oid"]

    for email, expected in (
        ("jo@alpha.example", "can already sign in"),
        ("bo@bravo.example", "isn't a sign-in address in Alpha Ltd's directory"),  # Bravo's, not Alpha's
        ("nobody@alpha.example", "isn't a sign-in address"),
        ("lee@partner.example", "is a guest"),
        ("owner", "Enter the address"),
        ("not an address", "Enter the address"),
        ("x" * 60 + "@alpha.example", "Enter the address"),
    ):
        client.post(f"/tenants/{alpha}/sign-in/people", data={"email": email})
        assert expected in page(client, f"/tenants/{alpha}/sign-in"), email
    assert user(db, "bo@bravo.example") is None and user(db, "lee@partner.example") is None
    taken = {"oid": "22222222-aaaa-bbbb-cccc-000000000006", "upn": "taken@bravo.example"}
    directory(db, bravo, BO, taken)
    make_user(db, "taken@bravo.example", role="analyst")
    db.commit()
    client.post(f"/tenants/{bravo}/sign-in/people", data={"email": "taken@bravo.example"})
    assert "already exists" in page(client, f"/tenants/{bravo}/sign-in")
    assert user(db, "taken@bravo.example")["role"] == "analyst"
    assert user(db, "taken@bravo.example")["entra_oid"] is None
    client.post("/logout")

    assert sign_in(client, JO).status_code == 302 and signed_in_as(client) == "jo@alpha.example"


@responses.activate
def test_a_clients_password_account_is_connected_from_the_page(client, db, world):
    alpha, bravo = world
    pat = client_account(db, "pat@alpha.example", [alpha], password=True)
    sign_in(client, PAT)
    assert signed_in_as(client) is None  # not connected yet, whatever its name
    login(client, "owner")
    client.post(f"/tenants/{alpha}/sign-in/people", data={"email": "pat@alpha.example"})
    assert "as well as their password" in page(client, f"/tenants/{alpha}/sign-in")
    row = user(db, "pat@alpha.example")
    assert (row["entra_oid"], row["entra_tid"]) == (PAT["oid"], ALPHA) and sso.has_password(row)
    assert db.execute("SELECT detail FROM audit_log WHERE action = 'microsoft_connected'").fetchone()[0] == (
        "pat@alpha.example: pat@alpha.example"
    )
    client.post("/logout")
    assert sign_in(client, PAT).status_code == 302 and signed_in_as(client) == "pat@alpha.example"
    client.post("/logout")

    # One that also sees another company's client isn't connected: it would lose that client.
    db.execute("UPDATE users SET entra_oid = NULL, entra_tid = NULL WHERE id = ?", (pat,))
    db.execute("INSERT INTO user_tenants (user_id, tenant_id) VALUES (?, ?)", (pat, bravo))
    db.commit()
    login(client, "owner")
    client.post(f"/tenants/{alpha}/sign-in/people", data={"email": "pat@alpha.example"})
    assert "already exists" in page(client, f"/tenants/{alpha}/sign-in")
    assert user(db, "pat@alpha.example")["entra_oid"] is None


def test_people_are_found_in_the_clients_latest_directory(db, world):
    alpha, bravo = world
    tenant = store.get_tenant(db, alpha)
    assert sso.directory_person(db, tenant, "PAT@alpha.example")["oid"] == PAT["oid"]
    assert sso.directory_person(db, tenant, "kim@alpha.example")["upn"] == KIM["upn"]
    # Two people with that email address: nobody is guessed.
    twin = (PAT["oid"][:-1] + "9", {"userPrincipalName": "x@alpha.example", "mail": "kim@alpha.example"})
    directory(db, alpha, JO, extra=[(KIM["oid"], {"userPrincipalName": KIM["upn"], "mail": "kim@alpha.example"}), twin])
    with pytest.raises(sso.Refused, match="isn't a sign-in address"):
        sso.directory_person(db, tenant, "kim@alpha.example")
    # Only the latest collection counts: Pat has left.
    with pytest.raises(sso.Refused, match="isn't a sign-in address"):
        sso.directory_person(db, tenant, "pat@alpha.example")
    db.execute("UPDATE runs SET status = 'failed' WHERE tenant_id = ? AND collector = 'users'", (bravo,))
    with pytest.raises(sso.Refused, match="hasn't collected"):
        sso.directory_person(db, store.get_tenant(db, bravo), "bo@bravo.example")


def test_nobody_is_added_while_client_sign_in_is_off(client, db, world):
    alpha, _ = world
    sso.save(db, {"sso_clients": ""})
    db.commit()
    login(client, "owner")
    text = page(client, f"/tenants/{alpha}/sign-in")
    assert "Client sign-in with Microsoft is off" in text and "Add someone" not in text
    client.post(f"/tenants/{alpha}/sign-in/people", data={"email": "jo@alpha.example"})
    assert user(db, "jo@alpha.example") is None


def test_the_msps_own_tenant_isnt_a_client_for_sign_in(client, db, world):
    alpha, _ = world
    db.execute("UPDATE tenants SET tenant_ref = ? WHERE id = ?", (MSP, alpha))
    db.commit()
    login(client, "owner")
    text = page(client, f"/tenants/{alpha}/sign-in")
    assert "is your own tenant" in text and "Copy approval link" not in text
    client.post(f"/tenants/{alpha}/sign-in/people", data={"email": "jo@alpha.example"})
    assert user(db, "jo@alpha.example") is None


def test_only_people_who_manage_users_open_the_client_sign_in_page(client, db, world):
    alpha, _ = world
    analyst = make_user(db, "ana", role="analyst", tenants=[alpha])
    db.execute("UPDATE users SET role_id = ? WHERE id = ?", (office_roles.by_key(db, "second_line").id, analyst))
    db.commit()
    login(client, "ana")
    assert client.get(f"/tenants/{alpha}/sign-in").status_code == 403
    assert client.post(f"/tenants/{alpha}/sign-in/people", data={"email": "jo@alpha.example"}).status_code == 403
    assert user(db, "jo@alpha.example") is None


# -- Settings → Microsoft sign-in --------------------------------------------------------------------------

DEVICE_CODE = "DAQABAAEAAAD-secret-device-code"
SETUP_TOKEN = jwt.encode({"tid": MSP, "oid": JACK["oid"]}, "not-checked-here", algorithm="HS256")


def _device_sign_in(token: str = SETUP_TOKEN):
    responses.post(
        f"{LOGIN}/{MSP}/oauth2/v2.0/devicecode",
        json={
            "device_code": DEVICE_CODE,
            "user_code": "HXQ7-PL2M",
            "verification_uri": "https://microsoft.com/devicelogin",
            "expires_in": 900,
            "interval": 5,
        },
    )
    return responses.post(f"{LOGIN}/{MSP}/oauth2/v2.0/token", json={"access_token": token})


@responses.activate
def test_the_wizard_lets_the_sign_in_app_accept_clients_accounts(client, db, world):
    sso.save(db, {"sso_multi_tenant": "", "sso_clients": ""})
    db.commit()
    login(client, "owner")
    text = page(client, "/admin/sign-in")
    assert "Your clients' people" in text and "Let the sign-in app accept" in text
    _device_sign_in()
    resp = client.post("/admin/sign-in/clients/setup")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("#clients")
    asked = parse_qs(responses.calls[0].request.body)["scope"][0].split()
    assert asked == ["https://graph.microsoft.com/Application.ReadWrite.All"]  # that, and nothing else
    text = page(client, "/admin/sign-in")
    assert "HXQ7-PL2M" in text and DEVICE_CODE not in text

    responses.get(
        f"{GRAPH}/applications",
        json={"value": [{"id": "app-object-1", "signInAudience": "AzureADMyOrg"}]},
        match=[matchers.query_param_matcher({"$filter": f"appId eq '{CLIENT_ID}'", "$select": "id,signInAudience"})],
    )
    patched = responses.patch(f"{GRAPH}/applications/app-object-1", status=204)
    db.execute("UPDATE setup_sign_ins SET next_poll_at = NULL")
    db.commit()
    assert client.post("/admin/sign-in/clients/setup/check", headers=JSON).get_json()["status"] == "done"
    assert json.loads(patched.calls[0].request.body) == {"signInAudience": "AzureADMultipleOrgs"}
    s = sso.settings(db)
    assert sso.multi_tenant(s) and not sso.clients_enabled(s) and s["sso_multi_tenant_by"] == "owner (wizard)"
    stored = "\n".join(str(v) for r in db.execute("SELECT * FROM setup_sign_ins") for v in r)
    assert SETUP_TOKEN not in stored and DEVICE_CODE not in stored
    assert "sign_in_app_updated" in [r["action"] for r in db.execute("SELECT action FROM audit_log")]

    client.post("/admin/sign-in/clients", data={"clients": "1"})
    assert sso.clients_enabled(sso.settings(db))
    assert "Client sign-in" in page(client, "/admin/sign-in")


@responses.activate
def test_the_wizard_refuses_a_sign_in_to_another_tenant(client, db, world):
    sso.save(db, {"sso_multi_tenant": "", "sso_clients": ""})
    db.commit()
    login(client, "owner")
    _device_sign_in(jwt.encode({"tid": ALPHA}, "x", algorithm="HS256"))
    client.post("/admin/sign-in/clients/setup")
    db.execute("UPDATE setup_sign_ins SET next_poll_at = NULL")
    db.commit()
    assert client.post("/admin/sign-in/clients/setup/check", headers=JSON).get_json()["status"] == "failed"
    assert "different tenant" in page(client, "/admin/sign-in")
    assert not sso.multi_tenant(sso.settings(db))
    assert not [c for c in responses.calls if urlsplit(c.request.url).hostname == "graph.microsoft.com"]


def test_client_sign_in_is_turned_on_after_the_app_and_off_again(client, db, world):
    sso.save(db, {"sso_multi_tenant": "", "sso_clients": ""})
    db.commit()
    login(client, "owner")
    client.post("/admin/sign-in/clients", data={"clients": "1"})
    assert not sso.clients_enabled(sso.settings(db))
    assert "First let the sign-in app accept" in page(client, "/admin/sign-in")
    client.post("/admin/sign-in/clients/by-hand")
    assert sso.multi_tenant(sso.settings(db)) and sso.settings(db)["sso_multi_tenant_by"] == "owner (by hand)"
    client.post("/admin/sign-in/clients", data={"clients": "1"})
    assert sso.clients_enabled(sso.settings(db))
    client.post("/admin/sign-in/clients", data={"clients": ""})
    assert not sso.clients_enabled(sso.settings(db))
    details = [r["detail"] for r in db.execute("SELECT detail FROM audit_log WHERE action = 'staff_sign_in_changed'")]
    assert details == ["clients=on", "clients=off"]


# -- Staff with no password: confirming a change with Microsoft ---------------------------------------------


@responses.activate
def test_an_admin_without_a_password_confirms_with_microsoft(client, db, world):
    amy = make_user(db, "amy", role="analyst")
    db.commit()
    login(client, "amy")  # Amy is signed in somewhere
    other = client.application.test_client()
    assert sign_in(other, JACK, roles=["Admin"]).status_code == 302
    assert signed_in_as(other) == "jack@msp.example"

    text = page(other, f"/admin/users/{amy}")
    assert "Confirm with Microsoft" in text and 'name="admin_password"' not in text
    other.post(f"/admin/users/{amy}/sign-out")
    assert "Confirm it's you with Microsoft first" in page(other, f"/admin/users/{amy}")
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (amy,)).fetchone()[0] == 1

    def confirm(person):
        resp = other.post(f"/account/microsoft/confirm?next=/admin/users/{amy}")
        assert resp.status_code == 302
        url = urlsplit(resp.headers["Location"])
        sent = {k: v[0] for k, v in parse_qs(url.query).items()}
        assert url.path == f"/{MSP}/oauth2/v2.0/authorize"
        assert sent["prompt"] == "login" and sent["login_hint"] == "jack@msp.example"
        return come_back(other, sent, id_token(person, sent["nonce"], roles=["Admin"]), MSP)[0]

    # Someone else's Microsoft account confirms nothing.
    resp = confirm(AMY)
    assert resp.headers["Location"] == f"/admin/users/{amy}"
    assert "not the account you sign in with here" in page(other, f"/admin/users/{amy}")
    other.post(f"/admin/users/{amy}/sign-out")
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (amy,)).fetchone()[0] == 1

    resp = confirm(JACK)
    assert resp.headers["Location"] == f"/admin/users/{amy}"
    text = page(other, f"/admin/users/{amy}")
    assert "Confirmed" in text and "Confirm with Microsoft" not in text
    other.post(f"/admin/users/{amy}/sign-out")
    assert db.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (amy,)).fetchone()[0] == 0
    actions = [r["action"] for r in db.execute("SELECT action FROM audit_log")]
    assert "confirm_failed" in actions and "confirmed_with_microsoft" in actions and "user_signed_out" in actions

    # Ten minutes later it no longer counts.
    db.execute("UPDATE sessions SET confirmed_at = '2020-01-01T00:00:00Z' WHERE method = 'microsoft'")
    db.commit()
    assert "Confirm with Microsoft" in page(other, f"/admin/users/{amy}")
    # Someone with a password confirms with it, never with Microsoft.
    login(client, "amy")
    assert client.post("/account/microsoft/confirm").status_code == 404


@responses.activate
def test_confirming_only_returns_to_this_site(client, db, world):
    assert sign_in(client, JACK, roles=["Admin"]).status_code == 302
    resp = client.post("/account/microsoft/confirm?next=https://evil.example/")
    sent = {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["Location"]).query).items()}
    resp, _ = come_back(client, sent, id_token(JACK, sent["nonce"], roles=["Admin"]), MSP)
    assert resp.status_code == 302 and resp.headers["Location"] == "/account"


# -- Users page ---------------------------------------------------------------------------------------------


def test_admins_add_someone_who_signs_in_with_microsoft(client, db, world):
    login(client, "owner")
    viewer = office_roles.by_key(db, "client_viewer").id
    analyst = office_roles.by_key(db, "second_line").id
    text = page(client, "/admin/users")
    assert "How they sign in" in text

    resp = client.post("/admin/users", data={"username": "sam@msp.example", "role": analyst, "signs_in": "microsoft"})
    assert resp.status_code == 302 and "/admin/users/" in resp.headers["Location"]
    sam = user(db, "sam@msp.example")
    assert sam["password_hash"] == sso.NO_PASSWORD and sam["must_change_password"] == 0 and sam["role"] == "analyst"
    assert "not signed in yet" in page(client, "/admin/users")
    client.post("/admin/users", data={"username": "al", "role": analyst, "signs_in": "microsoft"})
    assert user(db, "al") is None

    # A client's people are found in their own directory, on the client's page, not named here.
    client.post("/admin/users", data={"username": "jo@alpha.example", "role": viewer, "signs_in": "microsoft"})
    assert user(db, "jo@alpha.example") is None
    assert "on the client's Settings → Client sign-in page" in page(client, "/admin/users")
    # A password account still needs a good password.
    client.post("/admin/users", data={"username": "pat", "role": viewer, "password": "short"})
    assert user(db, "pat") is None
    client.post("/admin/users", data={"username": "pat", "role": viewer, "password": PASSWORD + " extra"})
    assert sso.has_password(user(db, "pat"))


@responses.activate
def test_a_client_signed_in_with_microsoft_keeps_a_client_role(client, db, world):
    alpha, _ = world
    jo = client_account(db, "jo@alpha.example", [alpha], person=JO)
    assert sign_in(client, JO).status_code == 302
    client.post("/logout")
    login(client, "owner")
    text = page(client, f"/admin/users/{jo}")
    assert "can only have a client role" in text and "(set in Entra)" not in text
    analyst = office_roles.by_key(db, "second_line").id
    client.post(f"/admin/users/{jo}", data={"role": analyst, "tenants": str(alpha)})
    assert user(db, "jo@alpha.example")["role"] == "client"
    assert "can only have a client role" in page(client, f"/admin/users/{jo}")


def test_two_step_advice_leaves_out_accounts_that_sign_in_with_microsoft(client, db, world):
    alpha, _ = world
    client_account(db, "jo@alpha.example", [alpha], person=JO)
    client_account(db, "pat@alpha.example", [alpha], password=True)
    login(client, "owner")
    assert "1 client account without two-step" in page(client, "/admin/users")
