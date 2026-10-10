from datetime import UTC, datetime

import pytest

from officesentry import store
from officesentry.licences import licence_summary
from officesentry.rules import evaluate
from officesentry.snapshot import TenantSnapshot
from tests import graphdata as gd
from tests.seed import seed_tenant
from tests.test_collectors import SKUS

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def results(db, tenant_id):
    return {r.key: r for r in evaluate(TenantSnapshot(db, store.get_tenant(db, tenant_id)), NOW)}


def test_everything_unavailable_before_collection(db, two_tenants):
    res = results(db, two_tenants[0])
    assert {r.status for r in res.values()} == {"unavailable"}


@pytest.fixture
def seeded(db, two_tenants):
    seed_tenant(db, two_tenants[0])
    return results(db, two_tenants[0])


def test_mfa_enforced_with_exclusion(seeded):
    r = seeded["mfa_enforced"]
    assert r.status == "fail"
    assert [(f.subject, f.severity) for f in r.findings] == [("bg@x.com", "medium")]


def test_mfa_not_enforced_without_policy(db, two_tenants):
    seed_tenant(db, two_tenants[0], ca=[gd.CA_REPORT_ONLY])
    r = results(db, two_tenants[0])["mfa_enforced"]
    assert r.status == "fail" and r.findings[0].severity == "high"
    assert "report-only" in r.findings[0].detail


def test_security_defaults_pass_mfa_and_legacy(db, two_tenants):
    seed_tenant(db, two_tenants[0], ca=[], defaults=True)
    res = results(db, two_tenants[0])
    assert res["mfa_enforced"].status == "pass"
    assert res["legacy_auth_blocked"].status == "pass"


def test_legacy_auth(seeded, db, two_tenants):
    assert seeded["legacy_auth_blocked"].status == "fail"
    seed_tenant(db, two_tenants[1], ca=[gd.CA_MFA_ALL, gd.CA_BLOCK_LEGACY])
    assert results(db, two_tenants[1])["legacy_auth_blocked"].status == "pass"


def _excluding(policy: dict, *apps: str) -> dict:
    cond = policy["conditions"]
    apps_ = {**cond["applications"], "excludeApplications": list(apps)}
    return {**policy, "conditions": {**cond, "applications": apps_}}


def test_policies_that_exclude_apps_dont_count_as_everywhere(db, two_tenants):
    from officesentry.reports.evidence import build as evidence

    exchange = "00000002-0000-0ff1-ce00-000000000000"
    seed_tenant(db, two_tenants[0], ca=[_excluding(gd.CA_MFA_ALL, exchange), _excluding(gd.CA_BLOCK_LEGACY, exchange)])
    res = results(db, two_tenants[0])
    for key in ("mfa_enforced", "legacy_auth_blocked"):
        assert res[key].status == "fail" and res[key].findings[0].severity == "high"
        assert "leaves out Exchange Online" in res[key].findings[0].detail
    pack = evidence(db, store.get_tenant(db, two_tenants[0]), NOW)
    answer = next(a for a in pack.answers if a.key == "mfa_remote")
    assert answer.answer == "No" and "leaves out Exchange Online" in answer.why

    # Leaving out Intune enrolment (Windows Autopilot) still covers every Microsoft 365 sign-in.
    seed_tenant(db, two_tenants[1], ca=[_excluding(gd.CA_MFA_ALL, "d4ebce55-015a-49b5-a083-c84d1797ae8c"),
                                        gd.CA_BLOCK_LEGACY])
    res = results(db, two_tenants[1])
    assert res["mfa_enforced"].findings[0].subject == "bg@x.com" and res["legacy_auth_blocked"].status == "pass"


def test_admin_mfa_flags_unregistered_admin(seeded):
    r = seeded["admin_mfa"]
    assert r.status == "fail"
    critical = [f.subject for f in r.findings if f.severity == "critical"]
    assert critical == ["bg@x.com"]
    # u3 (guest Exchange admin) has no registration row: unknown, not critical.
    assert any(f.subject == "g#EXT#@x.com" and f.severity == "low" for f in r.findings)


