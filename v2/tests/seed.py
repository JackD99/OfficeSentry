"""Run every collector for a tenant against recorded-style Graph responses."""

import json
import re
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import requests
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

# Microsoft answers a list a page at a time (100 to 999 items, by endpoint) with an ``@odata.nextLink`` to
# the next page, and so does the seed: every list here is cut into pages this small, so a collector that
# reads one page and stops, or an Exchange call that ignores its next link, loses most of the seed and the
# tests that count it fail. Hand-built logs in the collector tests still cover the odd shapes of paging
# (an empty last page, a delta token, a budget running out).
PAGE = 2


def _page(request) -> int:
    """Where the page a request asks for starts: the ``$skiptoken`` the previous page's link carried."""
    return int(parse_qs(urlsplit(request.url).query).get("$skiptoken", ["0"])[0])


def page_of(url: str, items: list, start: int, **extra) -> dict:
    """One page of ``items`` from ``start``, with the link to the next while there is one: the request's own
    URL (its ``$select``, ``$filter`` and ``$top`` kept, as Microsoft keeps them) with the next skip token.
    ``extra`` is anything else the body carries on every page (a delta link, an ``@odata.context``)."""
    body = {**extra, "value": items[start : start + PAGE]}
    if start + PAGE < len(items):
        parts = urlsplit(url)
        query = [(k, v) for k, v in parse_qs(parts.query).items() if k != "$skiptoken"]
        query = [(k, v[0]) for k, v in query] + [("$skiptoken", str(start + PAGE))]
        body["@odata.nextLink"] = urlunsplit(parts._replace(query=urlencode(query, safe="$,")))
    return body


def paged(rsps, url: str, items: list, **extra) -> None:
    """Mock ``url`` answering ``items`` in pages (``PAGE`` a time), as ``rsps.get(url, json={"value": items})``
    would in one go, whatever query the request carries."""
    items = list(items)

    def callback(request):
        return 200, {}, json.dumps(page_of(request.url, items, _page(request), **extra))

    rsps.add_callback(responses.GET, url, callback=callback, content_type="application/json")


def exchange_callback(data):
    def callback(request):
        body = json.loads(request.body)["CmdletInput"]
        name, params = body["CmdletName"], body.get("Parameters") or {}
        result = data.get(name, [])
        if isinstance(result, dict):  # keyed by Identity (or Mailbox)
            result = result.get(params.get("Identity") or params.get("Mailbox"), [])
        if isinstance(result, tuple):  # (status, error body)
            return result[0], {}, json.dumps(result[1])
        return 200, {}, json.dumps(page_of(request.url, list(result), _page(request)))

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
    paged(rsps, f"{V1}/oauth2PermissionGrants", gd.DELEGATED_GRANTS)
    paged(rsps, f"{V1}/applications", gd.APPLICATIONS)
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
    paged(rsps, f"{V1}/auditLogs/signIns", gd.LEGACY_SIGNINS)
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
    audits=None,
    admin_units=None,
    teams_kw=None,
    **phase4,
):
    paged(rsps, f"{V1}/organization", [gd.ORGANIZATION])
    paged(rsps, f"{V1}/subscribedSkus", skus)
    paged(rsps, f"{V1}/directory/subscriptions", ld.SUBSCRIPTIONS if subscriptions is None else subscriptions)
    paged(rsps, f"{V1}/users", users)
    paged(rsps, f"{V1}/roleManagement/directory/roleDefinitions", gd.ROLE_DEFINITIONS)
    paged(rsps, f"{V1}/roleManagement/directory/roleEligibilitySchedules", gd.ROLE_ELIGIBILITY)
    rsps.get(f"{V1}/policies/identitySecurityDefaultsEnforcementPolicy", json={"isEnabled": defaults})
    paged(rsps, f"{V1}/identity/conditionalAccess/policies", [gd.CA_MFA_ALL, gd.CA_REPORT_ONLY] if ca is None else ca)
    paged(
        rsps,
        f"{V1}/reports/authenticationMethods/userRegistrationDetails",
        gd.REGISTRATION_DETAILS if registration is None else registration,
    )
    rsps.get(
        f"{V1}/policies/authenticationMethodsPolicy",
        json=gd.AUTH_METHODS_POLICY if auth_methods_policy is None else auth_methods_policy,
    )
    scores = gd.SECURE_SCORES if secure_scores is None else secure_scores
    paged(rsps, f"{V1}/security/secureScores", scores)
    paged(rsps, f"{V1}/security/secureScoreControlProfiles", gd.SECURE_SCORE_PROFILES)
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
    paged(rsps, f"{V1}/devices", dd.ENTRA_DEVICES)
    paged(rsps, f"{V1}/deviceManagement/managedDevices", dd.MANAGED_DEVICES)
    paged(rsps, f"{V1}/groups", grp.GROUPS if groups is None else groups)

    def people(request):  # /v1.0/groups/<id>/owners or /members
        gid, kind = request.path_url.split("?")[0].split("/")[3:5]
        return 200, {}, json.dumps({"value": getattr(grp, kind.upper()).get(gid, [])})

    rsps.add_callback(
        responses.GET,
        re.compile(re.escape(V1) + r"/groups/[^/]+/(owners|members)"),
        callback=people,
        content_type="application/json",
    )
    paged(
        rsps,
        f"{V1}/directory/deletedItems/microsoft.graph.user",
        gd.DELETED_USERS if deleted_users is None else deleted_users,
    )
    paged(
        rsps,
        f"{V1}/directory/deletedItems/microsoft.graph.group",
        gd.DELETED_GROUPS if deleted_groups is None else deleted_groups,
    )
    paged(rsps, f"{V1}/directory/administrativeUnits", gd.ADMIN_UNITS if admin_units is None else admin_units)

    def unit_members(request):
        uid = request.path_url.split("?")[0].split("/")[4]
        return 200, {}, json.dumps(page_of(request.url, gd.ADMIN_UNIT_MEMBERS.get(uid, []), _page(request)))

    rsps.add_callback(
        responses.GET,
        re.compile(re.escape(V1) + r"/directory/administrativeUnits/[^/]+/members"),
        callback=unit_members,
        content_type="application/json",
    )
    paged(rsps, f"{V1}/groupSettings", grp.GROUP_SETTINGS)
    paged(rsps, f"{V1}/groupLifecyclePolicies", grp.LIFECYCLE_POLICIES)
    paged(rsps, f"{BETA}/organization", [gd.ORGANIZATION])
    paged(rsps, f"{V1}/domains", hd.DOMAINS)
    rsps.get(f"{V1}/policies/adminConsentRequestPolicy", json=tsd.ADMIN_CONSENT_POLICY)
    paged(rsps, f"{V1}/auditLogs/directoryAudits", ad.DIRECTORY_AUDITS if audits is None else audits)
    # The whole sign-in log, legacy clients included: the legacy sign-in collector reads them from the stored
    # log once it covers 30 days (the /v1.0 query above is its fallback until then).
    paged(rsps, f"{BETA}/auditLogs/signIns", [*gd.SIGNINS, *gd.LEGACY_SIGNINS] if signins is None else signins)
    paged(
        rsps,
        f"{BETA}/reports/servicePrincipalSignInActivities",
        gd.APP_SIGN_INS if app_sign_ins is None else app_sign_ins,
    )
    paged(rsps, f"{V1}/identityProtection/riskyUsers", gd.RISKY_USERS if risky_users is None else risky_users)
    mock_phase4(rsps, **phase4)
    mock_sharepoint(rsps, **(sharepoint_kw or {}))
    mock_usage(rsps, **(usage_kw or {}))
    mock_teams(rsps, **(teams_kw or {}))


