"""
tests/test_new_features.py — Tests for expanded features:
  - User management (list, create, delete, toggle admin, reset password)
  - Dashboard stats API
  - Tenant health check
  - Tenant export/import
  - Job cancellation
  - Report deletion
  - Admin-only access control
"""

import io
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class TestUserManagementDB:
    """Database-level tests for new user functions."""

    def test_list_users(self):
        db.create_user("alice", "pass1234", display_name="Alice", is_admin=1)
        db.create_user("bob", "pass1234", display_name="Bob", is_admin=0)
        users = db.list_users()
        assert len(users) == 2
        names = [u["username"] for u in users]
        assert "alice" in names
        assert "bob" in names

    def test_list_users_excludes_password_hash(self):
        db.create_user("safe", "pass1234")
        users = db.list_users()
        assert "password_hash" not in users[0]

    def test_delete_user(self):
        db.create_user("deleteme", "pass1234")
        assert db.delete_user("deleteme") is True
        assert db.get_user("deleteme") is None

    def test_delete_nonexistent_user(self):
        assert db.delete_user("ghost") is False

    def test_update_user_admin(self):
        db.create_user("toggleme", "pass1234", is_admin=0)
        db.update_user("toggleme", is_admin=1)
        user = db.get_user("toggleme")
        assert user["is_admin"] == 1

    def test_update_user_display_name(self):
        db.create_user("rename", "pass1234", display_name="Old")
        db.update_user("rename", display_name="New Name")
        user = db.get_user("rename")
        assert user["display_name"] == "New Name"

    def test_update_user_ignores_disallowed_fields(self):
        db.create_user("notouch", "pass1234")
        db.update_user("notouch", password_hash="hackattempt")
        user = db.get_user("notouch")
        assert user["password_hash"] != "hackattempt"


class TestDashboardStatsDB:
    """Database-level tests for get_dashboard_stats."""

    def test_empty_stats(self):
        stats = db.get_dashboard_stats()
        assert stats["total_tenants"] == 0
        assert stats["configured_tenants"] == 0
        assert stats["audited_tenants"] == 0
        assert stats["running_jobs"] == 0
        assert stats["total_jobs"] == 0

    def test_stats_with_data(self):
        tid = db.create_tenant("T1", "t1.com")
        db.update_tenant(tid, client_id="abc123")
        db.touch_tenant_audit(tid)
        db.create_tenant("T2", "t2.com")

        jid = db.create_job("Running", "spo")
        db.create_job("Done", "spo")
        db.finish_job(db.list_jobs()[0]["id"], "done")

        stats = db.get_dashboard_stats()
        assert stats["total_tenants"] == 2
        assert stats["configured_tenants"] == 1
        assert stats["audited_tenants"] == 1
        assert stats["total_jobs"] == 2


# ---------------------------------------------------------------------------
# Route tests
# ---------------------------------------------------------------------------

