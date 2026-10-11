"""Tenant isolation, walked over every route: nothing of a tenant someone wasn't granted reaches them.

Two tenants are seeded with full snapshots, and tenant B gets one of everything
a URL can point at (a run, finding, schedule, alert, contact, delivery, issued
report, recommendation and note), a report plan, and every person, group,
device, mailbox and site named with a word (``B_MARK``) that appears nowhere in
A's data, so a page that shows B's data gives itself away even where it never
names the tenant. Each is in its own tenant group. Signed in as an analyst and
as a client who were each granted only tenant A (the analyst directly or
through A's group; the client also holds a grant of B's group, which must count
for nothing), every route in the URL map that names a tenant or one of its
objects is opened with B's IDs, by GET and by POST with the form an authorised
person would submit (``FORMS``), and must be refused outright (403, 404 or the
sign-in page), never turned away by form validation. List and cross-tenant
pages must never mention B or carry its mark, whichever group picker choice is
asked for.

The routes are read from the app when the test runs, so a route added later is
walked too. A new kind of URL argument fails ``test_every_route_argument_is_classified``
until someone decides whether it points at something a tenant owns, and a new
POST route fails ``test_every_form_is_accepted_from_an_authorised_person``
until ``FORMS`` has the form it accepts.
"""

import re
from datetime import UTC, datetime

import pytest

from officesentry import delivery, issued, month_end, projects, store, tenant_groups
from officesentry.db import utcnow
from officesentry.projects import CATALOG
from officesentry.reports import designs, evidence_answers
from officesentry.reports.data_reports import REPORTS
from officesentry.reports.evidence_answers import QUESTIONS
from tests import sharepoint_data as sd
from tests.conftest import login, make_user
from tests.seed import seed_tenant
from tests.test_collectors import USERS

# URL arguments that identify a tenant or something it owns. Each must be refused for an ungranted tenant.
TENANT_ARGS = {
    "tenant_id",
    "run_id",
    "finding_id",
    "schedule_id",
    "alert_id",
    "contact_id",
    "delivery_id",
    "report_id",
    "action_id",
    "note_id",
}
# URL arguments that don't: names (report keys, table slugs, a month, a file extension, a static file),
# a person's account or a mailbox inside a tenant (only ever with tenant_id), and objects that belong to Office
# Sentry itself (portal users and roles, app connections, report plans, sign-in sessions, someone's own saved
# views, tenant groups, which are admin-only), and flags a route sets itself (``resolved`` on the findings
# downloads).
OTHER_ARGS = {
    "key",
    "table",
    "ext",
    "filename",
    "period",
    "user_id",
    "mailbox_id",
    "gid",
    "site_id",
    "conn_id",
    "plan_id",
    "session_id",
    "view_id",
    "resolved",
    "group_id",
    "scheme",
    "design_id",
    "role_id",
}
# Who is signed in for the walk. Each sees only tenant A.
ROLES = ["analyst", "client", "analyst-by-group", "client-with-group"]
# A refusal is the access check's: 403, 404 or the sign-in page. A 400 is form validation, which runs
# after the access check, so an ungranted tenant's URL never gets that far.
REFUSED = (403, 404)
B_NAMES = (b"Bravo Inc", b"bravo.onmicrosoft.com")
# The word in every display name of B's people, groups, devices, mailboxes and sites (seed.mark_names).
B_MARK = "Zebedee"
SKIP_LISTS = {"auth.logout", "auth.end_session", "static", "health", "health_ready"}

