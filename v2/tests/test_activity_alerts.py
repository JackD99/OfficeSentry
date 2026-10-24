"""Activity alerts: risky changes in a client's tenant, from the directory audit log, posted once each."""

import json
import socket

import pytest
import responses

from officesentry import activity_alerts, adminactivity, alerts, lifecycle, store
from officesentry.db import utcnow
from officesentry.graph import GraphClient
from tests import adminactivitydata as ad
from tests.conftest import login, make_user
from tests.test_collectors import V1, _run

AUDITS = f"{V1}/auditLogs/directoryAudits"
HOOK = "https://hooks.example.com/workflows/abc/triggers/manual/run?api-version=1&sig=TOPSECRETSIG"


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.delenv(alerts.ALLOW_PRIVATE_ENV, raising=False)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, port, *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("52.1.2.3", port))],
    )


@pytest.fixture
def factory():
    return lambda tenant, connection: GraphClient(lambda: "t", sleep=lambda s: None)


@pytest.fixture
def alerting(cfg, vault):
    return alerts.Alerting(cfg.base_url, vault)


def configure(db, vault, fmt="generic", kinds=None, enabled=True):
    alerts.save_settings(
        db,
        {
            "alerts.enabled": "1" if enabled else "0",
            "alerts.format": fmt,
            "alerts.url_enc": vault.encrypt(HOOK),
            **({} if kinds is None else {"alerts.activity": ",".join(kinds)}),
        },
    )


def admin_roles(db, tenant_id, roles):
    """An Admin roles run in which each (sign-in name, role) is held."""
    run_id = db.execute(
        "INSERT INTO runs (tenant_id, collector, status, trigger, created_at, finished_at)"
        " VALUES (?, 'admin_roles', 'succeeded', 'manual', ?, ?)",
        (tenant_id, utcnow(), utcnow()),
    ).lastrowid
    rows = [
        (f"a{i}", {"principalType": "user", "userPrincipalName": upn, "roleName": role, "privileged": True})
        for i, (upn, role) in enumerate(roles)
    ]
    store.add_objects(db, run_id, "role_assignment", rows)


def grant(eid, upn, role, days, activity="Add member to role"):
    return ad.event(
        eid,
        "RoleManagement",
        activity,
        ad.ago(days),
        ad.by_user(ad.ADMIN),
        [ad.target("User", None, upn, [ad.prop("Role.DisplayName", role)])],
    )


def defaults(eid, days, old, new):
    return ad.event(
        eid,
        "Policy",
        "Update security defaults",
        ad.ago(days),
        ad.by_user("mallory@x.com", "203.0.113.9"),
        [ad.target("Policy", "Security Defaults Policy", props=[ad.prop("IsEnabled", new, old)])],
        service="Conditional Access",
    )


def method_removed(eid, upn, days, activity="Admin deleted security info"):
    return ad.event(
        eid,
        "UserManagement",
        activity,
        ad.ago(days),
        ad.by_user(ad.ADMIN),
        [ad.target("User", None, upn)],
        service="Authentication Methods",
    )


NEW_GLOBAL_ADMIN = grant("new1", "eve@x.com", "Global Administrator", 0.1)
NEW_CA_DELETED = ad.event(
    "new2",
    "Policy",
    "Delete conditional access policy",
    ad.ago(0.2),
    ad.by_user(ad.ADMIN),
    [ad.target("Policy", "Require MFA for admins")],
    service="Conditional Access",
)
NEW_DEFAULTS_OFF = defaults("new3", 0.3, [True], [False])
NEW_METHOD_REMOVED = method_removed("new4", "ann@x.com", 0.4)
NEW_ROUTINE = [
    grant("new5", "carl@x.com", "Message Center Reader", 0.05),  # not an admin role that changes security
    method_removed("new6", "carl@x.com", 0.06),  # not an admin
    ad.event(
        "new7",
        "UserManagement",
        "Reset user password",
        ad.ago(0.07),
        ad.by_user(ad.ADMIN),
        [ad.target("User", None, "x@x.com")],
    ),
    grant("new8", "zed@x.com", "Global Administrator", 8),  # first read now, but more than a week old
    {**grant("new9", "mallory@x.com", "Global Administrator", 0.08), "result": "failure"},
]
NEW = [NEW_GLOBAL_ADMIN, NEW_CA_DELETED, NEW_DEFAULTS_OFF, NEW_METHOD_REMOVED, *NEW_ROUTINE]


