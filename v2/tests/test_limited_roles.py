"""Roles limited to some clients that also run part of Office Sentry (a custom role, or a capability added
for one person): they manage clients, users and the activity log only within their own clients, and can
neither see another client nor reach one through client groups, roles or settings that cover every client.

``tests/test_isolation_walk.py`` walks every route as such a role (``manager``); these are the cases
behind it, each with the one request that used to get through.
"""

from __future__ import annotations

from officesentry import permissions as p
from officesentry import roles, tenant_groups
from officesentry.db import audit
from tests.conftest import login, make_user


def _limited(db, username: str, capabilities, tenants=()) -> int:
    """A staff account on a custom role that sees only the clients granted to it."""
    role = roles.get(db, roles.create(db, "setup", f"Limited {username}", "", p.STAFF, p.GRANTED, capabilities))
    uid = make_user(db, username, role="analyst", tenants=tenants)
    roles.assign(db, uid, role)
    return uid


def _group(db, name: str, tenant_ids, granted_to=()) -> int:
    gid = tenant_groups.create(db, {"name": name, "category": "", "description": ""}, "setup")
    tenant_groups.set_members(db, gid, tenant_ids)
    for uid in granted_to:
        tenant_groups.set_user_groups(db, uid, [gid])
    return gid


def test_client_groups_need_every_client(client, db, two_tenants):
    """A group granted to an analyst hands them its clients, so someone who manages clients but sees only
    some can't open, fill, empty or delete groups, nor put a client in one."""
    alpha, bravo = two_tenants
    rm = _limited(db, "rm", [*p.SEE, "clients.manage"])
    north = _group(db, "North", [alpha], granted_to=[rm])  # rm sees Alpha through North
    other = make_user(db, "bravo-analyst", role="analyst")
    south = _group(db, "South", [bravo], granted_to=[other])
    login(client, "rm")
    assert client.get(f"/tenants/{bravo}/findings").status_code == 404
    for url in ("/admin/groups", f"/admin/groups/{north}", f"/admin/groups/{south}"):
        resp = client.get(url)
        assert resp.status_code == 403 and b"Bravo Inc" not in resp.data, url
    assert client.post(f"/admin/groups/{north}", data={"name": "North", "tenants": [alpha, bravo]}).status_code == 403
    assert client.post(f"/admin/groups/{south}", data={"name": "South", "tenants": []}).status_code == 403
    assert client.post(f"/admin/groups/{south}/delete").status_code == 403
    assert client.post("/admin/groups", data={"name": "Mine"}).status_code == 403
    assert client.post(f"/tenants/{alpha}/groups", data={"groups": [south]}).status_code == 403
    assert tenant_groups.members(db, north) == {alpha} and tenant_groups.members(db, south) == {bravo}
    assert client.get(f"/tenants/{bravo}/findings").status_code == 404  # still not theirs
    # The pages that offer groups don't, and the menu doesn't link them.
    for url in (f"/tenants/{alpha}/manage", "/tenants/new"):
        page = client.get(url).data
        assert b"South" not in page and b'name="groups"' not in page, url
    assert b"Client groups" not in client.get("/admin/tenants").data
    client.post("/tenants/new", data={"name": "Charlie", "tenant_ref": "charlie.example", "groups": [south]})
    charlie = db.execute("SELECT id FROM tenants WHERE name = 'Charlie'").fetchone()[0]
    assert tenant_groups.of_tenants(db, [charlie])[charlie] == []
    # Someone who manages clients and sees every one still does all of it.
    _limited(db, "lead", [*p.SEE, "clients.manage"])
    db.execute("UPDATE users SET all_tenants = 1 WHERE username = 'lead'")
    client.post("/logout")
    login(client, "lead")
    assert client.get(f"/admin/groups/{south}").status_code == 200
    assert client.post(f"/tenants/{alpha}/groups", data={"groups": [north, south]}).status_code == 302
    assert alpha in tenant_groups.members(db, south)


def test_import_preview_names_only_clients_the_importer_sees(client, db, two_tenants):
    alpha, bravo = two_tenants
    _limited(db, "rm", [*p.SEE, "clients.manage"], tenants=[alpha])
    login(client, "rm")
    csv = "name,tenant\nMine,alpha.onmicrosoft.com\nGuess,bravo.onmicrosoft.com\n"
    page = client.post("/admin/onboarding/import", data={"text": csv}).data.decode()
    assert "Already in Office Sentry as Alpha Ltd." in page
    assert "Already in Office Sentry." in page and "Bravo Inc" not in page
    # A group in the file would hand the new client to whoever the group is granted to.
    _group(db, "South", [bravo], granted_to=[make_user(db, "bravo-analyst", role="analyst")])
    csv = "name,tenant,group\nCharlie,charlie.example,South\n"
    page = client.post("/admin/onboarding/import", data={"text": csv, "confirm": "1"}).data.decode()
    assert "Only someone who sees every client puts clients in groups." in page
    assert db.execute("SELECT 1 FROM tenants WHERE tenant_ref = 'charlie.example'").fetchone() is None


