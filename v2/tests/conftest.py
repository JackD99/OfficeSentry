import importlib
import json
import os
import sqlite3
import sys
import traceback
import zlib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from werkzeug.security import generate_password_hash

from officesentry import store
from officesentry.certs import generate_certificate
from officesentry.config import ENV_PREFIX, load_config
from officesentry.crypto import Vault
from officesentry.db import connect, migrate, utcnow
from officesentry.web import create_app

PASSWORD = "correct horse battery"
# Hashed once, cheaply: the portal's scrypt costs ~0.1 s per hash and per sign-in, hundreds of times a run.
# Verification reads the method from the hash, so signing in as these users is cheap too. Accounts the
# portal creates still use scrypt (test_access.py::test_setup_creates_first_admin_once checks).
PASSWORD_HASH = generate_password_hash(PASSWORD, method="pbkdf2:sha256:1")


def pytest_addoption(parser):
    # tests/browser: screenshots and accessibility in a real browser (tests/browser/README.md).
    parser.addoption("--ui", action="store_true", help="run the browser tests in tests/browser")
    parser.addoption(
        "--update", action="store_true", help="with --ui or --nightly: save new screenshot or PDF page baselines"
    )
    parser.addoption("--save-all", action="store_true", help="with --ui: save every capture in tests/browser/output")
    # tests/nightly: release upgrades, PDF snapshots and the 200-tenant timing run (tests/nightly/README.md).
    parser.addoption("--nightly", action="store_true", help="run the slow scheduled suite in tests/nightly")
    # tests/layout: every page at three laptop widths and a phone, for sideways scrolling, cut-off text,
    # overlaps and controls off the screen (tests/layout/README.md).
    parser.addoption("--layout", action="store_true", help="run the layout checks in tests/layout")
    # CI splits the suite across machines that run at the same time (.github/workflows/v2.yml).
    parser.addoption("--shard", metavar="K/N", help="run only part K of N of the tests (every part, together: all)")
    parser.addoption(
        "--shard-list", metavar="FILE", help="with --shard: write the part's tests to FILE as JSON, for CI to check"
    )


# The test-data modules date what they hold from the moment they load ("renews in 150 days", "last signed in
# 2 days ago"), listed so each comes after the ones it reads. A run that passes midnight UTC loads them
# again before its next test, so a test never compares yesterday's data with today's clock.
DATED_DATA = (
    "tests.exchangedata",
    "tests.mailflowdata",
    "tests.mailprotectiondata",
    "tests.graphdata",
    "tests.licencedata",
    "tests.usagedata",
    "tests.teamsdata",
    "tests.adminactivitydata",
    "tests.devicedata",
    "tests.hybriddata",
    "tests.sharepoint_data",
)
_data_dated = {"on": datetime.now(UTC).date()}


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    """Before the test's fixtures, which may read the data."""
    today = datetime.now(UTC).date()
    if today != _data_dated["on"]:
        for name in DATED_DATA:
            if name in sys.modules:
                importlib.reload(sys.modules[name])
        _data_dated["on"] = today


def pytest_ignore_collect(collection_path, config):
    if collection_path.name == "browser" and not config.getoption("--ui"):
        return True
    if collection_path.name == "nightly" and not config.getoption("--nightly"):
        return True
    if collection_path.name == "layout" and not config.getoption("--layout"):
        return True
    return None


def shard_of(nodeid: str, parts: int) -> int:
    """Which part (1 to ``parts``) a test runs in: a hash of its id, so the parts are about the same size, a
    test stays in its part when others are added, and each test is in exactly one."""
    return zlib.crc32(nodeid.encode()) % parts + 1


def pytest_collection_modifyitems(config, items):
    shard = config.getoption("--shard")
    if not shard:
        return
    try:
        part, parts = (int(n) for n in shard.split("/"))
    except ValueError:
        part = parts = 0
    if not 1 <= part <= parts:
        raise pytest.UsageError(f"--shard takes K/N with 1 <= K <= N, such as 2/4, not {shard!r}")
    total = len(items)
    kept = [item for item in items if shard_of(item.nodeid, parts) == part]
    config.hook.pytest_deselected(items=[item for item in items if shard_of(item.nodeid, parts) != part])
    items[:] = kept
    # .github/scripts/check_shards.py reads these from every part and fails unless together they hold each
    # collected test exactly once. Under xdist every worker collects the same tests; the first one writes.
    out = config.getoption("--shard-list")
    if out and getattr(config, "workerinput", {}).get("workerid", "gw0") == "gw0":
        tests = [item.nodeid for item in kept]
        data = {"part": part, "parts": parts, "collected": total, "tests": tests}
        Path(out).write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_settings_from_the_shell(monkeypatch):
    """The developer's shell may carry OFFICESENTRY_* settings (a data folder, the public demo switch, a
    two-step policy, the live test tenant); a test sees none of them and sets the ones it needs itself."""
    for var in list(os.environ):
        if var.startswith(ENV_PREFIX):
            monkeypatch.delenv(var)


