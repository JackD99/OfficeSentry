"""Onboarding many clients: CSV import, the consent tracker, links sent to clients and confirming consent by
signing in (Microsoft simulated at the HTTP level)."""

import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from officesentry import onboarding, store, tasks
from officesentry.entra import EXCHANGE_PERMISSIONS, GRAPH_PERMISSIONS
from officesentry.graph import GRAPH_ROOT
from officesentry.worker import execute_run
from tests.conftest import login, make_user
from tests.seed import exchange_factory, mock_tenant
from tests.test_certificates import LOGIN, mock_sign_in

GUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _csv(n: int, group: str = "Tier 1") -> str:
    lines = ["Client Name;Tenant ID or domain;Group;Contact"]
    for i in range(n):
        lines.append(f"Client {i:02d};client{i:02d}.onmicrosoft.com;{group};Person {i} <p{i}@client{i:02d}.com>")
    return "﻿" + "\r\n".join(lines) + "\r\n"


def _admin(client, db):
    make_user(db, "admin")
    login(client, "admin")


def _link(app, db, tenant_id):
    """The consent link the tracker shows for a tenant, and its state."""
    from officesentry.web.onboarding import client_link

    with app.test_request_context():
        t = store.get_tenant(db, tenant_id)
        url = client_link(t, store.get_connection(db, t["connection_id"]))
    return url, parse_qs(urlparse(url).query)["state"][0]


# -- Reading the CSV ---------------------------------------------------------------------------


def test_parse_reads_excel_style_files(db, two_tenants):
    text = (
        "﻿Name,Domain,Groups,Contact Email,Contact Name,Cloud\r\n"
        "Contoso,contoso.com,Tier 1; London,jane@contoso.com,Jane Smith,\r\n"
        "Gov Co,gov.onmicrosoft.us,,,,GCC High\r\n"
        "Alpha again,ALPHA.onmicrosoft.com,,,,\r\n"
    )
    parsed = onboarding.parse(db, text)
    assert parsed.error is None
    contoso, gov, alpha = parsed.rows
    assert contoso.ok and contoso.groups == ["Tier 1", "London"]
    assert (contoso.contact_name, contoso.contact_email) == ("Jane Smith", "jane@contoso.com")
    assert gov.ok and gov.cloud == "gcc_high"
    assert not alpha.problems and alpha.skip == "Already in Office Sentry as Alpha Ltd."
    assert parsed.new_groups == ["Tier 1", "London"]


def test_parse_explains_every_problem(db):
    text = (
        "name,tenant,contact,cloud\n"
        ",nope,x@nowhere,\n"
        "Dup,dup.onmicrosoft.com,Jo <jo@dup.com>,\n"
        "Dup again,DUP.onmicrosoft.com,,\n"
        "Mars,mars.onmicrosoft.com,,Mars\n"
    )
    rows = onboarding.parse(db, text).rows
    assert rows[0].problems == [
        "No name.",
        "“nope” isn't a tenant ID or a domain like contoso.onmicrosoft.com.",
        "“x@nowhere” isn't an email address.",
    ]
    assert rows[1].ok and rows[1].contact_name == "Jo"
    assert rows[2].problems == ["The same tenant as line 3."]
    assert "The cloud should be" in rows[3].problems[0]
    assert onboarding.parse(db, "client,where\nA,b.com\n").error.startswith("The first row needs column names")
    assert onboarding.parse(db, "name,tenant\n").error == "The file has column names but no tenants under them."
    assert "import up to 1,000" in onboarding.parse(db, "name,tenant\n" + "a,b.com\n" * 1001).error
    assert onboarding.split_contact("Smith, Jane <j@x.com>") == ("Smith, Jane", "j@x.com")


def test_decode_takes_utf8_and_excel_ansi():
    assert onboarding.decode("﻿Café".encode()) == "Café"
    assert onboarding.decode("Café".encode("cp1252")) == "Café"


# -- Importing 50 tenants through the page -------------------------------------------------------


