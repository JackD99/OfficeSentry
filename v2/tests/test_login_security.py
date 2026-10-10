"""Sign-in hardening: two-step codes, lockout, server-side sessions, setup link, cookies, keys."""

import json
import os
import re
import stat
import time

import pytest
from flask import Flask

from officesentry import totp
from officesentry.__main__ import main as cli
from officesentry.config import load_config, setup_token
from officesentry.crypto import Vault
from officesentry.web import create_app
from officesentry.web.access import safe_next
from tests.conftest import PASSWORD, login, make_user

COOKIE = "officesentry"


@pytest.fixture
def staff_mfa(cfg):
    cfg.require_mfa = "staff"
    return cfg


def pending_secret(db, vault, username):
    row = db.execute("SELECT totp_pending_enc FROM users WHERE username = ?", (username,)).fetchone()
    return vault.decrypt(row["totp_pending_enc"])


def enrol(client, db, vault, username):
    """Sign in with a password and finish the forced two-step setup. Returns (secret, recovery codes)."""
    assert login(client, username).headers["Location"].endswith("/two-step/setup")
    assert client.get("/two-step/setup").status_code == 200
    secret = pending_secret(db, vault, username)
    page = client.post("/two-step/setup", data={"code": totp.code(secret)}).get_data(as_text=True)
    codes = re.findall(r"<li>([a-z0-9]{5}-[a-z0-9]{5})</li>", page)
    return secret, codes


def next_code(secret):
    # The step after the one just used, so the replay guard doesn't refuse it.
    return totp.code(secret, now=time.time() + totp.STEP)


# -- TOTP -------------------------------------------------------------------------


def test_totp_matches_rfc_6238_and_refuses_replay():
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # "12345678901234567890"
    assert totp.code(secret, now=59) == "287082"
    assert totp.code(secret, now=1111111109) == "081804"
    step = totp.verify(secret, "081 804", now=1111111109)
    assert step is not None
    assert totp.verify(secret, "081804", last_step=step, now=1111111109) is None
    assert totp.verify(secret, "000000", now=1111111109) is None
    assert totp.verify(secret, "abc", now=1111111109) is None


def test_qr_code_is_inline_svg():
    svg = totp.qr_svg(totp.provisioning_uri(totp.new_secret(), "jack@example.com"))
    assert svg.startswith("<svg") and "<script" not in svg


# -- Two-step sign-in ---------------------------------------------------------------


def test_staff_must_set_up_two_step_then_use_it(client, db, vault, staff_mfa):
    make_user(db, "admin")
    assert client.get("/").status_code == 302  # not signed in during setup
    login(client, "admin")
    assert client.get("/").headers["Location"].startswith("/login")
    secret, codes = enrol(client, db, vault, "admin")
    assert len(codes) == 10
    assert client.get("/").status_code == 200

    client.post("/logout")
    assert login(client, "admin").headers["Location"].endswith("/login/code")
    assert client.get("/").status_code == 302  # password alone isn't enough
    assert client.post("/login/code", data={"code": "000000"}).status_code == 200
    assert client.post("/login/code", data={"code": next_code(secret)}).status_code == 302
    assert client.get("/").status_code == 200


def test_recovery_code_works_once(client, db, vault, staff_mfa):
    make_user(db, "admin")
    _secret, codes = enrol(client, db, vault, "admin")
    client.post("/logout")
    login(client, "admin")
    assert client.post("/login/code", data={"code": codes[0].upper()}).headers["Location"] == "/"
    client.post("/logout")
    login(client, "admin")
    client.post("/login/code", data={"code": codes[0]})
    assert client.get("/").status_code == 302


def test_wrong_codes_lock_the_account(client, db, vault, staff_mfa):
    make_user(db, "admin")
    secret, _codes = enrol(client, db, vault, "admin")
    client.post("/logout")
    login(client, "admin")
    for _ in range(5):
        client.post("/login/code", data={"code": "000000"})
    assert db.execute("SELECT locked_until FROM users").fetchone()[0]
    # Locked: even the right password and code don't get in.
    assert login(client, "admin").status_code == 200
    client.post("/login/code", data={"code": next_code(secret)})
    assert client.get("/").status_code == 302


