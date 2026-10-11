"""Run every collector for a tenant against recorded-style Graph responses."""

import json
import re
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import responses

from officesentry import store
from officesentry.collectors import REGISTRY
from officesentry.exchange import EXCHANGE_ROOT, ExchangeClient
from officesentry.graph import GRAPH_ROOT, GraphClient
from officesentry.worker import execute_run
from tests import adminactivitydata as ad
from tests import devicedata as dd
from tests import exchangedata as xd
from tests import graphdata as gd
from tests import groupdata as grp
from tests import hybriddata as hd
from tests import licencedata as ld
from tests import tenantsettingsdata as tsd
from tests.sharepoint_data import mock_sharepoint
from tests.teamsdata import mock_teams
from tests.test_collectors import SKUS, USERS
from tests.usagedata import mock_usage

V1 = f"{GRAPH_ROOT}/v1.0"
BETA = f"{GRAPH_ROOT}/beta"


def exchange_callback(data):
    def callback(request):
        body = json.loads(request.body)["CmdletInput"]
        name, params = body["CmdletName"], body.get("Parameters") or {}
        result = data.get(name, [])
        if isinstance(result, dict):  # keyed by Identity (or Mailbox)
            result = result.get(params.get("Identity") or params.get("Mailbox"), [])
        if isinstance(result, tuple):  # (status, error body)
            return result[0], {}, json.dumps(result[1])
        return 200, {}, json.dumps({"value": result})

    return callback


def mock_exchange(rsps, data=None):
    rsps.add_callback(
        responses.POST,
        re.compile(re.escape(EXCHANGE_ROOT) + r"/adminapi/beta/[^/]+/InvokeCommand"),
        callback=exchange_callback(gd.EXCHANGE if data is None else data),
        content_type="application/json",
    )


def _filter(request, field: str) -> str | None:
    """The value in a ``$filter=<field> eq '<value>'`` query, if there is one."""
    found = re.search(field + r" eq '([^']*)'", parse_qs(urlsplit(request.url).query).get("$filter", [""])[0])
    return found.group(1) if found else None


def mock_our_app(rsps, *, found=True, missing=(), roles=None):
    """Office Sentry's own app in the tenant, for the connection collector: by default every permission is
    granted and it holds Global Reader. ``roles`` replaces its directory role assignments."""
    our_roles = gd.OUR_ROLE_ASSIGNMENTS if roles is None else roles

    def principals(request):  # /servicePrincipals, or one app by appId
        app_id = _filter(request, "appId")
        if app_id is None:
            return 200, {}, json.dumps({"value": gd.SERVICE_PRINCIPALS})
        every = gd.SERVICE_PRINCIPALS + ([gd.OUR_SP] if found else [])
        return 200, {}, json.dumps({"value": [sp for sp in every if sp["appId"] == app_id]})

    rsps.add_callback(
        responses.GET,
        re.compile(re.escape(V1) + r"/servicePrincipals(\?.*)?$"),
        callback=principals,
        content_type="application/json",
    )

    def assignments(request):
        sid = request.path_url.split("/")[3]
        ours = {gd.OUR_SP["id"]: gd.our_assignments(missing)}
        return 200, {}, json.dumps({"value": {**gd.APP_ROLE_ASSIGNMENTS, **ours}.get(sid, [])})

    rsps.add_callback(
        responses.GET,
        re.compile(re.escape(V1) + r"/servicePrincipals/[^/?]+/appRoleAssignments"),
        callback=assignments,
        content_type="application/json",
    )

    def resource(request):  # a resource app's roles, e.g. Microsoft Graph's permissions
        sid = request.path_url.split("?")[0].split("/")[3]
        return 200, {}, json.dumps(gd.RESOURCE_APP_ROLES.get(sid, gd.GRAPH_APP_ROLES))

    rsps.add_callback(
        responses.GET,
        re.compile(re.escape(V1) + r"/servicePrincipals/[^/?]+(\?.*)?$"),
        callback=resource,
        content_type="application/json",
    )

    def role_assignments(request):  # every directory role assignment, or one principal's
        principal = _filter(request, "principalId")
        if principal is None:
            return 200, {}, json.dumps({"value": gd.ROLE_ASSIGNMENTS})
        return (
            200,
            {},
            json.dumps({"value": [a for a in gd.ROLE_ASSIGNMENTS + our_roles if a["principalId"] == principal]}),
        )

    rsps.add_callback(
        responses.GET,
        re.compile(re.escape(V1) + r"/roleManagement/directory/roleAssignments(\?.*)?$"),
        callback=role_assignments,
        content_type="application/json",
    )


