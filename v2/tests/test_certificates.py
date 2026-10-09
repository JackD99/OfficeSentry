"""App certificates: expiry warnings (page, banner, email, webhook) and replacing one without a gap."""

import base64
import json
import re
import socket
from datetime import UTC, datetime, timedelta

import pytest
import responses
from cryptography import x509
from cryptography.hazmat.primitives import hashes

from officesentry import alerts, cert_lifecycle, delivery, mailer, store
from officesentry.cert_lifecycle import days_left, level
from officesentry.certs import generate_certificate
from officesentry.graph import GRAPH_ROOT
from officesentry.web import create_app
from officesentry.worker import default_graph_factory
from tests.conftest import login, make_user

LOGIN = "https://login.microsoftonline.com"
HOOK = "https://hooks.example.com/workflows/abc?sig=TOPSECRETSIG"
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _expires_in(db, conn_id: int, days: float, now: datetime | None = None) -> None:
    db.execute("UPDATE app_connections SET not_after = ? WHERE id = ?",
               (_iso((now or datetime.now(UTC)) + timedelta(days=days)), conn_id))


def _b64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def mock_sign_in(rsps, signed_in: list):
    """Microsoft's sign-in at the HTTP level, as MSAL calls it. Records the thumbprint each sign-in used,
    from the client assertion's x5t header, so a test can tell which certificate signed in."""
    def discovery(request):
        tenant = request.url.split("/")[3]
        base = f"{LOGIN}/{tenant}"
        return 200, {}, json.dumps({"token_endpoint": f"{base}/oauth2/v2.0/token",
                                    "authorization_endpoint": f"{base}/oauth2/v2.0/authorize",
                                    "issuer": f"{LOGIN}/{tenant}/v2.0"})

    def token(request):
        assertion = dict(p.split("=", 1) for p in request.body.split("&"))["client_assertion"]
        header = json.loads(_b64(assertion.split(".")[0]))
        signed_in.append(_b64(header["x5t"]).hex().upper())
        return 200, {}, json.dumps({"access_token": "tok", "expires_in": 3600, "token_type": "Bearer"})
    rsps.add_callback(responses.GET, re.compile(re.escape(LOGIN) + r"/[^/]+/v2\.0/\.well-known/openid-configuration"),
                      callback=discovery)
    rsps.get(re.compile(re.escape(LOGIN) + r"/common/discovery/instance"), json={
        "tenant_discovery_endpoint": f"{LOGIN}/common/v2.0/.well-known/openid-configuration",
        "metadata": [{"preferred_network": "login.microsoftonline.com", "preferred_cache": "login.windows.net",
                      "aliases": ["login.microsoftonline.com", "login.windows.net"]}]})
    rsps.add_callback(responses.POST, re.compile(re.escape(LOGIN) + r"/[^/]+/oauth2/v2\.0/token"), callback=token)


# -- Grading expiry ------------------------------------------------------------------------------------


def test_levels_at_60_30_and_7_days_then_expired():
    assert level(None) is None
    assert [level(d) for d in (400, 60, 59, 30, 29, 7, 6, 0, -1)] == \
        [None, None, "60", "60", "30", "30", "7", "7", "expired"]
    assert days_left(_iso(NOW + timedelta(days=10, hours=3)), NOW) == 10
    assert days_left(_iso(NOW - timedelta(hours=1)), NOW) == -1
    assert days_left("not-a-date", NOW) is None


def test_expiring_lists_certificates_in_use_soonest_first(db, vault, connection_id):
    assert cert_lifecycle.expiring(db) == []  # a new certificate lasts two years
    _expires_in(db, connection_id, 20)
    unused_cert = generate_certificate("x")
    unused = store.create_connection(db, vault, "Not set up", unused_cert)
    _expires_in(db, unused, 5)  # no client ID, so nothing signs in with it
    for key, value in {"mail_transport": "graph", "graph_auth": "certificate", "graph_thumbprint": "AB",
                       "graph_cert_not_after": _iso(datetime.now(UTC) + timedelta(days=3))}.items():
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
    found = cert_lifecycle.expiring(db)
    assert [(e.kind, e.level) for e in found] == [("mail", "7"), ("connection", "30")]
    assert unused_cert.thumbprint not in [e.thumbprint for e in found]
    assert found[1].headline.startswith("The Test app certificate expires in 19 days, on ")
    assert found[0].consequence == "Email from Office Sentry stops when it expires."


# -- What admins see -----------------------------------------------------------------------------------


