import hashlib
import hmac
import json
import socket
from unittest.mock import patch

import pytest
import requests
import responses

from officesentry import alerts, store
from officesentry.collectors import REGISTRY
from officesentry.db import utcnow
from officesentry.graph import GraphClient
from officesentry.worker import NotConnected, execute_run, run_worker
from tests import graphdata as gd
from tests.conftest import login, make_user
from tests.seed import exchange_factory, mock_tenant

HOOK = "https://hooks.example.com/workflows/abc/triggers/manual/run?api-version=1&sig=TOPSECRETSIG"
PUBLIC_IP = "52.1.2.3"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    """Every host resolves to a public address unless a test says otherwise."""
    monkeypatch.delenv(alerts.ALLOW_PRIVATE_ENV, raising=False)
    calls = []

    def fake(host, port, *args, **kwargs):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return calls


@pytest.fixture
def alerting(cfg, vault):
    return alerts.Alerting(cfg.base_url, vault)


def configure(db, vault, url=HOOK, fmt="teams", enabled=True, severity="high", failures=True, recovery=False,
              secret=None):
    alerts.save_settings(db, {
        "alerts.enabled": "1" if enabled else "0", "alerts.format": fmt, "alerts.min_severity": severity,
        "alerts.on_failures": "1" if failures else "0", "alerts.on_recovery": "1" if recovery else "0",
        "alerts.url_enc": vault.encrypt(url) if url else "",
        "alerts.secret_enc": vault.encrypt(secret) if secret else "",
    })