def collect(db, tenant_id, factory, items):
    responses.upsert(responses.GET, AUDITS, json={"value": items})
    run_id, status = _run(db, tenant_id, "admin_activity", factory)
    assert status == "succeeded"
    return run_id, status


def hook_calls():
    return [c for c in responses.calls if c.request.url == HOOK]


# -- What alerts ----------------------------------------------------------------------------------


def _normalised(e):
    return adminactivity.normalise(e)


def test_admin_roles_alert_but_activating_one_does_not():
    ga = activity_alerts.match(_normalised(ad.GLOBAL_ADMIN_GRANT), {})
    assert (ga.kind, ga.severity, ga.text) == ("admin_role", "high", "bob@x.com made Global Administrator")
    pim = activity_alerts.match(_normalised(ad.PIM_ELIGIBLE), {})
    assert (pim.severity, pim.text) == ("medium", "ann@x.com made eligible for Exchange Administrator")
    for event in (
        ad.ROLE_GRANT_FAILED,
        ad.ROLE_REMOVED,
        grant("r1", "a@x.com", "Message Center Reader", 1),
        grant("r2", "a@x.com", "Global Administrator", 1, "Add member to role completed (PIM activation)"),
    ):
        assert activity_alerts.match(_normalised(event), {}) is None, event["activityDisplayName"]


def test_conditional_access_deleted_turned_off_or_report_only():
    assert activity_alerts.match(_normalised(ad.CA_DELETED), {}).text == (
        "Conditional Access policy deleted: Block risky countries"
    )
    assert activity_alerts.match(_normalised(ad.CA_DISABLED), {}).text == (
        "Conditional Access policy turned off: Require MFA for all users"
    )
    report_only = json.loads(json.dumps(ad.CA_DISABLED))
    report_only["targetResources"][0]["modifiedProperties"][0]["newValue"] = json.dumps(
        {**ad.CA_ON, "state": "enabledForReportingButNotEnforced"}
    )
    assert activity_alerts.match(_normalised(report_only), {}).text == (
        "Conditional Access policy set to report-only: Require MFA for all users"
    )
    assert activity_alerts.match(_normalised(ad.CA_ADDED), {}) is None


@pytest.mark.parametrize(
    ("old", "new", "detail", "off", "text"),
    [
        ([True], [False], "On → Off", True, "Security defaults turned off"),
        ("True", "False", "On → Off", True, "Security defaults turned off"),
        (None, False, "Off", True, "Security defaults turned off"),
        ([False], [True], "Off → On", False, None),
        ([False], [False], "Off", False, None),
        (None, None, "", None, "Security defaults changed"),  # the event doesn't say: check rather than miss it
    ],
)
def test_security_defaults_turned_off(old, new, detail, off, text):
    event = defaults("sd", 1, old, new)
    if old is None and new is None:
        event["targetResources"][0]["modifiedProperties"] = []
    e = _normalised(event)
    assert (e["kind"], e["area"], e["detail"], e["defaults_off"]) == ("auth_policy", "Tenant policies", detail, off)
    found = activity_alerts.match(e, {})
    assert (found.text if found else None) == text
    assert (adminactivity.review(e) is not None) == bool(off)


def test_sign_in_method_removed_from_an_admin():
    held = {"ann@x.com": ["Exchange Administrator", "Global Administrator"]}
    found = activity_alerts.match(_normalised(method_removed("m1", "Ann@x.com", 1)), held)
    assert (found.kind, found.text) == (
        "admin_mfa_removed",
        "Sign-in method removed from Ann@x.com (Global Administrator and 1 other role)",
    )
    by_themselves = method_removed("m2", "ann@x.com", 1, "User deleted security info")
    assert adminactivity.kind_of(by_themselves) == "security_info"
    assert activity_alerts.match(_normalised(by_themselves), held).kind == "admin_mfa_removed"
    assert activity_alerts.match(_normalised(method_removed("m3", "carl@x.com", 1)), held) is None
    assert activity_alerts.match(_normalised(ad.ADMIN_SECURITY_INFO), held) is None  # registered, not removed