def test_password_lockout_and_admin_unlock(client, db):
    uid = make_user(db, "analyst", role="analyst")
    for _ in range(5):
        login(client, "analyst", "wrong password!")
    assert login(client, "analyst").status_code == 429  # this address waits, so not signed in
    make_user(db, "boss")
    other = client.application.test_client()
    login(other, "boss")
    other.post(f"/admin/users/{uid}/unlock")
    assert login(client, "analyst").status_code == 302


def test_clients_are_not_forced_by_default(client, db, two_tenants, staff_mfa):
    make_user(db, "client", role="client", tenants=[two_tenants[0]])
    assert login(client, "client").headers["Location"] == "/"


def test_turn_on_and_off_from_account_page(client, db, vault, staff_mfa):
    make_user(db, "client", role="client")
    login(client, "client")
    assert "Two-step sign-in is off" in client.get("/account").get_data(as_text=True)
    assert 'name="password"' in client.get("/two-step/setup").get_data(as_text=True)
    secret = pending_secret(db, vault, "client")
    # Someone using a stolen session can't add their own authenticator without the password.
    page = client.post("/two-step/setup", data={"code": totp.code(secret), "password": "not the password"})
    assert "That password isn&#39;t right" in page.get_data(as_text=True)
    assert db.execute("SELECT totp_enabled_at FROM users").fetchone()[0] is None
    client.post("/two-step/setup", data={"code": totp.code(secret), "password": PASSWORD})
    assert db.execute("SELECT totp_enabled_at FROM users").fetchone()[0]
    assert client.get("/account").status_code == 200  # still signed in on this browser
    client.post("/account/two-step/turn-off", data={"password": PASSWORD})
    assert db.execute("SELECT totp_enabled_at FROM users").fetchone()[0] is None


NEW_PASSWORD = "my own long passphrase"


def test_a_password_an_admin_chose_is_replaced_before_two_step_setup(app, cfg, db, vault):
    make_user(db, "boss")
    boss = app.test_client()
    login(boss, "boss")
    boss.post("/admin/users", data={"username": "ana", "role": "analyst", "password": PASSWORD})
    cfg.require_mfa = "staff"  # analysts must use two-step
    ana = app.test_client()
    # Whoever learns the password the admin chose can't go straight to adding their own authenticator...
    assert login(ana, "ana").headers["Location"].endswith("/login/new-password")
    assert ana.get("/two-step/setup").headers["Location"].endswith("/login")
    assert ana.get("/").status_code == 302
    page = ana.post("/login/new-password", data={"password": PASSWORD, "confirm": PASSWORD})
    assert "not the one you were given" in page.get_data(as_text=True)
    # ...the password is replaced first, then two-step is set up, and the given password stops working.
    resp = ana.post("/login/new-password", data={"password": NEW_PASSWORD, "confirm": NEW_PASSWORD})
    assert resp.headers["Location"].endswith("/two-step/setup")
    ana.get("/two-step/setup")
    ana.post("/two-step/setup", data={"code": totp.code(pending_secret(db, vault, "ana"))})
    assert ana.get("/").status_code == 200
    assert db.execute("SELECT must_change_password FROM users WHERE username = 'ana'").fetchone()[0] == 0
    assert login(app.test_client(), "ana").status_code == 200


def test_a_reset_password_is_replaced_after_the_two_step_code(client, db, vault, staff_mfa, app):
    uid = make_user(db, "ana", role="analyst")
    secret, _codes = enrol(client, db, vault, "ana")
    client.post("/logout")
    db.execute("UPDATE users SET must_change_password = 1 WHERE id = ?", (uid,))  # an admin reset it
    assert login(client, "ana").headers["Location"].endswith("/login/code")  # the code still comes first
    resp = client.post("/login/code", data={"code": next_code(secret)})
    assert resp.headers["Location"].endswith("/login/new-password")
    resp = client.post("/login/new-password", data={"password": NEW_PASSWORD, "confirm": NEW_PASSWORD})
    assert resp.headers["Location"] == "/" and client.get("/").status_code == 200


