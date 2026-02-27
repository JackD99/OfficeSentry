"""tests/test_licenses.py — Licenses blueprint tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


class TestLicenseInventory:
    def test_requires_auth(self, client):
        resp = client.get("/licenses")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/licenses")
        assert resp.status_code == 200


class TestLicenseUsage:
    def test_requires_auth(self, client):
        resp = client.get("/licenses/usage")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/licenses/usage")
        assert resp.status_code == 200


class TestLicenseOptimization:
    def test_requires_auth(self, client):
        resp = client.get("/licenses/optimization")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/licenses/optimization")
        assert resp.status_code == 200


class TestLicenseOverview:
    def test_requires_auth(self, client):
        resp = client.get("/licenses/overview")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/licenses/overview")
        assert resp.status_code == 200

    def test_scan_requires_tenants(self, authed_client):
        resp = authed_client.post("/licenses/overview/scan", data={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False


class TestFetchEndpoints:
    def test_licenses_fetch_validates_tenant(self, authed_client):
        tid = db.create_tenant("T", "t.com")
        resp = authed_client.post(f"/tenants/{tid}/licenses/fetch", data={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False

    def test_usage_fetch_validates_tenant(self, authed_client):
        tid = db.create_tenant("T", "t.com")
        resp = authed_client.post(f"/tenants/{tid}/licenses/usage/fetch", data={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False
