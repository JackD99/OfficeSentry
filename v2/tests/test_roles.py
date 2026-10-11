"""Roles, capabilities and overrides (permissions.py, roles.py, web/access.py).

Every route in the URL map is opened as a person holding each built-in role: a view that declares a
capability (or a kind) the role lacks must answer 403, and one the role holds must not. A new route
fails here until it says what it needs with ``@needs`` (or ``@login_required`` for pages anyone may
open, listed in ``ANYONE``).
"""

from __future__ import annotations

import json
import re

import pytest

from officesentry import permissions, roles, store
from officesentry.db import utcnow
from officesentry.web import access
from tests.conftest import login, make_user
from tests.seed import seed_tenant

# Pages every signed-in person may open: their own account, signing out, the home redirect, the shared
# report library, and the consent page a client's administrator reaches through a signed link.
ANYONE = {
    "auth.logout",
    "auth.end_session",
    "auth.end_other_sessions",
    "auth.account",
    "auth.change_password",
    "auth.two_step_setup",
    "auth.two_step_off",
    "auth.new_recovery_codes",
    "home.index",
    "home.choose",
    "reports.overview",
    "tenants.dashboard",
    "tenants.tenant",
    "views.save",
    "views.delete",
}
# Steps of signing in, which send someone already signed in back to the start.
SIGNING_IN = {"auth.new_password", "auth.login_code"}
# The consent link's return has no decorator (see onboarding.client_returned); for anyone signed in it
# is the admin's own return, which needs this.
IMPLIED = {"tenants.consent_callback": ("clients.manage",)}
PUBLIC = {"static", "health", "health_ready", "auth.login", "auth.setup", "branding.brand_css", "branding.logo"}


def role_user(db, username: str, key: str, tenants=(), all_tenants=False) -> int:
    """An account holding the built-in role ``key`` (the way the user page assigns it)."""
    role = roles.by_key(db, key)
    uid = make_user(db, username, role=role.column, all_tenants=all_tenants, tenants=tenants)
    db.execute("UPDATE users SET role_id = ? WHERE id = ?", (role.id, uid))
    return uid


# -- The seed and the model -----------------------------------------------------------------------------


def test_built_in_roles_match_the_code(db):
    rows = {r.key: r for r in roles.all_roles(db)}
    assert list(rows) == [t.key for t in permissions.TEMPLATES]
    for t in permissions.TEMPLATES:
        r = rows[t.key]
        assert (r.name, r.description, r.kind, r.scope) == (t.name, t.description, t.kind, t.scope), t.key
        assert r.capabilities == t.capabilities, t.key
        assert r.built_in and not r.changed_from_template
    assert rows["administrator"].capabilities == frozenset(permissions.KEYS)
    assert not rows["administrator"].editable and rows["team_leader"].editable
    # A client role only ever holds report capabilities.
    assert rows["client_viewer"].capabilities <= permissions.CLIENT_KEYS
    assert "export" in rows["client_viewer"].capabilities


def test_capabilities_are_well_formed():
    keys = [c.key for c in permissions.CAPABILITIES]
    assert len(keys) == len(set(keys))
    groups = {g for g, _, _ in permissions.GROUPS}
    assert all(c.group in groups for c in permissions.CAPABILITIES)
    assert all(c.title and c.help for c in permissions.CAPABILITIES)
    # Nothing an admin runs the product with is open to a client role.
    assert (
        not {"settings.manage", "users.manage", "clients.manage", "collect", "system.manage"} & permissions.CLIENT_KEYS
    )
    assert permissions.allowed_for("client", permissions.KEYS) == set(permissions.CLIENT_KEYS)
    assert permissions.allowed_for("staff", ["export", "nope"]) == {"export"}


def test_today_s_users_keep_their_access(db, two_tenants):
    """An account from before roles (role_id NULL, or a role_id that disagrees with users.role, as code
    that only writes users.role leaves it) gets the built-in role its old role meant."""
    alpha, _ = two_tenants
    for column, key in permissions.UPGRADE_ROLE.items():
        uid = make_user(db, f"old-{column}", role=column, tenants=[alpha])
        assert roles.role_of(db, store_user(db, uid)).key == key
    # users.role changed to admin by older code while role_id still says Client viewer: the kind wins.
    uid = make_user(db, "promoted", role="admin")
    db.execute("UPDATE users SET role_id = ? WHERE id = ?", (roles.by_key(db, "client_viewer").id, uid))
    assert roles.role_of(db, store_user(db, uid)).key == "administrator"
    assert roles.access_for(db, store_user(db, uid)).is_admin


