import json
from urllib.parse import parse_qs, urlsplit

import responses

from officesentry import store
from officesentry.snapshot import TenantSnapshot
from tests import graphdata as gd
from tests import trustdata as trust
from tests.test_collectors import V1, _metrics, _run, factory  # noqa: F401

GROUP_ELIGIBILITY = f"{V1}/identityGovernance/privilegedAccess/group/eligibilitySchedules"


def mock_group_eligibility(by_group: dict[str, list[dict]], status: int = 200) -> None:
    """PIM for groups: who can activate membership (or ownership) of each group, answered by the ``groupId`` in
    the request's ``$filter`` as Microsoft does."""

    def callback(request):
        if status != 200:
            body = {"error": {"code": "Forbidden", "message": "Missing role permissions on the request."}}
            return status, {}, json.dumps(body)
        group = parse_qs(urlsplit(request.url).query)["$filter"][0].split("'")[1]
        return 200, {}, json.dumps({"value": by_group.get(group, [])})

    responses.add_callback(responses.GET, GROUP_ELIGIBILITY, callback=callback, content_type="application/json")


def eligible(sid: str, group: str, uid: str, name: str, upn: str, access: str = "member") -> dict:
    return {
        "id": sid,
        "groupId": group,
        "principalId": uid,
        "accessId": access,
        "memberType": "direct",
        "principal": {"@odata.type": "#microsoft.graph.user", "id": uid, "displayName": name, "userPrincipalName": upn},
    }


@responses.activate
def test_admin_roles_active_and_eligible(db, two_tenants, factory):  # noqa: F811
    alpha, _ = two_tenants
    responses.get(f"{V1}/roleManagement/directory/roleDefinitions", json={"value": gd.ROLE_DEFINITIONS})
    responses.get(f"{V1}/roleManagement/directory/roleAssignments", json={"value": gd.ROLE_ASSIGNMENTS})
    responses.get(f"{V1}/roleManagement/directory/roleEligibilitySchedules", json={"value": gd.ROLE_ELIGIBILITY})
    run_id, status = _run(db, alpha, "admin_roles", factory)
    assert status == "succeeded"
    rows = {r["_id"]: r for r in store.run_objects(db, run_id, "role_assignment")}
    assert rows["a1"]["privileged"] and rows["a1"]["principalType"] == "user"
    assert not rows["a4"]["privileged"]
    assert rows["eligible:e1"]["assignment"] == "eligible"
    # isPrivileged only exists in beta; selecting it on v1.0 is a 400 from Graph.
    assert "isPrivileged" not in responses.calls[0].request.url
    m = _metrics(db, run_id)
    assert m["admins.global_admins"] == 2
    assert m["admins.privileged_principals"] == 4  # u1, u4, u3, sp1


@responses.activate
def test_admin_roles_without_pim(db, two_tenants, factory):  # noqa: F811
    alpha, _ = two_tenants
    responses.get(f"{V1}/roleManagement/directory/roleDefinitions", json={"value": gd.ROLE_DEFINITIONS})
    responses.get(f"{V1}/roleManagement/directory/roleAssignments", json={"value": gd.ROLE_ASSIGNMENTS})
    responses.get(
        f"{V1}/roleManagement/directory/roleEligibilitySchedules",
        status=403,
        json={"error": {"code": "AadPremiumLicenseRequired", "message": "The tenant needs an AAD Premium 2 license."}},
    )
    _, status = _run(db, alpha, "admin_roles", factory)
    assert status == "succeeded"


@responses.activate
def test_conditional_access(db, two_tenants, factory):  # noqa: F811
    alpha, _ = two_tenants
    responses.get(f"{V1}/policies/identitySecurityDefaultsEnforcementPolicy", json={"isEnabled": False})
    responses.get(
        f"{V1}/identity/conditionalAccess/policies",
        json={"value": [gd.CA_MFA_ALL, gd.CA_BLOCK_LEGACY, gd.CA_REPORT_ONLY]},
    )
    responses.get(f"{V1}/identity/conditionalAccess/namedLocations", json={"value": trust.NAMED_LOCATIONS})
    run_id, status = _run(db, alpha, "conditional_access", factory)
    assert status == "succeeded"
    m = _metrics(db, run_id)
    assert (m["ca.policies_total"], m["ca.policies_enabled"], m["ca.policies_report_only"]) == (3, 2, 1)
    snap = TenantSnapshot(db, store.get_tenant(db, alpha))
    assert sorted(loc["name"] for loc in snap.objects("conditional_access", "named_location")) == [
        "Head office",
        "Old VPN",
        "Test lab",
        "United Kingdom",
    ]