def test_admin_can_reset_lost_phone(client, db, vault, staff_mfa):
    uid = make_user(db, "analyst", role="analyst")
    enrol(client, db, vault, "analyst")
    admin = client.application.test_client()
    make_user(db, "boss")
    enrol(admin, db, vault, "boss")
    admin.post(f"/admin/users/{uid}/reset-two-step")
    assert client.get("/").status_code == 302  # signed out
    assert login(client, "analyst").headers["Location"].endswith("/two-step/setup")


# -- Sessions ------------------------------------------------------------------------


def test_logout_is_post_only_and_kills_the_cookie(client, db):
    make_user(db, "admin")
    login(client, "admin")
    stolen = client.get_cookie(COOKIE).value
    assert client.get("/logout").status_code == 405
    client.post("/logout")
    thief = client.application.test_client()
    thief.set_cookie(COOKIE, stolen)
    assert thief.get("/").status_code == 302
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_sign_out_other_browsers(app, db):
    make_user(db, "admin")
    laptop, phone = app.test_client(), app.test_client()
    login(laptop, "admin")
    login(phone, "admin")
    page = laptop.get("/account").get_data(as_text=True)
    assert "This browser" in page and "Sign out everywhere else" in page
    laptop.post("/account/sessions/end-others")
    assert phone.get("/").status_code == 302
    assert laptop.get("/").status_code == 200


def test_password_change_signs_out_other_browsers(app, db):
    make_user(db, "admin")
    laptop, phone = app.test_client(), app.test_client()
    login(laptop, "admin")
    login(phone, "admin")
    new = "a much longer new password"
    laptop.post("/account/password", data={"current": PASSWORD, "password": new, "confirm": new})
    assert laptop.get("/").status_code == 200
    assert phone.get("/").status_code == 302


def test_guessing_the_current_password_locks_the_account(app, db):
    """A stolen session can't be used to guess the password on the change-password form."""
    from officesentry.web.auth import MAX_FAILURES

    make_user(db, "admin")
    client = app.test_client()
    login(client, "admin")
    before = db.execute("SELECT password_hash FROM users WHERE username = 'admin'").fetchone()[0]
    new = "a much longer new password"
    for _ in range(MAX_FAILURES):
        client.post("/account/password", data={"current": "wrong guess", "password": new, "confirm": new})
    resp = client.post("/account/password", data={"current": PASSWORD, "password": new, "confirm": new},
                       follow_redirects=True)
    assert "Too many wrong passwords" in resp.get_data(as_text=True)
    row = db.execute("SELECT locked_until, password_hash FROM users WHERE username = 'admin'").fetchone()
    assert row["locked_until"] and row["password_hash"] == before


def test_idle_session_expires(client, db, cfg):
    make_user(db, "admin")
    login(client, "admin")
    db.execute("UPDATE sessions SET last_seen_at = '2020-01-01T00:00:00Z'")
    assert client.get("/").status_code == 302
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_disabling_a_user_ends_their_session(app, db):
    uid = make_user(db, "analyst", role="analyst")
    make_user(db, "boss")
    user, admin = app.test_client(), app.test_client()
    login(user, "analyst")
    login(admin, "boss")
    admin.post(f"/admin/users/{uid}", data={"role": "analyst", "disabled": "1"})
    assert user.get("/").status_code == 302


# -- First-run setup -----------------------------------------------------------------


def test_setup_needs_the_one_time_link(client, db, cfg, staff_mfa):
    token = setup_token(cfg.data_dir)
    page = client.get("/setup").get_data(as_text=True)
    assert "setup link" in page and "csrf_token" not in page
    form = {"username": "owner", "password": PASSWORD, "confirm": PASSWORD}
    client.post("/setup", data=form)
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
    assert "setup code isn" in client.get("/setup?token=guess").get_data(as_text=True)

    assert client.get(f"/setup?token={token}").headers["Location"] == "/setup"  # token leaves the URL
    resp = client.post("/setup", data=form)
    assert resp.headers["Location"].endswith("/two-step/setup")
    assert db.execute("SELECT role FROM users").fetchone()["role"] == "admin"
    assert not (cfg.data_dir / "setup-token").exists()


