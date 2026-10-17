"""Tenant isolation, walked over every route: nothing of a tenant someone wasn't granted reaches them.

Two tenants are seeded with full snapshots and the audit log add-on's records, and
tenant B gets one of everything a URL can point at (a run, finding, schedule,
alert, contact, delivery, issued report, recommendation and note), a report plan,
and every person, group, device, mailbox, site and audit record named with a word
(``B_MARK``) that appears nowhere in
A's data, so a page that shows B's data gives itself away even where it never
names the tenant. Each is in its own tenant group. Signed in as an analyst and
as a client who were each granted only tenant A (the analyst directly or
through A's group; the client also holds a grant of B's group, which must count
for nothing), and as one of A's people signed in with their own Microsoft
account from A's tenant (granted B too, which must count for nothing, since B
is another organisation), every route in the URL map that names a tenant or one of its
objects is opened with B's IDs, by GET and by POST with the form an authorised
person would submit (``FORMS``), and must be refused outright (403, 404 or the
sign-in page), never turned away by form validation. List and cross-tenant
pages must never mention B or carry its mark, whichever group picker choice is
asked for.

Then the other way round, since a page about A can leak B too: A gets the same
objects, and every GET route in the URL map (any arguments at all) is opened
with A's IDs, as the analyst, both clients and a ``manager`` (a custom role
limited to A that holds every capability, so no missing capability hides a
missing tenant check), plain and with ``?tenant=`` naming B, every download
in each format; none may carry anything of B's, read inside spreadsheets,
Word files and zips and in the page a PDF is made from. A's URLs with one of
B's objects beside A's tenant are refused, and the manager posting every form
with B's IDs in it (on A's URLs, and on routes that name no tenant) changes
nothing of B's and links nothing of A's to B.

The routes are read from the app when the test runs, so a route added later is
walked too. A new kind of URL argument fails ``test_every_route_argument_is_classified``
until someone decides whether it points at something a tenant owns, and a new
POST route fails ``test_every_form_is_accepted_from_an_authorised_person``
until ``FORMS`` has the form it accepts.
"""

import io
import json
import re
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
import responses

from officesentry import delivery, issued, month_end, permissions, projects, roles, sso, store, tenant_groups
from officesentry.db import utcnow
from officesentry.projects import CATALOG
from officesentry.reports import data_reports, designs, evidence_answers, export, views
from officesentry.reports.data_reports import REPORTS
from officesentry.reports.evidence_answers import QUESTIONS
from officesentry.web import saved_views
from tests import auditdata
from tests import sharepoint_data as sd
from tests import test_client_sign_in as csi
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
ROLES = ["analyst", "client", "analyst-by-group", "client-with-group", "client-by-microsoft", "manager"]
# A refusal is the access check's: 403, 404 or the sign-in page. A 400 is form validation, which runs
# after the access check, so an ungranted tenant's URL never gets that far.
REFUSED = (403, 404)
B_NAMES = (b"Bravo Inc", b"bravo.onmicrosoft.com")
# The word in every display name of B's people, groups, devices, mailboxes and sites (seed.mark_names).
B_MARK = "Zebedee"
SKIP_LISTS = {"auth.logout", "auth.end_session", "static", "health", "health_ready"}
# Everything of B's a page could give away: its name and domain, the mark on its people, groups, devices,
# mailboxes and sites, its group, its contact's and its client account's address, its recommendation and
# note, its issued file and the saved view of its client account.
B_WORDS = (
    *B_NAMES,
    B_MARK.encode(),
    b"Bravo group",
    b"@bravo.example",
    b"Bravo action",
    b"Bravo note",
    b"bravo.pdf",
    b"Bravo view",
)
# The reports where B's mark shows (its people, groups, devices and distribution lists), for the walks that
# open a report by key.
MARKED_REPORTS = ("all-users", "groups", "devices", "distribution-lists")
# What the walk of A's pages leaves out: signing out, static files and the health checks.
NOT_OPENED = {"auth.logout", "static", "health", "health_ready"}
# POST routes that reach Microsoft or a webhook, which the form walks leave alone (there's no network).
NO_NETWORK = {
    "tenants.test_connection",
    "admin.start_setup",
    "admin.check_setup",
    "admin.update_app",
    "admin.test_rollover",
    "sso.start_setup",
    "sso.check_setup",
    "alerts.send_test",
}
# ...and the signed-in person's own sign-in and account, which would sign them out or change how they sign in.
NOT_POSTED = NO_NETWORK | {
    "auth.logout",
    "auth.login",
    "auth.login_code",
    "auth.new_password",
    "auth.setup",
    "auth.change_password",
    "auth.two_step_setup",
    "auth.two_step_off",
    "auth.new_recovery_codes",
    "auth.end_session",
    "auth.end_other_sessions",
}

