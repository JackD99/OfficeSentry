"""The audit log add-on (auditlog/, collectors/audit_activity.py, web/audit_addon.py): the client's feed read
into the activity history, the opt-in from link to feed, switching off, and its pages."""

import html
import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import responses

from officesentry import events, retention, store, tasks
from officesentry.auditlog import addon, feed
from officesentry.entra import MANAGEMENT_APP_ID
from officesentry.reports.file_activity import KEY
from officesentry.reports.profile import timeline as profile_timeline
from officesentry.reports.profile.base import Person
from officesentry.snapshot import TenantSnapshot
from tests import test_setup_wizard as wizard
from tests.auditdata import CLIENT_ID, MSP, TENANT, addon_on, collect, iso, sample
from tests.conftest import login, make_user
from tests.test_bulk_onboarding import _sign_in_works

NOW = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
OTHER_APP = "cdcdcdcd-5555-6666-7777-888888888888"
KEPT = [
    "FileDeleted",
    "FileDownloaded",
    "AnonymousLinkCreated",
    "SharingSet",
    "FileMoved",
    "MailItemsAccessed",
    "New-InboxRule",
    "Set-Mailbox",
    "Add-MailboxPermission",
    "SendAs",
]


def _kept(db, tenant_id, records=False):
    return list(events.activity_between(db, tenant_id, NOW - timedelta(days=8), NOW + timedelta(days=1), records))


# -- Collecting ---------------------------------------------------------------------------------------------


def test_collection_keeps_what_matters_and_only_reads(db, vault, two_tenants):
    alpha, _ = two_tenants
    addon_on(db, vault, [alpha], started_at=iso(NOW - timedelta(hours=3)))
    status, calls = collect(db, alpha, sample(NOW), NOW)
    assert status == "succeeded"
    assert {c.request.method for c in calls} == {"GET"}  # a collection never starts or stops the feed

    kept = _kept(db, alpha, records=True)
    assert sorted(a.operation for a in kept) == sorted(KEPT)  # the owner's own reading and the rest are dropped
    mail = next(a for a in kept if a.operation == "MailItemsAccessed")
    assert (mail.actor, mail.owner, mail.attention) == (
        "admin@contoso.com",
        "ceo@contoso.com",
        "An admin opened someone else's mailbox.",
    )
    assert "Board pay review" not in json.dumps(mail.record)  # email subjects are never stored
    assert mail.record["Folders"][0]["FolderItems"][0]["InternetMessageId"] == "<1@contoso.com>"
    rule = next(a for a in kept if a.operation == "New-InboxRule")
    assert (rule.object, rule.attention) == ("Move it", "A rule forwards mail.")
    assert "ForwardTo drop@mailbox.example" in rule.summary
    forwarding = next(a for a in kept if a.operation == "Set-Mailbox")
    assert forwarding.summary == "ForwardingSmtpAddress smtp:drop@mailbox.example"
    shared = next(a for a in kept if a.operation == "SharingSet")
    assert (shared.owner, shared.summary) == ("guest@partner.example", "Guest")

    # Sync traffic is counted per person and day, not kept one by one.
    assert events.activity_counts(db, alpha, "2026-10-12", "2026-10-12") == [
        ("2026-10-12", "sam@contoso.com", "FileSyncDownloadedFull", 2)
    ]
    metrics = dict(db.execute("SELECT key, value FROM metrics WHERE tenant_id = ?", (alpha,)).fetchall())
    assert metrics["audit_activity.files_deleted"] == 1
    assert metrics["audit_activity.anyone_links"] == 1
    assert metrics["audit_activity.forwarding_set"] == 1
    assert metrics["audit_activity.mailboxes_opened"] == 1

    # The next run lists again from just before where this one stopped: nothing is stored twice.
    assert collect(db, alpha, sample(NOW), NOW + timedelta(minutes=30))[0] == "succeeded"
    assert len(_kept(db, alpha)) == len(KEPT)
    assert events.activity_counts(db, alpha, "2026-10-12", "2026-10-12")[0][3] == 2


def test_collection_only_runs_for_clients_the_addon_is_on_for(db, vault, two_tenants):
    alpha, bravo = two_tenants
    addon_on(db, vault, [alpha])
    t = store.get_tenant
    assert addon.active(db, t(db, alpha)) and not addon.active(db, t(db, bravo))
    status, calls = collect(db, bravo, sample(NOW), NOW)
    assert status == "succeeded" and calls == []
    db.execute("UPDATE settings SET value = '0' WHERE key = 'audit_addon'")  # switched off for the install
    assert not addon.active(db, t(db, alpha))