def test_a_role_held_by_someone_out_of_reach_only_changes_for_an_administrator(client, db, two_tenants):
    """Changing a role changes everyone holding it, so the user page's rule holds here too: someone who
    manages users but isn't an Administrator can't change a role held by anyone who sees or does more."""
    alpha, _ = two_tenants
    _limited(db, "lead", [*p.SEE, "users.manage"], tenants=[alpha])
    team_leader = roles.by_key(db, "team_leader")
    make_user(db, "tl", role="analyst")
    roles.assign(db, db.execute("SELECT id FROM users WHERE username = 'tl'").fetchone()[0], team_leader)
    login(client, "lead")
    page = client.get(f"/admin/roles/{team_leader.id}").data.decode()
    assert "Only an Administrator can change this role." in page and "Save role" not in page
    caps = sorted(team_leader.capabilities | {"users.manage"})
    client.post(f"/admin/roles/{team_leader.id}", data={"name": "Team leader", "scope": "all", "capabilities": caps})
    client.post(f"/admin/roles/{team_leader.id}", data={"name": "Team leader", "capabilities": ["export"]})
    assert roles.get(db, team_leader.id).capabilities == team_leader.capabilities
    client.post(f"/admin/roles/{team_leader.id}/reset")
    assert roles.get(db, team_leader.id) == team_leader


def test_activity_log_shows_only_the_clients_someone_sees(client, db, two_tenants):
    alpha, bravo = two_tenants
    _limited(db, "rev", [*p.SEE, "activity_log.read"], tenants=[alpha])
    make_user(db, "ceo@bravo.example", role="client", tenants=[bravo])
    make_user(db, "it@alpha.example", role="client", tenants=[alpha])
    audit(db, "admin", "contact_added", bravo, "finance@bravo.example")
    audit(db, "admin", "contact_added", alpha, "finance@alpha.example")
    audit(db, "ceo@bravo.example", "login")
    audit(db, "it@alpha.example", "login")
    audit(db, "admin", "branding_updated", detail="accent")
    login(client, "rev")
    for url in ("/admin/activity", f"/admin/activity?tenant={bravo}", "/admin/activity.csv"):
        body = client.get(url).data
        assert b"bravo.example" not in body and b"Bravo Inc" not in body, url
    page = client.get("/admin/activity").data
    assert b"finance@alpha.example" in page and b"it@alpha.example" in page and b"branding_updated" in page
    # Someone who sees every client reads every row.
    make_user(db, "admin")
    client.post("/logout")
    login(client, "admin")
    page = client.get("/admin/activity").data
    assert b"finance@bravo.example" in page and b"ceo@bravo.example" in page


def test_user_pages_leave_out_other_clients_accounts(client, db, two_tenants):
    """A client's account is usually named after an address at that client."""
    alpha, bravo = two_tenants
    _limited(db, "lead", [*p.SEE, "users.manage"], tenants=[alpha])
    theirs = make_user(db, "ceo@bravo.example", role="client", tenants=[bravo])
    mine = make_user(db, "it@alpha.example", role="client", tenants=[alpha])
    make_user(db, "colleague", role="analyst", tenants=[bravo])
    login(client, "lead")
    page = client.get("/admin/users").data
    assert b"ceo@bravo.example" not in page and b"it@alpha.example" in page and b"colleague" in page
    assert client.get(f"/admin/users/{theirs}").status_code == 404
    assert client.post(f"/admin/users/{theirs}/sign-out").status_code == 404
    assert client.get(f"/admin/users/{mine}").status_code == 200


def test_settings_and_system_status_need_every_client(client, db, two_tenants):
    """Team-wide emails, global alerts, report plans, the support package and installing an update cover every
    client, so a role limited to some clients doesn't get them, from the role or added for the person."""
    alpha, _ = two_tenants
    _limited(db, "boss", list(p.KEYS), tenants=[alpha])
    staff = make_user(db, "staff", role="analyst", tenants=[alpha])
    roles.set_overrides(
        db,
        "setup",
        db.execute("SELECT * FROM users WHERE id = ?", (staff,)).fetchone(),
        [
            roles.Override("settings.manage", None, True),
            roles.Override("system.manage", alpha, True),
            roles.Override("system.update", None, True),
        ],
    )
    for username in ("boss", "staff"):
        client.post("/logout")
        login(client, username)
        for url in (
            "/admin/email",
            "/emails/digests/new",
            "/reporting/plans",
            "/admin/status",
            "/admin/support",
            "/admin/updates",
        ):
            assert client.get(url).status_code == 403, (username, url)
    client.post("/logout")
    login(client, "boss")
    assert client.get("/admin/activity").status_code == 200  # the rest of what the role holds still works
    db.execute("UPDATE users SET all_tenants = 1 WHERE username = 'boss'")
    assert client.get("/admin/email").status_code == 200
