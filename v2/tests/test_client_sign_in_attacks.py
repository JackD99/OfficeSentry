"""Breaking tenant isolation through client sign-in with Microsoft itself.

Each test is one attempt by someone at a client (or a client's tenant administrator, who can put any name,
address or app role in a token their tenant issues) to reach another client, a staff account or a new
account through the sign-in, and by staff limited to some clients to connect someone outside them. Alpha
(added by its Directory ID) and Bravo (added by domain, its ID read by its last collection) are two
organisations; Microsoft is simulated at the HTTP level with ``responses``, as in ``test_client_sign_in``.
"""

from __future__ import annotations

import json

import pytest
import responses

from officesentry import permissions, roles, sso, store
from officesentry.web import sso as sso_web
from tests.conftest import PASSWORD, login, make_user
from tests.test_client_sign_in import (
    ALPHA,
    BO,
    BRAVO,
    CLIENT_ID,
    JACK,
    JO,
    KIM,
    MSP,
    PAT,
    client_account,
    come_back,
    directory,
    id_token,
    page,
    sign_in,
    signed_in_as,
    start,
    user,
)

NOBODYS = "c3c3c3c3-1111-2222-3333-444455556666"  # a tenant that is no client's
# Someone in Alpha's directory with no account here.
STRANGER = {"oid": "22222222-aaaa-bbbb-cccc-000000000009", "upn": "boss@alpha.example", "name": "Boss", "tid": ALPHA}
# Everything of Bravo's a page could carry.
BRAVO_WORDS = ("Bravo Inc", BRAVO, "bravo.onmicrosoft.com", "bo@bravo.example")


@pytest.fixture
def orgs(db, vault, two_tenants):
    """As test_client_sign_in's world: Microsoft sign-in on for staff and clients, an admin (owner), Alpha
    added by its Directory ID and Bravo by domain (its ID read by its last collection), each with Jo and Pat,
    or Bo, in its collected directory."""
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
    directory(db, alpha, JO, PAT, KIM)
    directory(db, bravo, BO)
    return alpha, bravo


def _accounts(db) -> list[tuple]:
    """Every account as stored, minus what a refused or successful sign-in may note about itself."""
    rows = db.execute("SELECT * FROM users ORDER BY id").fetchall()
    return [tuple(v for k, v in dict(r).items() if k not in ("last_login_at", "entra_upn")) for r in rows]


def _grants(db) -> list[tuple]:
    return [tuple(r) for r in db.execute("SELECT user_id, tenant_id FROM user_tenants ORDER BY 1, 2")]


def _refused(client, db, before) -> None:
    assert signed_in_as(client) is None
    assert _accounts(db) == before  # nobody made, connected, renamed or given a role
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def _sees_only_alpha(client, alpha: int, bravo: int) -> None:
    home = page(client, "/")
    assert "Alpha Ltd" in home and not [w for w in BRAVO_WORDS if w in home]
    for url in (f"/tenants/{bravo}", f"/tenants/{bravo}/findings", f"/tenants/{bravo}/report-history"):
        assert client.get(url).status_code == 404, url


# -- Tokens naming someone else --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    ["alpha's tenant, bravo's person", "alpha's tenant, bravo's person granted both", "bravo's tenant, jo"],
)
@responses.activate
def test_a_token_from_one_client_never_reaches_the_others_person(client, db, orgs, case):
    alpha, bravo = orgs
    if case == "bravo's tenant, jo":
        client_account(db, "jo@alpha.example", [alpha, bravo], person=JO)
        token = {**JO, "tid": BRAVO}  # Jo's object ID, issued (and signed) by Bravo's tenant
    else:
        grants = [bravo] if case.endswith("person") else [alpha, bravo]
        client_account(db, "bo@bravo.example", grants, person=BO)
        token = {**BO, "tid": ALPHA}  # Bo's object ID, issued by Alpha's tenant
    before = _accounts(db)
    sign_in(client, token)
    _refused(client, db, before)


