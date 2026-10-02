import pytest
from werkzeug.security import generate_password_hash

from officesentry import store
from officesentry.certs import generate_certificate
from officesentry.config import load_config
from officesentry.crypto import Vault
from officesentry.db import connect, migrate, utcnow
from officesentry.web import create_app

PASSWORD = "correct horse battery"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for var in ("SECRET_KEY", "ENCRYPTION_KEYS", "DATA_DIR"):
        monkeypatch.delenv("OFFICESENTRY_" + var, raising=False)
    return load_config(data_dir=tmp_path / "data", testing=True, secure_cookies=False,
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
        (username, generate_password_hash(PASSWORD), role, int(all_tenants), utcnow()),
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
    return a, b