def store_user(db, uid: int):
    return db.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def test_effective_access_adds_up(db, two_tenants):
    alpha, bravo = two_tenants
    uid = role_user(db, "fl", "first_line", tenants=[alpha, bravo])
    user = store_user(db, uid)
    a = roles.access_for(db, user)
    assert a.kind == "staff" and not a.sees_all and a.can("reports.identity", alpha)
    assert not a.can("collect") and not a.can("reports.security", alpha)
    roles.set_overrides(
        db,
        "admin",
        user,
        [
            roles.Override("collect", alpha, True),  # may refresh Alpha only
            roles.Override("reports.security", None, True),  # may see findings everywhere...
            roles.Override("reports.security", bravo, False),  # ...except Bravo
            roles.Override("reports.identity", None, False),  # taken away everywhere
            roles.Override("reports.identity", alpha, True),  # (a per-client allow never beats that)
            roles.Override("users.manage", None, True),
        ],
    )
    user = store_user(db, uid)
    a = roles.access_for(db, user)
    assert a.can("collect", alpha) and not a.can("collect", bravo) and a.can("collect")  # somewhere
    assert a.can("reports.security", alpha) and not a.can("reports.security", bravo)
    assert not a.can("reports.identity", alpha) and not a.can("reports.identity")
    assert a.can("users.manage") and "users.manage" in a.anywhere()
    assert "reports.identity" not in a.anywhere()
    # Saving the same overrides again changes nothing (and signs nobody out); the audit log has the change.
    before = user["session_version"]
    assert not roles.set_overrides(db, "admin", user, roles.overrides(db, uid))
    assert store_user(db, uid)["session_version"] == before
    detail = db.execute("SELECT detail FROM audit_log WHERE action = 'user_access_changed'").fetchone()[0]
    assert detail.startswith("fl: ") and f"+collect@{alpha}" in detail and "-reports.identity" in detail


def test_client_role_never_holds_staff_capabilities(db, two_tenants):
    alpha, _ = two_tenants
    uid = role_user(db, "cv", "client_viewer", tenants=[alpha])
    user = store_user(db, uid)
    roles.set_overrides(
        db, "admin", user, [roles.Override("collect", None, True), roles.Override("export", None, False)]
    )
    kept = {o.capability for o in roles.overrides(db, uid)}
    assert kept == {"export"}
    a = roles.access_for(db, store_user(db, uid))
    assert not a.can("collect") and not a.can("export", alpha) and a.can("reports.security", alpha)
    # Even a row written straight into the table counts for nothing.
    db.execute(
        "INSERT INTO user_access (user_id, capability, tenant_id, allow, created_at, created_by)"
        " VALUES (?, 'settings.manage', NULL, 1, ?, 'x')",
        (uid, utcnow()),
    )
    assert not roles.access_for(db, store_user(db, uid)).can("settings.manage")


def test_administrator_is_never_narrowed(db, two_tenants):
    alpha, _ = two_tenants
    uid = role_user(db, "boss", "administrator")
    user = store_user(db, uid)
    roles.set_overrides(db, "admin", user, [roles.Override("settings.manage", None, False)])
    a = roles.access_for(db, store_user(db, uid))
    assert a.can("settings.manage") and a.can("reports.email", alpha) and a.sees_all


# -- Every route as every role ------------------------------------------------------------------------


def _url(rule) -> str:
    return re.sub(r"<(?:\w+:)?\w+>", "1", rule.rule)


def test_every_route_says_what_it_needs(app):
    missing = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint in PUBLIC or rule.endpoint in ANYONE or rule.endpoint in SIGNING_IN:
            continue
        view = app.view_functions[rule.endpoint]
        if not hasattr(view, "needs") and rule.endpoint not in IMPLIED:
            missing.append(rule.endpoint)
    assert not missing, f"routes without @needs (add it, or list a page anyone may open in ANYONE): {missing}"


def _expected_403(role, view, endpoint: str) -> bool:
    needs = IMPLIED.get(endpoint, getattr(view, "needs", ()))
    kind = getattr(view, "kind", None)
    if kind == "staff" and role.kind == "client":
        return True
    if kind == "admin" and role.kind != "admin":
        return True
    return role.kind != "admin" and any(c not in role.capabilities for c in needs)


