"""
tests/test_auth.py — Authentication tests: login, logout, initial setup, session.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


class TestInitialSetup:
    def test_redirect_to_setup_when_no_users(self, client):
        """With no users, /login redirects to /initial-setup."""
        resp = client.get("/login", follow_redirects=False)
        assert resp.status_code == 302
        assert "/initial-setup" in resp.headers["Location"]

    def test_create_admin_account(self, client):
        """POST to /initial-setup creates an admin and logs in."""
        resp = client.post("/initial-setup", data={
            "username": "newadmin",
            "password": "securepass1",
            "confirm":  "securepass1",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert db.user_count() == 1
        user = db.get_user("newadmin")
        assert user is not None
        assert user["is_admin"] == 1

    def test_setup_blocked_when_user_exists(self, client, admin_user):
        """With an existing user, /initial-setup redirects to /login."""
        resp = client.get("/initial-setup", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_setup_password_mismatch(self, client):
        """Mismatched passwords show an error."""
        resp = client.post("/initial-setup", data={
            "username": "admin",
            "password": "password1",
            "confirm":  "different",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert db.user_count() == 0

    def test_setup_short_password(self, client):
        """Passwords shorter than 8 characters are rejected."""
        resp = client.post("/initial-setup", data={
            "username": "admin",
            "password": "short",
            "confirm":  "short",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert db.user_count() == 0


class TestLogin:
    def test_valid_login(self, client, admin_user):
        """Valid credentials redirect to dashboard."""
        resp = client.post("/login", data={
            "username": admin_user["username"],
            "password": admin_user["password"],
        }, follow_redirects=False)
        assert resp.status_code == 302
        assert "/" == resp.headers["Location"] or "dashboard" in resp.headers.get("Location", "")

    def test_invalid_password(self, client, admin_user):
        """Wrong password stays on login page."""
        resp = client.post("/login", data={
            "username": admin_user["username"],
            "password": "wrongpassword",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Invalid" in resp.data

    def test_invalid_username(self, client, admin_user):
        """Non-existent username stays on login page."""
        resp = client.post("/login", data={
            "username": "nobody",
            "password": "password123",
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert b"Invalid" in resp.data


class TestLogout:
    def test_logout_clears_session(self, authed_client):
        """Logout redirects to login and clears session."""
        resp = authed_client.get("/logout", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

        # Verify session is cleared — accessing / should redirect to login
        resp2 = authed_client.get("/", follow_redirects=False)
        assert resp2.status_code == 302
        assert "/login" in resp2.headers["Location"]


class TestProtectedRoutes:
    def test_dashboard_requires_login(self, client):
        """Accessing / without login redirects to /login."""
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_tenants_requires_login(self, client):
        """Accessing /tenants without login redirects."""
        resp = client.get("/tenants", follow_redirects=False)
        assert resp.status_code == 302

    def test_settings_requires_login(self, client):
        """Accessing /settings without login redirects."""
        resp = client.get("/settings", follow_redirects=False)
        assert resp.status_code == 302


class TestChangePassword:
    def test_change_password_success(self, authed_client):
        """Password change with correct current password succeeds."""
        resp = authed_client.post("/settings/password", data={
            "current_password":  "password123",
            "new_password":      "newpassword1",
            "confirm_password":  "newpassword1",
        })
        data = resp.get_json()
        assert data["ok"] is True

    def test_change_password_wrong_current(self, authed_client):
        """Wrong current password is rejected."""
        resp = authed_client.post("/settings/password", data={
            "current_password":  "wrongcurrent",
            "new_password":      "newpassword1",
            "confirm_password":  "newpassword1",
        })
        data = resp.get_json()
        assert data["ok"] is False
        assert "incorrect" in data["error"].lower()

    def test_change_password_mismatch(self, authed_client):
        """Mismatched new passwords are rejected."""
        resp = authed_client.post("/settings/password", data={
            "current_password":  "password123",
            "new_password":      "newpassword1",
            "confirm_password":  "different",
        })
        data = resp.get_json()
        assert data["ok"] is False