class TestUserManagementRoutes:
    """Test user management routes (admin-only)."""

    def test_users_page_accessible(self, authed_client):
        resp = authed_client.get("/users")
        assert resp.status_code == 200
        assert b"User Management" in resp.data

    def test_create_user_via_api(self, authed_client):
        resp = authed_client.post("/users/new", data={
            "username": "newuser",
            "password": "newpass123",
            "display_name": "New User",
        })
        data = resp.get_json()
        assert data["ok"] is True
        assert db.get_user("newuser") is not None

    def test_create_user_duplicate(self, authed_client):
        db.create_user("dupe", "pass1234")
        resp = authed_client.post("/users/new", data={
            "username": "dupe",
            "password": "pass1234",
        })
        data = resp.get_json()
        assert data["ok"] is False
        assert "already exists" in data["error"]

    def test_create_user_short_password(self, authed_client):
        resp = authed_client.post("/users/new", data={
            "username": "short",
            "password": "123",
        })
        data = resp.get_json()
        assert data["ok"] is False
        assert "8 characters" in data["error"]

    def test_create_user_empty_fields(self, authed_client):
        resp = authed_client.post("/users/new", data={
            "username": "",
            "password": "",
        })
        data = resp.get_json()
        assert data["ok"] is False

    def test_toggle_admin(self, authed_client):
        db.create_user("operator", "pass1234", is_admin=0)
        resp = authed_client.post("/users/operator/toggle-admin")
        data = resp.get_json()
        assert data["ok"] is True
        assert db.get_user("operator")["is_admin"] == 1

        # Toggle back
        resp = authed_client.post("/users/operator/toggle-admin")
        data = resp.get_json()
        assert data["ok"] is True
        assert db.get_user("operator")["is_admin"] == 0

    def test_toggle_own_admin_blocked(self, authed_client):
        resp = authed_client.post("/users/admin/toggle-admin")
        data = resp.get_json()
        assert data["ok"] is False
        assert "own admin" in data["error"].lower()

    def test_toggle_nonexistent_user(self, authed_client):
        resp = authed_client.post("/users/ghost/toggle-admin")
        assert resp.status_code == 404

    def test_delete_user_via_api(self, authed_client):
        db.create_user("victim", "pass1234")
        resp = authed_client.post("/users/victim/delete")
        data = resp.get_json()
        assert data["ok"] is True
        assert db.get_user("victim") is None

    def test_delete_self_blocked(self, authed_client):
        resp = authed_client.post("/users/admin/delete")
        data = resp.get_json()
        assert data["ok"] is False
        assert "own account" in data["error"].lower()

    def test_delete_nonexistent_user(self, authed_client):
        resp = authed_client.post("/users/ghost/delete")
        assert resp.status_code == 404

    def test_reset_password(self, authed_client):
        db.create_user("resetme", "oldpass1")
        resp = authed_client.post("/users/resetme/reset-password", data={
            "new_password": "newpass123",
        })
        data = resp.get_json()
        assert data["ok"] is True
        user = db.get_user("resetme")
        assert db.check_password(user["password_hash"], "newpass123")

    def test_reset_password_short(self, authed_client):
        db.create_user("resetshort", "pass1234")
        resp = authed_client.post("/users/resetshort/reset-password", data={
            "new_password": "123",
        })
        data = resp.get_json()
        assert data["ok"] is False

    def test_reset_password_nonexistent(self, authed_client):
        resp = authed_client.post("/users/ghost/reset-password", data={
            "new_password": "pass1234",
        })
        assert resp.status_code == 404


class TestAdminRequired:
    """Test that admin-only routes reject non-admin users."""

    def test_non_admin_cannot_access_users(self, client):
        # Create non-admin user and log in
        db.create_user("operator", "pass1234", is_admin=0)
        client.post("/login", data={
            "username": "operator",
            "password": "pass1234",
        })
        resp = client.get("/users")
        assert resp.status_code == 403

    def test_non_admin_cannot_create_user(self, client):
        db.create_user("operator", "pass1234", is_admin=0)
        client.post("/login", data={
            "username": "operator",
            "password": "pass1234",
        })
        resp = client.post("/users/new", data={
            "username": "sneaky",
            "password": "pass1234",
        })
        assert resp.status_code == 403

    def test_unauthenticated_redirects(self, client):
        resp = client.get("/users")
        assert resp.status_code == 302  # redirect to login


class TestDashboardStatsAPI:
    """Test the /api/stats endpoint."""

    def test_stats_returns_json(self, authed_client):
        resp = authed_client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "total_tenants" in data
        assert "running_jobs" in data
        assert "configured_tenants" in data

    def test_stats_reflects_data(self, authed_client):
        db.create_tenant("StatsT", "stats.com")
        resp = authed_client.get("/api/stats")
        data = resp.get_json()
        assert data["total_tenants"] == 1

    def test_stats_requires_auth(self, client):
        resp = client.get("/api/stats")
        assert resp.status_code == 302