# -- Once each, after a collection ----------------------------------------------------------------


@responses.activate
def test_new_changes_alert_once_after_the_first_collection(db, vault, alerting, two_tenants, factory):
    alpha, bravo = two_tenants
    configure(db, vault)
    admin_roles(db, alpha, [("ann@x.com", "Exchange Administrator")])
    responses.post(HOOK, status=202)

    # The first read is everything Microsoft still keeps: nothing alerts.
    run_id, status = collect(db, alpha, factory, ad.DIRECTORY_AUDITS)
    assert activity_alerts.after_run(db, alerting, run_id, status) is None

    run_id, status = collect(db, alpha, factory, NEW)
    assert activity_alerts.after_run(db, alerting, run_id, status) is True
    [call] = hook_calls()
    body = json.loads(call.request.body)
    assert body["event"] == "activity.changes"
    assert body["title"] == "Alpha Ltd: 4 changes to check"
    assert body["url"].endswith(f"/tenants/{alpha}/data/admin-activity")
    assert body["tenant"]["id"] == alpha and body["changes_total"] == 4 and "findings" not in body
    # Oldest first.
    assert [(c["type"], c["text"]) for c in body["changes"]] == [
        ("admin_mfa_removed", "Sign-in method removed from ann@x.com (Exchange Administrator)"),
        ("security_defaults_off", "Security defaults turned off"),
        ("ca_off", "Conditional Access policy deleted: Require MFA for admins"),
        ("admin_role", "eve@x.com made Global Administrator"),
    ]
    ga = body["changes"][-1]
    assert (ga["severity"], ga["actor"], ga["targets"], ga["detail"]) == (
        "high",
        ad.ADMIN,
        ["eve@x.com"],
        "Global Administrator",
    )
    sent = db.execute("SELECT detail FROM audit_log WHERE action = 'alert_sent'").fetchone()["detail"]
    assert sent == "activity.changes via generic, 4 changes"

    # The same log again: nothing new, nothing sent.
    run_id, status = collect(db, alpha, factory, NEW)
    assert activity_alerts.after_run(db, alerting, run_id, status) is None
    assert len(hook_calls()) == 1


@responses.activate
def test_only_the_kinds_chosen(db, vault, alerting, two_tenants, factory):
    alpha, _ = two_tenants
    configure(db, vault, kinds=["ca_off"])
    responses.post(HOOK, status=202)
    collect(db, alpha, factory, ad.DIRECTORY_AUDITS)
    run_id, status = collect(db, alpha, factory, NEW)
    assert activity_alerts.after_run(db, alerting, run_id, status) is True
    body = json.loads(hook_calls()[0].request.body)
    assert body["title"] == "Alpha Ltd: Conditional Access policy deleted: Require MFA for admins"
    assert [c["type"] for c in body["changes"]] == ["ca_off"]


@responses.activate
@pytest.mark.parametrize("why", ["none chosen", "alerts off", "paused", "archived", "another collector", "failed"])
def test_nothing_sent(db, vault, alerting, two_tenants, factory, why):
    alpha, _ = two_tenants
    configure(db, vault, kinds=[] if why == "none chosen" else None, enabled=why != "alerts off")
    responses.post(HOOK, status=202)
    collect(db, alpha, factory, ad.DIRECTORY_AUDITS)
    run_id, status = collect(db, alpha, factory, NEW)
    if why in ("paused", "archived"):
        getattr(lifecycle, why.removesuffix("d"))(db, store.get_tenant(db, alpha), "admin")
    elif why == "another collector":
        db.execute("UPDATE runs SET collector = 'users' WHERE id = ?", (run_id,))
    elif why == "failed":
        status = "failed"
    assert activity_alerts.after_run(db, alerting, run_id, status) is None
    assert hook_calls() == []


@responses.activate
def test_nothing_to_compare_with_while_the_audit_log_was_unreadable(db, vault, alerting, two_tenants, factory):
    alpha, _ = two_tenants
    configure(db, vault)
    responses.get(AUDITS, status=403, json={"error": {"code": "Authorization_RequestDenied", "message": "No"}})
    _run(db, alpha, "admin_activity", factory)
    run_id, status = collect(db, alpha, factory, NEW)
    assert activity_alerts.after_run(db, alerting, run_id, status) is None


