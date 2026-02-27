"""tests/test_devices.py — Devices blueprint tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


class TestDeviceInventory:
    def test_requires_auth(self, client):
        resp = client.get("/devices/inventory")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/devices/inventory")
        assert resp.status_code == 200


class TestDeviceCompliance:
    def test_requires_auth(self, client):
        resp = client.get("/devices/compliance")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/devices/compliance")
        assert resp.status_code == 200

    def test_scan_requires_tenants(self, authed_client):
        resp = authed_client.post("/devices/compliance/scan", data={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False


class TestAutopilot:
    def test_requires_auth(self, client):
        resp = client.get("/devices/autopilot")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/devices/autopilot")
        assert resp.status_code == 200


class TestDevicesOverview:
    def test_requires_auth(self, client):
        resp = client.get("/devices/overview")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/devices/overview")
        assert resp.status_code == 200

    def test_scan_requires_tenants(self, authed_client):
        resp = authed_client.post("/devices/overview/scan", data={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False


class TestFetchEndpoints:
    def test_devices_fetch_validates_tenant(self, authed_client):
        tid = db.create_tenant("T", "t.com")
        resp = authed_client.post(f"/tenants/{tid}/devices/fetch", data={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False

    def test_device_action_validates(self, authed_client):
        tid = db.create_tenant("T", "t.com")
        resp = authed_client.post(f"/tenants/{tid}/devices/action", data={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False
