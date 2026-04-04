"""tests/test_audits.py — Audits blueprint tests."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


def _create_configured_tenant():
    """Create a tenant with client_id set (simulating a configured tenant)."""
    tid = db.create_tenant("T", "t.com")
    db.update_tenant(tid, client_id="test-client-id")
    return tid


class TestAuditCatalogue:
    def test_requires_auth(self, client):
        resp = client.get("/audits")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/audits")
        assert resp.status_code == 200


class TestAuditBreach:
    def test_requires_auth(self, client):
        tid = _create_configured_tenant()
        resp = client.get(f"/tenants/{tid}/audit/breach")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        tid = _create_configured_tenant()
        resp = authed_client.get(f"/tenants/{tid}/audit/breach")
        assert resp.status_code == 200


class TestBatch:
    def test_requires_auth(self, client):
        resp = client.get("/batch")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/batch")
        assert resp.status_code == 200

    def test_batch_audit_requires_tenants(self, authed_client):
        resp = authed_client.post("/batch/audit", data={"audit_type": "breach_signin"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False