def add_finding(db, tenant_id, severity="high", rule_key="admin_mfa", object_id="u1", subject="ann@x.com",
                detail="No MFA method registered", **extra):
    cols = {"tenant_id": tenant_id, "rule_key": rule_key, "object_id": object_id, "severity": severity,
            "subject": subject, "detail": detail, "first_seen": utcnow(), "last_seen": utcnow(), **extra}
    cur = db.execute(f"INSERT INTO findings ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     tuple(cols.values()))
    return cur.lastrowid


def add_run(db, tenant_id, status, collector="users", error=None):
    cur = db.execute("INSERT INTO runs (tenant_id, collector, status, trigger, created_at, finished_at, error)"
                     " VALUES (?, ?, ?, 'manual', ?, ?, ?)", (tenant_id, collector, status, utcnow(), utcnow(), error))
    return cur.lastrowid


def posted(rsps, i=-1):
    return json.loads(rsps.calls[i].request.body)


def audit_rows(db, *actions):
    return db.execute(f"SELECT * FROM audit_log WHERE action IN ({','.join('?' * len(actions))}) ORDER BY id",
                      actions).fetchall()


# -- Findings alerts ---------------------------------------------------------


@responses.activate
def test_teams_payload_groups_findings_into_one_card(db, vault, alerting, two_tenants):
    alpha, _ = two_tenants
    configure(db, vault)
    responses.post(HOOK, status=202)
    ids = [add_finding(db, alpha, object_id=f"u{i}", subject=f"user{i:02}@x.com") for i in range(13)]
    tenant = store.get_tenant(db, alpha)
    assert alerts.after_sync(db, alerting, tenant, {"new": 13, "opened": ids}) is True
    assert len(responses.calls) == 1
    body = posted(responses)
    assert body["type"] == "message"
    attachment = body["attachments"][0]
    assert attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    card = attachment["content"]
    assert card["type"] == "AdaptiveCard" and card["version"] == "1.4"
    assert card["body"][0]["text"] == "Alpha Ltd: 13 new findings"
    facts = card["body"][2]["facts"]
    assert len(facts) == alerts.MAX_ITEMS
    assert facts[0] == {"title": "High",
                        "value": "user00@x.com: No MFA method registered (check: Every admin has MFA registered)"}
    assert card["body"][3]["text"] == "And 3 more."
    assert card["actions"][0]["url"] == f"http://localhost:8000/tenants/{alpha}"
    assert responses.calls[0].request.headers["Content-Type"] == "application/json"
    assert "X-OfficeSentry-Signature" not in responses.calls[0].request.headers
    sent = audit_rows(db, "alert_sent")
    assert len(sent) == 1 and sent[0]["tenant_id"] == alpha and "TOPSECRET" not in sent[0]["detail"]


@responses.activate
def test_slack_payload_escapes_and_caps(db, vault, alerting, two_tenants):
    alpha, _ = two_tenants
    configure(db, vault, fmt="slack")
    responses.post(HOOK)
    ids = [add_finding(db, alpha, object_id="evil", subject="<https://evil.example|click> & co")]
    ids += [add_finding(db, alpha, object_id=f"u{i}") for i in range(11)]
    alerts.after_sync(db, alerting, store.get_tenant(db, alpha), {"opened": ids})
    body = posted(responses)
    assert body["text"].startswith("Alpha Ltd: 12 new findings")
    assert body["blocks"][0]["type"] == "header"
    listing = body["blocks"][2]["text"]["text"]
    assert "&lt;https://evil.example|click&gt; &amp; co" in listing and "<https://evil" not in listing
    assert listing.count("• ") == alerts.MAX_ITEMS and listing.endswith("_and 2 more_")
    assert body["blocks"][-1]["elements"][0]["url"].endswith(f"/tenants/{alpha}")


@responses.activate
def test_teams_text_cannot_form_links(db, vault, alerting, two_tenants):
    alpha, _ = two_tenants
    configure(db, vault)
    responses.post(HOOK)
    fid = add_finding(db, alpha, subject="[Reset your password](https://evil.example)")
    alerts.after_sync(db, alerting, store.get_tenant(db, alpha), {"opened": [fid]})
    value = posted(responses)["attachments"][0]["content"]["body"][2]["facts"][0]["value"]
    assert "](" not in value


@responses.activate
def test_generic_payload_and_signature(db, vault, alerting, two_tenants):
    alpha, _ = two_tenants
    configure(db, vault, fmt="generic", secret="s3cret-key")
    responses.post(HOOK)
    fid = add_finding(db, alpha, severity="critical", rule_key="guest_admins", subject="g#EXT#@x.com")
    alerts.after_sync(db, alerting, store.get_tenant(db, alpha), {"opened": [fid]})
    request = responses.calls[0].request
    body = json.loads(request.body)
    assert body["event"] == "findings.new"
    assert body["tenant"] == {"id": alpha, "name": "Alpha Ltd", "tenant_ref": "alpha.onmicrosoft.com",
                              "url": f"http://localhost:8000/tenants/{alpha}"}
    assert body["findings_total"] == 1 and body["findings_more"] == 0
    f = body["findings"][0]
    assert (f["severity"], f["check"], f["subject"], f["id"]) == ("critical", "guest_admins", "g#EXT#@x.com", fid)
    expected = "sha256=" + hmac.new(b"s3cret-key", request.body, hashlib.sha256).hexdigest()
    assert request.headers["X-OfficeSentry-Signature"] == expected


@responses.activate
def test_generic_payload_caps_findings(db, vault, alerting, two_tenants):
    alpha, _ = two_tenants
    configure(db, vault, fmt="generic")
    responses.post(HOOK)
    ids = [add_finding(db, alpha, object_id=f"u{i}") for i in range(alerts.MAX_GENERIC_ITEMS + 5)]
    alerts.after_sync(db, alerting, store.get_tenant(db, alpha), {"opened": ids})
    body = posted(responses)
    assert len(body["findings"]) == alerts.MAX_GENERIC_ITEMS and body["findings_more"] == 5


@responses.activate
def test_severity_threshold_and_accepted_risks(db, vault, alerting, two_tenants):
    alpha, _ = two_tenants
    responses.post(HOOK)
    tenant = store.get_tenant(db, alpha)
    medium = add_finding(db, alpha, severity="medium", object_id="m")
    low = add_finding(db, alpha, severity="low", object_id="l")
    accepted = add_finding(db, alpha, severity="critical", object_id="a", accepted_at=utcnow(),
                           accepted_by="jack", reopened_at=utcnow())
    configure(db, vault, severity="high")
    assert alerts.after_sync(db, alerting, tenant, {"opened": [medium, low, accepted]}) is None
    assert not responses.calls
    configure(db, vault, severity="medium")
    alerts.after_sync(db, alerting, tenant, {"opened": [medium, low, accepted]})
    facts = posted(responses)["attachments"][0]["content"]["body"][2]["facts"]
    assert [f["title"] for f in facts] == ["Medium"]


@responses.activate
def test_disabled_or_unconfigured_sends_nothing(db, vault, alerting, two_tenants):
    alpha, _ = two_tenants
    fid = add_finding(db, alpha)
    tenant = store.get_tenant(db, alpha)
    assert alerts.after_sync(db, alerting, tenant, {"opened": [fid]}) is None  # never configured
    configure(db, vault, enabled=False)
    assert alerts.after_sync(db, alerting, tenant, {"opened": [fid]}) is None
    configure(db, vault, url=None)
    assert alerts.after_sync(db, alerting, tenant, {"opened": [fid]}) is None
    configure(db, vault)
    assert alerts.after_sync(db, None, tenant, {"opened": [fid]}) is None  # e.g. live-test runs
    assert not responses.calls


def _seed_with_alerts(db, tenant_id, alerting, hook_status=202, hook_body=None):
    factory = lambda tenant, connection: GraphClient(lambda: "t", sleep=lambda s: None)  # noqa: E731
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps, \
            patch("officesentry.dnscheck.lookup", gd.fake_dns):
        mock_tenant(rsps)
        rsps.post(HOOK, status=hook_status, body=hook_body)
        for key in REGISTRY:
            store.enqueue_run(db, tenant_id, key)
        statuses = []
        while (run := store.claim_next_run(db)) is not None:
            statuses.append(execute_run(db, run, factory, exchange_factory, alerting))
        hook_calls = [c for c in rsps.calls if c.request.url == HOOK]
    return statuses, hook_calls


def test_worker_alerts_on_new_findings_only_once(db, vault, alerting, two_tenants):
    alpha, _ = two_tenants
    configure(db, vault, fmt="generic")
    statuses, calls = _seed_with_alerts(db, alpha, alerting)
    assert set(statuses) <= {"succeeded", "partial"}
    assert calls, "new high findings should alert"
    alerted = {f["id"] for c in calls for f in json.loads(c.request.body)["findings"]}
    expected = {r["id"] for r in db.execute(
        "SELECT id FROM findings WHERE tenant_id = ? AND severity IN ('critical', 'high')", (alpha,))}
    assert alerted == expected
    assert all(json.loads(c.request.body)["tenant"]["id"] == alpha for c in calls)
    # The same data again: nothing new, so nothing sent.
    _, calls = _seed_with_alerts(db, alpha, alerting)
    assert calls == []


@pytest.mark.parametrize("hook", [{"hook_status": 500}, {"hook_body": requests.Timeout("slow")},
                                  {"hook_body": requests.ConnectionError(f"Max retries exceeded with url: {HOOK}")}])
def test_worker_survives_webhook_failure(db, vault, alerting, two_tenants, hook):
    alpha, _ = two_tenants
    configure(db, vault)
    statuses, calls = _seed_with_alerts(db, alpha, alerting, **hook)
    assert calls and set(statuses) <= {"succeeded", "partial"}
    failed = audit_rows(db, "alert_failed")
    assert failed and not audit_rows(db, "alert_sent")
    assert all("TOPSECRET" not in r["detail"] and "hooks.example.com" not in r["detail"] for r in failed)
    events = [r["message"] for r in db.execute("SELECT message FROM run_events WHERE message LIKE 'Alert%'")]
    assert events and all(m.startswith("Alert not sent") and "TOPSECRET" not in m for m in events)
    assert db.execute("SELECT COUNT(*) FROM findings WHERE tenant_id = ?", (alpha,)).fetchone()[0] > 0


def test_alert_bug_never_breaks_a_run(db, vault, alerting, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    configure(db, vault)
    monkeypatch.setattr(alerts, "opened_items", lambda *a, **k: 1 / 0)
    statuses, calls = _seed_with_alerts(db, alpha, alerting)
    assert set(statuses) <= {"succeeded", "partial"} and calls == []
    assert not db.execute("SELECT 1 FROM run_events WHERE message LIKE 'Updating findings failed%'").fetchone()


# -- Collection failures -----------------------------------------------------


@responses.activate
def test_failure_alerts_on_state_change_only(db, vault, alerting, two_tenants):
    alpha, _ = two_tenants
    configure(db, vault, fmt="generic")
    responses.post(HOOK)
    first = add_run(db, alpha, "failed", error="boom")
    assert alerts.after_run(db, alerting, first, "failed") is None  # no previous success: not a change
    ok = add_run(db, alpha, "succeeded")
    assert alerts.after_run(db, alerting, ok, "succeeded") is None  # recovery alerts are off
    add_run(db, alpha, "succeeded", collector="mfa")  # other collectors don't count
    failed = add_run(db, alpha, "failed", error="PermissionMissing: User.Read.All\nmore detail")
    assert alerts.after_run(db, alerting, failed, "failed") is True
    again = add_run(db, alpha, "failed", error="still broken")
    assert alerts.after_run(db, alerting, again, "failed") is None
    assert len(responses.calls) == 1
    body = posted(responses)
    assert body["event"] == "collection.failed" and body["collector"] == "users" and body["run_id"] == failed
    assert body["error"] == "PermissionMissing: User.Read.All"
    assert body["tenant"]["id"] == alpha and body["findings"] == []
    title = REGISTRY["users"].title
    assert body["title"] == f"Alpha Ltd: {title} collection failed"
    assert db.execute("SELECT message FROM run_events WHERE run_id = ?", (failed,)).fetchone()[0] == "Alert sent"


def test_run_worker_alerts_when_a_collection_starts_failing(cfg, db, vault, two_tenants):
    alpha, _ = two_tenants
    configure(db, vault, fmt="generic")
    add_run(db, alpha, "succeeded", collector="users")

    def broken(tenant, connection):
        raise NotConnected("tenant has no app connection with a client ID")
    with responses.RequestsMock() as rsps:
        rsps.post(HOOK)
        run_worker(cfg, once=True, graph_factory=broken, exchange_factory=broken)
        bodies = [json.loads(c.request.body) for c in rsps.calls]
    # Every collector of both tenants failed, but only one had worked before.
    assert [(b["event"], b["collector"], b["tenant"]["id"]) for b in bodies] == [("collection.failed", "users", alpha)]
    assert bodies[0]["error"] == "tenant has no app connection with a client ID"


@responses.activate
def test_partial_counts_as_working(db, vault, alerting, two_tenants):
    alpha, _ = two_tenants
    configure(db, vault)
    responses.post(HOOK)
    add_run(db, alpha, "partial")
    failed = add_run(db, alpha, "failed", error="x")
    assert alerts.after_run(db, alerting, failed, "failed") is True


@responses.activate
def test_recovery_alert_is_optional(db, vault, alerting, two_tenants):
    alpha, _ = two_tenants
    responses.post(HOOK)
    configure(db, vault, fmt="slack", recovery=True)
    add_run(db, alpha, "failed", error="x")
    ok = add_run(db, alpha, "succeeded")
    assert alerts.after_run(db, alerting, ok, "succeeded") is True
    assert "working again" in posted(responses)["text"]
    ok2 = add_run(db, alpha, "succeeded")
    assert alerts.after_run(db, alerting, ok2, "succeeded") is None
    configure(db, vault, failures=False, recovery=True)
    failed = add_run(db, alpha, "failed", error="x")
    assert alerts.after_run(db, alerting, failed, "failed") is None
    assert len(responses.calls) == 1


@responses.activate
def test_failure_alert_survives_webhook_error(db, vault, alerting, two_tenants):
    alpha, _ = two_tenants
    configure(db, vault)
    responses.post(HOOK, body=requests.Timeout())
    add_run(db, alpha, "succeeded")
    failed = add_run(db, alpha, "failed", error="x")
    assert alerts.after_run(db, alerting, failed, "failed") is False
    assert "No answer within" in audit_rows(db, "alert_failed")[0]["detail"]


# -- Delivery safety ---------------------------------------------------------


@pytest.mark.parametrize("ip", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.1", "100.64.0.1", "::1",
                                "fe80::1", "::ffff:10.0.0.1", "0.0.0.0", "240.0.0.1"])  # noqa: S104
@responses.activate
def test_ssrf_guard_refuses_internal_addresses(monkeypatch, ip):
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, port, *a, **k: [(family, socket.SOCK_STREAM, 6, "", (ip, port))])
    with pytest.raises(alerts.AlertError, match="internal address"):
        alerts.post(HOOK, {"x": 1})
    assert not responses.calls


