"""The roles pages (web/roles.py) and the override editor on the user page (admin.user_access)."""

from __future__ import annotations

from officesentry import permissions, roles
from tests.conftest import login, make_user
from tests.test_roles import role_user, store_user


def _audit(db, action: str) -> list[str]:
    return [r[0] for r in db.execute("SELECT detail FROM audit_log WHERE action = ? ORDER BY id", (action,))]


def test_roles_page_lists_every_role_with_holders(client, db, two_tenants):
    make_user(db, "admin")
    role_user(db, "sam", "team_leader")
    role_user(db, "jo", "team_leader")
    login(client, "admin")
    page = client.get("/admin/roles").data.decode()
    for t in permissions.TEMPLATES:
        assert t.name in page
    row = page.split("Team leader")[1].split("</tr>")[0]
    assert 'data-label="People">2<' in row
    assert "Custom" not in page and "Changed" not in page


def test_edit_a_built_in_role_and_reset_it(client, db, two_tenants, app):
    alpha, _ = two_tenants
    make_user(db, "admin")
    jo = role_user(db, "jo", "first_line", tenants=[alpha])
    login(client, "admin")
    role = roles.by_key(db, "first_line")
    page = client.get(f"/admin/roles/{role.id}").data.decode()
    assert 'name="capabilities" value="reports.identity" data-cap="reports.identity" checked' in page
    assert 'value="collect" data-cap="collect">' in page  # offered, not ticked
    assert (
        "users.manage" in page and "Reset to default" in page and "disabled" in page.split("Reset to default")[0][-200:]
    )
    before = store_user(db, jo)["session_version"]
    resp = client.post(
        f"/admin/roles/{role.id}",
        data={
            "name": "Service desk",
            "description": "Looks things up.",
            "scope": "all",
            "capabilities": ["reports.identity", "reports.people", "collect", "settings.manage"],
        },
    )
    assert resp.status_code == 302
    role = roles.get(db, role.id)
    assert role.name == "Service desk" and role.scope == "all" and role.changed_from_template
    assert role.capabilities == {"reports.identity", "reports.people", "collect", "settings.manage"}
    assert store_user(db, jo)["session_version"] == before + 1  # signed out to pick it up
    detail = _audit(db, "role_updated")[-1]
    assert detail.startswith("Service desk: renamed from First line; scope all; added") and "removed" in detail
    page = client.get("/admin/roles").data.decode()
    assert "Service desk" in page and "Changed" in page and "First line" not in page
    # The person now sees every client and may refresh data; the page they open says so.
    client.post("/logout")
    login(client, "jo")
    assert client.get(f"/tenants/{two_tenants[1]}/collection").status_code == 200
    client.post("/logout")
    login(client, "admin")
    assert client.post(f"/admin/roles/{role.id}/reset").status_code == 302
    role = roles.get(db, role.id)
    assert role.name == "First line" and not role.changed_from_template
    assert role.capabilities == permissions.TEMPLATE_BY_KEY["first_line"].capabilities


def test_administrator_cannot_be_changed(client, db, two_tenants):
    make_user(db, "admin")
    login(client, "admin")
    admin = roles.by_key(db, "administrator")
    page = client.get(f"/admin/roles/{admin.id}").data.decode()
    assert "can't be changed" in page and "Save role" not in page
    assert client.post(f"/admin/roles/{admin.id}", data={"name": "Boss"}).status_code == 403
    assert client.post(f"/admin/roles/{admin.id}/reset").status_code == 302
    assert client.post(f"/admin/roles/{admin.id}/delete").status_code == 302
    assert roles.get(db, admin.id).name == "Administrator"


def test_custom_roles(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "admin")
    login(client, "admin")
    # A copy of Second line, then narrowed; then a client role that can't be given staff capabilities.
    second = roles.by_key(db, "second_line")
    resp = client.post("/admin/roles", data={"name": "Night shift", "copy": str(second.id)})
    role_id = int(resp.headers["Location"].rsplit("/", 1)[1])
    role = roles.get(db, role_id)
    assert not role.built_in and role.kind == "staff" and role.capabilities == second.capabilities
    assert role.description == second.description
    client.post(
        f"/admin/roles/{role_id}", data={"name": "Night shift", "capabilities": ["collect"], "scope": "granted"}
    )
    assert roles.get(db, role_id).capabilities == {"collect"}
    resp = client.post("/admin/roles", data={"name": "Client admin", "copy": "client"})
    cid = int(resp.headers["Location"].rsplit("/", 1)[1])
    client.post(
        f"/admin/roles/{cid}",
        data={
            "name": "Client admin",
            "capabilities": ["reports.security", "settings.manage", "collect"],
            "scope": "all",
        },
    )
    crole = roles.get(db, cid)
    assert crole.kind == "client" and crole.scope == "granted" and crole.capabilities == {"reports.security"}
    page = client.get(f"/admin/roles/{cid}").data.decode()
    assert "settings.manage" not in page and "Delete Client admin" in page
    # Names are unique; a role in use can't be deleted; a free one can.
    assert client.post("/admin/roles", data={"name": "night SHIFT", "copy": ""}).status_code == 302
    assert db.execute("SELECT COUNT(*) FROM roles WHERE name = 'Night shift' COLLATE NOCASE").fetchone()[0] == 1
    uid = make_user(db, "nina", role="analyst", tenants=[alpha])
    roles.assign(db, uid, role)
    client.post(f"/admin/roles/{role_id}/delete")
    assert roles.get(db, role_id) is not None
    page = client.get(f"/admin/roles/{role_id}").data.decode()
    assert "nina" in page and "Move them to another role first" in page
    client.post(f"/admin/roles/{cid}/delete")
    assert roles.get(db, cid) is None
    assert _audit(db, "role_created")[0].startswith("Night shift (staff, granted): ")
    assert _audit(db, "role_deleted") == ["Client admin"]
    # Built-in roles can't be deleted, and a bad copy is refused.
    client.post(f"/admin/roles/{second.id}/delete")
    assert roles.get(db, second.id) is not None
    assert client.post("/admin/roles", data={"name": "", "copy": ""}).status_code == 302
    assert db.execute("SELECT COUNT(*) FROM roles WHERE name = ''").fetchone()[0] == 0


