"""tests/test_email_security.py — Email security blueprint tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


class TestQuarantinePage:
    def test_requires_auth(self, client):
        resp = client.get("/email-security/quarantine")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/email-security/quarantine")
        assert resp.status_code == 200


class TestTransportRules:
    def test_requires_auth(self, client):
        resp = client.get("/email-security/transport-rules")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/email-security/transport-rules")
        assert resp.status_code == 200


class TestAntiPhishing:
    def test_requires_auth(self, client):
        resp = client.get("/email-security/anti-phishing")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/email-security/anti-phishing")
        assert resp.status_code == 200


class TestDefenderPolicies:
    def test_requires_auth(self, client):
        resp = client.get("/email-security/defender-policies")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/email-security/defender-policies")
        assert resp.status_code == 200


class TestThreatReport:
    def test_requires_auth(self, client):
        resp = client.get("/email-security/threat-report")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/email-security/threat-report")
        assert resp.status_code == 200

    def test_run_requires_tenants(self, authed_client):
        resp = authed_client.post("/email-security/threat-report/run", data={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False


class TestFetchEndpoints:
    def test_quarantine_fetch_validates_tenant(self, authed_client):
        tid = db.create_tenant("T", "t.com")
        resp = authed_client.post(f"/tenants/{tid}/quarantine/fetch", data={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False

    def test_transport_rules_fetch_validates_tenant(self, authed_client):
        tid = db.create_tenant("T", "t.com")
        resp = authed_client.post(f"/tenants/{tid}/transport-rules/fetch", data={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False
