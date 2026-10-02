"""Exchange, inbox rules, apps, sharing: client, collectors, checks and coverage."""

import json
from datetime import UTC, datetime

import pytest
import responses

from officesentry import coverage, store
from officesentry.exchange import EXCHANGE_ROOT, ExchangeAccessDenied, ExchangeClient, ExchangeError
from officesentry.mailflow import inbox_rule_risk, is_external, smtp_address
from officesentry.rules import evaluate
from officesentry.snapshot import TenantSnapshot
from tests import graphdata as gd
from tests.seed import seed_tenant
from tests.test_collectors import USERS

URL = f"{EXCHANGE_ROOT}/adminapi/beta/tid/InvokeCommand"


def client():
    return ExchangeClient("tid", lambda: "tok", sleep=lambda s: None)


# -- Exchange client ------------------------------------------------------------


def test_exchange_refuses_cmdlets_outside_the_read_only_list():
    for cmdlet in ("Set-Mailbox", "New-InboxRule", "Remove-Mailbox", "Get-MessageTrace"):
        with pytest.raises(ValueError):
            client().invoke(cmdlet)


@responses.activate
def test_exchange_pages_and_sends_cmdlet():
    responses.post(URL, json={"value": [{"n": 1}], "@odata.nextLink": URL + "?$skiptoken=2"})
    responses.post(URL + "?$skiptoken=2", json={"value": [{"n": 2}]})
    assert client().invoke("Get-Mailbox", {"ResultSize": "Unlimited"}) == [{"n": 1}, {"n": 2}]
    body = json.loads(responses.calls[0].request.body)
    assert body == {"CmdletInput": {"CmdletName": "Get-Mailbox", "Parameters": {"ResultSize": "Unlimited"}}}
    assert responses.calls[0].request.headers["Authorization"] == "Bearer tok"


@responses.activate
def test_exchange_refuses_next_link_to_another_host():
    responses.post(URL, json={"value": [], "@odata.nextLink": "https://evil.example/adminapi/x"})
    with pytest.raises(ValueError):
        client().invoke("Get-Mailbox")


@responses.activate
def test_exchange_retries_throttling_then_explains_access_denied():
    responses.post(URL, status=429, headers={"Retry-After": "1"})
    responses.post(URL, status=401, body="")
    with pytest.raises(ExchangeAccessDenied) as exc:
        client().invoke("Get-Mailbox")
    assert "Global Reader" in str(exc.value)


@responses.activate
def test_exchange_reports_cmdlet_errors():
    responses.post(URL, status=400, json={"error": {"code": "BadRequest", "details": [{"message": "not found"}]}})
    with pytest.raises(ExchangeError, match="not found"):
        client().invoke("Get-MailboxPermission", {"Identity": "x"})


# -- Mail-flow helpers ------------------------------------------------------------


def test_smtp_address_and_external():
    assert smtp_address("SMTP:Ann@Gmail.com") == "ann@gmail.com"
    assert smtp_address("not an address") is None
    assert is_external("a@gmail.com", {"x.com"})
    assert not is_external("a@sub.x.com", {"x.com"})


def test_inbox_rule_risk():
    hide = gd.INBOX_RULES["u1"][0] | {"moveToFolderName": "RSS Feeds"}
    risk = inbox_rule_risk(hide, {"x.com"})
    assert risk.severity == "high" and any("invoice" in r for r in risk.reasons)
    assert any("blank" in r for r in risk.reasons)
    internal = inbox_rule_risk(gd.INBOX_RULES["u7"][0], {"x.com"})
    assert internal.severity is None and internal.forwards_to == ["boss@x.com"]
    disabled = inbox_rule_risk(dict(gd.INBOX_RULES["u1"][1], isEnabled=False), {"x.com"})
    assert disabled.severity == "low"


# -- Collectors and checks end to end ----------------------------------------------


