"""tests/test_incidents.py — Incident response blueprint tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


class TestIncidentsDashboard:
    def test_requires_auth(self, client):
        resp = client.get("/incidents")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/incidents")
        assert resp.status_code == 200


class TestBECResponse:
    def test_requires_auth(self, client):
        resp = client.get("/incidents/bec-response")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/incidents/bec-response")
        assert resp.status_code == 200

    def test_run_validates_tenant(self, authed_client):
        tid = db.create_tenant("T", "t.com")
        resp = authed_client.post(f"/tenants/{tid}/incidents/bec/run", data={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False


class TestAccountCompromise:
    def test_requires_auth(self, client):
        resp = client.get("/incidents/account-compromise")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/incidents/account-compromise")
        assert resp.status_code == 200

    def test_run_validates_tenant(self, authed_client):
        tid = db.create_tenant("T", "t.com")
        resp = authed_client.post(
            f"/tenants/{tid}/incidents/account-compromise/run", data={}
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False


class TestDataExfiltration:
    def test_requires_auth(self, client):
        resp = client.get("/incidents/data-exfiltration")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/incidents/data-exfiltration")
        assert resp.status_code == 200


class TestMalwareOutbreak:
    def test_requires_auth(self, client):
        resp = client.get("/incidents/malware-outbreak")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/incidents/malware-outbreak")
        assert resp.status_code == 200


class TestPrivilegeEscalation:
    def test_requires_auth(self, client):
        resp = client.get("/incidents/privilege-escalation")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/incidents/privilege-escalation")
        assert resp.status_code == 200
