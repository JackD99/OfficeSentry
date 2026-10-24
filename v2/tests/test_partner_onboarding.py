"""Adding clients from Partner Center: one device sign-in in the MSP's own tenant reads its GDAP relationships, the
admin ticks the customers to add, and they're added like a CSV import (Microsoft simulated at the HTTP level).
"""

from datetime import UTC, datetime
from urllib.parse import parse_qs

import jwt
import pytest
import responses

from officesentry import app_setup, onboarding, partner
from officesentry.entra import GRAPH_CLI_CLIENT_ID
from tests.conftest import login, make_user
from tests.test_limited_roles import _limited

LOGIN = "https://login.microsoftonline.com"
GRAPH = "https://graph.microsoft.com/v1.0"
RELATIONSHIPS = f"{GRAPH}/tenantRelationships/delegatedAdminRelationships"
HOME = "c0ffee00-1111-2222-3333-444455556666"  # the MSP's own (partner) tenant
DEVICE_CODE = "DAQABAAEAAAD-partner-device-code"
USER_CODE = "PRTN-R123"
TOKEN = jwt.encode(
    {"tid": HOME, "scp": "DelegatedAdminRelationship.Read.All"},
    "signature-isnt-checked-by-office-sentry",
    algorithm="HS256",
)
JSON = {"Accept": "application/json"}

CONTOSO = "11111111-aaaa-4aaa-8aaa-000000000001"
FABRIKAM = "11111111-aaaa-4aaa-8aaa-000000000002"
NORTHWIND = "11111111-aaaa-4aaa-8aaa-000000000003"
ADATUM = "11111111-aaaa-4aaa-8aaa-000000000004"
ALPHA = "11111111-aaaa-4aaa-8aaa-0000000000aa"  # Alpha Ltd, already added by its domain

PRA = partner.PRIVILEGED_ROLE_ADMINISTRATOR
GLOBAL_READER = "f2ef992c-3afb-46b9-b7cf-a126ee74c451"
USER_ADMIN = "fe930be7-5e62-47db-91af-98c3a49a38b1"


def _rel(tenant, name, status="active", roles=(PRA,), end="2027-09-30T00:00:00Z"):
    return {
        "id": f"rel-{tenant}-{status}",
        "displayName": f"{name} relationship",
        "customer": {"tenantId": tenant.upper(), "displayName": name},
        "accessDetails": {"unifiedRoles": [{"roleDefinitionId": r} for r in roles]},
        "status": status,
        "endDateTime": end,
    }


FIRST_PAGE = [
    _rel(CONTOSO, "Contoso Ltd"),
    # Fabrikam: an ended relationship that had the role, and an active one without it.
    _rel(FABRIKAM, "Fabrikam Inc", status="terminated", roles=(PRA,)),
    _rel(FABRIKAM, "Fabrikam Inc", roles=(GLOBAL_READER, USER_ADMIN)),
]
SECOND_PAGE = [
    _rel(NORTHWIND, "Northwind Traders", status="approvalPending", end=None),
    _rel(ADATUM, "Adatum  Corporation", roles=(partner.GLOBAL_ADMINISTRATOR,)),
    _rel(ALPHA, "Alpha Ltd"),
    {"customer": {"tenantId": "not-a-tenant"}, "status": "active"},  # nothing to add
]


def _device_code():
    return responses.post(
        f"{LOGIN}/organizations/oauth2/v2.0/devicecode",
        json={
            "device_code": DEVICE_CODE,
            "user_code": USER_CODE,
            "verification_uri": "https://microsoft.com/devicelogin",
            "expires_in": 900,
            "interval": 5,
        },
    )


def _signed_in():
    return responses.post(
        f"{LOGIN}/organizations/oauth2/v2.0/token",
        json={"token_type": "Bearer", "access_token": TOKEN, "expires_in": 3599},
    )


def _relationships(next_link=f"{RELATIONSHIPS}?$skiptoken=page2"):
    first = responses.get(
        RELATIONSHIPS,
        json={"value": FIRST_PAGE, "@odata.nextLink": next_link},
        match=[
            responses.matchers.query_param_matcher(
                {"$select": "id,customer,accessDetails,status,endDateTime", "$top": "300"}
            )
        ],
    )
    second = responses.get(
        RELATIONSHIPS,
        json={"value": SECOND_PAGE},
        match=[responses.matchers.query_param_matcher({"$skiptoken": "page2"})],
    )
    return first, second


def _metadata():
    """Microsoft's public sign-in metadata for the clients already added by a domain."""
    for domain, tid in (
        ("alpha.onmicrosoft.com", ALPHA),
        ("bravo.onmicrosoft.com", "22222222-0000-4000-8000-0000000000bb"),
    ):
        responses.get(
            f"{LOGIN}/{domain}/v2.0/.well-known/openid-configuration",
            json={"issuer": f"{LOGIN}/{tid}/v2.0", "token_endpoint": f"{LOGIN}/{tid}/oauth2/v2.0/token"},
        )


@pytest.fixture
def admin(client, db):
    make_user(db, "admin")
    login(client, "admin")
    return client