# The form an authorised person submits to each POST route that names a tenant or one of its objects:
# what its page sends, filled in with values the route accepts. A new POST route is listed here, so the
# walk sends it a form the route would act on, not an empty one it may refuse before looking at the
# tenant. ``ids`` is the world's: B's objects by URL argument, and its groups by name.
FORMS = {
    "email.add_tenant_contact": lambda ids: {"name": "Bravo Ops", "email": "ops@bravo.example"},
    "email.edit_schedule": lambda ids: SCHEDULE_FORM | {"contacts": str(ids["contact_id"])},
    "email.new_schedule": lambda ids: SCHEDULE_FORM | {"contacts": str(ids["contact_id"])},
    "email.save_tenant_alert": lambda ids: {"min_severity": "high", "contacts": str(ids["contact_id"])},
    "onboarding.contact": lambda ids: {"contact_name": "Bravo IT", "contact_email": "it@bravo.example"},
    "onboarding.mark_sent": lambda ids: {"sent": "1"},
    "projects.set_status": lambda ids: {"status": next(iter(projects.STATUSES)), "note": "Agreed by phone"},
    "reporting.add_action": lambda ids: {"title": "Turn on MFA for everyone", "detail": "By the end of the month"},
    "reporting.add_note": lambda ids: {"rule_key": "admin_mfa", "body": "Spoke to the client", "visibility": "staff"},
    "reporting.approve": lambda ids: {"reason": ""},
    "reporting.assign": lambda ids: {"reviewer": "me"},
    "reporting.save_commentary": lambda ids: {"commentary": "A quiet month."},
    "reporting.set_state": lambda ids: {"state": "in_review"},
    "reporting.update_action": lambda ids: {"op": "save", "status": "in_progress", "target_month": ""},
    "reports.accept_finding": lambda ids: {"note": "Accepted: the account is a break-glass account", "until": ""},
    "reports.evidence_answer": lambda ids: {"answer": next(iter(evidence_answers.ANSWERS)), "note": "Checked"},
    "tenant_admin.delete": lambda ids: {"confirm_name": "Bravo Inc"},
    "tenant_admin.edit": lambda ids: {"name": "Bravo Inc", "tenant_ref": "bravo.onmicrosoft.com", "notes": "VIP"},
    "tenant_admin.set_groups": lambda ids: {"groups": str(ids["Bravo group"])},
    "tenants.set_connection": lambda ids: {"connection_id": str(ids["connection_id"])},
}
SCHEDULE_FORM = {
    "reports": "review",
    "frequency": "monthly",
    "day_of_month": "1",
    "hour": "8",
    "timezone": "UTC",
    "include_xlsx": "1",
}
# POST routes with no form of their own: a button.
BUTTONS = {
    "email.delete_alert",
    "email.delete_contact",
    "email.delete_schedule",
    "email.retry",
    "email.send_now",
    "email.toggle_schedule",
    "onboarding.check",
    "reporting.delete_note",
    "reporting.send_again",
    "reports.unaccept_finding",
    "tenant_admin.archive",
    "tenant_admin.check_connection",
    "tenant_admin.pause",
    "tenant_admin.restore",
    "tenant_admin.resume",
    "tenants.collect",
    "tenants.test_connection",
}
# What an authorised admin gets back from a valid form, where it isn't the usual redirect or page:
# the month isn't approved yet, so there is nothing to send again (400).
EXPECTED_FROM_ADMIN = {"reporting.send_again": (400,)}
# The one POST the authorised walk leaves alone: it signs in to Microsoft (the walk has no network).
NO_NETWORK = {"tenants.test_connection"}