def test_import_fifty_tenants_previews_then_adds(client, db, connection_id):
    _admin(client, db)
    text = _csv(50)
    preview = client.post("/admin/onboarding/import", data={"text": text}).get_data(as_text=True)
    assert "Import 50 tenants" in preview and "New group: <strong>Tier 1</strong>" in preview
    assert db.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 0  # nothing until confirmed

    resp = client.post("/admin/onboarding/import", data={"text": text, "confirm": "1"})
    assert resp.status_code == 302 and "/admin/onboarding" in resp.headers["Location"]
    tenants = db.execute("SELECT * FROM tenants ORDER BY name").fetchall()
    assert len(tenants) == 50 and tenants[0]["name"] == "Client 00"
    assert all(t["connection_id"] == connection_id and not t["consented_at"] for t in tenants)
    group = db.execute("SELECT id FROM tenant_groups WHERE name = 'Tier 1'").fetchone()["id"]
    assert db.execute("SELECT COUNT(*) FROM tenant_group_members WHERE group_id = ?", (group,)).fetchone()[0] == 50
    assert db.execute("SELECT COUNT(*) FROM contacts WHERE tenant_id IS NOT NULL").fetchone()[0] == 50
    row = onboarding.row_for(db, tenants[0]["id"])
    assert (row["contact_name"], row["contact_email"], row["imported_by"]) == ("Person 0", "p0@client00.com", "admin")
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'tenant_created'").fetchone()[0] == 50

    page = client.get("/admin/onboarding").get_data(as_text=True)
    assert "0 of 50 tenants fully connected." in page and page.count('data-copy-text="https://login') == 50
    again = client.post("/admin/onboarding/import", data={"text": text}).get_data(as_text=True)
    assert 'Will be added</div><div class="value">0<' in again and 'name="confirm"' not in again
    assert again.count("Already in Office Sentry as") == 50


def test_imported_tenants_are_only_seen_by_people_granted_them(client, app, db, connection_id):
    _admin(client, db)
    client.post("/admin/onboarding/import", data={"text": _csv(3, "Tier 1"), "confirm": "1"})
    client.post("/admin/onboarding/import", data={"text": _csv(2, "Tier 2").replace("client", "other"), "confirm": "1"})
    tier1 = db.execute("SELECT id FROM tenant_groups WHERE name = 'Tier 1'").fetchone()["id"]
    by_group = make_user(db, "lead", role="analyst")
    db.execute("INSERT INTO user_tenant_groups (user_id, group_id) VALUES (?, ?)", (by_group, tier1))
    make_user(db, "newbie", role="analyst")
    make_user(db, "someone", role="client")

    lead = app.test_client()
    login(lead, "lead")
    home = lead.get("/").get_data(as_text=True)
    assert "Client 00" in home and "Other 00" not in home.replace("Client", "")  # only Tier 1
    assert "other00" not in home
    assert lead.get("/admin/onboarding").status_code == 403  # consent links are for admins
    for name in ("newbie", "someone"):
        other = app.test_client()
        login(other, name)
        assert "client00" not in other.get("/").get_data(as_text=True)


def test_import_preview_names_analysts_who_will_see_the_tenants(client, db, connection_id):
    _admin(client, db)
    db.execute("INSERT INTO tenant_groups (name, created_at) VALUES ('Tier 1', '2026-10-01T00:00:00Z')")
    lead = make_user(db, "lead", role="analyst")
    db.execute("INSERT INTO user_tenant_groups (user_id, group_id) VALUES (?, 1)", (lead,))
    page = client.post("/admin/onboarding/import", data={"text": _csv(2)}).get_data(as_text=True)
    assert "Analysts given the group <strong>Tier 1</strong> will see its new tenants: lead." in page


def test_import_upload_and_template(client, db, connection_id):
    import io

    _admin(client, db)
    template = client.get("/admin/onboarding/template.csv")
    assert template.mimetype == "text/csv"
    ansi = _csv(2).lstrip("\ufeff").replace("Person 0", "Ren\u00e9e").encode("cp1252")  # Excel's "CSV" on Windows
    data = {"file": (io.BytesIO(ansi), "clients.csv")}
    page = client.post("/admin/onboarding/import", data=data, content_type="multipart/form-data")
    assert "Import 2 tenants" in page.get_data(as_text=True)
    page = client.post("/admin/onboarding/import", data={"text": "nothing useful"}).get_data(as_text=True)
    assert "Nothing was imported." in page


