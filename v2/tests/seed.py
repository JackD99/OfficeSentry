"""Run every collector for a tenant against recorded-style Graph responses."""

import json
import re
from unittest.mock import patch

import responses

from officesentry import store
from officesentry.collectors import REGISTRY
from officesentry.exchange import EXCHANGE_ROOT, ExchangeClient
from officesentry.graph import GRAPH_ROOT, GraphClient
from officesentry.worker import execute_run
from tests import devicedata as dd
from tests import graphdata as gd
from tests.test_collectors import SKUS, USERS

V1 = f"{GRAPH_ROOT}/v1.0"


def exchange_callback(data):
    def callback(request):
        body = json.loads(request.body)["CmdletInput"]
        name, params = body["CmdletName"], body.get("Parameters") or {}
        result = data.get(name, [])
        if isinstance(result, dict):  # keyed by Identity
            result = result.get(params.get("Identity"), [])
        return 200, {}, json.dumps({"value": result})
    return callback


def mock_exchange(rsps, data=None):
    rsps.add_callback(responses.POST, re.compile(re.escape(EXCHANGE_ROOT) + r"/adminapi/beta/[^/]+/InvokeCommand"),
                      callback=exchange_callback(gd.EXCHANGE if data is None else data),
                      content_type="application/json")


def mock_phase4(rsps, *, exchange=None, inbox_rules=None, sharepoint=None, auth_policy=None):
    rsps.get(f"{V1}/policies/authorizationPolicy", json=gd.AUTHORIZATION_POLICY if auth_policy is None else auth_policy)
    rsps.get(f"{V1}/admin/sharepoint/settings", json=gd.SHAREPOINT_SETTINGS if sharepoint is None else sharepoint)
    rsps.get(f"{V1}/servicePrincipals", json={"value": gd.SERVICE_PRINCIPALS})
    rsps.get(f"{V1}/oauth2PermissionGrants", json={"value": gd.DELEGATED_GRANTS})
    rsps.get(f"{V1}/applications", json={"value": gd.APPLICATIONS})

    def assignments(request):
        sid = request.path_url.split("/")[3]
        return 200, {}, json.dumps({"value": gd.APP_ROLE_ASSIGNMENTS.get(sid, [])})
    rsps.add_callback(responses.GET, re.compile(re.escape(V1) + r"/servicePrincipals/[^/?]+/appRoleAssignments"),
                      callback=assignments, content_type="application/json")
    rsps.get(re.compile(re.escape(V1) + r"/servicePrincipals/[^/?]+(\?.*)?$"), json=gd.GRAPH_APP_ROLES)

    rules = gd.INBOX_RULES if inbox_rules is None else inbox_rules

    def messages(request):
        uid = request.path_url.split("/")[3]
        return 200, {}, json.dumps({"value": rules.get(uid, [])})
    rsps.add_callback(responses.GET, re.compile(re.escape(V1) + r"/users/[^/]+/mailFolders/inbox/messageRules"),
                      callback=messages, content_type="application/json")

    def folder(request):
        fid = request.path_url.split("?")[0].split("/")[5]
        return 200, {}, json.dumps(gd.FOLDERS.get(fid, {"displayName": fid}))
    rsps.add_callback(responses.GET, re.compile(re.escape(V1) + r"/users/[^/]+/mailFolders/(?!inbox)[^/?]+"),
                      callback=folder, content_type="application/json")
    if exchange is not False:
        mock_exchange(rsps, exchange)


def mock_tenant(rsps, *, users=USERS, skus=SKUS, ca=None, defaults=False, registration=None, **phase4):
    rsps.get(f"{V1}/organization", json={"value": [gd.ORGANIZATION]})
    rsps.get(f"{V1}/subscribedSkus", json={"value": skus})
    rsps.get(f"{V1}/users", json={"value": users})
    rsps.get(f"{V1}/roleManagement/directory/roleDefinitions", json={"value": gd.ROLE_DEFINITIONS})
    rsps.get(f"{V1}/roleManagement/directory/roleAssignments", json={"value": gd.ROLE_ASSIGNMENTS})
    rsps.get(f"{V1}/roleManagement/directory/roleEligibilitySchedules", json={"value": gd.ROLE_ELIGIBILITY})
    rsps.get(f"{V1}/policies/identitySecurityDefaultsEnforcementPolicy", json={"isEnabled": defaults})
    rsps.get(f"{V1}/identity/conditionalAccess/policies",
             json={"value": [gd.CA_MFA_ALL, gd.CA_REPORT_ONLY] if ca is None else ca})
    rsps.get(f"{V1}/reports/authenticationMethods/userRegistrationDetails",
             json={"value": gd.REGISTRATION_DETAILS if registration is None else registration})
    rsps.get(f"{V1}/security/secureScores", json={"value": gd.SECURE_SCORES})
    rsps.get(re.compile(re.escape(V1) + r"/users/[^/]+/authentication/methods"), json={"value": []})
    rsps.get(re.compile(re.escape(V1) + r"/groups/[^/?]+(\?.*)?$"), json={"displayName": "Break glass"})
    rsps.get(re.compile(re.escape(V1) + r"/groups/[^/]+/transitiveMembers"),
             json={"value": [{"@odata.type": "#microsoft.graph.user", "id": "u4"}]})
    rsps.get(f"{V1}/devices", json={"value": dd.ENTRA_DEVICES})
    rsps.get(f"{V1}/deviceManagement/managedDevices", json={"value": dd.MANAGED_DEVICES})
    mock_phase4(rsps, **phase4)


def exchange_factory(tenant, connection):
    return ExchangeClient(tenant["tenant_ref"], lambda: "t", sleep=lambda s: None)


def seed_tenant(db, tenant_id, dns=None, **kwargs):
    factory = lambda tenant, connection: GraphClient(lambda: "t", sleep=lambda s: None)  # noqa: E731
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps, \
            patch("officesentry.dnscheck.lookup", dns or gd.fake_dns):
        mock_tenant(rsps, **kwargs)
        for key in REGISTRY:
            store.enqueue_run(db, tenant_id, key)
        while (run := store.claim_next_run(db)) is not None:
            assert execute_run(db, run, factory, exchange_factory) in ("succeeded", "partial"), run["collector"]
