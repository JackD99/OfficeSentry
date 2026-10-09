import re

import pytest

from officesentry import store
from officesentry.config import setup_token
from tests.conftest import PASSWORD, login, make_user

# The brand colours and logo are public on purpose: the sign-in page shows them (see web/branding.py).
PUBLIC = {"static", "health", "health_ready", "auth.login", "auth.setup", "branding.brand_css", "branding.logo"}


def test_every_page_requires_login(app, client, db, two_tenants):
    make_user(db, "admin")
    for rule in app.url_map.iter_rules():
        if rule.endpoint in PUBLIC:
            continue
        url = re.sub(r"<(?:int:)?\w+>", "1", rule.rule)
        method = "GET" if "GET" in rule.methods else "POST"
        resp = client.open(url, method=method)
        assert resp.status_code == 302 and "/login" in resp.headers["Location"], (rule.endpoint, resp.status_code)


def test_setup_creates_first_admin_once(client, db, cfg):
    assert client.get("/login").headers["Location"].endswith("/setup")
    client.get(f"/setup?token={setup_token(cfg.data_dir)}")
    resp = client.post("/setup", data={"username": "owner", "password": PASSWORD, "confirm": PASSWORD})
    assert resp.status_code == 302
    owner = db.execute("SELECT role, password_hash FROM users").fetchone()
    assert owner["role"] == "admin"
    assert owner["password_hash"].startswith("scrypt:")  # tests' make_user takes a cheap shortcut; the portal doesn't
    client.post("/setup", data={"username": "intruder", "password": PASSWORD, "confirm": PASSWORD})
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1


def test_client_sees_only_granted_tenant(client, db, two_tenants):
    alpha, bravo = two_tenants
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    page = client.get("/").get_data(as_text=True)
    assert "Alpha Ltd" in page and "Bravo Inc" not in page
    assert "Monthly review" in page  # clients get the report home, not the collection dashboard
    assert client.get(f"/tenants/{alpha}").status_code == 302
    assert client.get(f"/tenants/{bravo}").status_code == 404
    run_id = store.enqueue_run(db, alpha, "users")
    assert client.get(f"/runs/{run_id}").status_code == 403


def test_run_of_other_tenant_is_hidden(client, db, two_tenants):
    alpha, bravo = two_tenants
    run_id = store.enqueue_run(db, bravo, "users")
    make_user(db, "analyst", role="analyst", tenants=[alpha])
    login(client, "analyst")
    assert client.get(f"/runs/{run_id}").status_code == 404
    assert client.post(f"/tenants/{bravo}/collect").status_code == 404


def test_user_without_grants_sees_nothing(client, db, two_tenants):
    make_user(db, "newbie", role="analyst")
    login(client, "newbie")
    page = client.get("/").get_data(as_text=True)
    assert "Alpha Ltd" not in page and "Bravo Inc" not in page


def test_analyst_with_all_tenants(client, db, two_tenants):
    make_user(db, "lead", role="analyst", all_tenants=True)
    login(client, "lead")
    page = client.get("/").get_data(as_text=True)
    assert "Alpha Ltd" in page and "Bravo Inc" in page


def test_client_cannot_trigger_collection(client, db, two_tenants):
    alpha, _ = two_tenants
    make_user(db, "client", role="client", tenants=[alpha])
    login(client, "client")
    assert client.post(f"/tenants/{alpha}/collect").status_code == 403


@pytest.mark.parametrize("role", ["analyst", "client"])
@pytest.mark.parametrize("url", ["/admin/users", "/admin/connections", "/tenants/new",
                                 "/admin/connections/1/certificate.cer"])
def test_admin_pages_forbidden_to_others(client, db, connection_id, role, url):
    make_user(db, "someone", role=role, all_tenants=True)
    login(client, "someone")
    assert client.get(url).status_code == 403


def test_private_key_is_never_served(client, db, connection_id):
    make_user(db, "admin")
    login(client, "admin")
    resp = client.get(f"/admin/connections/{connection_id}/certificate.cer")
    assert resp.status_code == 200
    assert b"PRIVATE KEY" not in resp.data
    row = store.get_connection(db, connection_id)
    assert "PRIVATE KEY" not in row["key_enc"]


@pytest.mark.parametrize("target", ["//evil.example/x", "https://evil.example", "/\\evil.example"])
def test_login_does_not_open_redirect(client, db, target):
    make_user(db, "admin")
    resp = client.post("/login?next=" + target, data={"username": "admin", "password": PASSWORD})
    assert resp.headers["Location"] == "/"


def test_lockout_after_repeated_failures(client, db):
    make_user(db, "admin")
    for _ in range(5):
        login(client, "admin", "wrong password!!")
    resp = login(client, "admin")  # right password, but this address has to wait
    assert resp.status_code == 429
    assert client.get("/").status_code == 302


def test_disabled_or_deleted_user_loses_session(client, db, two_tenants):
    uid = make_user(db, "temp", role="analyst", all_tenants=True)
    login(client, "temp")
    assert client.get("/").status_code == 200
    db.execute("UPDATE users SET disabled = 1 WHERE id = ?", (uid,))
    assert client.get("/").status_code == 302
    db.execute("DELETE FROM users WHERE id = ?", (uid,))
    assert client.get("/").status_code == 302


def test_role_change_ends_existing_sessions(app, db, two_tenants):
    make_user(db, "admin")
    uid = make_user(db, "lead", role="analyst", all_tenants=True)
    admin_client, lead_client = app.test_client(), app.test_client()
    login(admin_client, "admin")
    login(lead_client, "lead")
    assert lead_client.get("/").status_code == 200
    admin_client.post(f"/admin/users/{uid}", data={"role": "client"})
    assert lead_client.get("/").status_code == 302


def test_security_headers(client, db):
    resp = client.get("/login")
    csp = resp.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert resp.headers["X-Frame-Options"] == "DENY"


def test_csrf_enforced_when_enabled(cfg, db):
    from officesentry.web import create_app

    cfg.extra = {}
    app = create_app(cfg)
    make_user(db, "admin")
    resp = app.test_client().post("/login", data={"username": "admin", "password": PASSWORD})
    assert resp.status_code == 400
