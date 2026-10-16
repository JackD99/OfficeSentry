"""Trusted organisations and locations: cross-tenant access settings and Conditional Access named locations,
with the policies that use each location."""

from datetime import UTC, datetime

from officesentry import store
from officesentry.reports import data_reports
from officesentry.reports.trusted_access import describe
from officesentry.snapshot import TenantSnapshot
from tests import graphdata as gd
from tests import trustdata as trust
from tests.conftest import login, make_user
from tests.seed import seed_tenant

CA = [gd.CA_MFA_ALL, trust.CA_MFA_NOT_TRUSTED, trust.CA_BLOCK_ABROAD]


def _report(db, tenant_id):
    return data_reports.build(db, store.get_tenant(db, tenant_id), "trusted-access", datetime.now(UTC))


def _table(report, title) -> dict[str, dict]:
    section = next(s for s in report.sections if s.title == title)
    return {str(row[0]): dict(zip(section.columns, row, strict=True)) for row in section.rows}


def _flags(report, title) -> dict[str, str | None]:
    section = next(s for s in report.sections if s.title == title)
    return {str(row[0]): flag for row, flag in zip(section.rows, section.flags, strict=True)}


def test_collectors_keep_the_settings_and_locations(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, ca=CA)
    snap = TenantSnapshot(db, store.get_tenant(db, alpha))
    default = snap.objects("tenant_policies", "cross_tenant_default")[0]
    assert default["trust"]["isMfaAccepted"] is True
    assert default["b2bDirectConnectInbound"]["usersAndGroups"] == {
        "access": "blocked",
        "targets": ["AllUsers"],
        "count": 1,
    }
    partners = {p["_id"]: p for p in snap.objects("tenant_policies", "cross_tenant_partner")}
    assert partners["11111111-1111-1111-1111-111111111111"]["serviceProvider"] is True
    assert partners["22222222-2222-2222-2222-222222222222"]["trust"] is None  # left to the default
    locations = {loc["_id"]: loc for loc in snap.objects("conditional_access", "named_location")}
    assert locations["loc-wide"]["ranges"] == ["51.0.0.0/8", "2001:db8::/32"]
    assert {k: v for k, v in locations["loc-uk"].items() if not k.startswith("_")} == {
        "name": "United Kingdom",
        "kind": "countries",
        "created": "2025-03-01T09:00:00Z",
        "modified": "2025-03-01T09:00:00Z",
        "trusted": False,
        "countries": ["GB"],
        "unknown": False,
        "lookup": "clientIpAddress",
    }


def _side(users, apps, *, user_targets=("AllUsers",), app_targets=("AllApplications",)) -> dict:
    return {
        "usersAndGroups": {"access": users, "targets": list(user_targets), "count": len(user_targets)},
        "applications": {"access": apps, "targets": list(app_targets), "count": len(app_targets)},
    }


def test_b2b_settings_in_words():
    assert describe(None) == "Default"
    assert describe(_side("allowed", "allowed")) == "Allowed for all users, all apps"
    assert describe(_side("blocked", "blocked")) == "Blocked"
    two_groups = _side("allowed", "allowed", user_targets=("g-a", "g-b"), app_targets=("Office365",))
    assert describe(two_groups) == "Allowed for 2 users or groups, Office 365"
    mixed = _side("blocked", "allowed", user_targets=("g-a",))
    assert describe(mixed) == "Users: blocked for 1 user or group; Apps: allowed for all apps"


def test_report_flags_outside_mfa_trust_and_wide_trusted_ranges(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha, ca=CA)
    report = _report(db, alpha)
    assert [s.title for s in report.sections] == [
        "Default for other organisations",
        "Organisations with their own settings",
        "Named locations",
    ]
    defaults = _table(report, "Default for other organisations")
    assert defaults["B2B collaboration"]["Their people here (inbound)"] == "Allowed for all users, all apps"
    assert defaults["B2B direct connect"]["Their people here (inbound)"] == "Blocked"
    assert _flags(report, "Default for other organisations") == {
        "Trust their MFA": "medium",
        "B2B collaboration": None,
        "B2B direct connect": None,
        "Trust their compliant devices": None,
        "Trust their hybrid joined devices": None,
    }

    orgs = _table(report, "Organisations with their own settings")
    msp = orgs["11111111-1111-1111-1111-111111111111"]
    assert (msp["Relationship"], msp["Trusts their MFA"], msp["Trusts their devices"]) == (
        "Service provider",
        "Yes",
        "Compliant",
    )
    assert msp["B2B collaboration in"] == "Default" and msp["Invitations redeemed automatically"] == "Inbound"
    partner = orgs["22222222-2222-2222-2222-222222222222"]
    assert partner["Direct connect in"] == "Allowed for 2 users or groups, Office 365"
    assert (partner["B2B collaboration out"], partner["Trusts their MFA"]) == ("Blocked", "Default")

    places = _table(report, "Named locations")
    assert list(places) == ["Head office", "Old VPN", "Test lab", "United Kingdom"]
    assert places["Head office"]["Used by"] == "Left out of MFA outside the office"
    assert places["United Kingdom"]["Used by"] == "Left out of Block outside the UK (report-only)"
    assert places["United Kingdom"]["Ranges or countries"] == "GB"
    assert places["Test lab"]["Used by"] == "Not used"
    # A /8 of public addresses, trusted: flagged. 10.0.0.0/8 is private, and the lab isn't trusted anyway.
    assert _flags(report, "Named locations") == {
        "Old VPN": "medium",
        "Head office": None,
        "Test lab": None,
        "United Kingdom": None,
    }

    tiles = {t.label: t.value for t in report.tiles}
    assert tiles == {
        "Organisations with own settings": 2,
        "Outside MFA trusted": "Yes",
        "Trusted locations": 2,
        "Locations not used": 1,
    }

    make_user(db, "admin")
    login(client, "admin")
    for ext in ("", ".csv", ".xlsx"):
        assert client.get(f"/tenants/{alpha}/data/trusted-access{ext}").status_code == 200


def test_everywhere_trusted_is_high(db, two_tenants):
    alpha, _ = two_tenants
    everywhere = {**trust.NAMED_LOCATIONS[0], "id": "loc-all", "displayName": "Anywhere"}
    everywhere["ipRanges"] = [{"@odata.type": "#microsoft.graph.iPv4CidrRange", "cidrAddress": "0.0.0.0/0"}]
    seed_tenant(db, alpha, ca=CA, locations=[everywhere])
    assert _flags(_report(db, alpha), "Named locations") == {"Anywhere": "high"}


def test_without_entra_p1_the_locations_say_why(db, two_tenants):
    """No Entra ID P1: no Conditional Access, so no named locations; the cross-tenant settings still show."""
    alpha, _ = two_tenants
    seed_tenant(db, alpha, skus=[])
    report = _report(db, alpha)
    assert not report.unavailable
    section = next(s for s in report.sections if s.title == "Named locations")
    assert not section.rows and section.empty.endswith("which needs Entra ID P1.")
    assert [t.label for t in report.tiles] == ["Organisations with own settings", "Outside MFA trusted"]