@responses.activate
def test_an_alert_bug_never_breaks_the_worker(db, vault, alerting, two_tenants, factory, monkeypatch):
    alpha, _ = two_tenants
    configure(db, vault)
    collect(db, alpha, factory, ad.DIRECTORY_AUDITS)
    run_id, status = collect(db, alpha, factory, NEW)
    monkeypatch.setattr(activity_alerts, "match", lambda *a: 1 / 0)
    assert activity_alerts.after_run(db, alerting, run_id, status) is False


@responses.activate
def test_teams_and_slack_name_each_change(db, vault, alerting, two_tenants, factory):
    alpha, _ = two_tenants
    responses.post(HOOK, status=202)
    configure(db, vault, fmt="teams", kinds=["admin_role"])
    collect(db, alpha, factory, ad.DIRECTORY_AUDITS)
    run_id, status = collect(db, alpha, factory, NEW)
    assert activity_alerts.after_run(db, alerting, run_id, status) is True
    card = json.loads(hook_calls()[-1].request.body)["attachments"][0]["content"]
    assert card["body"][0]["text"] == "Alpha Ltd: eve@x.com made Global Administrator"
    [fact] = card["body"][2]["facts"]
    assert fact["title"] == "Admin role"
    assert fact["value"].startswith("eve@x.com made Global Administrator (by admin@x.com, ")
    assert card["actions"][0]["title"] == "Open Admin activity"

    message = activity_alerts.message("https://portal.example", store.get_tenant(db, alpha), [])
    message.items = [
        alerts.Item("high", "Admin role", "eve@x.com made <Global> Administrator", "by x", label="Admin role")
    ]
    slack = alerts.slack_payload(message)
    assert "• *Admin role* eve@x.com made &lt;Global&gt; Administrator (by x)" in slack["blocks"][2]["text"]["text"]
    assert slack["text"].endswith(": 1 change")


# -- Settings -------------------------------------------------------------------------------------


def test_settings_choose_which_changes(client, db, vault):
    make_user(db, "admin")
    login(client, "admin")
    html = client.get("/admin/alerts").get_data(as_text=True)
    assert "Changes in a client" in html and "Send a sample change" in html
    assert html.count('name="activity"') == len(alerts.ACTIVITY_ALERTS)
    assert html.count('name="activity" value="admin_role" checked') == 1  # every kind is on until changed

    form = {
        "enabled": "1",
        "format": "slack",
        "min_severity": "high",
        "url": HOOK,
        "activity": ["security_defaults_off", "admin_role", "bogus"],
    }
    client.post("/admin/alerts", data=form)
    assert alerts.settings(db)["alerts.activity"] == "admin_role,security_defaults_off"
    assert alerts.activity_kinds(alerts.settings(db)) == {"admin_role", "security_defaults_off"}
    detail = db.execute("SELECT detail FROM audit_log WHERE action = 'alert_settings_updated'").fetchone()["detail"]
    assert "activity=admin_role,security_defaults_off" in detail
    html = client.get("/admin/alerts").get_data(as_text=True)
    assert 'value="ca_off" checked' not in html and 'value="security_defaults_off" checked' in html

    client.post("/admin/alerts", data={**form, "url": "", "activity": []})
    assert alerts.settings(db)["alerts.activity"] == "" and alerts.activity_kinds(alerts.settings(db)) == set()


@responses.activate
def test_send_a_sample_change(client, db):
    make_user(db, "admin")
    login(client, "admin")
    client.post("/admin/alerts", data={"enabled": "1", "format": "generic", "min_severity": "high", "url": HOOK})
    responses.post(HOOK, status=200)
    html = client.post("/admin/alerts/test", data={"sample": "activity"}, follow_redirects=True).get_data(as_text=True)
    assert "Sample change sent to https://hooks.example.com/…" in html
    body = json.loads(responses.calls[-1].request.body)
    assert body["event"] == "test"
    assert body["title"] == "Contoso (example): new.starter@contoso.example made Global Administrator"
    assert body["changes"][0]["type"] == "admin_role"
    assert db.execute("SELECT detail FROM audit_log WHERE action = 'alert_test_sent'").fetchone()["detail"] == (
        "generic activity"
    )
