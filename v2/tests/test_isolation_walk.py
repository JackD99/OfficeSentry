"""Tenant isolation, walked over every route: nothing of a tenant someone wasn't granted reaches them.

Two tenants are seeded with full snapshots, and tenant B gets one of everything
a URL can point at (a run, finding, schedule, alert, contact, delivery, issued
report, recommendation and note). Each is in its own tenant group. Signed in as
an analyst and as a client who were each granted only tenant A (the analyst
directly or through A's group; the client also holds a grant of B's group,
which must count for nothing), every route in the URL map that names a tenant
or one of its objects is opened with B's IDs, by GET and POST, and must be
refused. List and cross-tenant pages must never mention B, whichever group
picker choice is asked for.

The routes are read from the app when the test runs, so a route added later is
walked too. A new kind of URL argument fails ``test_every_route_argument_is_classified``
until someone decides whether it points at something a tenant owns.
"""

import re
from datetime import UTC, datetime

import pytest

from officesentry import delivery, issued, store, tenant_groups
from officesentry.db import utcnow
from officesentry.projects import CATALOG
from officesentry.reports.data_reports import REPORTS
from officesentry.reports.evidence_answers import QUESTIONS
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.test_collectors import USERS

# URL arguments that identify a tenant or something it owns. Each must be refused for an ungranted tenant.
TENANT_ARGS = {"tenant_id", "run_id", "finding_id", "schedule_id", "alert_id", "contact_id", "delivery_id",
               "report_id", "action_id", "note_id"}
# URL arguments that don't: names (report keys, table slugs, a month, a file extension, a static file),
# a person's account inside a tenant (only ever with tenant_id), and objects that belong to Office Sentry
# itself (portal users, app connections, report plans, sign-in sessions, someone's own saved views, tenant
# groups, which are admin-only), and flags a route sets itself (``resolved`` on the findings downloads).
OTHER_ARGS = {"key", "table", "ext", "filename", "period", "user_id", "conn_id", "plan_id", "session_id", "view_id",
              "resolved", "group_id"}
# Who is signed in for the walk. Each sees only tenant A.
ROLES = ["analyst", "client", "analyst-by-group", "client-with-group"]
REFUSED = (400, 403, 404)
B_NAMES = (b"Bravo Inc", b"bravo.onmicrosoft.com")
SKIP_LISTS = {"auth.logout", "auth.end_session", "static", "health"}


@pytest.fixture
def world(app, db, connection_id):
    alpha = store.create_tenant(db, "Alpha Ltd", "alpha.onmicrosoft.com", connection_id)
    bravo = store.create_tenant(db, "Bravo Inc", "bravo.onmicrosoft.com", connection_id)
    db.execute("UPDATE tenants SET consented_at = ?", (utcnow(),))
    seed_tenant(db, alpha)
    seed_tenant(db, bravo)
    now = utcnow()
    ids = {"tenant_id": bravo}
    ids["run_id"] = db.execute("SELECT MAX(id) FROM runs WHERE tenant_id = ?", (bravo,)).fetchone()[0]
    ids["finding_id"] = db.execute("SELECT MAX(id) FROM findings WHERE tenant_id = ?", (bravo,)).fetchone()[0]
    ids["schedule_id"] = db.execute(
        "INSERT INTO email_schedules (kind, tenant_id, reports, frequency, hour, timezone, created_at)"
        " VALUES ('report', ?, '[\"review\"]', 'monthly', 8, 'UTC', ?)", (bravo, now)).lastrowid
    ids["alert_id"] = db.execute("INSERT INTO email_alerts (tenant_id, min_severity, created_at) VALUES (?, 'high', ?)",
                                 (bravo, now)).lastrowid
    ids["contact_id"] = delivery.add_contact(db, bravo, "Bravo IT", "it@bravo.example")
    ids["delivery_id"] = db.execute(
        "INSERT INTO email_deliveries (kind, schedule_id, tenant_id, subject, recipients, status, error, created_at)"
        " VALUES ('report', ?, ?, 'Monthly', 'it@bravo.example', 'failed', 'x', ?)",
        (ids["schedule_id"], bravo, now)).lastrowid
    ids["report_id"] = issued.archive(db, bravo, [issued.IssuedFile("review", "Monthly review", "bravo.pdf",
                                                                     "application/pdf", b"%PDF-1 bravo")],
                                      period="2026-09", trigger="manual", user="x")[0]
    ids["action_id"] = db.execute("INSERT INTO report_actions (tenant_id, title, created_at, updated_at)"
                                  " VALUES (?, 'Bravo action', ?, ?)", (bravo, now, now)).lastrowid
    ids["note_id"] = db.execute("INSERT INTO finding_notes (tenant_id, rule_key, body, visibility, created_at)"
                                " VALUES (?, 'admin_mfa', 'Bravo note', 'staff', ?)", (bravo, now)).lastrowid
    for name, tenant in (("Alpha group", alpha), ("Bravo group", bravo)):
        group = tenant_groups.create(db, {"name": name, "category": "", "description": ""}, "test")
        tenant_groups.set_members(db, group, [tenant])
        ids[name] = group
    return alpha, bravo, ids