class TestTenantHealthCheck:
    """Test the tenant health check endpoint."""

    def test_health_check_unconfigured(self, authed_client):
        tid = db.create_tenant("Unhealthy", "u.com")
        resp = authed_client.post(f"/tenants/{tid}/health")
        data = resp.get_json()
        assert data["ok"] is False
        assert "checks" in data
        assert data["checks"]["certificate"]["status"] == "fail"
        assert data["checks"]["entra_app"]["status"] == "fail"

    def test_health_check_not_found(self, authed_client):
        resp = authed_client.post("/tenants/nonexistent/health")
        assert resp.status_code == 404

    def test_health_check_partial_config(self, authed_client):
        tid = db.create_tenant("Partial", "p.com")
        db.update_tenant(tid, client_id="abc123")
        resp = authed_client.post(f"/tenants/{tid}/health")
        data = resp.get_json()
        assert data["ok"] is False
        assert data["checks"]["entra_app"]["status"] == "ok"
        assert data["checks"]["certificate"]["status"] == "fail"


class TestTenantExport:
    """Test tenant export/import."""

    def test_export_tenants(self, authed_client):
        db.create_tenant("Export1", "e1.com", notes="test")
        db.create_tenant("Export2", "e2.com")
        resp = authed_client.get("/tenants/export")
        assert resp.status_code == 200
        assert resp.content_type.startswith("application/json")
        data = json.loads(resp.data)
        assert len(data) == 2
        names = [t["name"] for t in data]
        assert "Export1" in names
        # Encrypted passwords should NOT be in export
        for t in data:
            assert "pfx_password_enc" not in t

    def test_import_tenants(self, authed_client):
        payload = json.dumps([
            {"name": "Imported1", "domain": "i1.com", "notes": "via import"},
            {"name": "Imported2", "domain": "i2.com"},
        ])
        resp = authed_client.post("/tenants/import",
            data={"file": (io.BytesIO(payload.encode()), "tenants.json")},
            content_type="multipart/form-data",
        )
        data = resp.get_json()
        assert data["ok"] is True
        assert "2" in data["message"]
        assert len(db.list_tenants()) == 2

    def test_import_skips_invalid(self, authed_client):
        payload = json.dumps([
            {"name": "Good", "domain": "g.com"},
            {"name": "", "domain": ""},  # missing required fields
            {"domain": "nodomain.com"},  # missing name
        ])
        resp = authed_client.post("/tenants/import",
            data={"file": (io.BytesIO(payload.encode()), "tenants.json")},
            content_type="multipart/form-data",
        )
        data = resp.get_json()
        assert data["ok"] is True
        assert "1" in data["message"]  # only 1 imported

    def test_import_invalid_json(self, authed_client):
        resp = authed_client.post("/tenants/import",
            data={"file": (io.BytesIO(b"not json"), "bad.json")},
            content_type="multipart/form-data",
        )
        data = resp.get_json()
        assert data["ok"] is False
        assert "Invalid JSON" in data["error"]

    def test_import_no_file(self, authed_client):
        resp = authed_client.post("/tenants/import")
        data = resp.get_json()
        assert data["ok"] is False

    def test_import_not_array(self, authed_client):
        payload = json.dumps({"name": "NotArray"})
        resp = authed_client.post("/tenants/import",
            data={"file": (io.BytesIO(payload.encode()), "obj.json")},
            content_type="multipart/form-data",
        )
        data = resp.get_json()
        assert data["ok"] is False
        assert "array" in data["error"].lower()


class TestJobCancellation:
    """Test job cancellation route."""

    def test_cancel_nonexistent_job(self, authed_client):
        resp = authed_client.post("/jobs/fakeid/cancel")
        assert resp.status_code == 404

    def test_cancel_finished_job(self, authed_client):
        jid = db.create_job("Done Job", "spo")
        db.finish_job(jid, "done")
        resp = authed_client.post(f"/jobs/{jid}/cancel")
        data = resp.get_json()
        assert data["ok"] is False
        assert "not running" in data["error"].lower()

    def test_cancel_running_job_no_process(self, authed_client):
        """Job is running in DB but process doesn't exist in memory."""
        jid = db.create_job("Orphan", "spo")
        resp = authed_client.post(f"/jobs/{jid}/cancel")
        data = resp.get_json()
        assert data["ok"] is False
        assert "not found" in data["error"].lower()