def mock_phase4(
    rsps, *, exchange=None, inbox_rules=None, sharepoint=None, auth_policy=None, our_app=None, auto_replies=None
):
    rsps.get(f"{V1}/policies/authorizationPolicy", json=gd.AUTHORIZATION_POLICY if auth_policy is None else auth_policy)
    rsps.get(f"{V1}/admin/sharepoint/settings", json=gd.SHAREPOINT_SETTINGS if sharepoint is None else sharepoint)
    rsps.get(f"{V1}/oauth2PermissionGrants", json={"value": gd.DELEGATED_GRANTS})
    rsps.get(f"{V1}/applications", json={"value": gd.APPLICATIONS})
    mock_our_app(rsps, **(our_app or {}))

    rules = gd.INBOX_RULES if inbox_rules is None else inbox_rules

    def messages(request):
        uid = request.path_url.split("/")[3]
        return 200, {}, json.dumps({"value": rules.get(uid, [])})

    rsps.add_callback(
        responses.GET,
        re.compile(re.escape(V1) + r"/users/[^/]+/mailFolders/inbox/messageRules"),
        callback=messages,
        content_type="application/json",
    )
    rsps.get(f"{V1}/auditLogs/signIns", json={"value": gd.LEGACY_SIGNINS})
    xd.mock_auto_replies(rsps, auto_replies)
    if exchange is not False:
        mock_exchange(rsps, exchange)


