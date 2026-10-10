import pytest
from werkzeug.security import generate_password_hash

from officesentry import store
from officesentry.certs import generate_certificate
from officesentry.config import load_config
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
    parser.addoption("--update", action="store_true",
                     help="with --ui or --nightly: save new screenshot or PDF page baselines")
    parser.addoption("--save-all", action="store_true", help="with --ui: save every capture in tests/browser/output")
    # tests/nightly: release upgrades, PDF snapshots and the 200-tenant timing run (tests/nightly/README.md).
    parser.addoption("--nightly", action="store_true", help="run the slow scheduled suite in tests/nightly")


def pytest_ignore_collect(collection_path, config):
    if collection_path.name == "browser" and not config.getoption("--ui"):
        return True
    if collection_path.name == "nightly" and not config.getoption("--nightly"):
        return True
    return None


@pytest.fixture(autouse=True)
def _roomy_disk(monkeypatch):
    """The test machine's disk is whatever it is: report it 40% full, so no test sends a disk alert by chance.
    tests/test_system_health.py sets its own figures."""
    from officesentry import system_health

    monkeypatch.setattr(system_health, "disk_usage", lambda path: (40.0, 60 * 2**30, 100 * 2**30))


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for var in ("SECRET_KEY", "ENCRYPTION_KEYS", "DATA_DIR"):
        monkeypatch.delenv("OFFICESENTRY_" + var, raising=False)
    # Two-step sign-in is off here so other tests can sign in with a password;
    # tests/test_login_security.py turns it on.
    return load_config(data_dir=tmp_path / "data", testing=True, secure_cookies=False, require_mfa="off",
                       extra={"disable_csrf": True})


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
    cid = store.create_connection(db, vault, "Test app", generate_certificate("test"),
                                  client_id="11111111-2222-3333-4444-555555555555")
    return cid


@pytest.fixture
def two_tenants(db, connection_id):
    a = store.create_tenant(db, "Alpha Ltd", "alpha.onmicrosoft.com", connection_id)
    b = store.create_tenant(db, "Bravo Inc", "bravo.onmicrosoft.com", connection_id)
    db.execute("UPDATE tenants SET consented_at = '2026-01-01T00:00:00Z'")  # connected and consented
    return a, b