@responses.activate
def test_an_account_connected_at_alpha_but_granted_only_bravo_gets_nothing_of_bravo(client, db, orgs):
    alpha, bravo = orgs
    client_account(db, "jo@alpha.example", [bravo], password=True, person=JO)
    before = _accounts(db)
    sign_in(client, JO)
    _refused(client, db, before)
    # Its password still signs it in, but Bravo is another organisation's: it sees nothing at all.
    login(client, "jo@alpha.example")
    assert signed_in_as(client) == "jo@alpha.example"
    home = page(client, "/")
    assert not [w for w in BRAVO_WORDS if w in home]
    for url in (f"/tenants/{bravo}", f"/tenants/{bravo}/findings", f"/tenants/{bravo}/report-history"):
        assert client.get(url).status_code == 404, url


@responses.activate
def test_a_clients_tenant_never_takes_the_staff_path(client, db, orgs):
    alpha, _ = orgs
    client_account(db, "jo@alpha.example", [alpha], person=JO)
    make_user(db, "boss@alpha.example", role="analyst", all_tenants=True)  # staff, named like a client address
    db.commit()
    before = _accounts(db)
    # "Sign in as staff" goes to the MSP's tenant: a token from Alpha's, however its issuer and app roles read.
    for overrides in ({}, {"iss": f"https://login.microsoftonline.com/{MSP}/v2.0"}):
        for person in (JO, STRANGER):
            authority, sent = start(client, "?as=staff")
            assert authority == MSP
            come_back(client, sent, id_token(person, sent["nonce"], roles=["Admin"], **overrides), MSP)
            _refused(client, db, before)
    # Through organizations, Alpha's tenant only ever reaches the client path, whatever the token says.
    sign_in(client, STRANGER, roles=["Admin"])
    _refused(client, db, before)
    assert user(db, "boss@alpha.example")["entra_oid"] is None


@pytest.mark.parametrize(
    "name", ["owner", "jack@msp.example", "boss@alpha.example", "pat@alpha.example", "bo@bravo.example"]
)
@responses.activate
def test_names_and_addresses_in_a_clients_token_reach_nobody(client, db, orgs, name):
    """preferred_username and email name a staff account, a staff address at the client, another client
    account of Alpha's, or Bravo's person: only the object ID and tenant count."""
    alpha, bravo = orgs
    make_user(db, "jack@msp.example", role="admin")
    make_user(db, "boss@alpha.example", role="analyst", all_tenants=True)
    client_account(db, "pat@alpha.example", [alpha], password=True)  # not connected yet
    client_account(db, "bo@bravo.example", [bravo], person=BO)
    client_account(db, "jo@alpha.example", [alpha, bravo], person=JO)
    before = _accounts(db)
    # Someone in Alpha's directory with no account: nobody.
    sign_in(client, STRANGER, roles=["Admin"], preferred_username=name, email=name)
    _refused(client, db, before)
    # Jo, renamed by Alpha's administrator: still Jo, a client of Alpha's only.
    assert sign_in(client, JO, roles=["Admin"], preferred_username=name, email=name).status_code == 302
    assert signed_in_as(client) == "jo@alpha.example"
    assert _accounts(db) == before
    assert client.get("/admin/users").status_code == 403
    _sees_only_alpha(client, alpha, bravo)


@pytest.mark.parametrize("where", ["no client's tenant", "archived", "paused"])
@responses.activate
def test_a_tenant_that_isnt_a_live_client(client, db, orgs, where):
    alpha, bravo = orgs
    if where == "no client's tenant":
        # Even an account connected to that tenant (by SQL), granted Alpha, finds no client of that tenant.
        client_account(db, "jo@alpha.example", [alpha, bravo], person={**JO, "tid": NOBODYS})
        token = {**JO, "tid": NOBODYS}
    else:
        client_account(db, "jo@alpha.example", [alpha, bravo], person=JO)
        token = JO
        column = "archived_at = '2026-01-01T00:00:00Z'" if where == "archived" else "enabled = 0"
        db.execute(f"UPDATE tenants SET {column} WHERE id = ?", (alpha,))
        db.commit()
    before = _accounts(db)
    sign_in(client, token)
    if where == "paused":
        # A paused client keeps its data and its people, as with a password; still only Alpha.
        assert signed_in_as(client) == "jo@alpha.example"
        _sees_only_alpha(client, alpha, bravo)
    else:
        _refused(client, db, before)