def test_banner_and_connections_page_warn_admins(client, db, connection_id, two_tenants):
    make_user(db, "admin")
    make_user(db, "analyst", role="analyst", all_tenants=True)
    login(client, "admin")
    assert "Replace it" not in client.get("/").get_data(as_text=True)
    _expires_in(db, connection_id, 25)
    page = client.get("/").get_data(as_text=True)
    assert "The Test app certificate expires in 24 days" in page
    assert "Collection from every client tenant stops when it expires." in page
    page = client.get("/admin/connections").get_data(as_text=True)
    assert "Certificate expires soon" in page and "Replace certificate" in page
    _expires_in(db, connection_id, -2)
    page = client.get("/admin/tenants").get_data(as_text=True)
    assert "expired 2 days ago" in page and "Collection from every client tenant has stopped." in page
    analyst = client.application.test_client()
    login(analyst, "analyst")  # only admins can replace it, so only they're told
    assert "certificate" not in analyst.get("/").get_data(as_text=True).lower()


# -- Replacing a certificate ------------------------------------------------------------------------------


def test_rollover_end_to_end(client, db, vault, cfg, connection_id, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    old = store.get_connection(db, connection_id)
    _expires_in(db, connection_id, 10)

    client.post(f"/admin/connections/{connection_id}/replace")
    c = store.get_connection(db, connection_id)
    new = c["next_thumbprint"]
    assert new and new != old["thumbprint"] and c["thumbprint"] == old["thumbprint"]  # collection unchanged
    assert vault.decrypt(c["next_key_enc"]).startswith("-----BEGIN PRIVATE KEY-----")
    # The new public certificate downloads; the old one still does too.
    der = client.get(f"/admin/connections/{connection_id}/certificate.cer?which=next").data
    assert x509.load_der_x509_certificate(der).fingerprint(hashes.SHA1()).hex().upper() == new  # noqa: S303
    page = client.get("/admin/connections").get_data(as_text=True)
    assert "Replacing the certificate" in page and new in page
    assert "alpha.onmicrosoft.com" in page  # suggested tenant to test against

    # Switching before a test is refused: the new certificate may not be on the app registration yet.
    page = client.post(f"/admin/connections/{connection_id}/replace/switch", follow_redirects=True).get_data(
        as_text=True)
    assert "Test the new certificate first" in page
    assert store.get_connection(db, connection_id)["thumbprint"] == old["thumbprint"]

    signed_in = []
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        mock_sign_in(rsps, signed_in)
        rsps.get(f"{GRAPH_ROOT}/v1.0/organization", json={"value": [{"displayName": "Alpha Ltd"}]})
        page = client.post(f"/admin/connections/{connection_id}/replace/test",
                           data={"tenant_ref": "alpha.onmicrosoft.com"}, follow_redirects=True).get_data(as_text=True)
    assert "The new certificate works" in page
    assert signed_in == [new]  # the test signed in with the replacement, not the current certificate

    client.post(f"/admin/connections/{connection_id}/replace/switch")
    c = store.get_connection(db, connection_id)
    assert (c["thumbprint"], c["old_thumbprint"], c["next_thumbprint"]) == (new, old["thumbprint"], None)
    assert c["not_after"] > _iso(datetime.now(UTC) + timedelta(days=700)) and c["expiry_warned"] is None
    assert cert_lifecycle.expiring(db) == []

    # Collection now signs in with the new certificate.
    signed_in.clear()
    graph = default_graph_factory(vault)(store.get_tenant(db, alpha), c)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        mock_sign_in(rsps, signed_in)
        rsps.get(f"{GRAPH_ROOT}/v1.0/organization", json={"value": [{"displayName": "Alpha Ltd"}]})
        graph.get("/organization")
    assert signed_in == [new]

    page = client.get("/admin/connections").get_data(as_text=True)
    assert "remove the old certificate from Entra" in page and old["thumbprint"] in page
    client.post(f"/admin/connections/{connection_id}/old-removed")
    assert store.get_connection(db, connection_id)["old_thumbprint"] is None
    actions = [r["action"] for r in db.execute("SELECT action FROM audit_log WHERE action LIKE 'certificate_%'"
                                               " ORDER BY id")]
    assert actions == ["certificate_rollover_started", "certificate_rollover_tested", "certificate_switched",
                       "certificate_old_removed"]


def test_failed_test_keeps_switch_locked_and_cancel_discards(client, db, vault, connection_id, two_tenants):
    make_user(db, "admin")
    login(client, "admin")
    client.post(f"/admin/connections/{connection_id}/replace")
    signed_in = []
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        mock_sign_in(rsps, signed_in)
        rsps.replace(responses.POST, re.compile(re.escape(LOGIN) + r"/[^/]+/oauth2/v2\.0/token"), status=401,
                     json={"error": "invalid_client", "error_description": "AADSTS700027: Client assertion failed"
                           " signature validation."})
        page = client.post(f"/admin/connections/{connection_id}/replace/test",
                           data={"tenant_ref": "alpha.onmicrosoft.com"}, follow_redirects=True).get_data(as_text=True)
    assert "didn&#39;t work yet" in page and "certificate" in page
    assert store.get_connection(db, connection_id)["next_checked_at"] is None
    # A second replacement can't start while one is waiting.
    page = client.post(f"/admin/connections/{connection_id}/replace", follow_redirects=True).get_data(as_text=True)
    assert "already waiting" in page
    client.post(f"/admin/connections/{connection_id}/replace/cancel")
    c = store.get_connection(db, connection_id)
    assert c["next_thumbprint"] is None and c["next_key_enc"] is None
    assert client.get(f"/admin/connections/{connection_id}/certificate.cer?which=next").status_code == 404


def test_only_admins_replace_certificates(client, db, connection_id, two_tenants):
    make_user(db, "analyst", role="analyst", all_tenants=True)
    login(client, "analyst")
    for path in ("replace", "replace/test", "replace/switch", "replace/cancel"):
        assert client.post(f"/admin/connections/{connection_id}/{path}").status_code == 403
    assert store.get_connection(db, connection_id)["next_thumbprint"] is None


def test_rotate_keys_covers_the_replacement_key(db, vault, connection_id):
    cert_lifecycle.start(db, vault, connection_id, "admin")
    from officesentry.__main__ import unregistered_encrypted

    assert unregistered_encrypted(db) == []


# -- Warnings by email and webhook -----------------------------------------------------------------------


@pytest.fixture
def public_dns(monkeypatch):
    monkeypatch.delenv(alerts.ALLOW_PRIVATE_ENV, raising=False)
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("52.1.2.3", port))])