def mock_tenant(
    rsps,
    *,
    users=USERS,
    skus=SKUS,
    ca=None,
    defaults=False,
    registration=None,
    sharepoint_kw=None,
    secure_scores=None,
    per_user=None,
    usage_kw=None,
    signins=None,
    auth_methods_policy=None,
    subscriptions=None,
    groups=None,
    deleted_users=None,
    deleted_groups=None,
    risky_users=None,
    app_sign_ins=None,
    admin_units=None,
    teams_kw=None,
    **phase4,
):
    rsps.get(f"{V1}/organization", json={"value": [gd.ORGANIZATION]})
    rsps.get(f"{V1}/subscribedSkus", json={"value": skus})
    rsps.get(
        f"{V1}/directory/subscriptions", json={"value": ld.SUBSCRIPTIONS if subscriptions is None else subscriptions}
    )
    rsps.get(f"{V1}/users", json={"value": users})
    rsps.get(f"{V1}/roleManagement/directory/roleDefinitions", json={"value": gd.ROLE_DEFINITIONS})
    rsps.get(f"{V1}/roleManagement/directory/roleEligibilitySchedules", json={"value": gd.ROLE_ELIGIBILITY})
    rsps.get(f"{V1}/policies/identitySecurityDefaultsEnforcementPolicy", json={"isEnabled": defaults})
    rsps.get(
        f"{V1}/identity/conditionalAccess/policies",
        json={"value": [gd.CA_MFA_ALL, gd.CA_REPORT_ONLY] if ca is None else ca},
    )
    rsps.get(
        f"{V1}/reports/authenticationMethods/userRegistrationDetails",
        json={"value": gd.REGISTRATION_DETAILS if registration is None else registration},
    )
    rsps.get(
        f"{V1}/policies/authenticationMethodsPolicy",
        json=gd.AUTH_METHODS_POLICY if auth_methods_policy is None else auth_methods_policy,
    )
    scores = gd.SECURE_SCORES if secure_scores is None else secure_scores
    rsps.get(f"{V1}/security/secureScores", json={"value": scores})
    rsps.get(f"{V1}/security/secureScoreControlProfiles", json={"value": gd.SECURE_SCORE_PROFILES})
    states = gd.PER_USER_MFA if per_user is None else per_user

    def requirements(request):
        return 200, {}, json.dumps({"perUserMfaState": states.get(request.path_url.split("/")[3], "disabled")})

    rsps.add_callback(
        responses.GET,
        re.compile(
            re.escape(f"{GRAPH_ROOT}/beta") + r"/users/[^/]+/authentication/"
            r"requirements"
        ),
        callback=requirements,
        content_type="application/json",
    )
    rsps.get(re.compile(re.escape(V1) + r"/users/[^/]+/authentication/methods"), json={"value": []})
    rsps.get(re.compile(re.escape(V1) + r"/groups/[^/?]+(\?.*)?$"), json={"displayName": "Break glass"})
    rsps.get(
        re.compile(re.escape(V1) + r"/groups/[^/]+/transitiveMembers"),
        json={"value": [{"@odata.type": "#microsoft.graph.user", "id": "u4"}]},
    )
    rsps.get(f"{V1}/devices", json={"value": dd.ENTRA_DEVICES})
    rsps.get(f"{V1}/deviceManagement/managedDevices", json={"value": dd.MANAGED_DEVICES})
    rsps.get(f"{V1}/groups", json={"value": grp.GROUPS if groups is None else groups})

    def people(request):  # /v1.0/groups/<id>/owners or /members
        gid, kind = request.path_url.split("?")[0].split("/")[3:5]
        return 200, {}, json.dumps({"value": getattr(grp, kind.upper()).get(gid, [])})

    rsps.add_callback(
        responses.GET,
        re.compile(re.escape(V1) + r"/groups/[^/]+/(owners|members)"),
        callback=people,
        content_type="application/json",
    )
    rsps.get(
        f"{V1}/directory/deletedItems/microsoft.graph.user",
        json={"value": gd.DELETED_USERS if deleted_users is None else deleted_users},
    )
    rsps.get(
        f"{V1}/directory/deletedItems/microsoft.graph.group",
        json={"value": gd.DELETED_GROUPS if deleted_groups is None else deleted_groups},
    )
    rsps.get(
        f"{V1}/directory/administrativeUnits",
        json={"value": gd.ADMIN_UNITS if admin_units is None else admin_units},
    )

    def unit_members(request):
        uid = request.path_url.split("?")[0].split("/")[4]
        return 200, {}, json.dumps({"value": gd.ADMIN_UNIT_MEMBERS.get(uid, [])})

    rsps.add_callback(
        responses.GET,
        re.compile(re.escape(V1) + r"/directory/administrativeUnits/[^/]+/members"),
        callback=unit_members,
        content_type="application/json",
    )
    rsps.get(f"{V1}/groupSettings", json={"value": grp.GROUP_SETTINGS})
    rsps.get(f"{V1}/groupLifecyclePolicies", json={"value": grp.LIFECYCLE_POLICIES})
    rsps.get(f"{BETA}/organization", json={"value": [gd.ORGANIZATION]})
    rsps.get(f"{V1}/domains", json={"value": hd.DOMAINS})
    rsps.get(f"{V1}/policies/adminConsentRequestPolicy", json=tsd.ADMIN_CONSENT_POLICY)
    rsps.get(f"{V1}/auditLogs/directoryAudits", json={"value": ad.DIRECTORY_AUDITS})
    # The whole sign-in log, legacy clients included: the legacy sign-in collector reads them from the stored
    # log once it covers 30 days (the /v1.0 query above is its fallback until then).
    rsps.get(
        f"{BETA}/auditLogs/signIns", json={"value": [*gd.SIGNINS, *gd.LEGACY_SIGNINS] if signins is None else signins}
    )
    rsps.get(
        f"{BETA}/reports/servicePrincipalSignInActivities",
        json={"value": gd.APP_SIGN_INS if app_sign_ins is None else app_sign_ins},
    )
    rsps.get(
        f"{V1}/identityProtection/riskyUsers", json={"value": gd.RISKY_USERS if risky_users is None else risky_users}
    )
    mock_phase4(rsps, **phase4)
    mock_sharepoint(rsps, **(sharepoint_kw or {}))
    mock_usage(rsps, **(usage_kw or {}))
    mock_teams(rsps, **(teams_kw or {}))


def exchange_factory(tenant, connection):
    return ExchangeClient(tenant["tenant_ref"], lambda: "t", sleep=lambda s: None)


def seed_tenant(db, tenant_id, dns=None, precompute=False, **kwargs):
    """Collect every source for the tenant from ``graphdata``, as the worker would. The collection
    queues the tenant's precomputed rows; they're dropped unless ``precompute``, so tests of other
    background tasks see only their own."""
    factory = lambda tenant, connection: GraphClient(lambda: "t", sleep=lambda s: None)  # noqa: E731
    with (
        responses.RequestsMock(assert_all_requests_are_fired=False) as rsps,
        patch("officesentry.dnscheck.lookup", dns or gd.fake_dns),
    ):
        mock_tenant(rsps, **kwargs)
        for key in REGISTRY:
            store.enqueue_run(db, tenant_id, key)
        while (run := store.claim_next_run(db)) is not None:
            assert execute_run(db, run, factory, exchange_factory) in ("succeeded", "partial"), run["collector"]
    if not precompute:
        db.execute("DELETE FROM tasks WHERE kind = 'precompute' AND status = 'queued'")