def test_admin_shape_rules(seeded):
    assert seeded["global_admin_count"].status == "pass"  # u1 + u4 (u5 is only eligible)
    assert [f.subject for f in seeded["guest_admins"].findings] == ["g#EXT#@x.com"]
    assert [f.subject for f in seeded["service_principal_admins"].findings] == ["Some app"]


def test_users_mfa_registered(seeded):
    r = seeded["users_mfa_registered"]
    assert {(f.subject, f.severity) for f in r.findings} == {("bg@x.com", "high"), ("carl@x.com", "medium")}


def test_stale_users(seeded):
    r = seeded["stale_users"]
    # Ann signed in two days ago; Guest has activity data but no sign-in and no created date.
    assert [f.subject for f in r.findings] == ["g#EXT#@x.com"]


def test_stale_users_unavailable_without_p1(db, two_tenants):
    no_p1 = [dict(s, servicePlans=[p for p in s["servicePlans"] if p["servicePlanName"] != "AAD_PREMIUM"])
             for s in SKUS]
    seed_tenant(db, two_tenants[0], skus=no_p1)
    r = results(db, two_tenants[0])["stale_users"]
    assert r.status == "unavailable" and "P1" in r.summary


def test_licensed_disabled(seeded):
    assert [f.subject for f in seeded["licensed_disabled"].findings] == ["bob@x.com"]


def test_unassigned_licences_with_price(db, two_tenants):
    db.execute("INSERT INTO sku_prices VALUES ('SPB', 18.1, '2026-10-01T00:00:00Z')")
    seed_tenant(db, two_tenants[0])
    r = results(db, two_tenants[0])["unassigned_licences"]
    assert r.status == "fail"
    assert r.findings[0].subject == "Microsoft 365 Business Premium"
    assert "3 of 10 unassigned, 54.30 GBP/month" in r.findings[0].detail


def test_licence_summary_counts_waste():
    users = [
        {"accountEnabled": False, "skuIds": ["sku-bp"]},
        {"accountEnabled": True, "skuIds": ["sku-bp"], "signInActivityAvailable": True,
         "lastSignInDateTime": "2026-01-01T00:00:00Z"},
        {"accountEnabled": True, "skuIds": ["sku-bp"], "signInActivityAvailable": True,
         "lastSignInDateTime": "2026-10-01T00:00:00Z"},
    ]
    free = {"skuId": "free", "skuPartNumber": "FLOW_FREE", "appliesTo": "User", "prepaidUnits": {"enabled": 10000},
            "consumedUnits": 3}
    summary = licence_summary(SKUS + [free], users, {"SPB": 20.0}, NOW)
    bp = next(line for line in summary.lines if line.part_number == "SPB")
    assert (bp.unassigned, bp.on_disabled_users, bp.on_stale_users) == (3, 1, 1)
    assert bp.monthly_waste == 100.0
    assert summary.monthly_waste == 100.0
    assert next(line for line in summary.lines if line.part_number == "FLOW_FREE").wasted_units == 0


def test_without_p1_advice_is_security_defaults(db, two_tenants):
    no_p1 = [dict(s, servicePlans=[p for p in s["servicePlans"] if not p["servicePlanName"].startswith("AAD_PREMIUM")])
             for s in SKUS]
    seed_tenant(db, two_tenants[0], skus=no_p1)
    res = results(db, two_tenants[0])
    for key in ("mfa_enforced", "legacy_auth_blocked"):
        assert res[key].status == "fail"
        assert "Security Defaults" in res[key].recommendation and "no Entra ID P1" in res[key].recommendation


def test_stale_users_label_covers_never_used_accounts(seeded):
    assert "never used" in seeded["stale_users"].summary