@responses.activate
def test_someone_in_the_clients_directory_without_an_account_gets_none(client, db, orgs):
    alpha, _ = orgs
    # Kim has a password account named as her email address; Pat has none. Both are in Alpha's directory.
    client_account(db, "kim@alpha.example", [alpha], password=True)
    before, grants = _accounts(db), _grants(db)
    for person in (PAT, KIM, STRANGER):
        sign_in(client, person, roles=["Admin"])
        _refused(client, db, before)
    assert _grants(db) == grants


@responses.activate
def test_app_roles_in_a_clients_token_count_for_nothing(client, db, orgs):
    alpha, bravo = orgs
    boss = roles.create(db, "test", "Boss", "", permissions.STAFF, permissions.ALL_CLIENTS, permissions.KEYS)
    sso.save(db, {"sso_role_values": json.dumps({"Boss": boss}), "sso_default_role": "analyst"})
    client_account(db, "jo@alpha.example", [alpha, bravo], person=JO)
    db.commit()
    before = _accounts(db)
    assert sign_in(client, JO, roles=["Admin", "Boss", "TeamLeader"]).status_code == 302
    assert signed_in_as(client) == "jo@alpha.example" and _accounts(db) == before
    for url in ("/admin/users", "/admin/sign-in", f"/tenants/{alpha}/sign-in", "/across/checks", "/today"):
        assert client.get(url).status_code in (403, 404), url
    _sees_only_alpha(client, alpha, bravo)
    client.post("/logout")
    # Nor does the install's default role for staff with none make anyone from a client's tenant.
    sign_in(client, STRANGER, roles=["Boss"])
    _refused(client, db, before)


@pytest.mark.parametrize("change", ["disabled", "grant removed"])
@responses.activate
def test_a_copied_session_stops_at_the_next_request(app, client, db, orgs, change):
    alpha, bravo = orgs
    jo = client_account(db, "jo@alpha.example", [alpha, bravo], person=JO)
    assert sign_in(client, JO).status_code == 302
    copy = app.test_client()
    with client.session_transaction() as mine, copy.session_transaction() as theirs:
        theirs["sid"] = mine["sid"]
    assert copy.get(f"/tenants/{alpha}/findings").status_code == 200
    # Behind the users page's back (no sign-out), so only the request's own checks stand.
    if change == "disabled":
        db.execute("UPDATE users SET disabled = 1 WHERE id = ?", (jo,))
    else:
        db.execute("DELETE FROM user_tenants WHERE user_id = ? AND tenant_id = ?", (jo, alpha))
    db.commit()
    for browser in (copy, client):
        resp = browser.get(f"/tenants/{alpha}/findings")
        if change == "disabled":
            assert resp.status_code == 302 and "/login" in resp.headers["Location"]
            assert signed_in_as(browser) is None
        else:
            assert resp.status_code == 404
            home = page(browser, "/")
            assert "Alpha Ltd" not in home and not [w for w in BRAVO_WORDS if w in home]
            assert browser.get(f"/tenants/{bravo}").status_code == 404
    client.post("/logout")
    sign_in(client, JO)
    assert signed_in_as(client) is None


# -- Pages client sign-in added ---------------------------------------------------------------------------


def _approval_state(app, tenant) -> str:
    with app.test_request_context():
        return sso_web._approval_serializer().dumps({"t": tenant["id"], "c": tenant["created_at"]})


