"""tests/test_service_health.py — Service health blueprint tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


class TestServiceHealthPage:
    def test_requires_auth(self, client):
        resp = client.get("/service-health")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/service-health")
        assert resp.status_code == 200

    def test_scan_requires_tenants(self, authed_client):
        resp = authed_client.post("/service-health/scan", data={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False


class TestServiceIncidents:
    def test_requires_auth(self, client):
        resp = client.get("/service-health/incidents")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/service-health/incidents")
        assert resp.status_code == 200


class TestMessageCenter:
    def test_requires_auth(self, client):
        resp = client.get("/service-health/message-center")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/service-health/message-center")
        assert resp.status_code == 200


class TestMaintenance:
    def test_requires_auth(self, client):
        resp = client.get("/service-health/maintenance")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/service-health/maintenance")
        assert resp.status_code == 200


class TestHealthOverview:
    def test_requires_auth(self, client):
        resp = client.get("/service-health/overview")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/service-health/overview")
        assert resp.status_code == 200

    def test_scan_requires_tenants(self, authed_client):
        resp = authed_client.post("/service-health/overview/scan", data={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False


class TestFetchEndpoints:
    def test_service_health_fetch_validates_tenant(self, authed_client):
        tid = db.create_tenant("T", "t.com")
        resp = authed_client.post(f"/tenants/{tid}/service-health/fetch", data={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False

    def test_incidents_fetch_validates_tenant(self, authed_client):
        tid = db.create_tenant("T", "t.com")
        resp = authed_client.post(
            f"/tenants/{tid}/service-health/incidents/fetch", data={}
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False
