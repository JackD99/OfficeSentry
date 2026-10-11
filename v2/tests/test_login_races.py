"""Sign-in limits hold when requests arrive in parallel (gunicorn runs several threads and workers)."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests
from werkzeug.serving import make_server

from officesentry import throttle, totp
from officesentry.web import auth
from tests.conftest import PASSWORD, make_user
from tests.test_login_security import enrol


@pytest.fixture
def live(app):
    """The app on a real threaded server, so requests overlap like they do in production."""
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _browser():
    s = requests.Session()
    s.trust_env = False  # never through the outbound proxy
    return s


def _slow(monkeypatch, module, name, counter, matches=lambda *a: True):
    """Wrap module.name so every call takes a moment (widening the race) and is counted."""
    real = getattr(module, name)
    lock = threading.Lock()

    def wrapper(*args, **kwargs):
        if matches(*args):
            with lock:
                counter.append(1)
        time.sleep(0.05)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, wrapper)


def test_parallel_wrong_passwords_cannot_exceed_the_limit(live, db, monkeypatch):
    make_user(db, "victim")
    victim_hash = db.execute("SELECT password_hash FROM users WHERE username = 'victim'").fetchone()[0]
    checked = []
    _slow(monkeypatch, auth, "check_password_hash", checked, lambda h, _p: h == victim_hash)

    def guess(i):
        return (
            _browser()
            .post(f"{live}/login", data={"username": "victim", "password": f"wrong-{i}"}, allow_redirects=False)
            .status_code
        )

    with ThreadPoolExecutor(max_workers=40) as pool:
        codes = list(pool.map(guess, range(40)))
    # 503 is the portal's "busy, try again" when a write waits too long for the database lock, which 40
    # sign-ins at once can cause on a slow machine. It isn't a sign-in either.
    assert all(code in (200, 429, 503) for code in codes)
    # Every password is counted before it is checked, so 40 at once still only get the limit's worth of tries.
    assert len(checked) <= throttle.PER_ADDRESS_AND_NAME
    # And the limit is in force: the right password from the same place is refused now.
    resp = _browser().post(f"{live}/login", data={"username": "victim", "password": PASSWORD}, allow_redirects=False)
    assert resp.status_code != 302


def _password_step(live, browsers):
    for b in browsers:
        resp = b.post(f"{live}/login", data={"username": "admin", "password": PASSWORD}, allow_redirects=False)
        assert resp.headers["Location"].endswith("/login/code")


def test_a_two_step_code_works_once_even_in_parallel(live, client, db, vault, cfg, monkeypatch):
    cfg.require_mfa = "staff"
    make_user(db, "admin")
    secret, _codes = enrol(client, db, vault, "admin")
    client.post("/logout")
    browsers = [_browser() for _ in range(4)]
    _password_step(live, browsers)  # someone who has the password and has phished one live code
    _slow(monkeypatch, totp, "verify", [])
    code = totp.code(secret, now=time.time() + totp.STEP)

    def submit(b):
        return b.post(f"{live}/login/code", data={"code": code}, allow_redirects=False).headers.get("Location")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(submit, browsers))
    assert sum(loc == "/today" for loc in results) == 1, results  # staff land on their home page
    assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1


def test_parallel_wrong_codes_cannot_exceed_the_limit(live, client, db, vault, cfg, monkeypatch):
    cfg.require_mfa = "staff"
    make_user(db, "admin")
    enrol(client, db, vault, "admin")
    client.post("/logout")
    browsers = [_browser() for _ in range(20)]
    _password_step(live, browsers)
    checked = []
    _slow(monkeypatch, totp, "verify", checked)

    def submit(b):
        return b.post(f"{live}/login/code", data={"code": "000000"}, allow_redirects=False).status_code

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(submit, browsers))
    assert len(checked) <= auth.MAX_FAILURES
