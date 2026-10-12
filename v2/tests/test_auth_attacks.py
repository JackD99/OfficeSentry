"""Sign-in, sessions and forms, attacked from outside and from a low-privilege account.

The rest of the suite proves each defence works when used as intended (two-step sign-in in
``test_login_security.py``, address limits in ``test_login_throttle.py``, races in ``test_login_races.py``,
roles in ``test_roles.py``, tenant isolation in ``test_isolation_walk.py``). This file tries to get past
them: forms sent from another site, links that change something when opened, cookies copied, kept, planted
or edited, sign-in answers that tell a guessed name or password apart, and a user manager reaching further
than they hold. The routes are read from the app when the tests run, so a route added later is attacked too.
"""

import base64
import hashlib
import hmac
import io
import json
import re
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
import responses
from cryptography.hazmat.primitives import serialization
from flask.sessions import SecureCookieSessionInterface
from itsdangerous import URLSafeTimedSerializer

from officesentry import alerts, permissions, roles, system_health, totp, worker
from officesentry.db import audit
from officesentry.web import alerts as web_alerts
from officesentry.web import auth, create_app
from tests import test_staff_sign_in as ms
from tests.conftest import PASSWORD, login, make_user
from tests.test_isolation_walk import _url, world  # noqa: F401 (world is a fixture)
from tests.test_login_security import enrol, next_code
from tests.test_staff_sign_in import configured  # noqa: F401 (a fixture)

STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}
COOKIE = "officesentry"

# Tables a page view may write without changing anything a person stored or acts on: when a session was last
# seen, the activity log (refused pages are counted), rows cached from stored data, and the sign-in limits.
BOOKKEEPING = {"sessions", "audit_log", "rollup_cache", "summary_cache", "search_index", "login_attempts"}
# Pages that write when opened, and what. Each is safe to open from a link on another site.
WRITES_WHEN_OPENED = {
    # Every visit makes a new authenticator secret, so whoever saw the page before can't add the one the
    # person goes on to scan (auth._setup_page). A visit from elsewhere only means scanning again.
    "auth.two_step_setup": {"users"},
    # The consent link on the page carries a one-time nonce tied to this browser's session
    # (tenants._use_consent_nonce); opening the page makes it, using it is a separate step.
    "tenants.tenant": {"consent_requests"},
    "tenant_admin.manage": {"consent_requests"},
}


# -- Helpers ------------------------------------------------------------------------------------------------


def _watch_writes(db) -> None:
    """Record every insert, update and delete on a table that isn't bookkeeping, by table name."""
    db.execute("CREATE TABLE IF NOT EXISTS _writes (tbl TEXT NOT NULL)")
    tables = [
        r[0]
        for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
        if r[0] not in BOOKKEEPING and r[0] != "_writes"
    ]
    for t in tables:
        for op in ("INSERT", "UPDATE", "DELETE"):
            db.execute(
                f'CREATE TRIGGER IF NOT EXISTS "_w_{t}_{op}" AFTER {op} ON "{t}" '
                f"BEGIN INSERT INTO _writes (tbl) VALUES ('{t}'); END"
            )
    db.commit()


def _writes(db) -> set[str]:
    found = {r[0] for r in db.execute("SELECT tbl FROM _writes")}
    db.execute("DELETE FROM _writes")
    db.commit()
    return found


def _walk_url(rule, ids: dict) -> str:
    """``test_isolation_walk._url``, with a number wherever the route takes one and the world names a thing by
    text (a person's directory id is a ``user_id`` there, a portal account's number here), so the route matches."""

    def number(m):
        value = ids.get(m.group(1))
        return str(value if isinstance(value, int) else 1)

    return _url(SimpleNamespace(rule=re.sub(r"<int:(\w+)>", number, rule.rule), endpoint=rule.endpoint), ids)


def _token(page: bytes) -> str:
    return re.search(rb'name="csrf_token" value="([^"]+)"', page).group(1).decode()


def _csrf_app(cfg):
    """The portal as it runs: form tokens checked (the other tests switch them off to keep forms short)."""
    cfg.extra = {}
    return create_app(cfg)


def _sign_in_with_token(client, username: str):
    token = _token(client.get("/login").data)
    resp = client.post("/login", data={"username": username, "password": PASSWORD, "csrf_token": token})
    assert resp.status_code == 302 and "/login" not in resp.headers["Location"]
    return resp


def _copy_cookie(source, target) -> None:
    """Put ``source``'s session cookie in ``target``, as someone who copied it from the browser would."""
    target.set_cookie(COOKIE, source.get_cookie(COOKIE).value)


def _signed_in(client) -> bool:
    resp = client.get("/account")
    return resp.status_code == 200


def _same_answer(page: str, username: str) -> str:
    """A sign-in page with the name typed, the script nonce (new on every page) and the form's token (signed
    with the time, so new every second) taken out, so two answers can be compared."""
    page = re.sub(r'nonce="[^"]*"', 'nonce=""', page.replace(f'value="{username}"', 'value=""'))
    return re.sub(r'name="csrf_token" value="[^"]*"', 'name="csrf_token" value=""', page)


