"""
tests/test_routes.py — Route access control and API response format tests.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


class TestDashboard:
    def test_dashboard_accessible_when_authed(self, authed_client):
        resp = authed_client.get("/")
        assert resp.status_code == 200
        assert b"Dashboard" in resp.data


class TestTenantRoutes:
    def test_tenants_list(self, authed_client):
        resp = authed_client.get("/tenants")
        assert resp.status_code == 200
        assert b"Tenants" in resp.data

    def test_create_tenant(self, authed_client):
        resp = authed_client.post("/tenants/new", data={
            "name": "Test Tenant",
            "domain": "test.onmicrosoft.com",
            "tenant_id": "",
            "notes": "A test",
        }, follow_redirects=True)
        assert resp.status_code == 200
        tenants = db.list_tenants()
        assert len(tenants) == 1
        assert tenants[0]["name"] == "Test Tenant"

    def test_tenant_detail_404(self, authed_client):
        resp = authed_client.get("/tenants/nonexistent")
        assert resp.status_code == 404

    def test_tenant_detail(self, authed_client):
        tid = db.create_tenant("Detail Test", "dt.com")
        resp = authed_client.get(f"/tenants/{tid}")
        assert resp.status_code == 200
        assert b"Detail Test" in resp.data

    def test_tenant_settings_update(self, authed_client):
        tid = db.create_tenant("Before", "before.com")
        resp = authed_client.post(f"/tenants/{tid}/settings", data={
            "name": "After",
            "domain": "after.com",
        })
        data = resp.get_json()
        assert data["ok"] is True
        t = db.get_tenant(tid)
        assert t["name"] == "After"
        assert t["domain"] == "after.com"

    def test_tenant_delete(self, authed_client):
        tid = db.create_tenant("Deletable", "del.com")
        resp = authed_client.post(f"/tenants/{tid}/delete", follow_redirects=True)
        assert resp.status_code == 200
        assert db.get_tenant(tid) is None


class TestJobRoutes:
    def test_jobs_page(self, authed_client):
        resp = authed_client.get("/jobs")
        assert resp.status_code == 200

    def test_job_status_not_found(self, authed_client):
        resp = authed_client.get("/jobs/nonexistent/status")
        assert resp.status_code == 404

    def test_job_status_json(self, authed_client):
        jid = db.create_job("Test", "spo")
        db.append_job_output(jid, "hello")
        resp = authed_client.get(f"/jobs/{jid}/status")
        data = resp.get_json()
        assert data["status"] == "running"
        assert "hello" in data["output"]


class TestReportRoutes:
    def test_reports_page(self, authed_client):
        resp = authed_client.get("/reports")
        assert resp.status_code == 200

    def test_audit_trail_page(self, authed_client):
        resp = authed_client.get("/audit-log")
        assert resp.status_code == 200


class TestAPIResponseFormat:
    def test_settings_password_returns_json(self, authed_client):
        resp = authed_client.post("/settings/password", data={
            "current_password": "password123",
            "new_password": "newpass123",
            "confirm_password": "newpass123",
        })
        assert resp.content_type.startswith("application/json")
        data = resp.get_json()
        assert "ok" in data
