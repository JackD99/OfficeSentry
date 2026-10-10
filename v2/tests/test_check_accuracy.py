"""Checks and reports that judged something wrongly: each test is the case that used to come out wrong."""

import copy
from datetime import UTC, datetime, timedelta

from officesentry import store
from officesentry.licences import licence_summary, trial_seats
from officesentry.reports import data_reports
from officesentry.reports.month_changes import compare_sections
from officesentry.rules import evaluate
from officesentry.snapshot import TenantSnapshot
from tests import devicedata as dd
from tests import graphdata as gd
from tests import licencedata as ld
from tests.seed import seed_tenant
from tests.stored import unpack
from tests.test_collectors import SKUS, USERS


def _results(db, tenant_id):
    return {r.key: r for r in evaluate(TenantSnapshot(db, store.get_tenant(db, tenant_id)))}


def _when(days: float) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- Email --------------------------------------------------------------------------------------


def _exchange(**cmdlets):
    return {**gd.EXCHANGE, **cmdlets}


def test_a_scoped_policy_allowing_forwarding_passes(db, two_tenants):
    alpha, bravo = two_tenants
    # What the recommendation asks for: Off by default, On only in a policy scoped to named mailboxes.
    scoped = [
        {"Name": "Default", "AutoForwardingMode": "Off", "IsDefault": True},
        {"Name": "Allow forwarding - Finance", "AutoForwardingMode": "On", "IsDefault": False},
    ]
    seed_tenant(db, alpha, exchange=_exchange(**{"Get-HostedOutboundSpamFilterPolicy": scoped}))
    result = _results(db, alpha)["auto_forward_policy"]
    assert (result.status, result.findings) == ("pass", [])
    assert "1 scoped policy allows it for the mailboxes it covers" in result.summary

    open_default = [{"Name": "Default", "AutoForwardingMode": "On", "IsDefault": True}]
    seed_tenant(db, bravo, exchange=_exchange(**{"Get-HostedOutboundSpamFilterPolicy": open_default}))
    result = _results(db, bravo)["auto_forward_policy"]
    assert result.status == "fail"
    [finding] = result.findings
    assert (finding.severity, finding.subject) == ("medium", "Default")


def test_forwarding_to_an_unresolved_recipient_is_flagged(db, two_tenants):
    alpha, _ = two_tenants
    mailboxes = copy.deepcopy(gd.EXCHANGE["Get-Mailbox"])
    mailboxes[0].pop("ForwardingSmtpAddress")  # Ann no longer forwards to Gmail...
    mailboxes[1]["ForwardingAddress"] = "John Smith"  # ...but Info forwards to a contact Get-Recipient can't find
    seed_tenant(
        db,
        alpha,
        exchange=_exchange(**{"Get-Mailbox": mailboxes, "Get-Recipient": []}),
        inbox_rules={},
    )
    result = _results(db, alpha)["external_forwarding"]
    assert result.status == "fail"
    [finding] = result.findings
    assert (finding.severity, finding.subject) == ("medium", "info@x.com")
    assert finding.detail.startswith("Forwards to an address Office Sentry couldn't resolve") and "John Smith" in (
        finding.detail
    )
    assert result.summary == "1 forward to an address Office Sentry couldn't resolve"


# -- Apps ---------------------------------------------------------------------------------------