def _mail_and_hook(db, vault):
    for key, value in {"mail_transport": "graph", "graph_auth": "secret", "graph_tenant": "msp.onmicrosoft.com",
                       "graph_client_id": "11111111-2222-3333-4444-555555555555",
                       "graph_secret_enc": vault.encrypt("shh"), "graph_sender": "reports@msp.com"}.items():
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
    delivery.add_contact(db, None, "Ops", "ops@msp.com")
    delivery.add_contact(db, 1, "Client IT", "it@alpha.com")  # a client never gets these
    alerts.save_settings(db, {"alerts.enabled": "1", "alerts.format": "generic", "alerts.url_enc": vault.encrypt(HOOK)})


def test_one_warning_per_threshold_by_email_and_webhook(cfg, db, vault, connection_id, two_tenants, public_dns,
                                                        monkeypatch):
    app = create_app(cfg)
    _mail_and_hook(db, vault)
    monkeypatch.setattr(mailer, "_graph_token", lambda s, v: "mail-token")
    now = datetime.now(UTC)
    with responses.RequestsMock() as rsps:
        rsps.post("https://graph.microsoft.com/v1.0/users/reports@msp.com/sendMail", status=202)
        rsps.post(HOOK, status=202)
        assert cert_lifecycle.warn(app, db, vault, cfg.base_url, now) == 0  # two years left
        _expires_in(db, connection_id, 45, now)
        assert cert_lifecycle.warn(app, db, vault, cfg.base_url, now) == 1
        assert cert_lifecycle.warn(app, db, vault, cfg.base_url, now) == 0  # once per threshold
        _expires_in(db, connection_id, 3, now)  # straight past 30 to 7: one warning, not two
        assert cert_lifecycle.warn(app, db, vault, cfg.base_url, now) == 1
        calls = [(c.request.method, c.request.url.split("?")[0]) for c in rsps.calls]
        mails = [json.loads(c.request.body) for c in rsps.calls if "sendMail" in c.request.url]
        hooks = [json.loads(c.request.body) for c in rsps.calls if c.request.url.startswith("https://hooks.")]
    assert len(calls) == 4
    message = mails[-1]["message"]
    assert [r["emailAddress"]["address"] for r in message["toRecipients"]] == ["ops@msp.com"]
    assert message["subject"].startswith("Action needed: The Test app certificate expires in 2 days")
    assert "/admin/connections#conn-" in message["body"]["content"]
    assert hooks[-1]["event"] == "certificate.expiry" and hooks[-1]["certificate"]["level"] == "7"
    assert db.execute("SELECT kind, status FROM email_deliveries").fetchall()[-1][:] == ("alert", "sent")
    assert store.get_connection(db, connection_id)["expiry_warned"] == "7"