@pytest.fixture
def seeded(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, users=USERS + [gd.INFO_USER])
    tenant = store.get_tenant(db, alpha)
    return TenantSnapshot(db, tenant), {r.key: r for r in evaluate(TenantSnapshot(db, tenant))}


def test_collectors_store_phase4_data(seeded):
    snap, _ = seeded
    for collector in ("mailboxes", "inbox_rules", "app_consents", "tenant_policies"):
        assert snap.runs[collector]["status"] == "succeeded", collector
    perms = snap.objects("mailboxes", "mailbox_permission")
    assert [(p["mailbox"], p["User"]) for p in perms] == [("info@x.com", "ann@x.com")]
    assert [p["Trustee"] for p in snap.objects("mailboxes", "send_as")] == ["ann@x.com"]
    rules = {r["displayName"]: r for r in snap.objects("inbox_rules", "inbox_rule")}
    assert rules["."]["moveToFolderName"] == "RSS Feeds"
    perm = snap.objects("app_consents", "app_permission")
    assert {(p["clientId"], p["permission"]) for p in perm} == {("sp-evil", "Mail.Read"), ("sp-zoom", "User.Read.All")}
    groups = snap.objects("conditional_access", "ca_group")
    assert groups == []  # the default policies name no groups


def test_email_checks(seeded):
    _, r = seeded
    fwd = r["external_forwarding"]
    assert fwd.status == "fail"
    assert {f.object_id for f in fwd.findings} == {"mailbox:u1", "rule:u1:r2"}
    assert r["suspicious_inbox_rules"].status == "fail"
    assert [f.object_id for f in r["suspicious_inbox_rules"].findings] == ["rule:u1:r1"]
    assert r["auto_forward_policy"].status == "pass"
    assert r["shared_mailbox_signin"].status == "fail"
    assert r["shared_mailbox_signin"].findings[0].subject == "info@x.com"
    assert r["mailbox_auditing"].status == "pass"
    assert r["smtp_auth"].status == "fail"


def test_app_and_sharing_checks(seeded):
    _, r = seeded
    apps = r["risky_app_permissions"]
    assert apps.status == "fail"
    assert [(f.subject, f.severity) for f in apps.findings] == [("Mail Sync Pro", "high")]
    assert r["user_consent"].status == "fail"
    creds = r["app_credentials"]
    assert [(f.subject, f.severity) for f in creds.findings] == [("Old connector", "low")]
    assert r["sharepoint_sharing"].status == "fail"
    assert r["guest_invites"].status == "fail"


def test_admin_mfa_coverage_flags_excluded_breakglass(seeded):
    _, r = seeded
    result = r["admins_ca_mfa"]
    assert result.status == "fail"
    assert [f.object_id for f in result.findings] == ["u4"]
    assert "excluded from Require MFA for all users" in result.findings[0].detail


def test_no_exchange_licence_makes_mail_checks_unavailable(db, two_tenants):
    alpha, _ = two_tenants
    skus = [dict(gd_sku, servicePlans=[p for p in gd_sku["servicePlans"] if "EXCHANGE" not in p["servicePlanName"]])
            for gd_sku in __import__("tests.test_collectors", fromlist=["SKUS"]).SKUS]
    seed_tenant(db, alpha, skus=skus)
    r = {x.key: x for x in evaluate(TenantSnapshot(db, store.get_tenant(db, alpha)))}
    for key in ("external_forwarding", "shared_mailbox_signin", "mailbox_auditing", "smtp_auth"):
        assert r[key].status == "unavailable", key