# -- The tracker and the link sent to clients ------------------------------------------------------


def test_states_from_stored_data(db, two_tenants):
    alpha, _ = two_tenants
    db.execute("UPDATE tenants SET consented_at = NULL")
    t = lambda: store.get_tenant(db, alpha)  # noqa: E731
    row = lambda: onboarding.row_for(db, alpha)  # noqa: E731
    assert onboarding.state(t(), None, None).key == "not_sent"
    onboarding.mark_sent(db, [alpha], "admin")
    assert onboarding.state(t(), row(), None).key == "sent"
    db.execute("UPDATE onboarding SET approved_at = '2026-10-09T10:00:00Z'")
    assert onboarding.state(t(), row(), None).detail.endswith("signing in to confirm.")
    db.execute("UPDATE onboarding SET checked_at = '2026-10-09T10:05:00Z', check_error = 'Not there yet.'")
    assert onboarding.state(t(), row(), None).detail.endswith("can't sign in yet. Not there yet.")
    db.execute("UPDATE tenants SET consented_at = '2026-10-09T10:10:00Z'")
    assert onboarding.state(t(), row(), None).key == "consented"
    assert (
        onboarding.state(t(), row(), {"roles_checked": True, "global_reader": False}).label == "Global Reader missing"
    )
    assert onboarding.state(t(), row(), {"roles_checked": True, "global_reader": True}).key == "connected"


def test_a_new_client_id_needs_every_tenant_to_approve_again(client, db, vault, two_tenants, connection_id):
    """Consent given to the old app says nothing about the new one, and links already sent name the old one."""
    from officesentry.certs import generate_certificate

    alpha, bravo = two_tenants
    other = store.create_connection(db, vault, "Other app", generate_certificate("o"), client_id=GUID)
    charlie = store.create_tenant(db, "Charlie", "charlie.onmicrosoft.com", other)
    db.execute("UPDATE tenants SET consented_at = '2026-01-01T00:00:00Z' WHERE id = ?", (charlie,))
    onboarding.mark_sent(db, [alpha, charlie], "admin")
    db.execute("UPDATE onboarding SET approved_at = '2026-10-09T10:00:00Z'")
    _admin(client, db)
    url = f"/admin/connections/{connection_id}/client-id"
    page = client.post(url, data={"client_id": "11111111-2222-3333-4444-555555555555"}, follow_redirects=True)
    assert "must approve" not in page.get_data(as_text=True)  # the same ID again changes nothing
    assert db.execute("SELECT COUNT(*) FROM tenants WHERE consented_at IS NULL").fetchone()[0] == 0

    new = "12345678-1234-1234-1234-123456789abc"
    page = client.post(url, data={"client_id": new}, follow_redirects=True).get_data(as_text=True)
    assert "Client ID saved." in page and "so 2 client tenants must approve the app again" in page
    for tenant_id in (alpha, bravo):
        t = store.get_tenant(db, tenant_id)
        assert t["consented_at"] is None
        assert onboarding.state(t, onboarding.row_for(db, tenant_id), None).key == "not_sent"
    # Another app's tenant keeps its consent and where its onboarding got to.
    assert store.get_tenant(db, charlie)["consented_at"]
    assert onboarding.row_for(db, charlie)["approved_at"]
    reset = db.execute("SELECT tenant_id, detail FROM audit_log WHERE action = 'tenant_consent_reset'").fetchall()
    assert sorted(r["tenant_id"] for r in reset) == [alpha, bravo]
    assert "11111111-2222-3333-4444-555555555555" in reset[0]["detail"]