def test_worker_runs_the_expiry_job(cfg, db, vault, connection_id, two_tenants, monkeypatch):
    from officesentry import schedule, worker

    sent = []
    monkeypatch.setattr(cert_lifecycle, "warn", lambda *a, **k: sent.append(a) or 0)
    worker.housekeeping(create_app(cfg), db, vault, cfg, NOW)
    assert "certificate_expiry" in schedule.PERIODIC and sent


# -- The mail app ------------------------------------------------------------------------------------------


GRAPH_FORM = {"mail_transport": "graph", "graph_tenant": "msp.onmicrosoft.com", "graph_auth": "certificate",
              "graph_client_id": "11111111-2222-3333-4444-555555555555", "graph_sender": "reports@msp.com",
              "graph_cloud": "commercial"}


def test_mail_app_signs_in_with_a_certificate(client, db, vault, monkeypatch):
    make_user(db, "admin")
    login(client, "admin")
    client.post("/admin/email", data=GRAPH_FORM)
    assert not mailer.is_configured(mailer.mail_settings(db))  # no certificate yet
    client.post("/admin/email", data={"action": "mail_certificate"})
    s = mailer.mail_settings(db)
    assert mailer.is_configured(s) and mailer.graph_auth(s) == "certificate"
    der = client.get("/admin/email/mail-certificate.cer").data
    assert b"PRIVATE" not in der and x509.load_der_x509_certificate(der)
    page = client.get("/admin/email").get_data(as_text=True)
    assert s["graph_thumbprint"] in page and "Limit the mail app to reports@msp.com" in page

    signed_in = []
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        mock_sign_in(rsps, signed_in)
        rsps.post("https://graph.microsoft.com/v1.0/users/reports@msp.com/sendMail", status=202)
        assert mailer.send(db, vault, mailer.Email(to=["a@x.com"], subject="s", text="t")) == "graph"
    assert signed_in == [s["graph_thumbprint"]]


def test_mail_sends_through_its_own_cloud(db, vault, monkeypatch):
    monkeypatch.setattr(mailer, "_graph_token", lambda s, v: "mail-token")
    for key, value in {"mail_transport": "graph", "graph_tenant": "msp.onmicrosoft.us", "graph_cloud": "gcc_high",
                       "graph_client_id": "11111111-2222-3333-4444-555555555555", "graph_auth": "secret",
                       "graph_secret_enc": vault.encrypt("shh"), "graph_sender": "reports@msp.us"}.items():
        db.execute("INSERT INTO settings (key, value) VALUES (?, ?)", (key, value))
    with responses.RequestsMock() as rsps:
        rsps.post("https://graph.microsoft.us/v1.0/users/reports@msp.us/sendMail", status=202)
        mailer.send(db, vault, mailer.Email(to=["a@x.com"], subject="s", text="t"))


def test_switching_to_a_certificate_deletes_the_secret(client, db, vault):
    make_user(db, "admin")
    login(client, "admin")
    client.post("/admin/email", data=GRAPH_FORM | {"graph_auth": "secret", "graph_secret": "shh"})
    assert mailer.mail_settings(db)["graph_secret_enc"]
    page = client.post("/admin/email", data=GRAPH_FORM, follow_redirects=True).get_data(as_text=True)
    assert mailer.mail_settings(db)["graph_secret_enc"] == ""
    assert "Delete it from the mail app in Entra too" in page


def test_limiting_to_one_mailbox_is_a_confirmed_step(client, db, vault):
    make_user(db, "admin")
    login(client, "admin")
    client.post("/admin/email", data={"action": "mail_certificate"})
    client.post("/admin/email", data=GRAPH_FORM | {"graph_scoped": "1"})
    s = mailer.mail_settings(db)
    assert mailer.scoped(s) and s["graph_scoped_by"] == "admin"
    page = client.get("/admin/email").get_data(as_text=True)
    assert "Limit the mail app to" not in page
    assert "New-ApplicationAccessPolicy -AppId 11111111-2222-3333-4444-555555555555 -PolicyScopeGroupId " \
           "reports@msp.com" in page
    # Saving again keeps it; a different mailbox needs confirming again.
    client.post("/admin/email", data=GRAPH_FORM | {"graph_scoped": "1"})
    assert mailer.mail_settings(db)["graph_scoped_at"] == s["graph_scoped_at"]
    page = client.post("/admin/email", data=GRAPH_FORM | {"graph_scoped": "1", "graph_sender": "other@msp.com"},
                       follow_redirects=True).get_data(as_text=True)
    assert not mailer.scoped(mailer.mail_settings(db)) and "tick that step again" in page
