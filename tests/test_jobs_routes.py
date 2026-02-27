"""tests/test_jobs_routes.py — Jobs blueprint route tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


class TestJobsPage:
    def test_requires_auth(self, client):
        resp = client.get("/jobs")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/jobs")
        assert resp.status_code == 200

    def test_page_loads_with_tenant_filter(self, authed_client):
        tid = db.create_tenant("T", "t.com")
        resp = authed_client.get(f"/jobs?tenant_id={tid}")
        assert resp.status_code == 200


class TestJobStatus:
    def test_requires_auth(self, client):
        jid = db.create_job("Test", "spo")
        resp = client.get(f"/jobs/{jid}/status")
        assert resp.status_code in (302, 308)

    def test_returns_json(self, authed_client):
        jid = db.create_job("Test", "spo", operator="admin")
        resp = authed_client.get(f"/jobs/{jid}/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "status" in data

    def test_nonexistent_job(self, authed_client):
        resp = authed_client.get("/jobs/nonexistent-id/status")
        assert resp.status_code in (200, 404)


class TestJobCancel:
    def test_requires_auth(self, client):
        jid = db.create_job("Test", "spo")
        resp = client.post(f"/jobs/{jid}/cancel")
        assert resp.status_code in (302, 308)

    def test_cancel_returns_json(self, authed_client):
        jid = db.create_job("Test", "spo", operator="admin")
        resp = authed_client.post(f"/jobs/{jid}/cancel")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "ok" in data


class TestRunningCount:
    def test_requires_auth(self, client):
        resp = client.get("/jobs/running-count")
        assert resp.status_code in (302, 308)

    def test_returns_count(self, authed_client):
        resp = authed_client.get("/jobs/running-count")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "count" in data