def _sign_in(client, db):
    """Start the sign-in, then have Microsoft say it's done at the next check."""
    start = _device_code()
    assert client.post("/admin/onboarding/partner/start", data={}).status_code == 302
    _signed_in()
    db.execute("UPDATE setup_sign_ins SET next_poll_at = NULL")
    return start, client.post("/admin/onboarding/partner/check", headers=JSON).get_json()


def _everything_stored(db) -> str:
    out = []
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        for row in db.execute(f'SELECT * FROM "{table}"').fetchall():
            out.extend(str(v) for v in row)
    return "\n".join(out)


@responses.activate
def test_one_sign_in_lists_every_gdap_customer_and_adds_the_ticked_ones(admin, db, two_tenants, connection_id):
    page = admin.get("/admin/onboarding").get_data(as_text=True)
    assert "Add from Partner Center" in page

    _relationships()
    _metadata()
    start, done = _sign_in(admin, db)
    # One delegated, read-only permission, in the MSP's own tenant; no refresh token (offline_access).
    assert parse_qs(start.calls[0].request.body) == {
        "client_id": [GRAPH_CLI_CLIENT_ID],
        "scope": ["https://graph.microsoft.com/DelegatedAdminRelationship.Read.All"],
    }
    assert done["status"] == "done"
    graph = [c.request for c in responses.calls if c.request.url.startswith(GRAPH)]
    assert [r.method for r in graph] == ["GET", "GET"]  # read only, and both pages
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in graph)
    stored = _everything_stored(db)
    assert TOKEN not in stored and TOKEN.split(".")[1] not in stored and DEVICE_CODE not in stored

    page = admin.get("/admin/onboarding/partner").get_data(as_text=True)
    assert "6 GDAP customers" not in page and "5 GDAP customers, 4 not in Office Sentry yet" in page
    for name in ("Contoso Ltd", "Fabrikam Inc", "Northwind Traders", "Adatum Corporation"):
        assert name in page
    assert "1 customer already in Office Sentry" in page and "as <a" in page  # Alpha, found by its domain
    assert "You can approve 2 of 4 yourself" in page  # Contoso (PRA) and Adatum (Global Administrator)
    assert "Waiting for the customer to approve" in page  # Northwind
    assert f'value="{ALPHA}"' not in page  # already there: nothing to tick
    # Every new customer with an active relationship starts ticked; one that isn't active yet doesn't.
    for tenant, name in ((CONTOSO, "Contoso Ltd"), (FABRIKAM, "Fabrikam Inc"), (ADATUM, "Adatum Corporation")):
        assert f'value="{tenant}" aria-label="Add {name}" checked' in page
    assert f'value="{NORTHWIND}" aria-label="Add Northwind Traders">' in page

    resp = admin.post(
        "/admin/onboarding/partner/add",
        data={"customer": [CONTOSO, ADATUM, ALPHA, "99999999-0000-4000-8000-000000000000"]},
    )
    added = {r["tenant_ref"]: r for r in db.execute("SELECT * FROM tenants WHERE tenant_ref NOT LIKE '%.%'")}
    assert set(added) == {CONTOSO, ADATUM}  # Alpha was already there, the unknown one was never found
    # Next, approving them: straight to the Approve page with the two just added.
    ids = f"{added[CONTOSO]['id']},{added[ADATUM]['id']}"
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/admin/onboarding/approve?t={ids}")
    assert added[ADATUM]["name"] == "Adatum Corporation" and added[ADATUM]["cloud"] == "commercial"
    assert added[CONTOSO]["connection_id"] == connection_id and not added[CONTOSO]["consented_at"]
    assert onboarding.row_for(db, added[CONTOSO]["id"])["imported_by"] == "admin"
    details = [r["detail"] for r in db.execute("SELECT detail FROM audit_log WHERE action = 'tenant_created'")]
    assert sorted(details) == [f"{CONTOSO} (Partner Center)", f"{ADATUM} (Partner Center)"]
    assert db.execute("SELECT 1 FROM audit_log WHERE action = 'partner_customers_read'").fetchone()

    page = admin.get("/admin/onboarding/partner").get_data(as_text=True)
    assert "3 customers already in Office Sentry" in page  # the list stays for more, now marked

    admin.post("/admin/onboarding/partner/cancel")
    assert db.execute("SELECT COUNT(*) FROM setup_sign_ins").fetchone()[0] == 0


@responses.activate
def test_groups_go_on_every_added_client(admin, db, connection_id):
    from officesentry import tenant_groups

    gid = tenant_groups.create(db, {"name": "Tier 1", "category": "", "description": ""}, "setup")
    _relationships()
    _sign_in(admin, db)
    admin.post("/admin/onboarding/partner/add", data={"customer": [CONTOSO, FABRIKAM], "groups": [str(gid)]})
    ids = [r["id"] for r in db.execute("SELECT id FROM tenants")]
    assert len(ids) == 2
    assert {r["tenant_id"] for r in db.execute("SELECT tenant_id FROM tenant_group_members")} == set(ids)