def test_client_link_is_stable_and_never_records_consent_itself(app, client, db, two_tenants):
    alpha, _ = two_tenants
    db.execute("UPDATE tenants SET consented_at = NULL")
    url, state = _link(app, db, alpha)
    assert url.startswith(f"{LOGIN}/alpha.onmicrosoft.com/adminconsent?client_id=") and _link(app, db, alpha)[0] == url

    # The client's admin is not signed in to Office Sentry: a thank-you page, no consent recorded.
    page = client.get("/consent/callback", query_string={"state": state, "admin_consent": "True", "tenant": GUID})
    text = page.get_data(as_text=True)
    assert page.status_code == 200 and "Thank you" in text and "Alpha" not in text
    assert "Global Reader" in text and "Test app" in text
    assert store.get_tenant(db, alpha)["consented_at"] is None
    assert onboarding.row_for(db, alpha)["approved_at"]
    assert tasks.pending(db, f"consent_check:{alpha}")["priority"] == tasks.PRIORITY_WAITING
    assert (
        db.execute("SELECT detail FROM audit_log WHERE action = 'client_consent_returned'").fetchone()[0] == "1 attempt"
    )


def test_client_return_shows_no_microsoft_text(app, client, db, two_tenants):
    alpha, _ = two_tenants
    _, state = _link(app, db, alpha)
    page = client.get(
        "/consent/callback",
        query_string={
            "state": state,
            "error": "access_denied",
            "error_description": "AADSTS65004: User declined to consent. <script>Visit evil.example</script>",
        },
    )
    text = page.get_data(as_text=True)
    assert "Access wasn't approved" in text and "AADSTS65004" in text
    assert "evil.example" not in text and "declined" not in text
    assert onboarding.row_for(db, alpha) is None and tasks.pending(db, f"consent_check:{alpha}") is None


def test_forged_or_stale_client_state_falls_back_to_the_admin_flow(app, client, db, two_tenants):
    alpha, _ = two_tenants
    _, state = _link(app, db, alpha)
    resp = client.get("/consent/callback", query_string={"state": state[:-2] + "xx", "admin_consent": "True"})
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]
    db.execute("UPDATE tenants SET created_at = '2026-10-10T00:00:00Z' WHERE id = ?", (alpha,))  # number reused
    resp = client.get("/consent/callback", query_string={"state": state, "admin_consent": "True"})
    assert resp.status_code == 302 and "/login" in resp.headers["Location"]