def test_a_feed_turned_off_at_microsoft_marks_the_client_off(db, vault, two_tenants):
    alpha, _ = two_tenants
    addon_on(db, vault, [alpha])
    status, calls = collect(db, alpha, sample(NOW), NOW, status="disabled")
    assert status == "partial"
    row = addon.row_for(db, alpha)
    assert row["stopped_at"] and "turned it off" in row["stop_reason"]
    assert addon.state(row).key == "off" and not addon.active(db, store.get_tenant(db, alpha))
    assert {c.request.method for c in calls} == {"GET"}  # never started again by itself


def test_a_client_that_removed_the_app_is_shown_off(db, vault, two_tenants):
    alpha, _ = two_tenants
    addon_on(db, vault, [alpha])
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(
            "GET",
            f"https://manage.office.com/api/v1.0/{TENANT}/activity/feed/subscriptions/list",
            json={
                "error": {"code": "AF10001", "message": "The permission set () sent in the request does not include"}
            },
            status=401,
        )
        status, _ = collect(db, alpha, {}, NOW, rsps=rsps)
    assert status == "partial"
    assert addon.row_for(db, alpha)["stop_reason"] == addon.REMOVED


def test_retention_moves_where_the_addons_history_is_detailed_from(db, two_tenants):
    alpha, _ = two_tenants
    sources = [f"activity:{c}" for c in feed.CONTENT_TYPES]
    assert set(sources) <= set(retention.HISTORY_SOURCES)
    for source in sources:  # read for three years
        cov = events.coverage(db, alpha, source)
        cov.since, cov.read_to = int((NOW - timedelta(days=3 * 365)).timestamp()), int(NOW.timestamp())
        events.save_coverage(db, alpha, cov)
    retention.prune_history(db, retention.Policy(history_activity_months=24), NOW)
    two_years_ago = int(datetime(2024, 10, 12, 9, 0, tzinfo=UTC).timestamp())
    assert [events.coverage(db, alpha, s).since for s in sources] == [two_years_ago, two_years_ago]


# -- From the link to the feed, and off again ----------------------------------------------------------------

ROOT = f"https://manage.office.com/api/v1.0/{TENANT}/activity/feed"


def _feed_calls(rsps, method: str) -> list[dict]:
    """The query of each ``method`` request to the client's feed."""
    return [
        {k: v[0] for k, v in parse_qs(urlsplit(c.request.url).query).items()}
        for c in rsps.calls
        if c.request.method == method and c.request.url.startswith(ROOT)
    ]


def test_from_the_link_to_the_feed_and_off_again(app, client, db, vault, cfg, two_tenants):
    alpha, bravo = two_tenants
    addon_on(db, vault)
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/tenants/{alpha}/audit-addon").get_data(as_text=True)
    assert "Send the add-on link and pack" in page and "Audit log add-on" in page
    link = html.unescape(re.search(r'data-copy-text="([^"]+)"', page).group(1))
    query = parse_qs(urlsplit(link).query)
    assert query["client_id"] == [CLIENT_ID]  # the add-on's own app, not the shared one
    assert client.post(f"/tenants/{alpha}/audit-addon/sent", data={"sent": "1"}).status_code == 302
    assert addon.state(addon.row_for(db, alpha)).key == "sent"
    tracker = client.get("/admin/onboarding?show=all").get_data(as_text=True)
    assert f'Add-on: <a href="/tenants/{alpha}/audit-addon">Sent</a>' in tracker
    assert f'Add-on: <a href="/tenants/{bravo}/audit-addon">Not asked</a>' in tracker

    # The client's Global Administrator comes back from Microsoft, not signed in to Office Sentry.
    resp = app.test_client().get(
        "/consent/callback", query_string={"admin_consent": "True", "tenant": TENANT, "state": query["state"][0]}
    )
    page = resp.get_data(as_text=True)
    assert resp.status_code == 200 and "Thank you" in page and addon.AUDIT_NAME in page and "Global Reader" not in page
    row = addon.row_for(db, alpha)
    assert row["approved_at"] and not row["consented_at"]  # their word isn't proof
    assert store.get_tenant(db, alpha)["consented_at"]  # and the main app's consent is untouched

    # The check signs in as the add-on, reads the feed (GET), and only then starts it: one POST per content type.
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        _sign_in_works(rsps, guid=TENANT)
        rsps.get(f"{ROOT}/subscriptions/list", json=[])
        rsps.post(re.compile(re.escape(ROOT) + "/subscriptions/start"), json={"status": "enabled"})
        assert tasks.drain(app, db, vault, cfg) == 1
        starts = _feed_calls(rsps, "POST")
    assert sorted(s["contentType"] for s in starts) == ["Audit.Exchange", "Audit.SharePoint"]
    assert {s["PublisherIdentifier"] for s in starts} == {MSP}
    row = addon.row_for(db, alpha)
    assert row["consented_at"] and row["started_at"] and addon.state(row).key == "on"
    assert db.execute(
        "SELECT 1 FROM runs WHERE tenant_id = ? AND collector = 'audit_activity' AND status = 'queued'", (alpha,)
    ).fetchone()
    assert db.execute("SELECT 1 FROM audit_log WHERE action = 'audit_feed_started' AND tenant_id = ?", (alpha,))
    assert addon.active(db, store.get_tenant(db, alpha)) and not addon.active(db, store.get_tenant(db, bravo))

    # Switching off: nothing more is collected from now on, and a task sends the two stop calls.
    resp = client.post(f"/tenants/{alpha}/audit-addon/off", data={"delete": "1"})
    assert resp.status_code == 302
    assert not addon.active(db, store.get_tenant(db, alpha))
    assert not db.execute("SELECT 1 FROM runs WHERE collector = 'audit_activity' AND status = 'queued'").fetchone()
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        _sign_in_works(rsps, guid=TENANT)
        rsps.post(re.compile(re.escape(ROOT) + "/subscriptions/stop"), body="")
        assert tasks.drain(app, db, vault, cfg) == 1
        stops = _feed_calls(rsps, "POST")
    assert sorted(s["contentType"] for s in stops) == ["Audit.Exchange", "Audit.SharePoint"]
    row = addon.row_for(db, alpha)
    assert addon.state(row).key == "off" and row["stopped_by"] == "admin"
    assert "Switch on again" in client.get(f"/tenants/{alpha}/audit-addon").get_data(as_text=True)


