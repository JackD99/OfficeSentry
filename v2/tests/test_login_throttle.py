"""Wrong passwords slow down where they come from; they can't lock the real person out from elsewhere."""

from datetime import UTC, datetime, timedelta

from officesentry import throttle
from officesentry.web import create_app
from tests.conftest import PASSWORD, login, make_user

ATTACKER = "198.51.100.7"
HOME = "203.0.113.20"


def _from(client, ip, username, password=PASSWORD):
    return client.post("/login", data={"username": username, "password": password}, environ_base={"REMOTE_ADDR": ip})


def test_wrong_passwords_from_one_place_dont_lock_the_person_out(app, db):
    make_user(db, "admin")
    attacker = app.test_client()
    for _ in range(50):
        _from(attacker, ATTACKER, "admin", "wrong password")
    # That address waits, even with the right password...
    resp = _from(attacker, ATTACKER, "admin")
    assert resp.status_code == 429 and int(resp.headers["Retry-After"]) > 0
    assert "Too many failed sign-ins from your network" in resp.get_data(as_text=True)
    # ...but the admin, from their own address, signs straight in.
    assert _from(app.test_client(), HOME, "admin").status_code == 302
    # Only the first few were checked at all.
    assert (
        db.execute("SELECT COUNT(*) FROM login_attempts WHERE ip = ?", (ATTACKER,)).fetchone()[0]
        == throttle.PER_ADDRESS_AND_NAME
    )


def test_one_address_is_limited_across_names(app, db):
    make_user(db, "admin")
    attacker = app.test_client()
    for i in range(throttle.PER_ADDRESS):
        assert _from(attacker, ATTACKER, f"guess{i}", "x").status_code == 200
    assert _from(attacker, ATTACKER, "admin").status_code == 429
    assert _from(app.test_client(), HOME, "admin").status_code == 302


def test_unknown_names_are_limited_like_real_ones(app, db):
    make_user(db, "admin")
    client = app.test_client()
    for name in ("admin", "nobody"):
        for _ in range(throttle.PER_ADDRESS_AND_NAME):
            assert _from(client, ATTACKER, name, "x").status_code == 200
        assert _from(client, ATTACKER, name.upper(), "x").status_code == 429


def test_spread_out_attack_slows_new_addresses_but_not_known_ones(app, db):
    make_user(db, "admin")
    assert _from(app.test_client(), HOME, "admin").status_code == 302  # where the admin usually signs in
    for i in range(throttle.NAME_SOFT_LIMIT):
        _from(app.test_client(), f"198.51.100.{i + 10}", "admin", "wrong password")
    # New addresses now get one try at a time on this account...
    assert _from(app.test_client(), "192.0.2.1", "admin", "wrong password").status_code == 200
    resp = _from(app.test_client(), "192.0.2.2", "admin")
    assert resp.status_code == 429 and "new devices are slowed down" in resp.get_data(as_text=True)
    assert int(resp.headers["Retry-After"]) <= throttle.SPACING_START.total_seconds() + 1
    # ...while the admin's own address isn't held up at all.
    assert _from(app.test_client(), HOME, "admin").status_code == 302


def test_forwarded_for_only_counts_behind_a_trusted_proxy(cfg, db):
    make_user(db, "admin")
    direct = create_app(cfg).test_client()
    for i in range(throttle.PER_ADDRESS_AND_NAME):
        direct.post(
            "/login", data={"username": "admin", "password": "wrong"}, headers={"X-Forwarded-For": f"198.51.100.{i}"}
        )
    # Sent straight to the portal, the header is ignored: those five came from one address.
    assert login(direct, "admin").status_code == 429
    cfg.trusted_proxies = 1
    proxied = create_app(cfg).test_client()
    resp = proxied.post("/login", data={"username": "admin", "password": PASSWORD}, headers={"X-Forwarded-For": HOME})
    assert resp.status_code == 302


def test_admin_unlock_clears_the_limits(app, db):
    uid = make_user(db, "analyst", role="analyst")
    client = app.test_client()
    for _ in range(throttle.PER_ADDRESS_AND_NAME):
        login(client, "analyst", "wrong password!")
    make_user(db, "boss")
    boss = app.test_client()
    _from(boss, HOME, "boss")
    page = boss.get(f"/admin/users/{uid}").get_data(as_text=True)
    assert "5 wrong passwords from 1 address in the last 15 minutes" in page
    boss.post(f"/admin/users/{uid}/unlock")
    assert login(client, "analyst").status_code == 302


def test_old_attempts_are_pruned(db):
    now = datetime.now(UTC)
    old = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ancient = (now - timedelta(days=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.executemany(
        "INSERT INTO login_attempts (ts, ip, username, outcome) VALUES (?, ?, ?, ?)",
        [(old, ATTACKER, "x", "failed"), (old, HOME, "admin", "ok"), (ancient, HOME, "admin", "ok")],
    )
    assert throttle.prune(db) == 2
    assert db.execute("SELECT outcome, ts FROM login_attempts").fetchall()[0]["ts"] == old