@responses.activate
def test_the_approve_page_only_offers_customers_your_gdap_roles_can_approve(admin, db, connection_id):
    _relationships()
    _sign_in(admin, db)
    resp = admin.post("/admin/onboarding/partner/add", data={"customer": [CONTOSO, FABRIKAM]})
    page = admin.get(resp.headers["Location"]).get_data(as_text=True)
    assert "Next: Contoso Ltd" in page and "You can, through GDAP." in page
    assert f"login.microsoftonline.com/{CONTOSO}/" in page  # Contoso: Privileged Role Administrator
    assert f"login.microsoftonline.com/{FABRIKAM}/" not in page  # Fabrikam: Global Reader and User Administrator
    assert "don't include Privileged Role Administrator" in page and "Send link" in page


@responses.activate
def test_an_account_that_cant_read_relationships_is_told_why(admin, db):
    responses.get(
        RELATIONSHIPS,
        status=403,
        json={"error": {"code": "Authorization_RequestDenied", "message": "Insufficient privileges."}},
    )
    _, done = _sign_in(admin, db)
    assert done["status"] == "failed"
    page = admin.get("/admin/onboarding/partner").get_data(as_text=True)
    assert "Your customers weren't read." in page and "such as an Admin agent" in page
    assert db.execute("SELECT device_code_enc FROM setup_sign_ins").fetchone()[0] is None
    assert db.execute("SELECT 1 FROM audit_log WHERE action = 'partner_sign_in_failed'").fetchone()


@responses.activate
def test_the_token_only_goes_to_graph(admin, db):
    """A next page somewhere other than Graph isn't asked for, so the token never leaves Microsoft Graph."""
    _relationships(next_link="https://attacker.example/steal?$skiptoken=page2")
    _, done = _sign_in(admin, db)
    assert done["status"] == "done"
    assert not [c for c in responses.calls if "attacker.example" in c.request.url]
    customers, _ = partner.load_found(db.execute("SELECT found FROM setup_sign_ins").fetchone()[0])
    assert [c.name for c in customers] == ["Contoso Ltd", "Fabrikam Inc"]


def test_customers_fold_their_relationships():
    customers = {c.tenant_id: c for c in partner.fold(FIRST_PAGE + SECOND_PAGE)}
    assert set(customers) == {CONTOSO, FABRIKAM, NORTHWIND, ADATUM, ALPHA}
    assert customers[CONTOSO].can_approve and customers[CONTOSO].usable
    # The ended relationship's role counts for nothing; the active one decides.
    assert customers[FABRIKAM].usable and not customers[FABRIKAM].can_approve
    assert customers[FABRIKAM].relationships == 2
    assert not customers[NORTHWIND].usable and not customers[NORTHWIND].can_approve
    assert customers[NORTHWIND].status_words == "Waiting for the customer to approve"
    assert customers[ADATUM].can_approve and customers[ADATUM].name == "Adatum Corporation"
    assert customers[CONTOSO].ends == "2027-09-30T00:00:00Z"
    again, _ = partner.load_found(partner.store_found(list(customers.values()), {}))
    assert {c.tenant_id: c for c in again} == customers


@responses.activate
def test_someone_limited_to_some_clients_isnt_told_the_names_of_others(client, db, two_tenants):
    """A client already added that the person doesn't see is said to be there, never named."""
    from officesentry import permissions as p

    alpha, _ = two_tenants
    _limited(db, "rm", [*p.SEE, "clients.manage"], tenants=[alpha])
    login(client, "rm")
    db.execute("UPDATE tenants SET tenant_ref = ? WHERE name = 'Bravo Inc'", (CONTOSO,))
    _relationships()
    _metadata()
    _sign_in(client, db)
    page = client.get("/admin/onboarding/partner").get_data(as_text=True)
    assert "Bravo Inc" not in page and "added by someone else" in page
    assert "as <a" in page  # Alpha, which they see, is named


def test_partner_centers_customer_export_imports_as_it_is(db):
    text = (
        "Microsoft ID,Company name,Primary domain name,Relationship\r\n"
        f"{CONTOSO},Contoso Ltd,contoso.onmicrosoft.com,Reseller\r\n"
    )
    parsed = onboarding.parse(db, text)
    assert parsed.error is None
    assert [(r.name, r.tenant_ref) for r in parsed.new] == [("Contoso Ltd", CONTOSO)]


def test_the_list_goes_with_the_sign_in_after_a_day(db):
    uid = make_user(db, "admin")
    db.execute(
        "INSERT INTO setup_sign_ins (user_id, cloud, tenant, name, user_code, verification_uri, poll_seconds,"
        " expires_at, status, created_at, updated_at, purpose, found)"
        " VALUES (?, 'commercial', 'organizations', 'Partner Center', 'X', 'u', 5, ?, 'done', ?, ?, 'partner', ?)",
        (uid, "2026-01-01T00:15:00Z", "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z", '{"customers": []}'),
    )
    app_setup.prune(db, now=datetime(2026, 1, 2, 0, 1, tzinfo=UTC))
    assert db.execute("SELECT COUNT(*) FROM setup_sign_ins").fetchone()[0] == 0