@responses.activate
def test_every_page_client_sign_in_added_shows_a_microsoft_client_nothing_of_another(app, client, db, orgs):
    """Every route of the sign-in blueprints, by GET and POST with Alpha's and Bravo's IDs, as Alpha's person
    signed in with Microsoft (granted Bravo too): the staff pages refuse, and nothing carries Bravo or changes."""
    alpha, bravo = orgs
    client_account(db, "jo@alpha.example", [alpha, bravo], person=JO)
    client_account(db, "bo@bravo.example", [bravo], password=True, person=BO)
    assert sign_in(client, JO).status_code == 302
    before, grants = _accounts(db), _grants(db)
    tenants = [tuple(r) for r in db.execute("SELECT id, sign_in_approved_at FROM tenants ORDER BY id")]
    bo = user(db, "bo@bravo.example")["id"]
    rules = [r for r in app.url_map.iter_rules() if r.endpoint.split(".")[0] in ("sso", "client_access")]
    assert len(rules) > 20
    opened = 0
    for rule in rules:
        if rule.endpoint in ("sso.callback", "sso.start"):
            continue  # signing in again; the approval return is below
        for tid in (alpha, bravo):
            url = rule.rule.replace("<int:tenant_id>", str(tid)).replace("<int:user_id>", str(bo))
            for method in sorted(rule.methods & {"GET", "POST"}):
                resp = client.open(url, method=method, data={"email": "bo@bravo.example", "clients": "1"})
                text = resp.get_data(as_text=True)
                assert not [w for w in BRAVO_WORDS if w in text], (method, url)
                if "/admin/" in url or "/sign-in" in url:
                    assert resp.status_code in (403, 404), (method, url, resp.status_code)
                opened += 1
    assert opened > 40
    assert signed_in_as(client) == "jo@alpha.example"
    assert _accounts(db) == before and _grants(db) == grants
    # The approval return is public: with Bravo's signed state it names nobody, and records an approval only
    # for the tenant the state names, only when Microsoft's answer is that very tenant.
    for state_of, answer in ((bravo, ALPHA), (alpha, BRAVO), (bravo, NOBODYS)):
        state = _approval_state(app, db.execute("SELECT * FROM tenants WHERE id = ?", (state_of,)).fetchone())
        resp = client.get(f"/login/microsoft/callback?admin_consent=True&tenant={answer}&state={state}")
        text = resp.get_data(as_text=True)
        assert resp.status_code == 200 and "another organisation" in text
        assert not [w for w in BRAVO_WORDS if w in text]
    assert [tuple(r) for r in db.execute("SELECT id, sign_in_approved_at FROM tenants ORDER BY id")] == tenants
    resp = client.get(f"/login/microsoft/callback?admin_consent=True&tenant={BRAVO}&state={state[:-2]}xx")
    assert resp.status_code == 302 and signed_in_as(client) == "jo@alpha.example"


# -- Staff limited to some clients ------------------------------------------------------------------------


def _manager_of_alpha(client, db, alpha: int) -> int:
    """Every capability, on Alpha only (the isolation walk's manager)."""
    role = roles.create(db, "test", "Manager of A", "", permissions.STAFF, permissions.GRANTED, permissions.KEYS)
    uid = make_user(db, "manager", role="analyst", tenants=[alpha])
    roles.assign(db, uid, roles.get(db, role))
    db.commit()
    login(client, "manager")
    return uid


def test_staff_limited_to_alpha_add_and_connect_nobody_outside_it(client, db, orgs):
    alpha, bravo = orgs
    # Bravo's person has an account here already; so does someone granted both organisations.
    bo = client_account(db, "bo@bravo.example", [bravo], password=True)
    both = client_account(db, "pat@alpha.example", [alpha, bravo], password=True)
    _manager_of_alpha(client, db, alpha)
    before, grants = _accounts(db), _grants(db)
    # Bravo's Client sign-in page and form: not there.
    assert client.get(f"/tenants/{bravo}/sign-in").status_code == 404
    assert client.post(f"/tenants/{bravo}/sign-in/people", data={"email": "bo@bravo.example"}).status_code == 404
    # On Alpha's: Bravo's person isn't in Alpha's directory, and Pat's account sees Bravo too.
    for email in ("bo@bravo.example", "pat@alpha.example"):
        client.post(f"/tenants/{alpha}/sign-in/people", data={"email": email})
        assert _accounts(db) == before and _grants(db) == grants, email
    assert client.get(f"/tenants/{alpha}/sign-in").status_code == 200
    text = page(client, f"/tenants/{alpha}/sign-in")
    assert "Bravo Inc" not in text and BRAVO not in text
    # Nor through the users page: Bravo's account isn't listed, and Pat's grant of Bravo isn't theirs to touch.
    assert client.get(f"/admin/users/{bo}").status_code == 404
    client.post(f"/admin/users/{both}", data={"tenants": [str(alpha)]})
    assert _grants(db) == grants


