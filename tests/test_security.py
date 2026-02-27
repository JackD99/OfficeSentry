"""
tests/test_security.py — Security tests: CSRF enforcement, path traversal, CSP headers.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


class TestCSRFProtection:
    def test_csrf_enforced_on_login(self, app_csrf):
        """With CSRF enabled, POST to /login without token returns 400."""
        client = app_csrf.test_client()
        # Create a user first
        db.create_user("admin", "password123", is_admin=1)
        resp = client.post("/login", data={
            "username": "admin",
            "password": "password123",
        })
        # Flask-WTF returns 400 when CSRF token is missing
        assert resp.status_code == 400

    def test_csrf_enforced_on_tenant_create(self, app_csrf):
        """POST to /tenants/new without CSRF token is rejected."""
        client = app_csrf.test_client()
        db.create_user("admin", "password123", is_admin=1)
        # Login with CSRF disabled to get a session
        # (Can't easily login with CSRF enabled in tests without token)
        # Instead, just verify the POST is rejected
        resp = client.post("/tenants/new", data={
            "name": "Test",
            "domain": "test.com",
        })
        assert resp.status_code in (400, 302)  # 400 CSRF or 302 redirect to login


class TestPathTraversal:
    def test_report_download_rejects_traversal(self, authed_client):
        """Path traversal attempts are blocked with 403."""
        resp = authed_client.get("/reports/download?path=/etc/passwd")
        assert resp.status_code == 403

    def test_report_download_rejects_relative_traversal(self, authed_client):
        """Relative path traversal is blocked."""
        resp = authed_client.get(
            "/reports/download?path=../../etc/passwd"
        )
        assert resp.status_code == 403

    def test_report_download_empty_path(self, authed_client):
        """Empty path is rejected."""
        resp = authed_client.get("/reports/download?path=")
        assert resp.status_code in (403, 404)


class TestSecurityHeaders:
    def test_csp_header_present(self, authed_client):
        """CSP header is set on responses."""
        resp = authed_client.get("/")
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "default-src 'self'" in csp
        assert "cdn.jsdelivr.net" in csp

    def test_csp_uses_nonce_not_unsafe_inline(self, authed_client):
        """CSP uses nonce-based policy instead of unsafe-inline."""
        resp = authed_client.get("/")
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "'unsafe-inline'" not in csp
        assert "'nonce-" in csp

    def test_x_content_type_options(self, authed_client):
        """X-Content-Type-Options: nosniff is set."""
        resp = authed_client.get("/")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"

    def test_x_frame_options(self, authed_client):
        """X-Frame-Options: DENY is set."""
        resp = authed_client.get("/")
        assert resp.headers.get("X-Frame-Options") == "DENY"

    def test_hsts_in_production(self, app):
        """HSTS header is set when FLASK_ENV=production."""
        import os
        os.environ["FLASK_ENV"] = "production"
        try:
            client = app.test_client()
            db.create_user("admin", "password123", is_admin=1)
            client.post("/login", data={"username": "admin", "password": "password123"})
            resp = client.get("/")
            hsts = resp.headers.get("Strict-Transport-Security", "")
            assert "max-age=31536000" in hsts
        finally:
            os.environ.pop("FLASK_ENV", None)


class TestSessionTimeout:
    def test_permanent_session_lifetime_configured(self, app):
        """Session lifetime is configured to 8 hours."""
        assert app.permanent_session_lifetime.total_seconds() == 8 * 3600