@responses.activate
def test_conditional_access_without_named_locations_is_partial(db, two_tenants, factory):  # noqa: F811
    """The policies are kept when the named locations can't be read, and the run says something is missing."""
    alpha, _ = two_tenants
    responses.get(f"{V1}/policies/identitySecurityDefaultsEnforcementPolicy", json={"isEnabled": False})
    responses.get(f"{V1}/identity/conditionalAccess/policies", json={"value": [gd.CA_MFA_ALL]})
    responses.get(
        f"{V1}/identity/conditionalAccess/namedLocations",
        status=500,
        json={"error": {"code": "InternalServerError", "message": "Something went wrong."}},
    )
    run_id, status = _run(db, alpha, "conditional_access", factory)
    assert status == "partial"
    snap = TenantSnapshot(db, store.get_tenant(db, alpha))
    assert len(snap.objects("conditional_access", "ca_policy")) == 1
    assert snap.objects("conditional_access", "named_location_status")[0]["state"] == "failed"


@responses.activate
def test_conditional_access_skipped_without_p1(db, two_tenants, factory):  # noqa: F811
    alpha, _ = two_tenants
    store.set_capabilities(db, alpha, {"entra_p1": False})
    responses.get(f"{V1}/policies/identitySecurityDefaultsEnforcementPolicy", json={"isEnabled": True})
    run_id, status = _run(db, alpha, "conditional_access", factory)
    assert status == "succeeded"
    assert len(responses.calls) == 1
    assert _metrics(db, run_id)["ca.security_defaults_enabled"] == 1


@responses.activate
def test_mfa_registration_report(db, two_tenants, factory):  # noqa: F811
    alpha, _ = two_tenants
    responses.get(
        f"{V1}/reports/authenticationMethods/userRegistrationDetails", json={"value": gd.REGISTRATION_DETAILS}
    )
    run_id, status = _run(db, alpha, "mfa_registration", factory)
    assert status == "succeeded"
    m = _metrics(db, run_id)
    assert (m["mfa.registered"], m["mfa.not_registered"], m["mfa.registered_pct"]) == (1, 2, 33.3)


@responses.activate
def test_mfa_falls_back_to_per_user_methods(db, two_tenants, factory):  # noqa: F811
    alpha, _ = two_tenants
    responses.get(
        f"{V1}/reports/authenticationMethods/userRegistrationDetails",
        status=403,
        json={"error": {"code": "Authentication_RequestFromNonPremiumTenantOrB2CTenant", "message": "premium license"}},
    )
    responses.get(
        f"{V1}/users",
        json={
            "value": [
                {"id": "u1", "userPrincipalName": "ann@x.com", "userType": "Member", "accountEnabled": True},
                {"id": "u6", "userPrincipalName": "carl@x.com", "userType": "Member", "accountEnabled": True},
                {"id": "u9", "userPrincipalName": "old@x.com", "userType": "Member", "accountEnabled": False},
            ]
        },
    )
    responses.get(
        f"{V1}/users/u1/authentication/methods",
        json={
            "value": [
                {"@odata.type": "#microsoft.graph.passwordAuthenticationMethod"},
                {"@odata.type": "#microsoft.graph.microsoftAuthenticatorAuthenticationMethod"},
            ]
        },
    )
    responses.get(
        f"{V1}/users/u6/authentication/methods",
        json={
            "value": [
                {"@odata.type": "#microsoft.graph.passwordAuthenticationMethod"},
                {"@odata.type": "#microsoft.graph.emailAuthenticationMethod"},
            ]
        },
    )
    run_id, status = _run(db, alpha, "mfa_registration", factory)
    assert status == "succeeded"
    rows = {r["_id"]: r for r in store.run_objects(db, run_id, "mfa_registration")}
    assert set(rows) == {"u1", "u6"}  # disabled users skipped
    assert rows["u1"]["isMfaRegistered"] and not rows["u6"]["isMfaRegistered"]
    assert rows["u1"]["source"] == "per_user_methods"