def test_exchange_access_denied_fails_only_that_run(db, two_tenants):
    alpha, _ = two_tenants
    import re

    from officesentry.collectors import REGISTRY
    from officesentry.graph import GraphClient
    from officesentry.worker import execute_run
    from tests.seed import exchange_factory, mock_tenant

    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        mock_tenant(rsps, exchange=False)
        rsps.post(re.compile(re.escape(EXCHANGE_ROOT) + r"/.*"), status=401)
        statuses = {}
        for key in REGISTRY:
            store.enqueue_run(db, alpha, key)
        while (run := store.claim_next_run(db)) is not None:
            statuses[run["collector"]] = execute_run(
                db, run, lambda t, c: GraphClient(lambda: "t", sleep=lambda s: None), exchange_factory)
    assert statuses["mailboxes"] == "failed"
    assert statuses["inbox_rules"] == "succeeded"  # falls back to licensed users via Graph
    run = store.latest_run(db, alpha, "mailboxes") or db.execute(
        "SELECT * FROM runs WHERE collector = 'mailboxes' ORDER BY id DESC").fetchone()
    assert "Global Reader" in run["error"]


# -- Conditional Access coverage ----------------------------------------------------


def _directory(**groups):
    return coverage.Directory(groups={k: {"displayName": k, "members": v} for k, v in groups.items()},
                              user_roles={"admin": {"role-ga"}}, role_names={"role-ga": "Global Administrator"},
                              names={})


def _policy(**users):
    return {"id": "p", "displayName": "P", "state": "enabled",
            "conditions": {"users": users, "applications": {"includeApplications": ["All"]},
                           "clientAppTypes": ["all"]},
            "grantControls": {"builtInControls": ["mfa"]}}


def test_coverage_groups_roles_and_unknown_membership():
    d = _directory(staff=["u1"], unknown=None)
    assert coverage.applies(_policy(includeGroups=["staff"]), "u1", "Member", d)
    assert not coverage.applies(_policy(includeGroups=["staff"]), "u2", "Member", d)
    assert coverage.applies(_policy(includeRoles=["role-ga"]), "admin", "Member", d)
    assert not coverage.applies(_policy(includeUsers=["All"], excludeGroups=["staff"]), "u1", "Member", d)
    # Unreadable group: never counts as included, always counts as excluded.
    assert not coverage.applies(_policy(includeGroups=["unknown"]), "u1", "Member", d)
    assert not coverage.applies(_policy(includeUsers=["All"], excludeGroups=["unknown"]), "u1", "Member", d)
    assert coverage.applies(_policy(includeUsers=["GuestsOrExternalUsers"]), "g", "Guest", d)


def test_describe_policy_in_plain_words():
    d = _directory()
    row = coverage.describe(gd.CA_BLOCK_LEGACY, d)
    assert (row.state, row.users, row.apps, row.grant) == ("On", "All users", "All apps", "Block")
    assert row.conditions == "Clients: ActiveSync, Other legacy"


def test_user_coverage_lists_gaps_first(seeded):
    snap, _ = seeded
    cov = coverage.user_coverage(snap)
    assert [c.mfa for c in cov] == sorted(c.mfa for c in cov)
    by_id = {c.id: c for c in cov}
    assert by_id["u1"].mfa and not by_id["u1"].legacy_blocked
    assert "u2" not in by_id  # disabled accounts are left out


def test_mfa_exclusion_names_the_group(db, two_tenants):
    alpha, _ = two_tenants
    policy = dict(gd.CA_MFA_ALL, conditions=dict(gd.CA_MFA_ALL["conditions"], users={
        "includeUsers": ["All"], "excludeUsers": [], "excludeGroups": ["grp-bg"]}))
    seed_tenant(db, alpha, ca=[policy])
    r = {x.key: x for x in evaluate(TenantSnapshot(db, store.get_tenant(db, alpha)))}
    assert [f.subject for f in r["mfa_enforced"].findings] == ["Break glass"]
    assert [f.object_id for f in r["admins_ca_mfa"].findings] == ["u4"]  # u4 is in the excluded group


def test_expiring_credentials(db, two_tenants):
    from officesentry.rules import app_credentials

    class Snap:
        def objects(self, collector, kind):
            return [{"_id": "a", "displayName": "Soon", "credentials": [{"endDateTime": "2026-10-20T00:00:00Z"}]}]

    status, _, found = app_credentials(Snap(), datetime(2026, 10, 2, tzinfo=UTC))
    assert status == "fail" and found[0].severity == "medium"