class TestReportDeletion:
    """Test report file deletion."""

    def test_delete_report_path_traversal(self, authed_client):
        resp = authed_client.post("/reports/delete", data={
            "path": "/etc/passwd",
        })
        assert resp.status_code == 403

    def test_delete_report_empty_path(self, authed_client):
        resp = authed_client.post("/reports/delete", data={
            "path": "",
        })
        assert resp.status_code == 403

    def test_delete_report_nonexistent(self, authed_client, tmp_path):
        """Path within allowed dir but file doesn't exist."""
        from helpers import TENANTS_DIR
        fake_path = str(TENANTS_DIR / "nonexistent" / "file.csv")
        resp = authed_client.post("/reports/delete", data={
            "path": fake_path,
        })
        # Either 403 (if resolved path doesn't start with TENANTS_DIR) or 404
        assert resp.status_code in (403, 404)

    def test_delete_report_success(self, authed_client):
        """Create a temp file within tenants dir and delete it."""
        from helpers import TENANTS_DIR
        test_dir = TENANTS_DIR / "_test_delete"
        test_dir.mkdir(parents=True, exist_ok=True)
        test_file = test_dir / "test_report.csv"
        test_file.write_text("col1,col2\n1,2\n")

        try:
            resp = authed_client.post("/reports/delete", data={
                "path": str(test_file),
            })
            data = resp.get_json()
            assert data["ok"] is True
            assert not test_file.exists()
        finally:
            if test_dir.exists():
                import shutil
                shutil.rmtree(test_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Health check endpoint
# ---------------------------------------------------------------------------

class TestHealthCheck:
    """Tests for the /health endpoint."""

    def test_health_check_returns_200(self, client):
        """Health check should return 200 when DB is accessible."""
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "healthy"
        assert data["database"] == "ok"

    def test_health_check_no_auth_required(self, client):
        """Health check should not require authentication."""
        resp = client.get("/health")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

class TestErrorHandlers:
    """Tests for custom error handlers."""

    def test_404_returns_html(self, authed_client):
        """Non-existent page returns styled 404 page."""
        resp = authed_client.get("/nonexistent-page")
        assert resp.status_code == 404
        assert b"Page not found" in resp.data

    def test_404_api_returns_json(self, authed_client):
        """API 404 returns JSON."""
        resp = authed_client.get("/api/nonexistent",
                                 headers={"Accept": "application/json"})
        assert resp.status_code == 404

    def test_403_on_non_admin(self, client, admin_user):
        """Non-admin accessing admin route gets 403."""
        db.create_user("operator", "password123", display_name="Op", is_admin=0)
        client.post("/login", data={
            "username": "operator",
            "password": "password123",
        })
        resp = client.get("/users")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

class TestNewSecurityHeaders:
    """Tests for additional security headers."""

    def test_referrer_policy_header(self, authed_client):
        """Referrer-Policy header should be present."""
        resp = authed_client.get("/")
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    def test_permissions_policy_header(self, authed_client):
        """Permissions-Policy header should be present."""
        resp = authed_client.get("/")
        assert "camera=()" in resp.headers.get("Permissions-Policy", "")

    def test_request_id_header(self, authed_client):
        """X-Request-ID header should be present."""
        resp = authed_client.get("/")
        assert resp.headers.get("X-Request-ID") is not None


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------

class TestInputValidation:
    """Tests for input validation helper functions."""

    def test_validate_domain_valid(self):
        from helpers import validate_domain
        assert validate_domain("contoso.onmicrosoft.com") is True
        assert validate_domain("example.com") is True
        assert validate_domain("sub.domain.co.uk") is True

    def test_validate_domain_invalid(self):
        from helpers import validate_domain
        assert validate_domain("") is False
        assert validate_domain("not a domain") is False
        assert validate_domain("-invalid.com") is False
        assert validate_domain("a" * 254) is False

    def test_validate_tenant_guid_valid(self):
        from helpers import validate_tenant_guid
        assert validate_tenant_guid("12345678-1234-1234-1234-123456789abc") is True

    def test_validate_tenant_guid_invalid(self):
        from helpers import validate_tenant_guid
        assert validate_tenant_guid("not-a-guid") is False
        assert validate_tenant_guid("") is False

    def test_validate_site_url_valid(self):
        from helpers import validate_site_url
        assert validate_site_url("https://contoso.sharepoint.com/sites/test") is True
        assert validate_site_url("https://contoso.sharepoint.com") is True

    def test_validate_site_url_invalid(self):
        from helpers import validate_site_url
        assert validate_site_url("http://contoso.sharepoint.com") is False
        assert validate_site_url("https://evil.com") is False
        assert validate_site_url("") is False

    def test_validate_email_valid(self):
        from helpers import validate_email
        assert validate_email("user@example.com") is True
        assert validate_email("admin@contoso.onmicrosoft.com") is True

    def test_validate_email_invalid(self):
        from helpers import validate_email
        assert validate_email("notanemail") is False
        assert validate_email("") is False

    def test_sanitize_text(self):
        from helpers import sanitize_text
        assert sanitize_text("  hello  ") == "hello"
        assert sanitize_text("a" * 600, max_length=500) == "a" * 500


# ---------------------------------------------------------------------------
# Database maintenance
# ---------------------------------------------------------------------------

class TestDatabaseMaintenance:
    """Tests for database backup and integrity check functions."""

    def test_check_integrity(self):
        """Integrity check should return OK for a valid database."""
        result = db.check_integrity()
        assert result["integrity"] == "ok"
        assert result["schema_version"] >= 1

    def test_purge_old_jobs(self):
        """purge_old_jobs should delete old completed jobs."""
        # Create a job and manually backdate it
        jid = db.create_job("test", "test", operator="admin")
        db.finish_job(jid, "done")
        # Backdate the job to 100 days ago
        with db.get_db() as conn:
            conn.execute(
                "UPDATE jobs SET started_at = datetime('now', '-100 days') WHERE id = ?",
                (jid,)
            )
        # Purge jobs older than 90 days
        deleted = db.purge_old_jobs(days=90)
        assert deleted >= 1

    def test_backup_db(self):
        """backup_db should create a backup file."""
        result = db.backup_db()
        assert result is not None
        assert Path(result).exists()
        # Clean up
        Path(result).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Tenant creation validation
# ---------------------------------------------------------------------------

class TestTenantValidation:
    """Tests for tenant creation input validation."""

    def test_create_tenant_invalid_domain(self, authed_client):
        """Creating a tenant with invalid domain should fail."""
        resp = authed_client.post("/tenants/new", data={
            "name": "Test Tenant",
            "domain": "not a valid domain!!",
        }, follow_redirects=True)
        assert b"Invalid domain format" in resp.data

    def test_create_tenant_invalid_guid(self, authed_client):
        """Creating a tenant with invalid tenant ID should fail."""
        resp = authed_client.post("/tenants/new", data={
            "name": "Test Tenant",
            "domain": "contoso.onmicrosoft.com",
            "tenant_id": "not-a-guid",
        }, follow_redirects=True)
        assert b"Tenant ID must be a valid GUID" in resp.data

    def test_create_tenant_valid(self, authed_client):
        """Creating a tenant with valid inputs should succeed."""
        resp = authed_client.post("/tenants/new", data={
            "name": "Test Tenant",
            "domain": "contoso.onmicrosoft.com",
        }, follow_redirects=True)
        assert resp.status_code == 200
        # Should redirect to tenant detail
        assert b"Test Tenant" in resp.data
