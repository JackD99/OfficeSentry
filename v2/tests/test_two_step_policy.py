"""Who must use two-step sign-in: everyone on new installs, unchanged on existing ones, visible to admins."""

import pytest

from officesentry import db as dbmod
from officesentry.config import load_config
from officesentry.crypto import Vault
from officesentry.db import connect, migrate
from officesentry.web import create_app
from tests.conftest import login, make_user
from tests.test_login_security import enrol


@pytest.fixture
def plain_cfg(tmp_path, monkeypatch):
    """Configuration as an install gets it: OFFICESENTRY_REQUIRE_MFA not set."""
    for var in ("SECRET_KEY", "ENCRYPTION_KEYS", "DATA_DIR", "REQUIRE_MFA"):
        monkeypatch.delenv("OFFICESENTRY_" + var, raising=False)
    return load_config(data_dir=tmp_path / "data", testing=True, secure_cookies=False, extra={"disable_csrf": True})


def _client_signs_in(cfg, conn):
    make_user(conn, "client", role="client")
    return login(create_app(cfg).test_client(), "client").headers["Location"]


def test_new_installs_require_two_step_for_client_accounts_too(plain_cfg):
    conn = connect(plain_cfg.db_path)
    migrate(conn)  # a fresh database: nobody has signed up yet
    assert _client_signs_in(plain_cfg, conn).endswith("/two-step/setup")


def test_existing_installs_keep_their_policy(plain_cfg, monkeypatch):
    conn = connect(plain_cfg.db_path)
    everything = dbmod.migration_files()
    policy = next(v for v, path in everything if "two_step" in path.name)
    monkeypatch.setattr(dbmod, "migration_files", lambda: [m for m in everything if m[0] < policy])
    migrate(conn)  # an install from before this release, already in use...
    make_user(conn, "admin")
    monkeypatch.setattr(dbmod, "migration_files", lambda: everything)
    migrate(conn)  # ...upgrades: its client accounts aren't suddenly asked to enrol
    assert _client_signs_in(plain_cfg, conn) == "/"


def test_the_environment_setting_wins(plain_cfg, monkeypatch):
    monkeypatch.setenv("OFFICESENTRY_REQUIRE_MFA", "staff")
    cfg = load_config(data_dir=plain_cfg.data_dir, testing=True, secure_cookies=False, extra={"disable_csrf": True})
    conn = connect(cfg.db_path)
    migrate(conn)
    assert _client_signs_in(cfg, conn) == "/"


def test_users_page_shows_who_must_use_two_step(cfg, db, client):
    cfg.require_mfa = "staff"
    make_user(db, "admin")
    make_user(db, "client", role="client")
    enrol(client, db, Vault(cfg.encryption_keys), "admin")
    page = client.get("/admin/users").get_data(as_text=True)
    assert "Two-step sign-in is required for staff" in page
    assert "OFFICESENTRY_REQUIRE_MFA=all" in page and "1 client account" in page