@pytest.fixture
def world(app, db, connection_id):
    alpha = store.create_tenant(db, "Alpha Ltd", "alpha.onmicrosoft.com", connection_id)
    bravo = store.create_tenant(db, "Bravo Inc", "bravo.onmicrosoft.com", connection_id)
    db.execute("UPDATE tenants SET consented_at = ?", (utcnow(),))
    seed_tenant(db, alpha)
    seed_tenant(db, bravo, mark=B_MARK)
    now = utcnow()
    ids = {"tenant_id": bravo, "connection_id": connection_id}
    ids["run_id"] = db.execute("SELECT MAX(id) FROM runs WHERE tenant_id = ?", (bravo,)).fetchone()[0]
    ids["finding_id"] = db.execute("SELECT MAX(id) FROM findings WHERE tenant_id = ?", (bravo,)).fetchone()[0]
    ids["schedule_id"] = db.execute(
        "INSERT INTO email_schedules (kind, tenant_id, reports, frequency, hour, timezone, created_at)"
        " VALUES ('report', ?, '[\"review\"]', 'monthly', 8, 'UTC', ?)",
        (bravo, now),
    ).lastrowid
    ids["alert_id"] = db.execute(
        "INSERT INTO email_alerts (tenant_id, min_severity, created_at) VALUES (?, 'high', ?)", (bravo, now)
    ).lastrowid
    ids["contact_id"] = delivery.add_contact(db, bravo, "Bravo IT", "it@bravo.example")
    ids["delivery_id"] = db.execute(
        "INSERT INTO email_deliveries (kind, schedule_id, tenant_id, subject, recipients, status, error, created_at)"
        " VALUES ('report', ?, ?, 'Monthly', 'it@bravo.example', 'failed', 'x', ?)",
        (ids["schedule_id"], bravo, now),
    ).lastrowid
    ids["report_id"] = issued.archive(
        db,
        bravo,
        [issued.IssuedFile("review", "Monthly review", "bravo.pdf", "application/pdf", b"%PDF-1 bravo")],
        period="2026-09",
        trigger="manual",
        user="x",
    )[0]
    ids["action_id"] = db.execute(
        "INSERT INTO report_actions (tenant_id, title, created_at, updated_at) VALUES (?, 'Bravo action', ?, ?)",
        (bravo, now, now),
    ).lastrowid
    ids["note_id"] = db.execute(
        "INSERT INTO finding_notes (tenant_id, rule_key, body, visibility, created_at)"
        " VALUES (?, 'admin_mfa', 'Bravo note', 'staff', ?)",
        (bravo, now),
    ).lastrowid
    ids["design_id"] = designs.create(db, "Walk design", "test", designs.STARTERS["executive"][2])
    plan = month_end.save_plan(
        db,
        {
            "name": "Bravo plan",
            "reports": ["review"],
            "include_xlsx": 0,
            "day_of_month": 1,
            "hour": 8,
            "timezone": "UTC",
            "subject": "{client}: {month}",
            "message": "",
            "mode": "approval",
            "delivery": "email",
            "bcc_team": 0,
        },
        "test",
    )
    month_end.apply_plan(db, month_end.get_plan(db, plan), [bravo], "test", "UTC")
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
    names = {
        "key": key,
        "period": datetime.now(UTC).strftime("%Y-%m"),
        "ext": "csv",
        "table": "x",
        "user_id": USERS[0]["id"],
        "scheme": "cyber-essentials",
        "mailbox_id": USERS[0]["id"],  # Ann's mailbox has her account's object ID
        "gid": "g1",  # the Sales team
        "site_id": sd.SITE_USAGE[0]["siteId"],  # Finance
    }

    def value(m):
        name = m.group(1)
        return str(ids.get(name, names.get(name, "1")))

    return re.sub(r"<(?:\w+:)?(\w+)>", value, rule.rule)


def _tenant_rules(app):
    return [r for r in app.url_map.iter_rules() if set(r.arguments) & TENANT_ARGS]


def test_every_route_argument_is_classified(app):
    unknown = {arg for rule in app.url_map.iter_rules() for arg in rule.arguments} - TENANT_ARGS - OTHER_ARGS
    assert not unknown, (
        f"New URL argument(s) {sorted(unknown)}: add each to TENANT_ARGS if it identifies a tenant "
        "or something a tenant owns (then the walk below checks it), else to OTHER_ARGS"
    )


def _form(rule, ids: dict) -> dict:
    return FORMS[rule.endpoint](ids) if rule.endpoint in FORMS else {}