def exchange_factory(tenant, connection):
    return ExchangeClient(tenant["tenant_ref"], lambda: "t", sleep=lambda s: None)


# An object whose display name is someone's or something's: people (and the manager inside a person),
# groups, devices, mailboxes and sites. Policies, roles, apps and settings keep theirs, since collectors
# and checks look some of those up by name.
NAMED_BY = {"userPrincipalName", "UserPrincipalName", "groupTypes", "deviceId", "webUrl", "PrimarySmtpAddress"}


def mark_names(data, mark: str) -> None:
    """Add ``mark`` to every person's, group's, device's, mailbox's and site's display name in a response."""
    if isinstance(data, dict):
        if NAMED_BY & data.keys():
            for key in ("displayName", "DisplayName"):
                if isinstance(data.get(key), str) and data[key]:
                    data[key] += f" {mark}"
        for value in data.values():
            mark_names(value, mark)
    elif isinstance(data, list):
        for value in data:
            mark_names(value, mark)


def _marking_session(mark: str) -> requests.Session:
    """A session whose responses carry ``mark`` in every display name, rewritten at the HTTP level, so
    the collectors read it as Microsoft's answer."""

    def rewrite(resp, **kwargs):
        if "json" not in resp.headers.get("content-type", "") or not resp.content:
            return
        data = resp.json()
        mark_names(data, mark)
        resp._content = json.dumps(data).encode()

    session = requests.Session()
    session.hooks["response"].append(rewrite)
    return session


def seed_tenant(db, tenant_id, dns=None, precompute=False, mark: str | None = None, **kwargs):
    """Collect every source for the tenant from ``graphdata``, as the worker would. The collection
    queues the tenant's precomputed rows; they're dropped unless ``precompute``, so tests of other
    background tasks see only their own. With ``mark``, every person, group, device, mailbox and site is
    named "<name> <mark>" (``mark_names``), so this tenant's data can be told from another's seeded from the
    same ``graphdata``."""
    session = _marking_session(mark) if mark else requests.Session()
    factory = lambda tenant, connection: GraphClient(lambda: "t", session=session, sleep=lambda s: None)  # noqa: E731

    def exchange(tenant, connection):
        return ExchangeClient(tenant["tenant_ref"], lambda: "t", session=session, sleep=lambda s: None)

    with (
        responses.RequestsMock(assert_all_requests_are_fired=False) as rsps,
        patch("officesentry.dnscheck.lookup", dns or gd.fake_dns),
    ):
        mock_tenant(rsps, **kwargs)
        for key in REGISTRY:
            store.enqueue_run(db, tenant_id, key)
        while (run := store.claim_next_run(db)) is not None:
            assert execute_run(db, run, factory, exchange) in ("succeeded", "partial"), run["collector"]
    if not precompute:
        db.execute("DELETE FROM tasks WHERE kind = 'precompute' AND status = 'queued'")
