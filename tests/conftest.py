"""
tests/conftest.py — Shared fixtures for the Office Sentry test suite.
Uses an in-memory SQLite database and a Flask test client.
"""

import os
import sys
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

# Add portal/ to sys.path so imports work as they do when running the app
PORTAL_DIR = Path(__file__).parent.parent / "portal"
sys.path.insert(0, str(PORTAL_DIR))

# Override the DB_PATH to use a temp file before any imports
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()

import database as db_module
db_module.DB_PATH = Path(_tmp_db.name)


@pytest.fixture(autouse=True)
def _clean_db():
    """Reset the database to a clean state before each test."""
    db_module.DB_PATH = Path(_tmp_db.name)
    # Wipe and re-create
    if db_module.DB_PATH.exists():
        db_module.DB_PATH.unlink()
    db_module.init_db()
    yield
    # Cleanup after test
    if db_module.DB_PATH.exists():
        try:
            db_module.DB_PATH.unlink()
        except OSError:
            pass


@pytest.fixture()
def app():
    """Create and configure a Flask test application."""
    from app import create_app

    os.environ.pop("AUDIT_PFX_PASSWORD", None)

    test_app = create_app()
    test_app.config.update({
        "TESTING": True,
        "WTF_CSRF_ENABLED": False,  # Disable CSRF for most tests
        "SECRET_KEY": "test-secret-key-for-testing",
    })
    return test_app


@pytest.fixture()
def app_csrf():
    """Create a Flask test app WITH CSRF enabled for security tests."""
    from app import create_app

    test_app = create_app()
    test_app.config.update({
        "TESTING": True,
        "WTF_CSRF_ENABLED": True,
        "SECRET_KEY": "test-secret-key-for-testing",
    })
    return test_app


@pytest.fixture()
def client(app):
    """Flask test client (CSRF disabled)."""
    return app.test_client()


@pytest.fixture()
def client_csrf(app_csrf):
    """Flask test client (CSRF enabled)."""
    return app_csrf.test_client()


@pytest.fixture()
def admin_user():
    """Create a default admin user and return credentials."""
    db_module.create_user("admin", "password123", display_name="Admin", is_admin=1)
    return {"username": "admin", "password": "password123"}


@pytest.fixture()
def authed_client(client, admin_user):
    """Flask test client already logged in as admin."""
    client.post("/login", data={
        "username": admin_user["username"],
        "password": admin_user["password"],
    }, follow_redirects=True)
    return client