def test_the_walk_opens_real_pages(app, client, db, world):
    """Proof the walk isn't vacuous: with access to both tenants, B's URLs open, and B's pages carry its
    mark where a leak would show it (people, groups, devices, distribution lists), while A's don't."""
    alpha, bravo, ids = world
    make_user(db, "admin")
    login(client, "admin")
    opened = 0
    for rule in _tenant_rules(app):
        if "GET" not in rule.methods:
            continue
        resp = client.get(_url(rule, ids))
        assert resp.status_code in (200, 302), (rule.endpoint, resp.status_code)
        assert "/login" not in resp.headers.get("Location", ""), rule.endpoint
        opened += 1
    assert opened >= 40
    for page in ("users", "data/all-users", "data/groups", "data/devices", "data/distribution-lists"):
        assert B_MARK.encode() in client.get(f"/tenants/{bravo}/{page}").data, page  # or a leak couldn't be seen
        assert B_MARK.encode() not in client.get(f"/tenants/{alpha}/{page}").data, page


def test_every_form_is_accepted_from_an_authorised_person(app, client, db, world):
    """Proof the forms are the real thing: an admin granted B submits each one and the route acts on it
    (a redirect or a page, never a validation error). So when the walk below gets a refusal with the same
    form, it is the access check refusing, not the form."""
    _, bravo, ids = world
    make_user(db, "admin")
    login(client, "admin")
    rules = [r for r in _tenant_rules(app) if "POST" in r.methods]
    unlisted = {r.endpoint for r in rules} - FORMS.keys() - BUTTONS
    assert not unlisted, f"New POST route(s) {sorted(unlisted)}: add the form they accept to FORMS, or to BUTTONS"
    # Deletes after the other forms for the same object, and deleting B itself last, since nothing of B's
    # opens after it.
    rules.sort(key=lambda r: (r.endpoint == "tenant_admin.delete", "delete" in r.endpoint, r.rule))
    accepted = 0
    for rule in rules:
        if rule.endpoint in NO_NETWORK:
            continue
        resp = client.post(_url(rule, ids), data=_form(rule, ids))
        expected = EXPECTED_FROM_ADMIN.get(rule.endpoint, (200, 302))
        assert resp.status_code in expected, (rule.endpoint, resp.status_code)
        assert "/login" not in resp.headers.get("Location", ""), rule.endpoint
        accepted += 1
    assert accepted >= 30
    assert store.get_tenant(db, bravo) is None  # the last form deleted B, so every form reached its route


@pytest.mark.parametrize("role", ROLES)
def test_ungranted_tenant_is_refused_on_every_route(app, client, db, world, role):
    alpha, bravo, ids = world
    _sign_in(client, db, role, alpha, ids)
    before = _row_counts(db)
    walked = 0
    for rule in _tenant_rules(app):
        url = _url(rule, ids)
        for method in sorted(rule.methods & {"GET", "POST"}):
            resp = client.open(url, method=method, data=_form(rule, ids) if method == "POST" else None)
            body = resp.data
            refused = resp.status_code in REFUSED or (resp.status_code == 302 and "/login" in resp.headers["Location"])
            assert refused, (role, method, url, resp.status_code)
            assert not any(name in body for name in B_NAMES), (role, method, url)
            assert B_MARK.encode() not in body, (role, method, url)
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
    urls = [
        r.rule
        for r in app.url_map.iter_rules()
        if not r.arguments and "GET" in r.methods and r.endpoint not in SKIP_LISTS
    ]
    # The search page echoes what was typed, so it is asked for the start of the mark and not the mark.
    urls += ["/search?q=a", "/search?q=Bravo", f"/search?q={B_MARK[:5]}", f"/reporting/{period}"]
    urls += [f"/across/{key}" for key in REPORTS] + [f"/across/{key}.csv" for key in list(REPORTS)[:5]]
    seen = 0
    for url in urls:
        resp = client.get(url)
        if resp.status_code != 200:
            continue
        body = resp.data
        assert not any(name in body for name in B_NAMES), (role, pick, url)
        assert B_MARK.encode() not in body, (role, pick, url)  # nor any of B's people, groups or devices
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