@responses.activate
def test_secure_score(db, two_tenants, factory):  # noqa: F811
    alpha, _ = two_tenants
    responses.get(f"{V1}/security/secureScores", json={"value": gd.SECURE_SCORES})
    run_id, status = _run(db, alpha, "secure_score", factory)
    assert status == "succeeded"
    assert _metrics(db, run_id) == {"secure_score.current": 42, "secure_score.max": 100, "secure_score.pct": 42}
    assert len(store.run_objects(db, run_id, "secure_score_daily")) == 2


ROLE_GROUP = {
    "id": "a-grp",
    "roleDefinitionId": gd.GA,
    "principalId": "rg1",
    "directoryScopeId": "/",
    "principal": {"@odata.type": "#microsoft.graph.group", "id": "rg1", "displayName": "Tenant admins"},
}
ROLE_GROUP_ELIGIBLE = {
    "id": "e-grp",
    "roleDefinitionId": gd.EXCH,
    "principalId": "rg2",
    "directoryScopeId": "/",
    "principal": {"@odata.type": "#microsoft.graph.group", "id": "rg2", "displayName": "Mail admins"},
}


@responses.activate
def test_admin_roles_held_through_a_group(db, two_tenants, factory):  # noqa: F811
    """Members of a role-assignable group hold its roles, active or eligible, and are recorded with the group."""
    alpha, _ = two_tenants
    responses.get(f"{V1}/roleManagement/directory/roleDefinitions", json={"value": gd.ROLE_DEFINITIONS})
    responses.get(f"{V1}/roleManagement/directory/roleAssignments", json={"value": [*gd.ROLE_ASSIGNMENTS, ROLE_GROUP]})
    responses.get(
        f"{V1}/roleManagement/directory/roleEligibilitySchedules",
        json={"value": [*gd.ROLE_ELIGIBILITY, ROLE_GROUP_ELIGIBLE]},
    )
    responses.get(
        f"{V1}/groups/rg1/members",
        json={
            "value": [
                {
                    "@odata.type": "#microsoft.graph.user",
                    "id": "u9",
                    "displayName": "Hidden admin",
                    "userPrincipalName": "hidden@x.com",
                    "accountEnabled": True,
                    "userType": "Member",
                },
                {
                    "@odata.type": "#microsoft.graph.user",
                    "id": "u10",
                    "displayName": "Left",
                    "userPrincipalName": "left@x.com",
                    "accountEnabled": False,
                    "userType": "Member",
                },
            ]
        },
    )
    responses.get(
        f"{V1}/groups/rg2/members",
        json={"value": [{"@odata.type": "#microsoft.graph.user", "id": "u1", "userPrincipalName": "ann@x.com"}]},
    )
    mock_group_eligibility(
        {
            "rg1": [
                eligible("s1", "rg1", "u11", "Standby admin", "standby@x.com"),
                eligible("s2", "rg1", "u12", "Group owner", "owner@x.com", access="owner"),  # owns, isn't a member
            ]
        }
    )
    run_id, status = _run(db, alpha, "admin_roles", factory)
    assert status == "succeeded"
    rows = {r["_id"]: r for r in store.run_objects(db, run_id, "role_assignment")}
    standby = rows["a-grp:eligible-member:u11"]
    assert (standby["principalId"], standby["userPrincipalName"], standby["roleName"]) == (
        "u11",
        "standby@x.com",
        "Global Administrator",
    )
    # Active through the group once they activate membership, so eligible until then.
    assert (standby["assignment"], standby["viaGroup"], standby["viaPim"]) == ("eligible", "Tenant admins", True)
    assert not any(r.endswith(":u12") for r in rows)
    assert {r["state"] for r in store.run_objects(db, run_id, "pim_groups_status")} == {"read"}
    asked = [c.request.url for c in responses.calls if c.request.url.startswith(GROUP_ELIGIBILITY)]
    assert len(asked) == 2 and all("%24expand=principal" in u for u in asked)  # one call per group holding a role
    hidden = rows["a-grp:member:u9"]
    assert (hidden["principalId"], hidden["principalType"], hidden["roleName"]) == (
        "u9",
        "user",
        "Global Administrator",
    )
    assert (hidden["assignment"], hidden["viaGroup"], hidden["viaGroupId"]) == ("active", "Tenant admins", "rg1")
    assert rows["eligible:e-grp:member:u1"]["assignment"] == "eligible"
    assert rows["a-grp"]["principalType"] == "group"  # the group's own row stays
    assert all("%24select=" in c.request.url for c in responses.calls if "/members" in c.request.url)
    m = _metrics(db, run_id)
    assert m["admins.global_admins"] == 3  # u1, u4 and u9; not the group, not the disabled u10, not eligible u11
    assert m["admins.privileged_principals"] == 6  # u1, u4, u3, sp1, u9, u10