def test_staff_limited_to_alpha_cant_connect_an_object_id_from_bravos_directory(client, db, orgs):
    alpha, bravo = orgs
    # Alpha's last Users collection lists an object with Bo's object ID from Bravo's directory (as no real
    # directory would), and Bravo's account for Bo is connected to it in Bravo.
    directory(db, alpha, JO, {**BO, "upn": "bo.copy@alpha.example"})
    bo = client_account(db, "bo@bravo.example", [bravo], person=BO)
    _manager_of_alpha(client, db, alpha)
    before, grants = _accounts(db), _grants(db)
    client.post(f"/tenants/{alpha}/sign-in/people", data={"email": "bo.copy@alpha.example"})
    assert _accounts(db) == before and _grants(db) == grants
    assert (user(db, "bo@bravo.example")["entra_tid"], user(db, "bo@bravo.example")["id"]) == (BRAVO, bo)


def test_staff_of_the_msp_never_become_a_client_by_the_sign_in_page(client, db, orgs):
    """The MSP's own people (Jack) aren't added as a client's, and a staff account is never connected."""
    alpha, _ = orgs
    directory(db, alpha, JO, JACK)
    make_user(db, "jack@msp.example", role="analyst", tenants=[alpha])
    _manager_of_alpha(client, db, alpha)
    before = _accounts(db)
    client.post(f"/tenants/{alpha}/sign-in/people", data={"email": "jack@msp.example"})
    assert _accounts(db) == before


def test_staff_limited_to_alpha_cant_wake_a_grant_of_bravo_by_disconnecting_microsoft(client, db, orgs):
    """Alpha's person signs in with Microsoft and also holds a grant of Bravo, which counts for nothing while
    they're connected (``access._organisation``). Staff limited to Alpha must not be able to turn it on by
    setting the account a password, disconnecting it from Microsoft and signing in with it: the grant of
    Bravo puts the account out of their reach (``access.out_of_reach`` counts every grant), so nothing
    changes."""
    alpha, bravo = orgs
    jo = client_account(db, "jo@alpha.example", [alpha, bravo], person=JO)
    _manager_of_alpha(client, db, alpha)
    before, grants = _accounts(db), _grants(db)
    new = "Kettle-Drum-Orchard-42"
    client.post(f"/admin/users/{jo}", data={"tenants": [str(alpha)], "password": new, "admin_password": PASSWORD})
    client.post(f"/admin/users/{jo}/microsoft/disconnect")
    assert _accounts(db) == before and _grants(db) == grants  # still connected, no password set
    client.post("/logout")
    client.post("/login", data={"username": "jo@alpha.example", "password": new})
    assert signed_in_as(client) is None


@responses.activate
def test_a_client_cant_wake_a_grant_of_another_organisation_by_disconnecting_microsoft(client, db, orgs):
    """The same grant, held by Alpha's person who also has a password: disconnecting their own Microsoft
    account on the Account page mustn't make Bravo count either."""
    alpha, bravo = orgs
    client_account(db, "jo@alpha.example", [alpha, bravo], password=True, person=JO)
    assert sign_in(client, JO).status_code == 302
    assert client.get(f"/tenants/{bravo}/findings").status_code == 404
    client.post("/account/microsoft/disconnect", data={"password": PASSWORD})
    assert signed_in_as(client) == "jo@alpha.example"
    assert client.get(f"/tenants/{bravo}/findings").status_code == 404
    assert "Bravo Inc" not in page(client, "/")
