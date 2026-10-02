"""Run every collector for a tenant against recorded-style Graph responses."""

import re

import responses

from officesentry import store
from officesentry.collectors import REGISTRY
from officesentry.graph import GRAPH_ROOT, GraphClient
from officesentry.worker import execute_run
from tests import graphdata as gd
from tests.test_collectors import SKUS, USERS

V1 = f"{GRAPH_ROOT}/v1.0"


def mock_tenant(rsps, *, users=USERS, skus=SKUS, ca=None, defaults=False, registration=None):
    rsps.get(f"{V1}/organization", json={"value": [{"id": "org", "displayName": "Org"}]})
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


def seed_tenant(db, tenant_id, **kwargs):
    factory = lambda tenant, connection: GraphClient(lambda: "t", sleep=lambda s: None)  # noqa: E731
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        mock_tenant(rsps, **kwargs)
        for key in REGISTRY:
            store.enqueue_run(db, tenant_id, key)
        while (run := store.claim_next_run(db)) is not None:
            assert execute_run(db, run, factory) in ("succeeded", "partial")