def test_setup_link_is_logged_and_private(cfg, db, caplog):
    create_app(cfg)
    token = setup_token(cfg.data_dir)
    assert f"/setup?token={token}" in caplog.text
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(cfg.data_dir / "setup-token").st_mode) == 0o600


# -- Redirects, cookies, proxies -------------------------------------------------------


@pytest.mark.parametrize("target", [
    "//evil.example/x", "/\\evil.example", "/\t/evil.example", "/\n/evil.example", "https://evil.example",
    "javascript:alert(1)", "", None, "evil.example",
])
def test_next_must_stay_on_site(target):
    app = Flask(__name__)
    app.add_url_rule("/", "tenants.dashboard", lambda: "")
    with app.test_request_context():
        assert safe_next(target) == "/"


def test_next_allows_local_paths(app):
    with app.test_request_context():
        assert safe_next("/tenants/3?tab=users") == "/tenants/3?tab=users"


def test_cookie_flags_when_secure(cfg, db):
    cfg.secure_cookies = True
    app = create_app(cfg)
    make_user(db, "admin")
    resp = login(app.test_client(), "admin")
    cookie = resp.headers["Set-Cookie"]
    assert cookie.startswith("__Host-officesentry=")
    for flag in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/"):
        assert flag in cookie
    assert "Domain" not in cookie


def test_proxy_headers_trusted_only_when_configured(cfg, db):
    make_user(db, "admin")
    headers = {"X-Forwarded-For": "203.0.113.9"}
    login(create_app(cfg).test_client(), "admin").close()
    cfg.trusted_proxies = 1
    client = create_app(cfg).test_client()
    client.post("/login", data={"username": "admin", "password": PASSWORD}, headers=headers)
    ips = [r[0] for r in db.execute("SELECT ip FROM sessions ORDER BY id")]
    assert ips == ["127.0.0.1", "203.0.113.9"]


# -- Key files ------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_loose_secrets_file_is_tightened(tmp_path, monkeypatch):
    for var in ("SECRET_KEY", "ENCRYPTION_KEYS"):
        monkeypatch.delenv("OFFICESENTRY_" + var, raising=False)
    load_config(data_dir=tmp_path / "d")
    path = tmp_path / "d" / "secrets.json"
    os.chmod(path, 0o644)
    load_config(data_dir=tmp_path / "d")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_rotate_keys_new_replaces_both_keys(cfg, db, vault, connection_id, monkeypatch):
    for var in ("SECRET_KEY", "ENCRYPTION_KEYS", "DATA_DIR"):
        monkeypatch.delenv("OFFICESENTRY_" + var, raising=False)
    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(cfg.data_dir))
    uid = make_user(db, "admin")
    db.execute("UPDATE users SET totp_secret_enc = ? WHERE id = ?", (vault.encrypt("SECRETSECRET"), uid))
    db.execute("INSERT INTO settings (key, value) VALUES ('smtp_password_enc', ?)", (vault.encrypt("smtp"),))
    db.execute("INSERT INTO settings (key, value) VALUES ('graph_secret_enc', ?)", (vault.encrypt("graph"),))
    before = json.loads(cfg.secrets_path.read_text(encoding="utf-8"))

    cli(["rotate-keys", "--new"])

    after = json.loads(cfg.secrets_path.read_text(encoding="utf-8"))
    assert after["secret_key"] != before["secret_key"]
    assert after["encryption_keys"] != before["encryption_keys"] and len(after["encryption_keys"]) == 1
    new = Vault(after["encryption_keys"])
    assert new.decrypt(db.execute("SELECT totp_secret_enc FROM users").fetchone()[0]) == "SECRETSECRET"
    assert new.decrypt(db.execute("SELECT value FROM settings WHERE key = 'smtp_password_enc'").fetchone()[0]) == "smtp"
    assert new.decrypt(db.execute("SELECT value FROM settings WHERE key = 'graph_secret_enc'").fetchone()[0]) == "graph"
    assert "PRIVATE KEY" in new.decrypt(db.execute("SELECT key_enc FROM app_connections").fetchone()[0])