@responses.activate
def test_pim_for_groups_waits_for_approval(db, two_tenants, factory):  # noqa: F811
    """Until the client approves the new permission, Microsoft refuses the eligibility list: the run still
    succeeds, with the members it could read, and the state says it's waiting."""
    alpha, _ = two_tenants
    responses.get(f"{V1}/roleManagement/directory/roleDefinitions", json={"value": gd.ROLE_DEFINITIONS})
    responses.get(f"{V1}/roleManagement/directory/roleAssignments", json={"value": [ROLE_GROUP]})
    responses.get(f"{V1}/roleManagement/directory/roleEligibilitySchedules", json={"value": []})
    responses.get(f"{V1}/groups/rg1/members", json={"value": []})
    mock_group_eligibility({}, status=403)
    run_id, status = _run(db, alpha, "admin_roles", factory)
    assert status == "succeeded"
    assert [r["state"] for r in store.run_objects(db, run_id, "pim_groups_status")] == ["waiting"]


@responses.activate
def test_pim_for_groups_needs_p2(db, two_tenants, factory):  # noqa: F811
    alpha, _ = two_tenants
    store.set_capabilities(db, alpha, {"entra_p2": False})
    responses.get(f"{V1}/roleManagement/directory/roleDefinitions", json={"value": gd.ROLE_DEFINITIONS})
    responses.get(f"{V1}/roleManagement/directory/roleAssignments", json={"value": [ROLE_GROUP]})
    responses.get(f"{V1}/roleManagement/directory/roleEligibilitySchedules", json={"value": []})
    responses.get(f"{V1}/groups/rg1/members", json={"value": []})
    run_id, status = _run(db, alpha, "admin_roles", factory)
    assert status == "succeeded"
    assert [r["state"] for r in store.run_objects(db, run_id, "pim_groups_status")] == ["unlicensed"]
    assert not any(c.request.url.startswith(GROUP_ELIGIBILITY) for c in responses.calls)


@responses.activate
def test_admin_role_group_unreadable_makes_the_run_partial(db, two_tenants, factory):  # noqa: F811
    alpha, _ = two_tenants
    responses.get(f"{V1}/roleManagement/directory/roleDefinitions", json={"value": gd.ROLE_DEFINITIONS})
    responses.get(f"{V1}/roleManagement/directory/roleAssignments", json={"value": [ROLE_GROUP]})
    responses.get(f"{V1}/roleManagement/directory/roleEligibilitySchedules", json={"value": []})
    responses.get(
        f"{V1}/groups/rg1/members",
        status=404,
        json={"error": {"code": "Request_ResourceNotFound", "message": "Resource 'rg1' does not exist."}},
    )
    mock_group_eligibility({})
    run_id, status = _run(db, alpha, "admin_roles", factory)
    assert status == "partial"
    assert [r["_id"] for r in store.run_objects(db, run_id, "role_assignment")] == ["a-grp"]
