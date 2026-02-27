"""
tests/test_api_v1.py — Tests for the REST API v1 layer.

Tests cover:
- API key creation and validation
- Bearer token authentication
- All API endpoints
- RBAC scoping on API
- CSRF exemption
- API key management
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


# ---------------------------------------------------------------------------
# API Key database operations
# ---------------------------------------------------------------------------

class TestAPIKeyCRUD:
    """Test API key database operations."""

    def test_create_api_key(self):
        key_id, raw_key = db.create_api_key(
            name="Test Key", role="viewer", created_by="admin"
        )
        assert key_id is not None
        assert raw_key.startswith("spat_")
        assert len(raw_key) == 69  # "spat_" + 64 hex chars

    def test_validate_api_key(self):
        key_id, raw_key = db.create_api_key(
            name="Valid", role="operator", created_by="admin"
        )
        record = db.validate_api_key(raw_key)
        assert record is not None
        assert record["name"] == "Valid"
        assert record["role"] == "operator"

    def test_validate_invalid_key(self):
        assert db.validate_api_key("spat_invalid_key_here") is None

    def test_validate_non_spat_key(self):
        assert db.validate_api_key("not_a_valid_key") is None

    def test_validate_empty_key(self):
        assert db.validate_api_key("") is None
        assert db.validate_api_key(None) is None

    def test_list_api_keys(self):
        db.create_api_key(name="K1", created_by="admin")
        db.create_api_key(name="K2", created_by="admin")
        keys = db.list_api_keys()
        assert len(keys) == 2
        # Keys should not include the raw key or hash
        for k in keys:
            assert "key_hash" not in k
            assert k["key_prefix"].startswith("spat_")

    def test_delete_api_key(self):
        key_id, raw_key = db.create_api_key(name="Delete", created_by="admin")
        assert db.delete_api_key(key_id)
        assert db.validate_api_key(raw_key) is None

    def test_delete_nonexistent_key(self):
        assert not db.delete_api_key("nonexistent")

    def test_api_key_last_used_updated(self):
        key_id, raw_key = db.create_api_key(name="Used", created_by="admin")
        # Initially no last_used_at
        keys = db.list_api_keys()
        key = [k for k in keys if k["id"] == key_id][0]
        assert key["last_used_at"] is None

        # Validate to trigger last_used_at update
        db.validate_api_key(raw_key)
        keys = db.list_api_keys()
        key = [k for k in keys if k["id"] == key_id][0]
        assert key["last_used_at"] is not None


# ---------------------------------------------------------------------------
# API authentication
# ---------------------------------------------------------------------------

class TestAPIAuth:
    """Test Bearer token authentication on API endpoints."""

    def test_missing_auth_header(self, client):
        resp = client.get("/api/v1/tenants")
        assert resp.status_code == 401
        data = resp.get_json()
        assert not data["ok"]
        assert "authorization" in data["error"].lower()

    def test_invalid_bearer_token(self, client):
        resp = client.get("/api/v1/tenants",
                          headers={"Authorization": "Bearer spat_invalid"})
        assert resp.status_code == 401

    def test_non_bearer_auth(self, client):
        resp = client.get("/api/v1/tenants",
                          headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert resp.status_code == 401

    def test_valid_bearer_token(self, client):
        _, raw_key = db.create_api_key(name="Auth", role="viewer",
                                        created_by="admin")
        resp = client.get("/api/v1/tenants",
                          headers={"Authorization": f"Bearer {raw_key}"})
        assert resp.status_code == 200

    def test_health_endpoint_no_auth(self, client):
        """Health endpoint should not require authentication."""
        resp = client.get("/api/v1/health")
        assert resp.status_code in (200, 503)
        data = resp.get_json()
        assert "status" in data


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

class TestAPIEndpoints:
    """Test individual API endpoints."""

    @pytest.fixture(autouse=True)
    def setup_api_key(self):
        """Create an API key for testing."""
        key_id, raw_key = db.create_api_key(
            name="Test", role="super_admin", created_by="admin"
        )
        self.api_key = raw_key
        self.key_id = key_id
        self.auth_headers = {"Authorization": f"Bearer {raw_key}"}

    def test_list_tenants(self, client):
        db.create_tenant("Test Tenant", "test.com", tenant_id="t1")
        resp = client.get("/api/v1/tenants", headers=self.auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"]
        assert len(data["data"]) == 1
        assert data["data"][0]["domain"] == "test.com"

    def test_get_tenant(self, client):
        t_id = db.create_tenant("Detail Tenant", "detail.com", tenant_id="t2")
        resp = client.get(f"/api/v1/tenants/{t_id}",
                          headers=self.auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"]
        assert data["data"]["domain"] == "detail.com"

    def test_get_tenant_not_found(self, client):
        resp = client.get("/api/v1/tenants/nonexistent",
                          headers=self.auth_headers)
        assert resp.status_code == 404

    def test_list_jobs(self, client):
        db.create_job("API Job", "test")
        resp = client.get("/api/v1/jobs", headers=self.auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"]
        assert len(data["data"]) >= 1

    def test_get_job(self, client):
        job_id = db.create_job("Single Job", "test")
        resp = client.get(f"/api/v1/jobs/{job_id}",
                          headers=self.auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"]
        assert data["data"]["label"] == "Single Job"

    def test_get_job_not_found(self, client):
        resp = client.get("/api/v1/jobs/nonexistent",
                          headers=self.auth_headers)
        assert resp.status_code == 404

    def test_run_audit_requires_operator(self, client):
        """Viewer-role API key cannot run audits."""
        _, viewer_key = db.create_api_key(
            name="Viewer", role="viewer", created_by="admin"
        )
        resp = client.post(
            "/api/v1/audits/run",
            headers={"Authorization": f"Bearer {viewer_key}",
                     "Content-Type": "application/json"},
            data=json.dumps({"tenant_id": "t1", "audit_type": "spo"}),
        )
        assert resp.status_code == 403

    def test_run_audit_missing_tenant_id(self, client):
        resp = client.post(
            "/api/v1/audits/run",
            headers={**self.auth_headers,
                     "Content-Type": "application/json"},
            data=json.dumps({"audit_type": "spo"}),
        )
        data = resp.get_json()
        assert not data["ok"]
        assert "tenant_id" in data["error"]

    def test_run_audit_missing_audit_type(self, client):
        resp = client.post(
            "/api/v1/audits/run",
            headers={**self.auth_headers,
                     "Content-Type": "application/json"},
            data=json.dumps({"tenant_id": "t1"}),
        )
        data = resp.get_json()
        assert not data["ok"]
        assert "audit_type" in data["error"]

    def test_bpa_results(self, client):
        t_id = db.create_tenant("BPA Tenant", "bpa.com", tenant_id="tbpa")
        resp = client.get(f"/api/v1/bpa/{t_id}",
                          headers=self.auth_headers)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"]

    def test_health_returns_status(self, client):
        resp = client.get("/api/v1/health")
        data = resp.get_json()
        assert "status" in data


# ---------------------------------------------------------------------------
# API key management via API
# ---------------------------------------------------------------------------

class TestAPIKeyManagement:
    """Test API key management endpoints."""

    @pytest.fixture(autouse=True)
    def setup_admin_key(self):
        """Create a super_admin API key."""
        _, raw_key = db.create_api_key(
            name="Admin Key", role="super_admin", created_by="admin"
        )
        self.admin_key = raw_key
        self.admin_headers = {"Authorization": f"Bearer {raw_key}"}

    def test_create_key_via_api(self, client):
        resp = client.post(
            "/api/v1/keys",
            headers={**self.admin_headers,
                     "Content-Type": "application/json"},
            data=json.dumps({"name": "New Key", "role": "viewer"}),
        )
        data = resp.get_json()
        assert data["ok"]
        assert "key" in data["data"]
        assert data["data"]["key"].startswith("spat_")

    def test_create_key_missing_name(self, client):
        resp = client.post(
            "/api/v1/keys",
            headers={**self.admin_headers,
                     "Content-Type": "application/json"},
            data=json.dumps({"role": "viewer"}),
        )
        data = resp.get_json()
        assert not data["ok"]

    def test_list_keys_via_api(self, client):
        resp = client.get("/api/v1/keys", headers=self.admin_headers)
        data = resp.get_json()
        assert data["ok"]
        assert isinstance(data["data"], list)

    def test_delete_key_via_api(self, client):
        key_id, _ = db.create_api_key(name="Del", created_by="admin")
        resp = client.delete(f"/api/v1/keys/{key_id}",
                             headers=self.admin_headers)
        data = resp.get_json()
        assert data["ok"]

    def test_delete_nonexistent_key(self, client):
        resp = client.delete("/api/v1/keys/nonexistent",
                             headers=self.admin_headers)
        assert resp.status_code == 404

    def test_non_admin_cannot_manage_keys(self, client):
        """Only super_admin can manage API keys."""
        _, viewer_key = db.create_api_key(
            name="Viewer", role="viewer", created_by="admin"
        )
        viewer_headers = {"Authorization": f"Bearer {viewer_key}"}

        resp = client.get("/api/v1/keys", headers=viewer_headers)
        assert resp.status_code == 403

        resp = client.post(
            "/api/v1/keys",
            headers={**viewer_headers, "Content-Type": "application/json"},
            data=json.dumps({"name": "X"}),
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# CSRF exemption
# ---------------------------------------------------------------------------

class TestCSRFExemption:
    """Test that API endpoints are exempt from CSRF protection."""

    def test_api_post_without_csrf_token(self, app_csrf):
        """API POST should work without CSRF token (uses Bearer auth)."""
        client = app_csrf.test_client()
        _, raw_key = db.create_api_key(
            name="CSRF Test", role="super_admin", created_by="admin"
        )
        resp = client.post(
            "/api/v1/keys",
            headers={
                "Authorization": f"Bearer {raw_key}",
                "Content-Type": "application/json",
            },
            data=json.dumps({"name": "New Key"}),
        )
        # Should succeed (not 400 CSRF error)
        data = resp.get_json()
        assert data["ok"]


# ---------------------------------------------------------------------------
# API RBAC scoping
# ---------------------------------------------------------------------------

class TestAPIRBACScoping:
    """Test that API endpoints respect RBAC scoping."""

    def test_tenant_access_denied(self, client):
        """API key with restricted groups can't access unassigned tenants."""
        group_id = db.create_tenant_group("Restricted")
        t_id = db.create_tenant("Restricted", "restricted.com",
                                 tenant_id="tr1")
        # Create key with groups but DON'T add tenant to group
        _, raw_key = db.create_api_key(
            name="Restricted", role="operator",
            allowed_tenant_groups=json.dumps([str(group_id)]),
            created_by="admin",
        )
        resp = client.get(
            f"/api/v1/tenants/{t_id}",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert resp.status_code == 403

    def test_jobs_limit_parameter(self, client):
        """Jobs endpoint respects limit parameter."""
        _, raw_key = db.create_api_key(
            name="Limit", role="viewer", created_by="admin"
        )
        for i in range(5):
            db.create_job(f"Job {i}", "test")

        resp = client.get(
            "/api/v1/jobs?limit=2",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        data = resp.get_json()
        assert data["ok"]
        assert len(data["data"]) <= 2


# ---------------------------------------------------------------------------
# Portal API key management routes (settings page)
# ---------------------------------------------------------------------------

class TestPortalAPIKeyRoutes:
    """Test API key management from the portal settings page."""

    def test_create_key_from_settings(self, authed_client):
        resp = authed_client.post("/settings/api-keys/create", data={
            "name": "Portal Key",
            "role": "viewer",
        })
        data = resp.get_json()
        assert data["ok"]
        assert "key" in data
        assert data["key"].startswith("spat_")

    def test_create_key_missing_name(self, authed_client):
        resp = authed_client.post("/settings/api-keys/create", data={
            "role": "viewer",
        })
        data = resp.get_json()
        assert not data["ok"]

    def test_delete_key_from_settings(self, authed_client):
        key_id, _ = db.create_api_key(name="PortalDel", created_by="admin")
        resp = authed_client.post(f"/settings/api-keys/{key_id}/delete")
        data = resp.get_json()
        assert data["ok"]

    def test_delete_nonexistent_key_from_settings(self, authed_client):
        resp = authed_client.post("/settings/api-keys/nonexistent/delete")
        assert resp.status_code == 404

    def test_settings_page_shows_api_keys(self, authed_client):
        db.create_api_key(name="Visible Key", created_by="admin")
        resp = authed_client.get("/settings")
        assert resp.status_code == 200
        assert b"API Keys" in resp.data