class TrackedConnection(sqlite3.Connection):
    """A connection that knows whether it was closed, so the test can tell at its end."""

    closed = False

    def close(self):
        self.closed = True
        super().close()


@pytest.fixture(autouse=True)
def _every_connection_is_closed(monkeypatch):
    """A database connection opened during a test and still open when it ends fails the test. Python only
    warns about those (and only when the object is collected, so the warning lands on whichever test runs
    next), and nothing turned the warnings into failures; a connection the code under test forgets stays
    open on a server for as long as the process runs. Fixtures of a wider scope (``seeded`` and the like)
    open theirs before this runs and close them in their own teardown."""
    real, opened = sqlite3.connect, []

    def connect(*args, **kwargs):
        kwargs.setdefault("factory", TrackedConnection)
        conn = real(*args, **kwargs)
        if isinstance(conn, TrackedConnection):
            where = traceback.StackSummary.extract(traceback.walk_stack(None), limit=10, lookup_lines=False)
            where.reverse()
            opened.append((conn, where, args[0] if args else kwargs.get("database")))
        return conn

    monkeypatch.setattr(sqlite3, "connect", connect)
    yield
    monkeypatch.undo()
    left = []
    for conn, where, database in opened:
        if conn.closed:
            continue
        left.append(f"{database}, opened from:\n" + "".join(where.format()))
        try:
            conn.close()
        except sqlite3.ProgrammingError:  # another thread's; it goes when the thread does
            pass
    assert not left, f"{len(left)} database connection(s) left open at the end of the test:\n" + "\n".join(left)


@pytest.fixture(autouse=True)
def _roomy_disk(monkeypatch):
    """The test machine's disk is whatever it is: report it 40% full, so no test sends a disk alert by chance.
    tests/test_system_health.py sets its own figures."""
    from officesentry import system_health

    monkeypatch.setattr(system_health, "disk_usage", lambda path: (40.0, 60 * 2**30, 100 * 2**30))


@pytest.fixture(autouse=True)
def _fresh_mail_pacer(monkeypatch):
    """Each test starts with nothing sent this minute, so emails sent by earlier tests never make it wait."""
    from officesentry import mailer

    monkeypatch.setattr(mailer, "PACER", mailer.Pacer())


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for var in ("SECRET_KEY", "ENCRYPTION_KEYS", "DATA_DIR"):
        monkeypatch.delenv("OFFICESENTRY_" + var, raising=False)
    # Two-step sign-in is off here so other tests can sign in with a password;
    # tests/test_login_security.py turns it on. So is the worker's nightly backup (tests/test_backups.py).
    return load_config(
        data_dir=tmp_path / "data",
        testing=True,
        secure_cookies=False,
        require_mfa="off",
        backup_cron="",
        extra={"disable_csrf": True},
    )


@pytest.fixture
def db(cfg):
    conn = connect(cfg.db_path)
    migrate(conn)
    yield conn
    conn.close()


@pytest.fixture
def vault(cfg):
    return Vault(cfg.encryption_keys)


@pytest.fixture
def app(cfg, db):
    return create_app(cfg)


@pytest.fixture
def client(app):
    return app.test_client()


def make_user(db, username, role="admin", all_tenants=False, tenants=()):
    cur = db.execute(
        "INSERT INTO users (username, password_hash, role, all_tenants, created_at) VALUES (?, ?, ?, ?, ?)",
        (username, PASSWORD_HASH, role, int(all_tenants), utcnow()),
    )
    for t in tenants:
        db.execute("INSERT INTO user_tenants (user_id, tenant_id) VALUES (?, ?)", (cur.lastrowid, t))
    return cur.lastrowid


def login(client, username, password=PASSWORD):
    return client.post("/login", data={"username": username, "password": password})


@pytest.fixture
def connection_id(db, vault):
    cid = store.create_connection(
        db, vault, "Test app", generate_certificate("test"), client_id="11111111-2222-3333-4444-555555555555"
    )
    return cid


@pytest.fixture
def two_tenants(db, connection_id):
    a = store.create_tenant(db, "Alpha Ltd", "alpha.onmicrosoft.com", connection_id)
    b = store.create_tenant(db, "Bravo Inc", "bravo.onmicrosoft.com", connection_id)
    db.execute("UPDATE tenants SET consented_at = '2026-01-01T00:00:00Z'")  # connected and consented
    return a, b