def _custom_role(db, name: str, capabilities, scope=permissions.GRANTED, kind=permissions.STAFF) -> roles.Role:
    return roles.get(db, roles.create(db, "test", name, "", kind, scope, capabilities))


def _holder(db, username: str, role: roles.Role, tenants=()) -> int:
    uid = make_user(db, username, role=role.column, tenants=tenants)
    db.execute("UPDATE users SET role_id = ? WHERE id = ?", (role.id, uid))
    return uid


# -- Forms sent from another site ---------------------------------------------------------------------------


def test_every_form_refuses_a_request_without_this_browsers_token(cfg, db, world):  # noqa: F811
    """A page on another site can make a signed-in admin's browser post to any form. Every route that changes
    something refuses unless the post carries the token from this browser's own session: none at all, one
    from another signed-in browser, or one sent as a header."""
    _, _, ids = world
    make_user(db, "admin")
    make_user(db, "other-admin")
    app = _csrf_app(cfg)
    victim, attacker = app.test_client(), app.test_client()
    _sign_in_with_token(victim, "admin")
    _sign_in_with_token(attacker, "other-admin")
    foreign = _token(attacker.get("/account").data)
    _watch_writes(db)
    walked = 0
    for rule in app.url_map.iter_rules():
        if not rule.methods & STATE_CHANGING:
            continue
        url = _walk_url(rule, ids)
        for method in sorted(rule.methods & STATE_CHANGING):
            for data, headers in (({}, {}), ({"csrf_token": foreign}, {}), ({}, {"X-CSRFToken": foreign})):
                resp = victim.open(url, method=method, data=data, headers=headers)
                assert resp.status_code == 400, (rule.endpoint, method, resp.status_code)
                assert "That form couldn&#39;t be sent." in resp.get_data(as_text=True), rule.endpoint
                walked += 1
    assert walked >= 3 * 100  # every form in the portal, not a handful
    assert _writes(db) == set()  # nothing was changed
    assert _signed_in(victim)  # and the refusals didn't sign the person out
    # The walk isn't vacuous: the same browser's own token gets through.
    own = _token(victim.get("/account").data)
    resp = victim.post("/account/sessions/end-others", data={"csrf_token": own})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/account")


def test_a_token_from_before_sign_in_or_from_a_signed_out_session_stops_working(cfg, db):
    """Signing in starts a new session and with it a new token, so a token someone saw on the sign-in page
    (on a shared computer, say) is useless once the person is in, and one from a signed-in page is useless
    after sign-out."""
    make_user(db, "admin")
    client = _csrf_app(cfg).test_client()
    before = _token(client.get("/login").data)
    client.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": before})
    assert _signed_in(client)
    assert client.post("/account/sessions/end-others", data={"csrf_token": before}).status_code == 400
    during = _token(client.get("/account").data)
    assert during != before
    assert client.post("/logout", data={"csrf_token": during}).status_code == 302
    # Signed out: the old token doesn't sign anyone in again, nor reach a form.
    resp = client.post("/login", data={"username": "admin", "password": PASSWORD, "csrf_token": during})
    assert resp.status_code == 400 and not _signed_in(client)


def test_the_sign_in_form_itself_needs_the_token(cfg, db):
    """Without it, another site could sign a visitor in to the attacker's own account (login CSRF) and watch
    what they then do there."""
    make_user(db, "admin")
    client = _csrf_app(cfg).test_client()
    assert client.post("/login", data={"username": "admin", "password": PASSWORD}).status_code == 400
    assert not _signed_in(client)
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


# -- Links that change something when opened ---------------------------------------------------------------


def test_opening_a_link_changes_nothing_stored(app, client, db, world):  # noqa: F811
    """The session cookie is SameSite=Lax, so a link on another site that a signed-in admin follows arrives
    with their cookie. No page changes anything stored when opened (GET), except the ones listed in
    ``WRITES_WHEN_OPENED``, each harmless. Every GET route is opened with real ids, as an admin."""
    make_user(db, "admin")
    login(client, "admin")
    _, _, ids = world
    _watch_writes(db)
    opened, wrote = 0, {}
    for rule in app.url_map.iter_rules():
        if "GET" not in rule.methods or rule.endpoint == "static":
            continue
        resp = client.get(_walk_url(rule, ids))
        opened += resp.status_code != 404  # a few are only there once set up (Microsoft sign-in, mail app)
        if found := _writes(db):
            wrote[rule.endpoint] = found
    assert opened >= 130
    unexpected = {k: v for k, v in wrote.items() if not v <= WRITES_WHEN_OPENED.get(k, set())}
    assert not unexpected, f"Opening these pages changed stored data: {unexpected}"