# The form an authorised person submits to each POST route that names a tenant or one of its objects:
# what its page sends, filled in with values the route accepts. A new POST route is listed here, so the
# walk sends it a form the route would act on, not an empty one it may refuse before looking at the
# tenant. ``ids`` is the world's: B's objects by URL argument, and its groups by name.
FORMS = {
    "client_access.add": lambda ids: {"email": "jo@bravo.example"},
    "email.add_tenant_contact": lambda ids: {"name": "Bravo Ops", "email": "ops@bravo.example"},
    "email.edit_schedule": lambda ids: SCHEDULE_FORM | {"contacts": str(ids["contact_id"])},
    "email.new_schedule": lambda ids: SCHEDULE_FORM | {"contacts": str(ids["contact_id"])},
    "email.save_tenant_alert": lambda ids: {"min_severity": "high", "contacts": str(ids["contact_id"])},
    "onboarding.contact": lambda ids: {"contact_name": "Bravo IT", "contact_email": "it@bravo.example"},
    "onboarding.mark_sent": lambda ids: {"sent": "1"},
    "audit_addon.mark_sent": lambda ids: {"sent": "1"},
    "audit_addon.switch_off": lambda ids: {"delete": "1"},
    "projects.set_status": lambda ids: {"status": next(iter(projects.STATUSES)), "note": "Agreed by phone"},
    "reporting.add_action": lambda ids: {"title": "Turn on MFA for everyone", "detail": "By the end of the month"},
    "reporting.add_note": lambda ids: {"rule_key": "admin_mfa", "body": "Spoke to the client", "visibility": "staff"},
    "reporting.approve": lambda ids: {"reason": ""},
    "reporting.assign": lambda ids: {"reviewer": "me"},
    "reporting.leave_out": lambda ids: {"op": "out", "item": "part:licences"},
    "reporting.save_commentary": lambda ids: {"commentary": "A quiet month."},
    "reporting.set_state": lambda ids: {"state": "in_review"},
    "reporting.update_action": lambda ids: {"op": "save", "status": "in_progress", "target_month": ""},
    "finding.hand": lambda ids: {"to": ""},
    "finding.note": lambda ids: {"note": "Waiting on the client"},
    "home.hand_line": lambda ids: {"key": "collection", "to": ""},
    "home.snooze_line": lambda ids: {"key": "collection", "until": "tomorrow"},
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
    "audit_addon.check",
    "audit_addon.switch_on",
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


def _objects(db, tenant: int, word: str) -> dict:
    """One of everything a URL can point at, owned by ``tenant`` and named with ``word`` (Bravo or Alpha)."""
    now, low = utcnow(), word.lower()
    ids = {"tenant_id": tenant}
    ids["run_id"] = db.execute("SELECT MAX(id) FROM runs WHERE tenant_id = ?", (tenant,)).fetchone()[0]
    ids["finding_id"] = db.execute("SELECT MAX(id) FROM findings WHERE tenant_id = ?", (tenant,)).fetchone()[0]
    ids["schedule_id"] = db.execute(
        "INSERT INTO email_schedules (kind, tenant_id, reports, frequency, hour, timezone, created_at)"
        " VALUES ('report', ?, '[\"review\"]', 'monthly', 8, 'UTC', ?)",
        (tenant, now),
    ).lastrowid
    ids["alert_id"] = db.execute(
        "INSERT INTO email_alerts (tenant_id, min_severity, created_at) VALUES (?, 'high', ?)", (tenant, now)
    ).lastrowid
    ids["contact_id"] = delivery.add_contact(db, tenant, f"{word} IT", f"it@{low}.example")
    ids["delivery_id"] = db.execute(
        "INSERT INTO email_deliveries (kind, schedule_id, tenant_id, subject, recipients, status, error, created_at)"
        " VALUES ('report', ?, ?, 'Monthly', ?, 'failed', 'x', ?)",
        (ids["schedule_id"], tenant, f"it@{low}.example", now),
    ).lastrowid
    ids["report_id"] = issued.archive(
        db,
        tenant,
        [issued.IssuedFile("review", "Monthly review", f"{low}.pdf", "application/pdf", b"%PDF-1 " + low.encode())],
        period="2026-09",
        trigger="manual",
        user="x",
    )[0]
    ids["action_id"] = db.execute(
        "INSERT INTO report_actions (tenant_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
        (tenant, f"{word} action", now, now),
    ).lastrowid
    ids["note_id"] = db.execute(
        "INSERT INTO finding_notes (tenant_id, rule_key, body, visibility, created_at)"
        " VALUES (?, 'admin_mfa', ?, 'staff', ?)",
        (tenant, f"{word} note", now),
    ).lastrowid
    return ids


def _plan(db, name: str, tenant: int) -> int:
    plan = month_end.save_plan(
        db,
        {
            "name": name,
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
    month_end.apply_plan(db, month_end.get_plan(db, plan), [tenant], "test", "UTC")
    return plan


def _marked(batches: dict, word: str) -> dict:
    """The add-on's sample records with every person, mailbox and file in them named with ``word``."""
    return {
        kind: [(when, json.loads(json.dumps(records).replace("contoso", word))) for when, records in items]
        for kind, items in batches.items()
    }


@pytest.fixture
def world(app, db, vault, connection_id):
    alpha = store.create_tenant(db, "Alpha Ltd", "alpha.onmicrosoft.com", connection_id)
    bravo = store.create_tenant(db, "Bravo Inc", "bravo.onmicrosoft.com", connection_id)
    db.execute("UPDATE tenants SET consented_at = ?", (utcnow(),))
    seed_tenant(db, alpha)
    seed_tenant(db, bravo, mark=B_MARK)
    # The audit log add-on on for both, B's audit log naming B's mark (its report and pages are walked too).
    now = datetime.now(UTC)
    auditdata.addon_on(db, vault, [alpha, bravo], started_at=auditdata.iso(now - timedelta(hours=3)))
    auditdata.collect(db, alpha, auditdata.sample(now), now)
    auditdata.collect(db, bravo, _marked(auditdata.sample(now), B_MARK), now)
    ids = {**_objects(db, bravo, "Bravo"), "connection_id": connection_id}
    ids["alpha"] = _objects(db, alpha, "Alpha")  # A's own, for walking the pages someone granted A may open
    ids["design_id"] = designs.create(db, "Walk design", "test", designs.STARTERS["executive"][2])
    ids["plan_id"] = _plan(db, "Bravo plan", bravo)
    ids["alpha"]["plan_id"] = _plan(db, "Alpha plan", alpha)
    # B's own people: a client account (named, as they often are, by its address) and its saved view.
    ids["account_id"] = make_user(db, "ceo@bravo.example", role="client", tenants=[bravo])
    saved_views.store(db, ids["account_id"], "all-users", "Bravo view", "sort=all-users/name")
    ids["view_id"] = db.execute("SELECT id FROM saved_views WHERE name = 'Bravo view'").fetchone()[0]
    for name, tenant in (("Alpha group", alpha), ("Bravo group", bravo)):
        group = tenant_groups.create(db, {"name": name, "category": "", "description": ""}, "test")
        tenant_groups.set_members(db, group, [tenant])
        ids[name] = group
    return alpha, bravo, ids


def microsoft_client(db, vault, username: str, alpha: int, tenants) -> int:
    """One of A's people with a client account here and no password, connected to their work account in A's
    own tenant (Jo, ``csi.JO``) as the Client sign-in page connects it, and granted ``tenants``. Client sign-in
    with Microsoft is set up and on (keeping a sign-in certificate already made), and A's Directory ID is
    A's tenant. A grant of a client in another organisation must count for nothing (``access._organisation``)."""
    sso.forget_keys()
    if not sso.settings(db).get("sso_key_enc"):
        sso.new_certificate(db, vault)
    sso.store_manual(db, vault, csi.MSP, csi.CLIENT_ID, "commercial", "Office Sentry sign-in", "admin")
    sso.save(db, {"sso_enabled": "1", "sso_multi_tenant": "1", "sso_clients": "1"})
    db.execute("UPDATE tenants SET tenant_ref = ? WHERE id = ?", (csi.ALPHA, alpha))
    user = make_user(db, username, role="client", tenants=tenants)
    db.execute(
        "UPDATE users SET password_hash = ?, entra_oid = ?, entra_tid = ?, entra_upn = ? WHERE id = ?",
        (sso.NO_PASSWORD, csi.JO["oid"], csi.ALPHA, csi.JO["upn"], user),
    )
    db.commit()
    return user


def sign_in_with_microsoft(client, username: str, person: dict = csi.JO) -> None:
    """Sign in with Microsoft as ``person``, from their own tenant, as test_client_sign_in does (Microsoft
    simulated at the HTTP level), and check it reached ``username``'s account."""
    responses.start()
    try:
        assert csi.sign_in(client, person).status_code == 302
    finally:
        responses.stop()
        responses.reset()
    assert csi.signed_in_as(client) == username


def _sign_in(client, db, role: str, alpha: int, ids: dict, vault=None) -> None:
    """Someone who may see tenant A only, however ``role`` grants it."""
    if role == "client-by-microsoft":
        # One of A's people, signed in with their own work account from A's tenant. B is granted too, by
        # mistake, but it isn't in A's organisation, so that grant counts for nothing.
        microsoft_client(db, vault, "someone", alpha, [alpha, ids["tenant_id"]])
        sign_in_with_microsoft(client, "someone")
        return
    if role == "analyst-by-group":
        user = make_user(db, "someone", role="analyst")
        tenant_groups.set_user_groups(db, user, [ids["Alpha group"]])
    elif role == "client-with-group":
        # Group grants are for analysts only: a client holding one (say, after a role change) gets nothing from it.
        user = make_user(db, "someone", role="client", tenants=[alpha])
        tenant_groups.set_user_groups(db, user, [ids["Bravo group"]])
    elif role == "manager":
        # Every capability, on A only: settings and system status, which cover every client, never take
        # effect for a role like this (permissions.EVERY_CLIENT); the rest must stop at A.
        manager = roles.create(db, "test", "Manager of A", "", permissions.STAFF, permissions.GRANTED, permissions.KEYS)
        user = make_user(db, "someone", role="analyst", tenants=[alpha])
        roles.assign(db, user, roles.get(db, manager))
    else:
        make_user(db, "someone", role=role, tenants=[alpha])
    login(client, "someone")


def _url(rule, ids: dict, ext: str = "csv") -> str:
    """The rule's URL with the objects in ``ids`` in it (tenant B's, unless a walk passes A's), and a real
    name wherever it takes one."""
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
        "ext": ext,
        "table": "x",
        "user_id": USERS[0]["id"],
        "scheme": "cyber-essentials",
        "mailbox_id": USERS[0]["id"],  # Ann's mailbox has her account's object ID
        "gid": "g1",  # the Sales team
        "site_id": sd.SITE_USAGE[0]["siteId"],  # Finance
    }

    def value(m):
        converter, name = m.group(1), m.group(2)
        if name == "user_id" and converter == "int":
            return str(ids.get("account_id", 1))  # a portal account; without ``int:``, a person in the tenant
        return str(ids.get(name, names.get(name, "1")))

    return re.sub(r"<(?:(\w+):)?(\w+)>", value, rule.rule)


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
    pages = ("users", "data/all-users", "data/groups", "data/devices", "data/distribution-lists")
    for page in (*pages, "data/files-and-mailboxes"):
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
def test_ungranted_tenant_is_refused_on_every_route(app, client, db, vault, world, role):
    alpha, bravo, ids = world
    _sign_in(client, db, role, alpha, ids, vault)
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
def test_lists_and_rollups_never_mention_an_ungranted_tenant(app, client, db, vault, world, role, pick):
    alpha, _, ids = world
    _sign_in(client, db, role, alpha, ids, vault)
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
    assert seen > (40 if role.startswith("analyst") or role == "manager" else 3)


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


def _readable(resp) -> bytes:
    """What a person gets from a response: the page or file, with every file inside a spreadsheet, Word
    file or zip read out (PDFs come as the page they're made from, see ``_pdf_as_page``)."""
    body = resp.data
    if body[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(body)) as z:
            return b"\n".join(z.read(name) for name in z.namelist())
    return body


def _leaks(body: bytes) -> list[bytes]:
    return [word for word in B_WORDS if word in body]


@pytest.fixture
def _pdf_as_page(monkeypatch):
    """Every PDF is the page WeasyPrint is handed, so what it would print can be searched (and the walk
    doesn't spend minutes typesetting)."""
    monkeypatch.setattr(export, "to_pdf", lambda html, base_url: html.encode())


def _a_urls(app, db, alpha: int, ids: dict) -> list[tuple[str, str]]:
    """Every GET route with A's objects in it: each report by key once per marked report, each table of it,
    and each download in every format. (endpoint, URL) pairs."""
    tenant = store.get_tenant(db, alpha)
    tables = {key: views.table_slugs(data_reports.build(db, tenant, key))[0] for key in MARKED_REPORTS}
    out = []
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.endpoint in NOT_OPENED:
            continue
        keys = MARKED_REPORTS if "key" in rule.arguments and rule.endpoint != "reports.evidence_answer" else (None,)
        exts = ("csv", "xlsx", "pdf", "docx", "txt") if "ext" in rule.arguments else ("csv",)
        for key in keys:
            for ext in exts:
                url = _url(rule, {**ids, **ids["alpha"]}, ext=ext)
                if key is not None:
                    url = url.replace(f"/{next(iter(REPORTS))}", f"/{key}", 1)
                    url = re.sub(r"/x(?=$|\.|/)", f"/{tables[key]}", url) if "table" in rule.arguments else url
                out.append((rule.endpoint, url))
    return out


@pytest.mark.parametrize("role", ["analyst", "client", "client-by-microsoft", "manager"])
def test_nothing_of_another_tenant_on_any_page_or_download(app, client, db, vault, world, role, _pdf_as_page):
    """Every GET route, opened with A's IDs by someone granted A, plain and with ``?tenant=`` naming B: no page,
    download or file inside one carries anything of B's."""
    alpha, bravo, ids = world
    _sign_in(client, db, role, alpha, ids, vault)
    opened = 0
    for endpoint, url in _a_urls(app, db, alpha, ids):
        for query in ("", f"tenant={bravo}&tenant_id={bravo}"):
            full = url + ("&" if "?" in url else "?") + query if query else url
            resp = client.get(full)
            assert not _leaks(_readable(resp)), (role, endpoint, full, resp.status_code, _leaks(_readable(resp)))
            opened += resp.status_code == 200
    assert opened >= {"analyst": 450, "client": 120, "client-by-microsoft": 120, "manager": 480}[role], opened


@pytest.mark.parametrize("role", ["analyst", "manager"])
def test_another_tenant_s_object_beside_your_own_tenant_is_refused(app, client, db, world, role):
    """A URL naming A (which they may open) and one of B's objects: refused, by GET and POST."""
    alpha, bravo, ids = world
    _sign_in(client, db, role, alpha, ids)
    mixed = [r for r in _tenant_rules(app) if "tenant_id" in r.arguments and len(set(r.arguments) & TENANT_ARGS) > 1]
    assert mixed  # /tenants/<A>/report-history/<B's file>, today
    for rule in mixed:
        url = _url(rule, {**ids, "tenant_id": alpha})
        for method in sorted(rule.methods & {"GET", "POST"}):
            resp = client.open(url, method=method, data=_form(rule, ids) if method == "POST" else None)
            assert resp.status_code in REFUSED and not _leaks(_readable(resp)), (role, method, url, resp.status_code)


def _b_state(db, bravo: int, ids: dict) -> dict:
    """Everything of B's a form could change, and every link from someone else's object to one of B's."""
    state = {}
    for (table,) in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
        columns = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        if "tenant_id" in columns and table not in ("audit_log", "rollup_cache", "summary_cache", "search_index"):
            rows = db.execute(f"SELECT * FROM {table} WHERE tenant_id = ?", (bravo,)).fetchall()
            state[table] = sorted(map(tuple, rows), key=repr)
    for name, sql in {
        "B's recipients": "SELECT r.* FROM email_schedule_recipients r JOIN contacts c ON c.id = r.contact_id"
        " WHERE c.tenant_id = :b",
        "B's alert recipients": "SELECT r.* FROM email_alert_recipients r JOIN contacts c ON c.id = r.contact_id"
        " WHERE c.tenant_id = :b",
        "B's group": "SELECT * FROM tenant_group_members WHERE tenant_id = :b OR group_id = :g",
        "grants of B's group": "SELECT * FROM user_tenant_groups WHERE group_id = :g",
        "B's account": "SELECT * FROM users WHERE id = :u",
        "B's account's access": "SELECT * FROM user_access WHERE user_id = :u",
        "B's account's views": "SELECT * FROM saved_views WHERE user_id = :u",
    }.items():
        params = {"b": bravo, "g": ids["Bravo group"], "u": ids["account_id"]}
        state[name] = sorted(map(tuple, db.execute(sql, params).fetchall()), key=repr)
    return state


def test_forms_naming_another_tenant_change_nothing_of_it(app, client, db, world):
    """The manager (every capability, on A only) posts every form there is: on A's URLs with B's objects in
    the form, and on the routes that name no tenant with B's tenant, group, contact and view IDs in every
    field that takes one. Nothing of B's changes, nothing of A's is linked to B's, and B is granted to nobody."""
    alpha, bravo, ids = world
    make_user(db, "owner")  # the walk may change roles and accounts; there is always an Administrator
    _sign_in(client, db, "manager", alpha, ids)
    before = _b_state(db, bravo, ids)
    naming_b = {
        "tenants": str(bravo),
        "tenant": str(bravo),
        "tenant_id": str(bravo),
        "contacts": str(ids["contact_id"]),
        "groups": str(ids["Bravo group"]),
        "group_id": str(ids["Bravo group"]),
        "views": [f"s{ids['view_id']}", f"k{ids['view_id']}"],
    }
    rules = [r for r in app.url_map.iter_rules() if "POST" in r.methods and r.endpoint not in NOT_POSTED]
    rules.sort(key=lambda r: ("delete" in r.endpoint, r.rule))  # deletes last, so the rest find their objects
    posted = 0
    for rule in rules:
        # A's tenant and objects in the URL, except that beside A's tenant an object is B's.
        url_ids = {**ids, **ids["alpha"]} if "tenant_id" not in rule.arguments else {**ids, "tenant_id": alpha}
        resp = client.post(_url(rule, url_ids), data={**_form(rule, ids), **naming_b})
        if "/login" in resp.headers.get("Location", ""):
            login(client, "someone")  # a form that signs everyone holding a role out; carry on
        posted += 1
    assert posted >= 90
    assert _b_state(db, bravo, ids) == before
    crossed = db.execute(
        "SELECT s.id FROM email_schedule_recipients r JOIN email_schedules s ON s.id = r.schedule_id"
        " JOIN contacts c ON c.id = r.contact_id WHERE s.tenant_id = ? AND c.tenant_id = ?"
        " UNION ALL SELECT a.id FROM email_alert_recipients r JOIN email_alerts a ON a.id = r.alert_id"
        " JOIN contacts c ON c.id = r.contact_id WHERE a.tenant_id = ? AND c.tenant_id = ?",
        (alpha, bravo, alpha, bravo),
    ).fetchall()
    assert not crossed
    granted = db.execute("SELECT user_id FROM user_tenants WHERE tenant_id = ?", (bravo,)).fetchall()
    assert [r[0] for r in granted] == [ids["account_id"]]


def _row_counts(db) -> dict:
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    noise = {"audit_log", "sessions", "login_failures", "rollup_cache"}
    return {t: db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables if t not in noise}