@pytest.mark.parametrize("key", [t.key for t in permissions.TEMPLATES])
def test_every_route_as_each_role(app, client, db, two_tenants, key):
    """A view answers 403 exactly when the role lacks something it declared. Pages the role may open are
    only opened by GET (a POST would act); pages it may not are tried every way, since the refusal
    comes before anything happens."""
    alpha, bravo = two_tenants
    make_user(db, "owner")  # so the walk never deletes the last Administrator
    role = roles.by_key(db, key)
    role_user(db, "walker", key, tenants=[alpha, bravo])
    login(client, "walker")
    wrong = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint in PUBLIC or rule.endpoint in ANYONE or rule.endpoint in SIGNING_IN:
            continue
        view = app.view_functions[rule.endpoint]
        url = _url(rule)
        if _expected_403(role, view, rule.endpoint):
            for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
                resp = client.open(url, method=method)
                if resp.status_code != 403:
                    wrong.append((rule.endpoint, method, resp.status_code, "expected 403"))
        elif "GET" in rule.methods:
            resp = client.get(url)
            if resp.status_code == 403:
                wrong.append((rule.endpoint, "GET", 403, f"{role.name} holds what it needs"))
            elif resp.status_code == 302 and "/login" in resp.headers.get("Location", ""):
                wrong.append((rule.endpoint, "GET", 302, "signed out"))
    assert not wrong, wrong


def test_private_key_is_never_served_to_any_role(client, db, connection_id):
    make_user(db, "owner")
    for t in permissions.TEMPLATES:
        role_user(db, f"u-{t.key}", t.key, all_tenants=True)
        login(client, f"u-{t.key}")
        resp = client.get(f"/admin/connections/{connection_id}/certificate.cer")
        assert b"PRIVATE KEY" not in resp.data, t.key
        assert resp.status_code == (200 if t.key == "administrator" else 403), t.key
        client.post("/logout")
    row = store.get_connection(db, connection_id)
    assert "PRIVATE KEY" not in row["key_enc"]


# -- Overrides on the web -----------------------------------------------------------------------------


def _first_in(group: str) -> str:
    from officesentry.reports.registry import DECLARED

    return next(k for k, d in DECLARED.items() if d.group == group)


def test_a_capability_taken_away_on_one_client(client, db, two_tenants):
    """Second line everywhere, but no email reports on Alpha: the report answers 403 there, opens on
    Bravo, the tab is gone on Alpha, and the cross-client rollup leaves Alpha out."""
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    seed_tenant(db, bravo)
    uid = role_user(db, "sl", "second_line", all_tenants=True)
    user = store_user(db, uid)
    roles.set_overrides(db, "admin", user, [roles.Override("reports.email", alpha, False)])
    login(client, "sl")
    key = _first_in("email")
    assert client.get(f"/tenants/{alpha}/data/{key}").status_code == 403
    assert client.get(f"/tenants/{bravo}/data/{key}").status_code == 200
    assert client.get(f"/tenants/{alpha}/data/{key}.csv").status_code == 403
    page = client.get(f"/across/{key}").data
    assert b"Bravo Inc" in page and b"Alpha Ltd" not in page
    # An identity report still opens on Alpha; it's the one capability that's gone.
    assert client.get(f"/tenants/{alpha}/data/{_first_in('identity')}").status_code == 200


def test_a_capability_added_on_one_client(client, db, two_tenants):
    alpha, bravo = two_tenants
    uid = role_user(db, "fl", "first_line", tenants=[alpha, bravo])
    roles.set_overrides(db, "admin", store_user(db, uid), [roles.Override("collect", alpha, True)])
    login(client, "fl")
    assert client.get(f"/tenants/{alpha}/collection").status_code == 200
    assert client.get(f"/tenants/{bravo}/collection").status_code == 403
    assert client.post(f"/tenants/{bravo}/collect").status_code == 403
    assert client.post(f"/tenants/{alpha}/collect").status_code == 302