def _sign_in(client, db, role: str, alpha: int, ids: dict) -> None:
    """Someone who may see tenant A only, however ``role`` grants it."""
    if role == "analyst-by-group":
        user = make_user(db, "someone", role="analyst")
        tenant_groups.set_user_groups(db, user, [ids["Alpha group"]])
    elif role == "client-with-group":
        # Group grants are for analysts only: a client holding one (say, after a role change) gets nothing from it.
        user = make_user(db, "someone", role="client", tenants=[alpha])
        tenant_groups.set_user_groups(db, user, [ids["Bravo group"]])
    else:
        make_user(db, "someone", role=role, tenants=[alpha])
    login(client, "someone")


def _url(rule, ids: dict) -> str:
    """The rule's URL with tenant B's objects in it, and a real name wherever it takes one."""
    endpoint = rule.endpoint
    if endpoint == "reports.evidence_answer":
        key = QUESTIONS[0].key
    elif endpoint == "projects.set_status":
        key = CATALOG[0].key
    else:
        key = next(iter(REPORTS))
    names = {"key": key, "period": datetime.now(UTC).strftime("%Y-%m"), "ext": "csv", "table": "x",
             "user_id": USERS[0]["id"]}

    def value(m):
        name = m.group(1)
        return str(ids.get(name, names.get(name, "1")))
    return re.sub(r"<(?:\w+:)?(\w+)>", value, rule.rule)


def _tenant_rules(app):
    return [r for r in app.url_map.iter_rules() if set(r.arguments) & TENANT_ARGS]


def test_every_route_argument_is_classified(app):
    unknown = {arg for rule in app.url_map.iter_rules() for arg in rule.arguments} - TENANT_ARGS - OTHER_ARGS
    assert not unknown, (f"New URL argument(s) {sorted(unknown)}: add each to TENANT_ARGS if it identifies a tenant "
                         "or something a tenant owns (then the walk below checks it), else to OTHER_ARGS")


def test_the_walk_opens_real_pages(app, client, db, world):
    """Proof the walk isn't vacuous: with access to both tenants, B's URLs open."""
    _, _, ids = world
    make_user(db, "admin")
    login(client, "admin")
    opened = 0
    for rule in _tenant_rules(app):
        if "GET" not in rule.methods or rule.endpoint.startswith("reporting."):
            continue  # month-end review pages only exist for tenants on a report plan
        resp = client.get(_url(rule, ids))
        assert resp.status_code in (200, 302), (rule.endpoint, resp.status_code)
        assert "/login" not in resp.headers.get("Location", ""), rule.endpoint
        opened += 1
    assert opened >= 35


@pytest.mark.parametrize("role", ROLES)
def test_ungranted_tenant_is_refused_on_every_route(app, client, db, world, role):
    alpha, bravo, ids = world
    _sign_in(client, db, role, alpha, ids)
    before = _row_counts(db)
    walked = 0
    for rule in _tenant_rules(app):
        url = _url(rule, ids)
        for method in sorted(rule.methods & {"GET", "POST"}):
            resp = client.open(url, method=method)
            body = resp.data
            refused = resp.status_code in REFUSED or (resp.status_code == 302 and "/login" in resp.headers["Location"])
            assert refused, (role, method, url, resp.status_code)
            assert not any(name in body for name in B_NAMES), (role, method, url)
            walked += 1
    assert walked >= 70
    assert _row_counts(db) == before  # nothing of B's was changed, added or removed


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("pick", [None, "Alpha group", "Bravo group"])
def test_lists_and_rollups_never_mention_an_ungranted_tenant(app, client, db, world, role, pick):
    alpha, _, ids = world
    _sign_in(client, db, role, alpha, ids)
    if pick:  # the group picker remembers a choice; asking for B's group must not bring B in
        client.get(f"/?group={ids[pick]}")
    period = datetime.now(UTC).strftime("%Y-%m")
    urls = [r.rule for r in app.url_map.iter_rules()
            if not r.arguments and "GET" in r.methods and r.endpoint not in SKIP_LISTS]
    urls += ["/search?q=a", "/search?q=Bravo", f"/reporting/{period}"]
    urls += [f"/across/{key}" for key in REPORTS] + [f"/across/{key}.csv" for key in list(REPORTS)[:5]]
    seen = 0
    for url in urls:
        resp = client.get(url)
        if resp.status_code != 200:
            continue
        body = resp.data
        assert not any(name in body for name in B_NAMES), (role, pick, url)
        assert b"Bravo group" not in body, (role, pick, url)  # nor the name of a group holding only B
        seen += 1
    assert seen > (40 if role.startswith("analyst") else 3)


@pytest.mark.parametrize("role", ["analyst-by-group"])
def test_a_group_grant_opens_the_groups_tenants(app, client, db, world, role):
    """The other side of the walk: an analyst granted A's group really can open A's pages, and stops when
    A leaves the group."""
    alpha, _, ids = world
    _sign_in(client, db, role, alpha, ids)
    assert client.get(f"/tenants/{alpha}").status_code == 200
    assert b"Alpha Ltd" in client.get("/").data
    tenant_groups.set_members(db, ids["Alpha group"], [])
    assert client.get(f"/tenants/{alpha}").status_code == 404
    assert b"Alpha Ltd" not in client.get("/").data


def _row_counts(db) -> dict:
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    noise = {"audit_log", "sessions", "login_failures", "rollup_cache"}
    return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables if t not in noise}