def test_a_disabled_app_is_low_risk(db, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    principals = [dict(sp, accountEnabled=False) if sp["id"] == "sp-evil" else sp for sp in gd.SERVICE_PRINCIPALS]
    monkeypatch.setattr(gd, "SERVICE_PRINCIPALS", principals)
    seed_tenant(db, alpha)
    found = {f.subject: f for f in _results(db, alpha)["risky_app_permissions"].findings}
    evil = found["Mail Sync Pro"]  # Mail.Read without anyone signed in, but it can't get a token
    assert evil.severity == "low" and evil.detail.endswith("(disabled)")


def test_entra_connect_certificates_renew_themselves(db, two_tenants, monkeypatch):
    alpha, _ = two_tenants

    def app(aid, name, days):
        return {
            "id": aid,
            "appId": aid,
            "displayName": name,
            "createdDateTime": "2024-01-01T00:00:00Z",
            "passwordCredentials": [],
            "keyCredentials": [{"displayName": "cert", "endDateTime": _when(days)}],
        }

    monkeypatch.setattr(
        gd,
        "APPLICATIONS",
        [
            app("sync-ok", "ConnectSyncProvisioning_SRV01_1a2b3c", 28),  # renews itself in the next few days
            app("sync-late", "ConnectSyncProvisioning_SRV02_4d5e6f", 3),  # should have renewed by now
            app("other", "Payroll connector", 28),
        ],
    )
    seed_tenant(db, alpha)
    result = _results(db, alpha)["app_credentials"]
    found = {f.object_id: f for f in result.findings}
    assert set(found) == {"sync-late", "other"}
    assert (
        found["sync-late"].severity == "medium" and "Entra Connect should have renewed it" in found["sync-late"].detail
    )

    monkeypatch.setattr(gd, "APPLICATIONS", [app("sync-ok", "ConnectSyncProvisioning_SRV01_1a2b3c", 28)])
    seed_tenant(db, alpha)
    result = _results(db, alpha)["app_credentials"]
    assert result.status == "pass" and "Entra Connect" in result.summary


def _consent(*policies):
    return {
        **gd.AUTHORIZATION_POLICY,
        "defaultUserRolePermissions": {
            **gd.AUTHORIZATION_POLICY["defaultUserRolePermissions"],
            "permissionGrantPoliciesAssigned": [f"ManagePermissionGrantsForSelf.{p}" for p in policies],
        },
    }


def test_a_custom_consent_policy_is_flagged_for_review(db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha, auth_policy=_consent("contoso-sales-apps"))
    result = _results(db, alpha)["user_consent"]
    assert result.status == "fail" and result.summary.startswith("Custom consent policy")
    [finding] = result.findings
    assert finding.severity == "low" and "contoso-sales-apps" in finding.detail

    seed_tenant(db, bravo, auth_policy=_consent("microsoft-user-default-recommended"))
    result = _results(db, bravo)["user_consent"]
    assert result.status == "pass" and "verified publishers" in result.summary


def test_the_guests_report_names_a_custom_consent_policy(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, auth_policy=_consent("contoso-sales-apps"))
    report = data_reports.build(db, store.get_tenant(db, alpha), "guests")
    section = next(s for s in report.sections if s.title == "External access settings")
    [row] = [r for r in section.rows if r[0] == "User consent to apps"]
    assert row[1] == "Custom policy: contoso-sales-apps"


# -- Guests -------------------------------------------------------------------------------------


def test_unknown_guest_invite_setting_is_unavailable(db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha, auth_policy={**gd.AUTHORIZATION_POLICY, "allowInvitesFrom": None})
    assert _results(db, alpha)["guest_invites"].status == "unavailable"
    missing = {k: v for k, v in gd.AUTHORIZATION_POLICY.items() if k != "allowInvitesFrom"}
    seed_tenant(db, alpha, auth_policy=missing)
    assert _results(db, alpha)["guest_invites"].status == "unavailable"

    seed_tenant(
        db, bravo, auth_policy={**gd.AUTHORIZATION_POLICY, "allowInvitesFrom": "adminsGuestInvitersAndAllMembers"}
    )
    result = _results(db, bravo)["guest_invites"]
    assert (result.status, result.summary) == ("pass", "Members and admins can invite guests")


# -- Devices ------------------------------------------------------------------------------------


def test_devices_intune_couldnt_evaluate_are_not_compliant(db, two_tenants, monkeypatch):
    alpha, bravo = two_tenants
    good = {**dd.MANAGED_DEVICES[0], "jailBroken": "False"}
    devices = [
        good,
        {**good, "id": "md-err", "deviceName": "LAPTOP-ERR", "complianceState": "error"},
        {**good, "id": "md-con", "deviceName": "LAPTOP-CON", "complianceState": "conflict"},
        {**good, "id": "md-unk", "deviceName": "LAPTOP-UNK", "complianceState": "unknown"},
    ]
    monkeypatch.setattr(dd, "MANAGED_DEVICES", devices)
    seed_tenant(db, alpha)
    result = _results(db, alpha)["device_compliance"]
    assert result.status == "fail" and result.summary == "3 of 4 managed devices not compliant"
    found = {f.object_id: (f.severity, f.detail) for f in result.findings}
    assert found == {
        "md-err": ("low", "Compliance state: error"),
        "md-con": ("low", "Compliance state: conflict"),
        "md-unk": ("low", "Compliance state: unknown"),
    }

    # A state with no verdict either way (managed by Configuration Manager) isn't counted as compliant.
    monkeypatch.setattr(dd, "MANAGED_DEVICES", [good, {**good, "id": "md-cm", "complianceState": "configManager"}])
    seed_tenant(db, bravo)
    result = _results(db, bravo)["device_compliance"]
    assert result.status == "pass" and result.summary.startswith("1 of 2 managed devices compliant")


# -- Sign-ins -----------------------------------------------------------------------------------


P2_SKUS = [
    dict(
        SKUS[0],
        servicePlans=SKUS[0]["servicePlans"] + [{"servicePlanName": "AAD_PREMIUM_P2", "provisioningStatus": "Success"}],
    )
]


def test_risky_sign_ins_unavailable_when_the_log_was_cut_short(db, two_tenants):
    alpha, _ = two_tenants
    quiet = copy.deepcopy(gd.SIGNINS)
    for s in quiet:
        s["riskLevelDuringSignIn"], s["riskState"] = "none", "none"
    seed_tenant(db, alpha, users=[*USERS, gd.INFO_USER], skus=P2_SKUS, signins=quiet)
    assert _results(db, alpha)["risky_sign_ins"].status == "pass"
    unpack(db)
    db.execute("UPDATE objects SET data = json_set(data, '$.truncated', json('true')) WHERE kind = 'sign_in_status'")
    result = _results(db, alpha)["risky_sign_ins"]
    assert result.status == "unavailable" and "cut short" in result.summary


# -- Licences and subscriptions ----------------------------------------------------------------


def _price(db, part, price):
    db.execute(
        "INSERT INTO sku_prices (sku_part_number, monthly_price, updated_at) VALUES (?, ?, ?)",
        (part, price, "2026-10-01T00:00:00Z"),
    )


# Business Premium: 10 bought and a 5-licence trial on top, 10 in use. Copilot: a trial only, 2 of 5 in use.
TRIAL_SKUS = [
    {**ld.SKUS[0], "prepaidUnits": {"enabled": 15, "warning": 0, "suspended": 0, "lockedOut": 0}},
    ld.SKUS[1],
]
TRIAL_SUBSCRIPTIONS = [
    ld.subscription("s-bp", "sku-bp", "SPB", total=10, ends_in=300),
    ld.subscription("s-bp-trial", "sku-bp", "SPB", trial=True, total=5, ends_in=20),
    ld.subscription("s-copilot", "sku-copilot", "Microsoft_365_Copilot", trial=True, total=5, ends_in=12),
]


def test_trial_licences_cost_nothing(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, users=ld.USERS, skus=TRIAL_SKUS, subscriptions=TRIAL_SUBSCRIPTIONS)
    _price(db, "SPB", 20.0)
    _price(db, "Microsoft_365_Copilot", 25.0)
    report = data_reports.build(db, store.get_tenant(db, alpha), "subscriptions")
    section = next(s for s in report.sections if s.title == "Subscriptions")
    rows = {str(r[0]): r for r in section.rows}
    assert float(rows["Microsoft 365 Business Premium"][8]) == 200.0  # the 10 paid for, not 15
    assert (rows["Microsoft 365 Copilot"][7], rows["Microsoft 365 Copilot"][8]) == ("Trial", "")
    figures = {h.label: h for h in report.highlights}
    assert float(figures["Monthly cost"].value) == 200.0

    assert trial_seats(TRIAL_SUBSCRIPTIONS) == {"sku-bp": 5, "sku-copilot": 5}
    summary = licence_summary(TRIAL_SKUS, None, {"SPB": 20.0, "Microsoft_365_Copilot": 25.0}, None, TRIAL_SUBSCRIPTIONS)
    lines = {line.sku_id: line for line in summary.lines}
    assert (lines["sku-bp"].unassigned, lines["sku-bp"].wasted_units) == (0, 0)  # the trial's 5 are spare
    assert (lines["sku-copilot"].unassigned, lines["sku-copilot"].monthly_waste) == (0, 0)
    assert summary.monthly_waste == 0
    # Without the subscriptions, every unused licence is charged for, as before.
    assert licence_summary(TRIAL_SKUS, None, {"SPB": 20.0}).lines[0].unassigned == 5


def test_two_subscriptions_of_one_licence_are_compared_one_by_one(db, two_tenants):
    alpha, _ = two_tenants
    two = [
        ld.subscription("s-bp1", "sku-bp", "SPB", total=10, ends_in=300),
        ld.subscription("s-bp2", "sku-bp", "SPB", total=5, ends_in=120),
    ]
    tenant = store.get_tenant(db, alpha)

    def renewals(subs):
        seed_tenant(db, alpha, users=ld.USERS, skus=ld.SKUS, subscriptions=subs)
        report = data_reports.build(db, tenant, "subscriptions")
        return next(s for s in report.sections if s.title == "Renewals and expiry")

    before = renewals(two)
    after = renewals([two[0], {**two[1], "totalLicenses": 8}])
    diff = compare_sections(before, after)
    assert [(c.kind, c.diffs) for c in diff.changes] == [("changed", [("Licences", 5, 8)])]
    assert str(diff.changes[0].row[1]) == "c-s-bp2"


def test_unused_trial_licences_are_not_waste_in_the_check_or_the_report(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, users=ld.USERS, skus=TRIAL_SKUS, subscriptions=TRIAL_SUBSCRIPTIONS)
    _price(db, "SPB", 20.0)
    _price(db, "Microsoft_365_Copilot", 25.0)
    result = _results(db, alpha)["unassigned_licences"]
    assert not any("Business Premium" in f.subject or "Copilot" in f.subject for f in result.findings)

    report = data_reports.build(db, store.get_tenant(db, alpha), "licences")
    section = next(s for s in report.sections if s.title == "Waste by subscription")
    rows = {str(r[0]): r for r in section.rows}
    assert rows["Microsoft 365 Business Premium"][3] == 0  # Unassigned: the 5 spare are the trial's
    assert rows["Microsoft 365 Copilot"][3] == 0