def test_a_capability_taken_away_everywhere_wins(client, db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    uid = role_user(db, "tl", "team_leader")
    roles.set_overrides(
        db,
        "admin",
        store_user(db, uid),
        [roles.Override("export", None, False), roles.Override("export", alpha, True)],
    )
    login(client, "tl")
    key = _first_in("identity")
    assert client.get(f"/tenants/{alpha}/data/{key}").status_code == 200
    assert client.get(f"/tenants/{alpha}/data/{key}.csv").status_code == 403
    assert client.get(f"/tenants/{alpha}/data/{key}.xlsx").status_code == 403


def test_menus_follow_capabilities(client, db, two_tenants):
    alpha, _ = two_tenants
    role_user(db, "fl", "first_line", tenants=[alpha])
    login(client, "fl")
    page = client.get(f"/tenants/{alpha}").data.decode()
    tabs = page.split('class="tabs"')[1].split("</nav>")[0]
    assert ">Findings<" not in tabs and ">Reports<" in tabs and ">Users<" in tabs and ">Settings<" not in tabs
    menu = page.split("<nav")[1].split("</nav>")[0]
    assert 'href="/upcoming"' not in menu and ">Month-end<" not in menu and 'href="/projects"' not in menu
    client.post("/logout")
    role_user(db, "am", "account_manager", tenants=[alpha])
    login(client, "am")
    page = client.get(f"/tenants/{alpha}").data.decode()
    tabs = page.split('class="tabs"')[1].split("</nav>")[0]
    assert ">Findings<" in tabs and ">Users<" not in tabs
    menu = page.split("<nav")[1].split("</nav>")[0]
    assert ">Month-end<" in menu and 'href="/projects"' in menu and 'href="/upcoming"' in menu


# -- The user page ------------------------------------------------------------------------------------


def _edit(client, uid: int, **form):
    return client.post(f"/admin/users/{uid}", data=form)


def test_user_page_assigns_roles(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    resp = client.post("/admin/users", data={"username": "ann", "password": "Correct-horse-battery-1", "role": "2"})
    uid = int(resp.headers["Location"].rsplit("/", 1)[1])
    assert roles.role_of(db, store_user(db, uid)).key == "team_leader"
    assert store_user(db, uid)["role"] == "analyst"
    page = client.get(f"/admin/users/{uid}").data.decode()
    assert "Team leader" in page and "What ann can do" in page and "Manage clients" in page
    assert client.get("/admin/users").data.decode().count("Team leader") >= 1
    # The old names still work (the SSO sign-in and scripts write users.role), landing on the built-in role.
    _edit(client, uid, role="client", tenants=[str(alpha)])
    u = store_user(db, uid)
    assert u["role"] == "client" and roles.role_of(db, u).key == "client_viewer"
    _edit(client, uid, role=str(roles.by_key(db, "auditor").id), all_tenants="1")
    u = store_user(db, uid)
    assert u["role"] == "analyst" and u["all_tenants"] == 1 and roles.role_of(db, u).key == "auditor"
    # A client role never keeps "every client" or group grants.
    _edit(client, uid, role=str(roles.by_key(db, "client_viewer").id), all_tenants="1")
    assert store_user(db, uid)["all_tenants"] == 0
    assert (
        client.post(
            "/admin/users", data={"username": "bob", "password": "Correct-horse-battery-1", "role": "99"}
        ).status_code
        == 400
    )


def test_there_is_always_an_administrator(client, db, two_tenants):
    admin = make_user(db, "admin")
    tl = str(roles.by_key(db, "team_leader").id)
    login(client, "admin")
    _edit(client, admin, role=tl)  # never your own Administrator role
    assert store_user(db, admin)["role"] == "admin"
    # Someone else who manages users (a Team leader given users.manage) can't demote, disable or delete the
    # only Administrator either; once there's a second one, they can.
    hr = role_user(db, "hr", "team_leader")
    roles.set_overrides(db, "admin", store_user(db, hr), [roles.Override("users.manage", None, True)])
    client.post("/logout")
    login(client, "hr")
    _edit(client, admin, role=tl)
    _edit(client, admin, disabled="1")
    assert store_user(db, admin)["role"] == "admin" and store_user(db, admin)["disabled"] == 0
    assert client.post(f"/admin/users/{admin}/delete").status_code == 302
    assert store_user(db, admin) is not None
    assert "only Administrator" in client.get(f"/admin/users/{admin}").data.decode()
    role_user(db, "second", "administrator")
    _edit(client, admin, role=tl)
    assert store_user(db, admin)["role"] == "analyst"


def test_role_change_signs_the_person_out(client, db, two_tenants, app):
    alpha, _ = two_tenants
    uid = role_user(db, "sl", "second_line", tenants=[alpha])
    login(client, "sl")
    assert client.get(f"/tenants/{alpha}").status_code == 200
    with app.app_context():
        roles.assign(db, uid, roles.by_key(db, "first_line"))
    assert client.get(f"/tenants/{alpha}").status_code == 302  # back to sign-in


def test_role_json_is_defensive(db):
    db.execute("UPDATE roles SET capabilities = 'not json' WHERE key = 'auditor'")
    assert roles.by_key(db, "auditor").capabilities == frozenset()
    db.execute("UPDATE roles SET capabilities = ? WHERE key = 'client_viewer'", (json.dumps(["collect", "export"]),))
    assert roles.by_key(db, "client_viewer").capabilities == frozenset({"export"})


def test_visible_tenants_follow_scope_and_denies(app, db, two_tenants):
    alpha, bravo = two_tenants
    uid = role_user(db, "am", "account_manager", tenants=[alpha])
    assert [t["id"] for t in access.visible_tenants(db, store_user(db, uid))] == [alpha]
    uid = role_user(db, "tl", "team_leader")
    assert {t["id"] for t in access.visible_tenants(db, store_user(db, uid))} == {alpha, bravo}
    assert access.can_see_tenant(db, store_user(db, uid), bravo)
    uid = role_user(db, "cv", "client_viewer", tenants=[bravo])
    assert not access.can_see_tenant(db, store_user(db, uid), alpha)