def test_admin_opening_a_client_link_goes_back_to_the_tenant(app, client, db, two_tenants):
    alpha, _ = two_tenants
    db.execute("UPDATE tenants SET consented_at = NULL")
    _admin(client, db)
    _, state = _link(app, db, alpha)
    resp = client.get("/consent/callback", query_string={"state": state, "admin_consent": "True"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/admin/onboarding/{alpha}")


def test_approving_again_after_new_permissions_rereads_them(app, client, db, two_tenants):
    alpha, _ = two_tenants  # already consented
    _, state = _link(app, db, alpha)
    client.get("/consent/callback", query_string={"state": state, "admin_consent": "True"})
    assert (
        db.execute("SELECT collector FROM runs WHERE tenant_id = ? AND status = 'queued'", (alpha,)).fetchone()[
            "collector"
        ]
        == "connection"
    )


# -- Confirming consent by signing in ------------------------------------------------------------


def _sign_in_works(rsps, organisation="Alpha Ltd", guid=GUID):
    """Microsoft's sign-in for the app (as MSAL calls it), its public metadata naming ``guid`` as the domain's
    tenant ID, and the one Graph read the check makes."""
    metadata = {
        "issuer": f"{LOGIN}/{guid}/v2.0",
        "token_endpoint": f"{LOGIN}/{guid}/oauth2/v2.0/token",
        "authorization_endpoint": f"{LOGIN}/{guid}/oauth2/v2.0/authorize",
    }
    rsps.add_callback(
        responses.GET,
        re.compile(re.escape(LOGIN) + r"/[^/]+/v2\.0/\.well-known/openid-configuration"),
        callback=lambda request: (200, {}, json.dumps(metadata)),
    )
    rsps.get(
        re.compile(re.escape(LOGIN) + r"/common/discovery/instance"),
        json={
            "tenant_discovery_endpoint": f"{LOGIN}/common/v2.0/.well-known/openid-configuration",
            "metadata": [
                {
                    "preferred_network": "login.microsoftonline.com",
                    "preferred_cache": "login.windows.net",
                    "aliases": ["login.microsoftonline.com", "login.windows.net"],
                }
            ],
        },
    )
    rsps.post(
        re.compile(re.escape(LOGIN) + r"/[^/]+/oauth2/v2\.0/token"),
        json={"access_token": "tok", "expires_in": 3600, "token_type": "Bearer"},
    )
    rsps.get(f"{GRAPH_ROOT}/v1.0/organization", json={"value": [{"displayName": organisation}]})


def test_check_records_consent_swaps_in_the_tenant_id_and_starts_collection(app, db, vault, cfg, two_tenants):
    alpha, _ = two_tenants
    db.execute("UPDATE tenants SET consented_at = NULL")
    onboarding.client_approved(db, store.get_tenant(db, alpha))
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        _sign_in_works(rsps)
        assert tasks.drain(app, db, vault, cfg) == 1
        calls = [c.request for c in rsps.calls]
    t = store.get_tenant(db, alpha)
    assert t["consented_at"] and t["tenant_ref"] == GUID
    assert onboarding.row_for(db, alpha)["check_error"] is None
    queued = {r["collector"] for r in db.execute("SELECT collector FROM runs WHERE tenant_id = ?", (alpha,))}
    assert "connection" in queued and "tenant_profile" in queued
    graph = [c for c in calls if c.url.startswith(GRAPH_ROOT)]
    assert graph and all(c.method == "GET" for c in graph)  # signing in to check never writes to the tenant
    assert (
        db.execute("SELECT detail FROM audit_log WHERE action = 'consent_confirmed'").fetchone()[0]
        == f"Alpha Ltd; tenant ID {GUID}"
    )

    # The first collection reads Global Reader: missing, then assigned.
    from officesentry.graph import GraphClient

    def graph_factory(tenant, connection):
        return GraphClient(lambda: "t", sleep=lambda s: None)

    for roles, expected in (([], "global_reader"), (["Global Reader"], "connected")):
        db.execute("DELETE FROM runs WHERE status = 'queued'")
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            mock_tenant(rsps, our_app={"roles": roles} if not roles else {})
            store.enqueue_run(db, alpha, "connection")
            execute_run(db, store.claim_next_run(db), graph_factory, exchange_factory)
        entry = onboarding.tracker(db, [store.get_tenant(db, alpha)])[0]
        assert entry.state.key == expected


def test_failed_check_says_why_and_records_nothing(app, db, vault, cfg, two_tenants):
    alpha, _ = two_tenants
    db.execute("UPDATE tenants SET consented_at = NULL")
    onboarding.client_approved(db, store.get_tenant(db, alpha))
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        mock_sign_in(rsps, [])
        rsps.replace(
            responses.POST,
            re.compile(re.escape(LOGIN) + r"/[^/]+/oauth2/v2\.0/token"),
            status=400,
            json={
                "error": "unauthorized_client",
                "error_description": "AADSTS700016: Application with identifier 'x' was not found.",
            },
        )
        tasks.drain(app, db, vault, cfg)
    t, row = store.get_tenant(db, alpha), onboarding.row_for(db, alpha)
    assert t["consented_at"] is None and "doesn't have the app yet" in row["check_error"]
    assert "can't sign in yet" in onboarding.state(t, row, None).detail
    assert not db.execute("SELECT 1 FROM runs WHERE tenant_id = ?", (alpha,)).fetchone()


def test_checks_are_queued_soon_after_approval_then_less_often(db, two_tenants):
    alpha, bravo = two_tenants
    db.execute("UPDATE tenants SET consented_at = NULL")
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    db.execute(
        "INSERT INTO onboarding (tenant_id, approved_at, checked_at) VALUES (?, ?, ?)",
        (alpha, iso(now - timedelta(minutes=10)), iso(now - timedelta(minutes=1))),
    )
    db.execute(
        "INSERT INTO onboarding (tenant_id, sent_at, checked_at) VALUES (?, ?, ?)",
        (bravo, iso(now - timedelta(days=3)), iso(now - timedelta(minutes=50))),
    )
    assert onboarding.due(db, now) == []
    assert onboarding.due(db, now + timedelta(minutes=1)) == [alpha]
    assert onboarding.due(db, now + timedelta(minutes=10)) == [alpha, bravo]
    assert onboarding.due(db, now + timedelta(days=40)) == []  # gave up: nobody came back
    db.execute("UPDATE tenants SET enabled = 0 WHERE id = ?", (alpha,))  # paused tenants aren't checked
    assert onboarding.due(db, now + timedelta(minutes=10)) == [bravo]


def test_bulk_actions_on_the_tracker(client, db, two_tenants):
    alpha, bravo = two_tenants
    db.execute("UPDATE tenants SET consented_at = NULL WHERE id = ?", (alpha,))
    _admin(client, db)
    client.post("/admin/onboarding/bulk", data={"action": "sent", "tenant": [alpha, bravo]})
    assert onboarding.row_for(db, alpha)["sent_by"] == "admin"
    waiting = client.get("/admin/onboarding?show=all&state=client").get_data(as_text=True)
    assert "Alpha Ltd" in waiting and "Bravo Inc" not in waiting  # bravo consented already
    page = client.post(
        "/admin/onboarding/bulk", data={"action": "check", "tenant": [alpha, bravo]}, follow_redirects=True
    ).get_data(as_text=True)
    assert "Checking 1 tenant by signing in" in page
    assert tasks.pending(db, f"consent_check:{alpha}") and not tasks.pending(db, f"consent_check:{bravo}")
    client.post("/admin/onboarding/bulk", data={"action": "unsent", "tenant": [alpha]})
    assert onboarding.row_for(db, alpha)["sent_at"] is None
    assert client.post("/admin/onboarding/bulk", data={"action": "delete", "tenant": [alpha]}).status_code == 400


# -- What gets sent ---------------------------------------------------------------------------------


def test_send_page_email_and_mail_merge(client, db, two_tenants):
    alpha, _ = two_tenants
    db.execute("UPDATE tenants SET consented_at = NULL WHERE id = ?", (alpha,))
    _admin(client, db)
    client.post(
        f"/admin/onboarding/{alpha}/contact", data={"contact_name": "Jane Smith", "contact_email": "jane@alpha.com"}
    )
    page = client.get(f"/admin/onboarding/{alpha}").get_data(as_text=True)
    assert "Hi Jane," in page and "Alpha Ltd&#39;s Microsoft 365 security" in page
    assert "select &#34;Test app&#34;" in page and "Privileged Role Administrator" in page
    assert "mailto:jane@alpha.com?subject=" in page
    bad = client.post(f"/admin/onboarding/{alpha}/contact", data={"contact_email": "nope"}, follow_redirects=True)
    assert "That isn&#39;t an email address." in bad.get_data(as_text=True)

    db.execute("UPDATE tenants SET name = '=HYPERLINK(1)' WHERE id = ?", (alpha,))
    csv_text = client.get("/admin/onboarding/export.csv").get_data(as_text=True)
    assert "'=HYPERLINK(1)" in csv_text and "jane@alpha.com" in csv_text
    assert "https://login.microsoftonline.com/alpha.onmicrosoft.com/adminconsent?client_id=" in csv_text
    assert "Bravo" not in csv_text  # already consented: nothing to send


def test_consent_pack_lists_every_permission(client, db, two_tenants):
    alpha, _ = two_tenants
    assert set(onboarding.CLIENT_WORDS) == set(GRAPH_PERMISSIONS) | set(EXCHANGE_PERMISSIONS)
    _admin(client, db)
    resp = client.get(f"/admin/onboarding/{alpha}/pack.pdf")
    assert resp.status_code == 200
    from officesentry.reports import export

    if export.pdf_available():
        assert resp.mimetype == "application/pdf" and resp.data.startswith(b"%PDF")
    else:  # the print-ready page instead
        assert "Approving read-only access" in resp.get_data(as_text=True)


@pytest.mark.parametrize("url", ["/admin/onboarding", "/admin/onboarding/import", "/admin/onboarding/export.csv"])
def test_analysts_and_clients_cannot_open_onboarding(app, db, two_tenants, url):
    alpha, _ = two_tenants
    for role in ("analyst", "client"):
        make_user(db, role, role=role, tenants=[alpha])
        c = app.test_client()
        login(c, role)
        assert c.get(url).status_code == 403
        assert c.get(f"/admin/onboarding/{alpha}").status_code == 403