@responses.activate
def test_ssrf_guard_checks_every_address(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.1.1", port))])
    with pytest.raises(alerts.AlertError):
        alerts.post(HOOK, {"x": 1})


@responses.activate
def test_private_webhooks_can_be_allowed(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))])
    monkeypatch.setenv(alerts.ALLOW_PRIVATE_ENV, "1")
    url = "https://teams-relay.internal/hook"
    responses.post(url)
    alerts.post(url, {"x": 1})
    assert len(responses.calls) == 1


@responses.activate
def test_unresolvable_host(monkeypatch):
    def fail(*a, **k):
        raise socket.gaierror("nope")
    monkeypatch.setattr(socket, "getaddrinfo", fail)
    with pytest.raises(alerts.AlertError, match="Couldn't look up hooks.example.com"):
        alerts.post(HOOK, {"x": 1})


@pytest.mark.parametrize("url", ["http://hooks.example.com/x", "ftp://hooks.example.com/x", "https:///x",
                                 "https://user:pw@hooks.example.com/x", "hooks.example.com/x"])
@responses.activate
def test_https_only(url):
    with pytest.raises(alerts.AlertError):
        alerts.post(url, {"x": 1})
    assert not responses.calls


@responses.activate
def test_redirects_are_not_followed():
    responses.post(HOOK, status=302, headers={"Location": "https://169.254.169.254/latest"})
    with pytest.raises(alerts.AlertError, match="redirect"):
        alerts.post(HOOK, {"x": 1})
    assert len(responses.calls) == 1