def test_a_feed_is_never_started_for_a_client_nobody_asked_for(app, db, vault, cfg, two_tenants):
    alpha, _ = two_tenants
    addon_on(db, vault)
    addon.client_approved(db, store.get_tenant(db, alpha))  # approved without a link being sent from here
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        _sign_in_works(rsps, guid=TENANT)
        rsps.get(f"{ROOT}/subscriptions/list", json=[])
        assert tasks.drain(app, db, vault, cfg) == 1
        assert _feed_calls(rsps, "POST") == []
    row = addon.row_for(db, alpha)
    assert row["consented_at"] and not row["started_at"] and addon.state(row).key == "approved"


def test_nothing_shows_while_the_addon_is_off(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    for url in (
        f"/tenants/{alpha}/audit-addon",
        f"/tenants/{alpha}/audit-addon/pack.pdf",
        f"/tenants/{alpha}/data/{KEY}",
        f"/across/{KEY}",
    ):
        assert client.get(url).status_code == 404, url
    for url in (
        f"/tenants/{alpha}/collection",
        "/admin/onboarding?show=all",
        "/reports",
        f"/tenants/{alpha}/data/mailboxes",
    ):
        resp = client.get(url)
        page = resp.get_data(as_text=True)
        assert resp.status_code == 200 and "Audit log add-on" not in page and "Files and mailbox access" not in page, (
            url
        )
    assert "Files and mailbox events" not in client.get("/admin/retention").get_data(as_text=True)
    # The add-on's own page is there, for an admin to set it up.
    assert "Create the add-on's app" in client.get("/admin/audit-addon").get_data(as_text=True)


def test_the_report_shows_only_where_the_addon_is(client, db, vault, two_tenants):
    alpha, bravo = two_tenants
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    addon_on(db, vault, [alpha], started_at=iso(now - timedelta(hours=3)))
    assert collect(db, alpha, sample(now), now)[0] == "succeeded"
    make_user(db, "admin")
    login(client, "admin")

    page = client.get(f"/tenants/{alpha}/data/{KEY}").get_data(as_text=True)
    for text in ("Needs attention", "An admin opened someone else", "A rule forwards mail", "Anyone link created"):
        assert text in page, text
    assert "Board pay review" not in page
    assert "Files and mailbox access" in client.get(f"/tenants/{alpha}/data/mailboxes").get_data(as_text=True)
    assert "Files and mailbox access" not in client.get(f"/tenants/{bravo}/data/mailboxes").get_data(as_text=True)
    assert client.get(f"/tenants/{bravo}/data/{KEY}").status_code == 404
    for ext in ("pdf", "xlsx", "csv"):
        assert client.get(f"/tenants/{alpha}/data/{KEY}.{ext}").status_code == 200, ext
    rollup = client.get(f"/across/{KEY}").get_data(as_text=True)
    assert "Alpha Ltd" in rollup and "Bravo Inc" not in rollup
    assert "10 records kept" in client.get(f"/tenants/{alpha}/activity").get_data(as_text=True)
    assert "Files and mailbox events" not in client.get(f"/tenants/{bravo}/activity").get_data(as_text=True)

    # The add-on's own page and the client's: where each client has got.
    page = client.get("/admin/audit-addon").get_data(as_text=True)
    assert "Alpha Ltd" in page and "Bravo Inc" not in page
    resp = client.post("/admin/audit-addon/switch", data={"on": "0"})
    assert resp.status_code == 302 and addon.enabled(db)  # not while a client's feed is on
    page = client.get(f"/tenants/{alpha}/audit-addon").get_data(as_text=True)
    assert "Switch off" in page and "delete the 10 events collected" in page


def test_a_persons_timeline_has_what_they_did_and_what_was_done_to_their_mailbox(db, vault, two_tenants):
    alpha, bravo = two_tenants
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    addon_on(db, vault, [alpha], started_at=iso(now - timedelta(hours=3)))
    collect(db, alpha, sample(now), now)

    def timeline(tenant_id, upn):
        person = Person(
            TenantSnapshot(db, store.get_tenant(db, tenant_id)), {"_id": upn, "userPrincipalName": upn}, now
        )
        return [(e.area, e.text, e.tone) for e in profile_timeline.timeline(person)]

    ceo = timeline(alpha, "CEO@contoso.com")
    assert ("Mailbox", "Mail read by admin@contoso.com: \\Inbox (As admin, Client=OWA)", "warn") in ceo
    assert ("Mailbox", "Sent as this account by pa@contoso.com (As delegate)", None) in ceo
    assert ("Mailbox", "Mailbox access granted by admin@contoso.com: pa@contoso.com (FullAccess)", None) in ceo
    jo = timeline(alpha, "jo@contoso.com")
    assert ("Files", "File deleted: /sites/finance/Shared Documents/Budget 2027.xlsx", None) in jo
    assert any(area == "Mailbox" and text.startswith("Inbox rule created") and tone for area, text, tone in jo)
    assert not any("Mail read" in text for _, text, _ in jo)  # only what's about them
    assert not timeline(bravo, "ceo@contoso.com")  # nor another client's


@responses.activate
def test_the_wizard_creates_the_addons_app_asking_only_for_the_audit_log(client, db):
    make_user(db, "admin")
    login(client, "admin")
    assert "Sign in to Microsoft" in client.get("/admin/audit-addon").get_data(as_text=True)
    wizard._device_code()
    assert client.post("/admin/audit-addon/setup", data={}).status_code == 302
    wizard._token(wizard.SIGNED_IN)
    feed_roles = ["ActivityFeed.Read", "ActivityFeed.ReadDlp", "ServiceHealth.Read"]
    responses.get(
        f"{wizard.GRAPH}/servicePrincipals",
        json={"value": [{"id": "sp-feed", "appId": MANAGEMENT_APP_ID, "appRoles": wizard._roles(feed_roles, [])}]},
    )
    created = responses.post(f"{wizard.GRAPH}/applications", status=201, json={"id": "o", "appId": wizard.APP_ID})
    check = client.post("/admin/audit-addon/setup/check", headers=wizard.JSON).get_json()
    assert check["status"] == "done"

    body = json.loads(created.calls[0].request.body)
    assert body["displayName"] == addon.AUDIT_NAME and body["signInAudience"] == "AzureADMultipleOrgs"
    assert body["requiredResourceAccess"] == [
        {"resourceAppId": MANAGEMENT_APP_ID, "resourceAccess": [{"id": "role-ActivityFeed.Read", "type": "Role"}]}
    ]
    assert not store.list_connections(db)  # never offered as the shared app
    (app,) = store.list_connections(db, kind=store.AUDIT)
    assert (app["client_id"], app["home_tenant"]) == (wizard.APP_ID.lower(), wizard.HOME)
    assert addon.enabled(db)  # creating the app switches the add-on on (each client still has to be asked)


def test_a_new_client_id_for_the_addon_app_asks_every_client_again(client, db, vault, two_tenants):
    alpha, _ = two_tenants
    conn_id = addon_on(db, vault, [alpha])
    make_user(db, "admin")
    login(client, "admin")
    resp = client.post(f"/admin/connections/{conn_id}/client-id", data={"client_id": OTHER_APP})
    assert resp.status_code == 302
    row = addon.row_for(db, alpha)
    assert addon.state(row).key == "off" and "app changed" in row["stop_reason"]
    assert not row["consented_at"] and not addon.active(db, store.get_tenant(db, alpha))
    assert store.get_tenant(db, alpha)["consented_at"]  # the shared app's consent stays
