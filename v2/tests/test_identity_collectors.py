import responses

from officesentry import store
from tests import graphdata as gd
from tests.test_collectors import V1, _metrics, _run, factory  # noqa: F401


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
    m = _metrics(db, run_id)
    assert m["admins.global_admins"] == 2
    assert m["admins.privileged_principals"] == 4  # u1, u4, u3, sp1


@responses.activate
def test_admin_roles_without_pim(db, two_tenants, factory):  # noqa: F811
    alpha, _ = two_tenants
    responses.get(f"{V1}/roleManagement/directory/roleDefinitions", json={"value": gd.ROLE_DEFINITIONS})
    responses.get(f"{V1}/roleManagement/directory/roleAssignments", json={"value": gd.ROLE_ASSIGNMENTS})
    responses.get(f"{V1}/roleManagement/directory/roleEligibilitySchedules", status=403, json={"error": {
        "code": "AadPremiumLicenseRequired", "message": "The tenant needs an AAD Premium 2 license."}})
    _, status = _run(db, alpha, "admin_roles", factory)
    assert status == "succeeded"


@responses.activate
def test_conditional_access(db, two_tenants, factory):  # noqa: F811
    alpha, _ = two_tenants
    responses.get(f"{V1}/policies/identitySecurityDefaultsEnforcementPolicy", json={"isEnabled": False})
    responses.get(f"{V1}/identity/conditionalAccess/policies",
                  json={"value": [gd.CA_MFA_ALL, gd.CA_BLOCK_LEGACY, gd.CA_REPORT_ONLY]})
    run_id, status = _run(db, alpha, "conditional_access", factory)
    assert status == "succeeded"
    m = _metrics(db, run_id)
    assert (m["ca.policies_total"], m["ca.policies_enabled"], m["ca.policies_report_only"]) == (3, 2, 1)


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
    responses.get(f"{V1}/reports/authenticationMethods/userRegistrationDetails",
                  json={"value": gd.REGISTRATION_DETAILS})
    run_id, status = _run(db, alpha, "mfa_registration", factory)
    assert status == "succeeded"
    m = _metrics(db, run_id)
    assert (m["mfa.registered"], m["mfa.not_registered"], m["mfa.registered_pct"]) == (1, 2, 33.3)


@responses.activate
def test_mfa_falls_back_to_per_user_methods(db, two_tenants, factory):  # noqa: F811
    alpha, _ = two_tenants
    responses.get(f"{V1}/reports/authenticationMethods/userRegistrationDetails", status=403, json={"error": {
        "code": "Authentication_RequestFromNonPremiumTenantOrB2CTenant", "message": "premium license"}})
    responses.get(f"{V1}/users", json={"value": [
        {"id": "u1", "userPrincipalName": "ann@x.com", "userType": "Member", "accountEnabled": True},
        {"id": "u6", "userPrincipalName": "carl@x.com", "userType": "Member", "accountEnabled": True},
        {"id": "u9", "userPrincipalName": "old@x.com", "userType": "Member", "accountEnabled": False},
    ]})
    responses.get(f"{V1}/users/u1/authentication/methods", json={"value": [
        {"@odata.type": "#microsoft.graph.passwordAuthenticationMethod"},
        {"@odata.type": "#microsoft.graph.microsoftAuthenticatorAuthenticationMethod"}]})
    responses.get(f"{V1}/users/u6/authentication/methods", json={"value": [
        {"@odata.type": "#microsoft.graph.passwordAuthenticationMethod"},
        {"@odata.type": "#microsoft.graph.emailAuthenticationMethod"}]})
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