# -- Admin page --------------------------------------------------------------


def _form(**overrides):
    data = {"enabled": "1", "format": "teams", "url": HOOK, "min_severity": "high", "on_failures": "1",
            "secret": ""}
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


def test_page_renders_for_admin(client, db):
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/admin/alerts")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "Webhook URL" in html and "Send test alert" in html and 'href="/admin/alerts"' in html


def test_settings_saved_encrypted_and_masked(client, db, vault):
    make_user(db, "admin")
    login(client, "admin")
    resp = client.post("/admin/alerts", data=_form(secret="hmac-secret-value"), follow_redirects=True)
    assert "Alert settings saved." in resp.get_data(as_text=True)
    raw = dict(db.execute("SELECT key, value FROM settings WHERE key LIKE 'alerts.%'").fetchall())
    assert raw["alerts.url_enc"] != HOOK and vault.decrypt(raw["alerts.url_enc"]) == HOOK
    assert vault.decrypt(raw["alerts.secret_enc"]) == "hmac-secret-value"
    dump = "\n".join(db.iterdump())
    assert "TOPSECRETSIG" not in dump and "hmac-secret-value" not in dump
    assert raw["alerts.enabled"] == "1" and raw["alerts.on_recovery"] == "0"
    html = client.get("/admin/alerts").get_data(as_text=True)
    assert "https://hooks.example.com/…" in html
    assert "TOPSECRETSIG" not in html and "/workflows/" not in html and "hmac-secret-value" not in html
    detail = audit_rows(db, "alert_settings_updated")[0]["detail"]
    assert "url=changed" in detail and "secret=changed" in detail and "hooks.example.com" not in detail

    # Blank URL and secret keep the saved ones.
    client.post("/admin/alerts", data=_form(url="", format="slack", min_severity="medium", on_recovery="1"))
    s = alerts.settings(db)
    assert vault.decrypt(s["alerts.url_enc"]) == HOOK and vault.decrypt(s["alerts.secret_enc"]) == "hmac-secret-value"
    assert (s["alerts.format"], s["alerts.min_severity"], s["alerts.on_recovery"]) == ("slack", "medium", "1")

    client.post("/admin/alerts", data=_form(url="", enabled=None, clear_secret="1", clear_url="1"))
    s = alerts.settings(db)
    assert s["alerts.url_enc"] == "" and s["alerts.secret_enc"] == "" and s["alerts.enabled"] == "0"


