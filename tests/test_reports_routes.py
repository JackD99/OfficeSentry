"""tests/test_reports_routes.py — Reports blueprint route tests."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


class TestReportsPage:
    def test_requires_auth(self, client):
        resp = client.get("/reports")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/reports")
        assert resp.status_code == 200


class TestReportViewer:
    def test_requires_auth(self, client):
        resp = client.get("/reports/view")
        assert resp.status_code in (302, 308)

    def test_no_path_handled(self, authed_client):
        resp = authed_client.get("/reports/view")
        assert resp.status_code in (200, 302, 400, 403)


class TestReportDownload:
    def test_requires_auth(self, client):
        resp = client.get("/reports/download?path=test.csv")
        assert resp.status_code in (302, 308)

    def test_rejects_traversal(self, authed_client):
        resp = authed_client.get("/reports/download?path=../../etc/passwd")
        assert resp.status_code == 403


class TestSchedules:
    def test_requires_auth(self, client):
        resp = client.get("/reports/schedules")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/reports/schedules")
        assert resp.status_code == 200

    def test_create_schedule_validates(self, authed_client):
        resp = authed_client.post("/reports/schedules/create", data={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False


class TestSavedViews:
    def test_save_view_validates(self, authed_client):
        resp = authed_client.post("/reports/views", data={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False


class TestAuditTrail:
    def test_requires_auth(self, client):
        resp = client.get("/audit-log")
        assert resp.status_code in (302, 308)

    def test_page_loads(self, authed_client):
        resp = authed_client.get("/audit-log")
        assert resp.status_code == 200