def test_override_editor_on_the_user_page(client, db, two_tenants):
    alpha, bravo = two_tenants
    make_user(db, "admin")
    uid = role_user(db, "jo", "first_line", tenants=[alpha, bravo])
    login(client, "admin")
    page = client.get(f"/admin/users/{uid}").data.decode()
    assert 'id="access"' in page and 'name="g_collect"' in page and 'name="g_settings.manage"' in page
    resp = client.post(
        f"/admin/users/{uid}/access",
        data={
            "g_collect": "allow",
            "g_export": "deny",
            "g_nonsense": "allow",
            "add_tenant": str(bravo),
            "add_capability": "reports.security",
            "add_allow": "allow",
        },
    )
    assert resp.status_code == 302 and resp.headers["Location"].endswith("#access")
    got = {(o.capability, o.tenant_id, o.allow) for o in roles.overrides(db, uid)}
    assert got == {("collect", None, True), ("export", None, False), ("reports.security", bravo, True)}
    page = client.get(f"/admin/users/{uid}").data.decode()
    assert 'name="g_collect" value="allow" checked' in page and 'name="g_export" value="deny" checked' in page
    assert "allowed on Bravo Inc" in page
    # Keep the everywhere choices, drop the per-client row, add a deny on Alpha.
    resp = client.post(
        f"/admin/users/{uid}/access",
        data={
            "g_collect": "allow",
            "g_export": "deny",
            "drop": [f"reports.security|{bravo}"],
            "add_tenant": str(alpha),
            "add_capability": "reports.identity",
            "add_allow": "deny",
        },
    )
    got = {(o.capability, o.tenant_id, o.allow) for o in roles.overrides(db, uid)}
    assert got == {("collect", None, True), ("export", None, False), ("reports.identity", alpha, False)}
    assert "taken away on Alpha Ltd" in client.get(f"/admin/users/{uid}").data.decode()
    # ...and it holds on the pages themselves.
    client.post("/logout")
    login(client, "jo")
    assert client.get(f"/tenants/{alpha}/data/mfa").status_code == 403
    assert client.get(f"/tenants/{bravo}/data/mfa").status_code == 200
    assert client.get(f"/tenants/{bravo}/data/mfa.csv").status_code == 403
    assert client.get(f"/tenants/{bravo}/collection").status_code == 200
    # A client account only gets report capabilities; an unknown client is refused.
    client.post("/logout")
    login(client, "admin")
    cv = role_user(db, "cv", "client_viewer", tenants=[alpha])
    client.post(f"/admin/users/{cv}/access", data={"g_collect": "allow", "g_export": "deny"})
    assert {o.capability for o in roles.overrides(db, cv)} == {"export"}
    assert (
        client.post(
            f"/admin/users/{cv}/access", data={"add_tenant": "999", "add_capability": "export", "add_allow": "allow"}
        ).status_code
        == 400
    )
    # An Administrator has nothing to customise.
    boss = role_user(db, "boss", "administrator")
    assert 'id="access"' not in client.get(f"/admin/users/{boss}").data.decode()
    client.post(f"/admin/users/{boss}/access", data={"g_export": "deny"})
    assert roles.overrides(db, boss) == []


def test_preview_lists_what_the_role_opens(client, db, two_tenants):
    make_user(db, "admin")
    login(client, "admin")
    page = client.get(f"/admin/roles/{roles.by_key(db, 'client_viewer').id}").data.decode()
    assert "Menu" not in page.split('id="preview"')[1] and "On a client" in page
    page = client.get(f"/admin/roles/{roles.by_key(db, 'team_leader').id}").data.decode()
    assert 'data-needs="users.manage"' in page and 'data-needs="clients.compare reports.tenant"' in page