def test_settings_validation(client, db):
    make_user(db, "admin")
    login(client, "admin")
    resp = client.post("/admin/alerts", data=_form(url="http://hooks.example.com/x"), follow_redirects=True)
    assert "must start with https://" in resp.get_data(as_text=True)
    resp = client.post("/admin/alerts", data=_form(url=""), follow_redirects=True)
    assert "Enter the webhook URL to turn alerts on." in resp.get_data(as_text=True)
    assert alerts.settings(db)["alerts.enabled"] == "0"
    assert client.post("/admin/alerts", data=_form(format="email")).status_code == 400
    assert client.post("/admin/alerts", data=_form(min_severity="info")).status_code == 400


@responses.activate
def test_send_test_button(client, db):
    make_user(db, "admin")
    login(client, "admin")
    client.post("/admin/alerts", data=_form(format="slack"))
    responses.post(HOOK, status=200)
    html = client.post("/admin/alerts/test", follow_redirects=True).get_data(as_text=True)
    assert "Test alert sent to https://hooks.example.com/…" in html
    body = posted(responses)
    assert body["text"] == "Office Sentry test alert"
    responses.replace(responses.POST, HOOK, status=404)
    html = client.post("/admin/alerts/test", follow_redirects=True).get_data(as_text=True)
    assert "Test failed: The webhook answered HTTP 404." in html
    assert [r["action"] for r in audit_rows(db, "alert_test_sent", "alert_test_failed")] == \
        ["alert_test_sent", "alert_test_failed"]


def test_send_test_without_url(client, db):
    make_user(db, "admin")
    login(client, "admin")
    html = client.post("/admin/alerts/test", follow_redirects=True).get_data(as_text=True)
    assert "Test failed: No webhook URL is saved." in html


@pytest.mark.parametrize("role", ["analyst", "client"])
def test_alerts_admin_only(client, db, role, two_tenants):
    make_user(db, "someone", role=role, all_tenants=role == "analyst", tenants=[two_tenants[0]])
    login(client, "someone")
    assert client.get("/admin/alerts").status_code == 403
    assert client.post("/admin/alerts", data=_form()).status_code == 403
    assert client.post("/admin/alerts/test").status_code == 403
    assert alerts.settings(db)["alerts.url_enc"] == ""
    assert "/admin/alerts" not in client.get("/").get_data(as_text=True)


def test_alerts_anonymous_redirected(client, db):
    make_user(db, "admin")
    for method, url in [("GET", "/admin/alerts"), ("POST", "/admin/alerts"), ("POST", "/admin/alerts/test")]:
        resp = client.open(url, method=method)
        assert resp.status_code == 302 and "/login" in resp.headers["Location"]
