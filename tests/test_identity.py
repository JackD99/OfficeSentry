"""tests/test_identity.py — Identity protection blueprint tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


class TestRiskyUsers:
    def test_requires_auth(self, client):
        resp = client.get("/identity/risky-users")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/identity/risky-users")
        assert resp.status_code == 200


class TestRiskySignins:
    def test_requires_auth(self, client):
        resp = client.get("/identity/risky-signins")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/identity/risky-signins")
        assert resp.status_code == 200


class TestMFAReport:
    def test_requires_auth(self, client):
        resp = client.get("/identity/mfa-report")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/identity/mfa-report")
        assert resp.status_code == 200

    def test_run_requires_tenants(self, authed_client):
        resp = authed_client.post("/identity/mfa-report/run", data={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False


class TestRiskOverview:
    def test_requires_auth(self, client):
        resp = client.get("/identity/risk-overview")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/identity/risk-overview")
        assert resp.status_code == 200

    def test_scan_requires_tenants(self, authed_client):
        resp = authed_client.post("/identity/risk-overview/scan", data={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False


class TestFetchEndpoints:
    def test_risky_users_fetch_validates_tenant(self, authed_client):
        tid = db.create_tenant("T", "t.com")
        resp = authed_client.post(f"/tenants/{tid}/risky-users/fetch", data={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False

    def test_risky_signins_fetch_validates_tenant(self, authed_client):
        tid = db.create_tenant("T", "t.com")
        resp = authed_client.post(f"/tenants/{tid}/risky-signins/fetch", data={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False