def test_opening_every_page_signed_out_changes_nothing(app, client, db, world):  # noqa: F811
    """Strangers reach every address too: signed out, nothing stored changes (sign-in limits and the activity
    log are bookkeeping)."""
    _, _, ids = world
    make_user(db, "admin")
    _watch_writes(db)
    for rule in app.url_map.iter_rules():
        if "GET" in rule.methods and rule.endpoint != "static":
            client.get(_walk_url(rule, ids))
    assert _writes(db) == set()


# -- Sign-in answers -----------------------------------------------------------------------------------------


def test_sign_in_answers_the_same_whatever_was_wrong(client, db, monkeypatch):
    """An unknown name, a wrong password, a disabled account and an account whose two-step codes are locked
    (even with the right password) all get the same page and status, and every one pays for a password hash,
    so neither the answer nor its timing says which names exist or which password was right."""
    make_user(db, "alice")
    bob = make_user(db, "bob")
    db.execute("UPDATE users SET disabled = 1 WHERE id = ?", (bob,))
    carol = make_user(db, "carol")
    until = (datetime.now(UTC) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("UPDATE users SET locked_until = ? WHERE id = ?", (until, carol))
    hashed = []
    real = auth.check_password_hash
    monkeypatch.setattr(auth, "check_password_hash", lambda h, p: hashed.append(h) or real(h, p))
    answers = []
    for i, (username, password) in enumerate(
        [("nobody", PASSWORD), ("alice", "wrong password"), ("bob", PASSWORD), ("bob", "wrong"), ("carol", PASSWORD)]
    ):
        resp = client.post(
            "/login", data={"username": username, "password": password}, environ_base={"REMOTE_ADDR": f"192.0.2.{i}"}
        )
        answers.append((resp.status_code, _same_answer(resp.get_data(as_text=True), username)))
    assert len(hashed) == 5  # the unknown name was checked against a hash too
    assert len(set(answers)) == 1, "the sign-in page answered differently"
    assert answers[0][0] == 200 and auth._SIGN_IN_FAILED.replace(",", "&#44;")[:20] in answers[0][1].replace(
        ",", "&#44;"
    )
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_a_name_typed_differently_counts_towards_the_same_limit(cfg, db):
    """Capitals and spaces don't make a new name for the address limits: the account is found the same way
    (usernames compare without case), so the guesses are counted together."""
    make_user(db, "alice")
    client = create_app(cfg).test_client()
    for name in ("alice", "Alice", " ALICE ", "aLiCe", "alice "):
        resp = client.post("/login", data={"username": name, "password": "wrong"})
        assert resp.status_code == 200
    resp = client.post("/login", data={"username": "ALICE", "password": PASSWORD})
    assert resp.status_code == 429  # even the right password waits now, from this address
    assert not _signed_in(client)


# -- Cookies --------------------------------------------------------------------------------------------------


def test_signing_in_never_keeps_a_cookie_the_browser_arrived_with(client, db):
    """Session fixation: someone who gets a cookie into a browser before its owner signs in (a shared
    computer, a cookie set by a sibling site, which ``__Host-`` also stops) gains nothing. Signing in replaces
    everything in the cookie with a new random session, and the planted cookie stays signed out."""
    make_user(db, "admin")
    client.get("/login")  # a cookie with a form token in it
    planted = client.get_cookie(COOKIE)
    attacker = client.application.test_client()
    attacker.set_cookie(COOKIE, planted.value)
    login(client, "admin")
    assert _signed_in(client) and client.get_cookie(COOKIE).value != planted.value
    assert not _signed_in(attacker)
    # Nothing in the new cookie came from the old one: a new session token and a new form token.
    serializer = SecureCookieSessionInterface().get_signing_serializer(client.application)
    old, new = serializer.loads(planted.value), serializer.loads(client.get_cookie(COOKIE).value)
    assert "sid" not in old and old["csrf_token"]
    assert new["sid"] and not set(new.values()) & set(old.values()) - {True}


def test_an_edited_or_forged_cookie_signs_nobody_in(app, client, db):
    """The cookie is signed with the portal's secret key and only carries the session token. Changing a byte,
    or signing a cookie with any other key (even one carrying a real session token), gets the sign-in page;
    the server never trusts anything in the cookie but a token it issued."""
    make_user(db, "admin")
    login(client, "admin")
    value = client.get_cookie(COOKIE).value
    sid = SecureCookieSessionInterface().get_signing_serializer(app).loads(value)["sid"]
    stranger = app.test_client()
    flipped = value[:-3] + ("A" if value[-3] != "A" else "B") + value[-2:]
    stranger.set_cookie(COOKIE, flipped)
    assert not _signed_in(stranger)
    forged = URLSafeTimedSerializer(
        "a guessed secret key", salt="cookie-session", signer_kwargs={"key_derivation": "hmac", "digest_method": "sha1"}
    ).dumps({"sid": sid, "_permanent": True})
    stranger.set_cookie(COOKIE, forged)
    assert not _signed_in(stranger)
    # A cookie claiming a user rather than a session means nothing either, even signed with the real key.
    real = SecureCookieSessionInterface().get_signing_serializer(app)
    for claim in ({"user_id": 1}, {"uid": 1}, {"pending": {"uid": 1}}, {"sid": "made-up"}):
        stranger.set_cookie(COOKIE, real.dumps(claim))
        assert not _signed_in(stranger), claim
    # Proof the forgery test isn't vacuous: the real key with the real token is a signed-in browser.
    stranger.set_cookie(COOKIE, real.dumps({"sid": sid, "_permanent": True}))
    assert _signed_in(stranger)


def _end_by_sign_out(main, other, db, uid):
    main.post("/logout")


def _end_from_another_browser(main, other, db, uid):
    login(other, "victim")
    other.post("/account/sessions/end-others")


def _end_by_password_change(main, other, db, uid):
    login(other, "victim")
    other.post(
        "/account/password",
        data={"current": PASSWORD, "password": "a brand new passphrase", "confirm": "a brand new passphrase"},
    )


def _end_by_admin_sign_out(main, other, db, uid):
    make_user(db, "boss")
    login(other, "boss")
    other.post(f"/admin/users/{uid}/sign-out", data={"admin_password": PASSWORD})


def _end_by_admin_disable(main, other, db, uid):
    make_user(db, "boss")
    login(other, "boss")
    other.post(f"/admin/users/{uid}", data={"role": "analyst", "disabled": "1"})


def _end_by_role_change(main, other, db, uid):
    make_user(db, "boss")
    login(other, "boss")
    other.post(f"/admin/users/{uid}", data={"role": "client"})


def _end_by_deletion(main, other, db, uid):
    make_user(db, "boss")
    login(other, "boss")
    other.post(f"/admin/users/{uid}/delete")


def _end_by_idle_time(main, other, db, uid):
    db.execute("UPDATE sessions SET last_seen_at = '2020-01-01T00:00:00Z' WHERE user_id = ?", (uid,))


def _end_by_age(main, other, db, uid):
    # Used all along (never idle), but past its total lifetime.
    db.execute("UPDATE sessions SET expires_at = '2020-01-01T00:00:00Z' WHERE user_id = ?", (uid,))


@pytest.mark.parametrize(
    "end",
    [
        _end_by_sign_out,
        _end_from_another_browser,
        _end_by_password_change,
        _end_by_admin_sign_out,
        _end_by_admin_disable,
        _end_by_role_change,
        _end_by_deletion,
        _end_by_idle_time,
        _end_by_age,
    ],
    ids=lambda f: f.__name__.removeprefix("_end_by_").removeprefix("_end_"),
)
def test_a_copied_cookie_dies_with_the_session(app, db, end):
    """Whoever copied the cookie (from a shared computer, a stolen laptop, a browser extension) is signed out
    by every way a session ends, as surely as its owner is."""
    uid = make_user(db, "victim", role="analyst")
    main, thief, other = app.test_client(), app.test_client(), app.test_client()
    login(main, "victim")
    _copy_cookie(main, thief)
    assert _signed_in(thief)
    end(main, other, db, uid)
    assert not _signed_in(thief)


def test_the_cookie_lasts_no_longer_than_the_session(app, client, db, cfg):
    make_user(db, "admin")
    resp = login(client, "admin")
    cookie = resp.headers["Set-Cookie"]
    expires = datetime.strptime(re.search(r"Expires=([^;]+)", cookie).group(1), "%a, %d %b %Y %H:%M:%S GMT")
    lifetime = expires.replace(tzinfo=UTC) - datetime.now(UTC)
    assert timedelta(hours=cfg.session_hours) - timedelta(minutes=1) < lifetime <= timedelta(hours=cfg.session_hours)
    row = db.execute("SELECT created_at, expires_at FROM sessions").fetchone()
    start = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    assert end - start == timedelta(hours=cfg.session_hours)


def test_nobody_ends_someone_elses_session_by_its_number(app, db):
    """The Account page's "Sign out" button names a session by number; another person's number does nothing."""
    make_user(db, "alice")
    make_user(db, "mallory")
    alice, mallory = app.test_client(), app.test_client()
    login(alice, "alice")
    login(mallory, "mallory")
    theirs = db.execute("SELECT s.id FROM sessions s JOIN users u ON u.id = s.user_id WHERE u.username = 'alice'")
    for (session_id,) in theirs.fetchall():
        mallory.post(f"/account/sessions/{session_id}/end")
    assert _signed_in(alice) and _signed_in(mallory)


def test_the_session_token_is_stored_only_as_a_hash(app, client, db):
    """Someone who reads the database (a backup, say) can't use what's in the sessions table as a cookie."""
    make_user(db, "admin")
    login(client, "admin")
    sid = SecureCookieSessionInterface().get_signing_serializer(app).loads(client.get_cookie(COOKIE).value)["sid"]
    stored = db.execute("SELECT * FROM sessions").fetchone()
    assert len(sid) >= 40 and sid not in [str(v) for v in dict(stored).values()]
    stranger = app.test_client()
    stranger.set_cookie(
        COOKIE,
        SecureCookieSessionInterface().get_signing_serializer(app).dumps({"sid": stored["token_hash"]}),
    )
    assert not _signed_in(stranger)


# -- Two-step sign-in ------------------------------------------------------------------------------------------


@pytest.fixture
def two_step(cfg, db, vault):
    """An app that requires two-step sign-in for staff, and an admin who has set it up."""
    cfg.require_mfa = "staff"
    app = create_app(cfg)
    make_user(db, "admin")
    secret, codes = enrol(app.test_client(), db, vault, "admin")
    return app, secret, codes


def test_the_half_signed_in_cookie_opens_nothing(app, db, two_step):
    """After the password and before the code, the browser holds a short-lived marker, not a session. It opens
    no page, no form, the authenticator set-up or the new-password step; only the code page."""
    app, _secret, _codes = two_step
    sessions_before = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    client = app.test_client()
    assert login(client, "admin").headers["Location"].endswith("/login/code")
    for rule in app.url_map.iter_rules():
        if rule.arguments or "GET" not in rule.methods or rule.endpoint in ("static", "auth.login_code"):
            continue
        resp = client.get(rule.rule)
        assert resp.status_code != 200 or rule.endpoint in (
            "auth.login",  # the sign-in page itself (which also forgets the marker, so sign in again)
            "health",
            "health_ready",
            "branding.brand_css",
            "branding.logo",
        ), rule.endpoint
        if resp.status_code == 302:
            assert "/login" in resp.headers["Location"], rule.endpoint
        if rule.endpoint == "auth.login":
            login(client, "admin")
    assert client.post("/account/sessions/end-others").headers["Location"].startswith("/login")
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == sessions_before


def test_the_half_signed_in_cookie_expires_and_dies_with_a_password_reset(app, db, two_step, monkeypatch):
    app, secret, _codes = two_step
    client = app.test_client()
    login(client, "admin")
    later = time.time() + auth.PENDING_SECONDS + 1
    monkeypatch.setattr(auth.time, "time", lambda: later)
    resp = client.post("/login/code", data={"code": totp.code(secret, now=later)})
    assert resp.headers["Location"].endswith("/login") and not _signed_in(client)
    monkeypatch.undo()
    # A marker made before the password was reset (by an admin, after a phone was stolen) is no good.
    login(client, "admin")
    db.execute("UPDATE users SET session_version = session_version + 1 WHERE username = 'admin'")
    resp = client.post("/login/code", data={"code": next_code(secret)})
    assert resp.headers["Location"].endswith("/login") and not _signed_in(client)


def test_wrong_recovery_codes_count_towards_the_lock(app, db, two_step):
    """Recovery codes are tried on the same counter as authenticator codes: guessing them locks the step too,
    and then even the right code doesn't get in until the lock lifts."""
    app, secret, codes = two_step
    client = app.test_client()
    login(client, "admin")
    for i in range(auth.MAX_FAILURES):
        client.post("/login/code", data={"code": f"zzzz{i}-zzzzz"})
    assert not _signed_in(client)
    login(client, "admin")
    client.post("/login/code", data={"code": codes[0]})
    assert not _signed_in(client)
    assert db.execute("SELECT COUNT(*) FROM user_recovery_codes WHERE used_at IS NOT NULL").fetchone()[0] == 0


def test_codes_from_another_account_or_long_ago_are_refused(app, db, two_step, vault):
    app, secret, _codes = two_step
    make_user(db, "second")
    other_secret, _ = enrol(app.test_client(), db, vault, "second")
    client = app.test_client()
    login(client, "admin")
    assert client.post("/login/code", data={"code": next_code(other_secret)}).status_code == 200
    stale = totp.code(secret, now=time.time() - 3 * totp.STEP)
    assert client.post("/login/code", data={"code": stale}).status_code == 200
    assert not _signed_in(client)
    client.post("/login/code", data={"code": next_code(secret)})
    assert _signed_in(client)


def test_a_stolen_session_guessing_the_password_is_signed_out_and_cannot_lock_the_owner_out(app, db):
    """Every form that asks for the password again counts wrong answers on that browser's own session: five,
    on any mix of forms, sign that browser out. They never lock the account, so someone holding a stolen
    session can't keep its owner from signing in, or their other browsers from working, while they hold it."""
    make_user(db, "admin")
    ana = make_user(db, "ana", role="analyst")
    thief, owner = app.test_client(), app.test_client()
    login(thief, "admin")
    login(owner, "admin")
    new = "a much longer new password"
    assert thief.get("/two-step/setup").status_code == 200  # an authenticator waiting to be added
    guesses = [
        ("/account/password", {"current": "guess 1", "password": new, "confirm": new}),
        ("/account/two-step/turn-off", {"password": "guess 2"}),
        (f"/admin/users/{ana}/sign-out", {"admin_password": "guess 3"}),
        ("/account/password", {"current": "guess 4", "password": new, "confirm": new}),
    ]
    for path, data in guesses:
        assert thief.post(path, data=data).status_code in (200, 302), path
        assert _signed_in(thief), path
    resp = thief.post("/two-step/setup", data={"password": "guess 5", "code": "000000"})
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")
    assert not _signed_in(thief)
    assert db.execute("SELECT locked_until FROM users WHERE username = 'admin'").fetchone()[0] is None
    assert _signed_in(owner)
    fresh = app.test_client()
    login(fresh, "admin")
    assert _signed_in(fresh)
    # The owner's right password works first time on their own browser, whatever the thief typed.
    owner.post("/account/password", data={"current": PASSWORD, "password": new, "confirm": new})
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'password_changed'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM audit_log WHERE action = 'password_confirm_failed'").fetchone()[0] == 5


def test_recovery_codes_from_before_keyed_hashes_leave_no_bare_hash_behind(cfg, db, vault):
    """A bare SHA-256 of a ten-character code can be found by trying every code, so a copy of the database
    would give old codes away. The portal re-keys them when it starts; the bare hash itself is no code."""
    uid = make_user(db, "admin")
    bare = [hashlib.sha256(c.encode()).hexdigest() for c in ("abcdefghjk", "mnpqrstuvw")]
    db.executemany("INSERT INTO user_recovery_codes (user_id, code_hash) VALUES (?, ?)", [(uid, h) for h in bare])
    create_app(cfg)
    stored = [r[0] for r in db.execute("SELECT code_hash FROM user_recovery_codes ORDER BY id")]
    assert all(h.startswith("hmac") for h in stored) and not set(stored) & set(bare)
    key = totp.recovery_key(db, vault)
    assert totp.recovery_code_hashes("abcde-fghjk", key)[1] == stored[0]
    assert not {*totp.recovery_code_hashes(bare[0], key)} & set(stored)


# -- Sign in with Microsoft ------------------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unsigned(token: str, alg: str, key: bytes = b"") -> str:
    """``token``'s claims under another header: no signature at all, or an HMAC with ``key``."""
    _head, body, _sig = token.split(".")
    head = _b64(json.dumps({"alg": alg, "kid": ms.MS_KID, "typ": "JWT"}).encode())
    sig = _b64(hmac.new(key, f"{head}.{body}".encode(), hashlib.sha256).digest()) if alg == "HS256" else ""
    return f"{head}.{body}.{sig}"


@responses.activate
@pytest.mark.parametrize(
    "case", ["alg none", "HMAC with the published key", "another issuer", "no object id", "someone else's sign-in"]
)
def test_microsoft_sign_in_refuses_tokens_and_codes_made_another_way(client, db, configured, case):  # noqa: F811
    """``test_staff_sign_in.py`` covers a wrong nonce, tenant, audience, key, signature or state. These are the
    other classic tricks: a token that says it needs no signature, one signed with the tenant's public key as
    an HMAC secret, one from another issuer, one without the object id accounts are matched on, and a link
    that finishes someone else's sign-in in this browser (sign-in CSRF)."""
    sent = ms.start(client)
    token = ms.id_token(ms.JACK, sent["nonce"])
    if case == "alg none":
        token = _unsigned(token, "none")
    elif case == "HMAC with the published key":
        public = jwt.PyJWK(ms.MS_JWK).key.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        token = _unsigned(token, "HS256", public)
    elif case == "another issuer":
        token = ms.id_token(ms.JACK, sent["nonce"], iss=f"https://sts.windows.net/{ms.TENANT}/")
    elif case == "no object id":
        token = ms.id_token(ms.JACK, sent["nonce"], oid=None)
    elif case == "someone else's sign-in":
        # The attacker started a sign-in in their own browser and sends this one the link back from Microsoft.
        attacker = client.application.test_client()
        sent = ms.start(attacker)
        token = ms.id_token(ms.JACK, sent["nonce"])
    resp, _call = ms.come_back(client, sent, token)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login"), case
    assert ms.signed_in_as(client) is None, case
    assert ms.accounts(db) == [], case


# -- The monitoring token ------------------------------------------------------------------------------------


def test_the_monitoring_token_must_be_exact(client, db):
    worker.beat(db)
    token = system_health.new_token(db)
    near_misses = [
        f"Bearer {token[:-1]}",
        f"Bearer {token}x",
        f"Bearer {token.upper()}",
        f"Basic {token}",
        token[:20],
        "Bearer ",
        "Bearer " + "x" * 10_000,
        f"Bearer {system_health._digest(token)}",  # what the database holds isn't a key either
    ]
    for value in near_misses:
        resp = client.get("/health/ready", headers={"Authorization": value})
        assert resp.json == {"ok": True}, value
    assert client.get("/health/ready", headers={"X-Token": token}).json == {"ok": True}
    assert "checks" in client.get("/health/ready", headers={"Authorization": f"Bearer {token}"}).json
    system_health.remove_token(db)
    assert client.get("/health/ready", headers={"Authorization": f"Bearer {token}"}).json == {"ok": True}


# -- Signed webhooks ------------------------------------------------------------------------------------------


def _documented_receiver():
    """The receiver DEPLOY.md tells people to write ("Signed webhooks"), run as written."""
    text = (Path(__file__).parents[1] / "DEPLOY.md").read_text(encoding="utf-8")
    code = re.search(r"### Signed webhooks.*?```python\n(.*?)```", text, re.S).group(1)
    scope: dict = {}
    exec(compile(code, "DEPLOY.md", "exec"), scope)  # noqa: S102 (our own documentation)
    return scope["genuine"]


def test_the_documented_receiver_refuses_forged_copied_and_edited_requests():
    """What a receiver checks, following DEPLOY.md: a request Office Sentry signs now passes; one signed with
    another secret, sent again after five minutes, with its body or timestamp changed, or signed the pre-2.2.0
    way, doesn't."""
    genuine = _documented_receiver()
    secret, body, now = "a-long-random-signing-secret", b'{"event":"findings.new"}', int(time.time())
    headers = alerts.sign(secret, body, now)
    assert genuine(headers, body, secret)
    assert not genuine(alerts.sign("another-secret-of-length", body, now), body, secret)
    assert not genuine(alerts.sign(secret, body, now - 301), body, secret)  # a copy sent again later
    assert not genuine(headers, body + b" ", secret)
    assert not genuine({**headers, "X-OfficeSentry-Timestamp": str(now + 1)}, body, secret)
    old_style = {
        **headers,
        "X-OfficeSentry-Signature": "sha256=" + hmac.new(secret.encode(), body, "sha256").hexdigest(),
    }
    assert not genuine(old_style, body, secret)
    assert not genuine({}, body, secret)


def test_a_short_signing_secret_is_refused(client, db, vault):
    """Every signed request shows a known body and timestamp with its HMAC, so a short secret can be found by
    trying them all. Shorter than ``MIN_SECRET`` is refused; a saved one is kept."""
    make_user(db, "admin")
    login(client, "admin")
    form = {"enabled": "1", "format": "generic", "url": "https://hooks.example.com/x", "min_severity": "high"}
    page = client.post("/admin/alerts", data={**form, "secret": "short"}, follow_redirects=True).get_data(as_text=True)
    assert f"signing secret of {web_alerts.MIN_SECRET} to 256 characters" in page
    assert not alerts.settings(db).get("alerts.secret_enc")
    client.post("/admin/alerts", data={**form, "secret": "x" * web_alerts.MIN_SECRET})
    assert vault.decrypt(alerts.settings(db)["alerts.secret_enc"]) == "x" * web_alerts.MIN_SECRET


# -- Users and roles, from someone who manages users but isn't an Administrator --------------------------------


def test_a_capability_taken_away_on_one_client_is_not_one_to_hand_out(client, db, two_tenants):
    """A user manager who can't download files from Alpha (taken away on that one client) doesn't hold
    downloads everywhere, so they can't give downloads to someone on Alpha, put them in a role, or take over
    an account that can download from Alpha (by resetting its password) to get them that way."""
    alpha, bravo = two_tenants
    make_user(db, "admin")
    lead_role = _custom_role(db, "Helpdesk lead", {"users.manage", "reports.identity", "export"})
    viewer_role = _custom_role(db, "Viewer", {"reports.identity", "export"})
    lead = _holder(db, "lead", lead_role, tenants=[alpha, bravo])
    roles.set_overrides(
        db,
        "admin",
        db.execute("SELECT * FROM users WHERE id = ?", (lead,)).fetchone(),
        [roles.Override("export", alpha, False)],
    )
    target = _holder(db, "target", viewer_role, tenants=[alpha])
    plain_role = _custom_role(db, "Reader", {"reports.identity"})
    plain = _holder(db, "plain", plain_role, tenants=[alpha])
    login(client, "lead")

    # Not an account that can download from Alpha: no password reset, no two-step reset, no sign-out.
    page = client.get(f"/admin/users/{target}").get_data(as_text=True)
    assert "only an administrator can change" in page.lower()
    before = db.execute("SELECT password_hash FROM users WHERE id = ?", (target,)).fetchone()[0]
    client.post(
        f"/admin/users/{target}",
        data={
            "role": str(viewer_role.id),
            "password": "a-new-passphrase-1",
            "admin_password": PASSWORD,
            "tenants": [str(alpha)],
        },
    )
    assert db.execute("SELECT password_hash FROM users WHERE id = ?", (target,)).fetchone()[0] == before

    # Not downloads for someone else, everywhere or on Alpha.
    client.post(f"/admin/users/{plain}/access", data={"g_export": "allow"})
    client.post(
        f"/admin/users/{plain}/access",
        data={"add_tenant": str(alpha), "add_capability": "export", "add_allow": "allow"},
    )
    assert roles.overrides(db, plain) == []
    # Not a role that holds them, given or edited.
    client.post(f"/admin/users/{plain}", data={"role": str(viewer_role.id), "tenants": [str(alpha)]})
    assert db.execute("SELECT role_id FROM users WHERE id = ?", (plain,)).fetchone()[0] == plain_role.id
    client.post(
        f"/admin/roles/{plain_role.id}", data={"name": "Reader", "capabilities": ["reports.identity", "export"]}
    )
    assert roles.get(db, plain_role.id).capabilities == {"reports.identity"}
    client.post(
        "/admin/users", data={"username": "new-one", "password": "a-long-password-1", "role": str(viewer_role.id)}
    )
    assert db.execute("SELECT 1 FROM users WHERE username = 'new-one'").fetchone() is None

    # What they do hold everywhere still works.
    client.post(f"/admin/users/{plain}/access", data={"g_reports.identity": "deny"})
    assert {(o.capability, o.allow) for o in roles.overrides(db, plain)} == {("reports.identity", False)}


def test_user_pages_never_show_a_client_account_of_a_client_out_of_scope(client, db, two_tenants):
    """Client accounts are named after the client's people (jane@bravo.example), so a user manager who only
    sees Alpha isn't shown Bravo's accounts: not in the list, and their pages are not found."""
    alpha, bravo = two_tenants
    make_user(db, "admin")
    lead_role = _custom_role(db, "Helpdesk lead", {"users.manage", "reports.identity"})
    _holder(db, "lead", lead_role, tenants=[alpha])
    mine = make_user(db, "sam@alpha.example", role="client", tenants=[alpha])
    theirs = make_user(db, "jane@bravo.example", role="client", tenants=[bravo])
    login(client, "lead")
    page = client.get("/admin/users").get_data(as_text=True)
    assert "sam@alpha.example" in page and "jane@bravo.example" not in page
    assert client.get(f"/admin/users/{mine}").status_code == 200
    for url in (
        f"/admin/users/{theirs}",
        f"/admin/users/{theirs}/access",
        f"/admin/users/{theirs}/reset-two-step",
        f"/admin/users/{theirs}/sign-out",
        f"/admin/users/{theirs}/unlock",
        f"/admin/users/{theirs}/delete",
    ):
        method = "GET" if url.endswith(str(theirs)) else "POST"
        resp = client.open(url, method=method, data={"admin_password": PASSWORD})
        assert resp.status_code == 404 and b"jane@bravo.example" not in resp.data, url
    # Whoever sees every client sees every account.
    client.post("/logout")
    login(client, "admin")
    assert "jane@bravo.example" in client.get("/admin/users").get_data(as_text=True)


def test_the_support_package_names_clients_only_for_someone_who_sees_them_all(client, db, two_tenants):
    """System status given to a role that only sees some clients doesn't take effect (it covers every client,
    ``permissions.EVERY_CLIENT``), so nobody limited to some clients builds the support package, whose clients
    part would otherwise list every client's name."""
    alpha, _bravo = two_tenants
    make_user(db, "admin")
    ops = _custom_role(db, "Ops", {"system.manage"})
    _holder(db, "ops", ops, tenants=[alpha])
    login(client, "ops")
    assert client.get("/admin/support").status_code == 403
    assert client.post("/admin/support/package", data={"identifiers": "1"}).status_code == 403
    client.post("/logout")
    login(client, "admin")
    resp = client.post("/admin/support/package", data={"identifiers": "1"})
    data = b"".join(
        zipfile.ZipFile(io.BytesIO(resp.data)).read(n) for n in zipfile.ZipFile(io.BytesIO(resp.data)).namelist()
    )
    assert b"Bravo Inc" in data  # the tick box works for whoever sees every client


def test_the_activity_log_shows_only_clients_in_scope(client, db, two_tenants):
    """Someone who reads the activity log but only sees some clients doesn't read what was done on the others
    (the detail can name their people), in the page or the download."""
    alpha, bravo = two_tenants
    make_user(db, "admin")
    auditor = _custom_role(db, "Log reader", {"activity_log.read"})
    _holder(db, "reader", auditor, tenants=[alpha])
    audit(db, "admin", "finding_accepted", alpha, "alpha-detail: sam@alpha.example")
    audit(db, "admin", "finding_accepted", bravo, "bravo-detail: jane@bravo.example")
    audit(db, "admin", "settings_changed", None, "portal-wide detail")
    login(client, "reader")
    for url in ("/admin/activity", "/admin/activity.csv", f"/admin/activity?tenant={bravo}"):
        body = client.get(url).get_data(as_text=True)
        assert "bravo-detail" not in body and "jane@bravo.example" not in body, url
    body = client.get("/admin/activity").get_data(as_text=True)
    assert "alpha-detail" in body and "portal-wide detail" in body
    client.post("/logout")
    login(client, "admin")
    assert "bravo-detail" in client.get("/admin/activity").get_data(as_text=True)
