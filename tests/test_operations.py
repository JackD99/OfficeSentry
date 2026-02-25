"""
tests/test_operations.py — Tests for the MSP operations routes.
"""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


class TestOperationsPage:
    def test_operations_page_requires_auth(self, client):
        resp = client.get("/operations")
        assert resp.status_code == 302
        assert "/login" in resp.location

    def test_operations_page_accessible(self, authed_client):
        resp = authed_client.get("/operations")
        assert resp.status_code == 200
        assert b"MSP Operations" in resp.data

    def test_operations_page_shows_tiers(self, authed_client):
        # Need a configured tenant so the form renders instead of empty state
        tid = db.create_tenant("TierTest", "tier.onmicrosoft.com")
        db.update_tenant(tid, client_id="fake-client")
        resp = authed_client.get("/operations")
        assert b"1st Line" in resp.data
        assert b"2nd Line" in resp.data
        assert b"3rd Line" in resp.data

    def test_operations_page_shows_empty_state(self, authed_client):
        """When no tenants are configured, show guidance."""
        resp = authed_client.get("/operations")
        # No tenants exist yet, so should show empty state
        assert b"No tenants are ready" in resp.data


class TestOperationsRun:
    def test_run_requires_auth(self, client):
        resp = client.post("/operations/run")
        assert resp.status_code == 302

    def test_run_unknown_action(self, authed_client):
        resp = authed_client.post("/operations/run", data={
            "action": "nonexistent_action",
            "tenant_id": "fake",
        })
        data = resp.get_json()
        assert data["ok"] is False
        assert "Unknown" in data["error"]

    def test_run_missing_target_user(self, authed_client):
        resp = authed_client.post("/operations/run", data={
            "action": "block",
            "tenant_id": "fake",
            "target_user": "",
        })
        data = resp.get_json()
        assert data["ok"] is False
        assert "required" in data["error"].lower()

    def test_run_invalid_email(self, authed_client):
        resp = authed_client.post("/operations/run", data={
            "action": "block",
            "tenant_id": "fake",
            "target_user": "not-an-email",
        })
        data = resp.get_json()
        assert data["ok"] is False
        assert "Invalid email" in data["error"]

    def test_run_tenant_not_found(self, authed_client):
        resp = authed_client.post("/operations/run", data={
            "action": "block",
            "tenant_id": "nonexistent",
            "target_user": "user@test.com",
        })
        data = resp.get_json()
        assert data["ok"] is False
        assert "not found" in data["error"].lower()

    def test_run_tenant_not_configured(self, authed_client):
        tid = db.create_tenant("Unconfigured", "test.onmicrosoft.com")
        resp = authed_client.post("/operations/run", data={
            "action": "block",
            "tenant_id": tid,
            "target_user": "user@test.com",
        })
        data = resp.get_json()
        assert data["ok"] is False
        assert "not configured" in data["error"].lower()

    def test_run_no_pfx_password(self, authed_client):
        tid = db.create_tenant("NoPFX", "test.onmicrosoft.com")
        db.update_tenant(tid, client_id="fake-client-id")
        resp = authed_client.post("/operations/run", data={
            "action": "block",
            "tenant_id": tid,
            "target_user": "user@test.com",
        })
        data = resp.get_json()
        assert data["ok"] is False
        assert "PFX" in data["error"]

    def test_run_set_forwarding_requires_forward_to(self, authed_client):
        resp = authed_client.post("/operations/run", data={
            "action": "set_forwarding",
            "tenant_id": "fake",
            "target_user": "user@test.com",
            "forward_to": "",
        })
        data = resp.get_json()
        assert data["ok"] is False
        assert "Forwarding" in data["error"]

    def test_run_invalid_forward_email(self, authed_client):
        resp = authed_client.post("/operations/run", data={
            "action": "set_forwarding",
            "tenant_id": "fake",
            "target_user": "user@test.com",
            "forward_to": "bad-email",
        })
        data = resp.get_json()
        assert data["ok"] is False
        assert "Invalid forwarding" in data["error"]

    @patch("routes.operations.start_job", return_value="test-job-1")
    @patch("routes.operations.get_tenant_pfx_password", return_value="testpass")
    @patch("routes.operations.tenant_pfx_path")
    def test_run_successful_block(self, mock_pfx, mock_pass, mock_start, authed_client):
        tid = db.create_tenant("TestCo", "testco.onmicrosoft.com")
        db.update_tenant(tid, client_id="fake-client-id")
        mock_pfx.return_value = Path("/fake/path.pfx")

        resp = authed_client.post("/operations/run", data={
            "action": "block",
            "tenant_id": tid,
            "target_user": "user@testco.com",
        })
        data = resp.get_json()
        assert data["ok"] is True
        assert data["job_id"] == "test-job-1"
        mock_start.assert_called_once()

    @patch("routes.operations.start_job", return_value="test-job-2")
    @patch("routes.operations.get_tenant_pfx_password", return_value="testpass")
    @patch("routes.operations.tenant_pfx_path")
    def test_run_successful_license_report(self, mock_pfx, mock_pass, mock_start, authed_client):
        """License report does not require a target_user."""
        tid = db.create_tenant("LicTest", "lic.onmicrosoft.com")
        db.update_tenant(tid, client_id="fake-client-id")
        mock_pfx.return_value = Path("/fake/path.pfx")

        resp = authed_client.post("/operations/run", data={
            "action": "licenses",
            "tenant_id": tid,
        })
        data = resp.get_json()
        assert data["ok"] is True
        assert data["job_id"] == "test-job-2"

    @patch("routes.operations.start_job", return_value="test-job-3")
    @patch("routes.operations.get_tenant_pfx_password", return_value="testpass")
    @patch("routes.operations.tenant_pfx_path")
    def test_run_successful_offboard(self, mock_pfx, mock_pass, mock_start, authed_client):
        tid = db.create_tenant("OffCo", "off.onmicrosoft.com")
        db.update_tenant(tid, client_id="fake-client-id")
        mock_pfx.return_value = Path("/fake/path.pfx")

        resp = authed_client.post("/operations/run", data={
            "action": "offboard",
            "tenant_id": tid,
            "target_user": "leaver@off.com",
            "ooo_message": "This person has left the company.",
            "forward_to": "manager@off.com",
            "skip_convert": "",
            "skip_licenses": "on",
        })
        data = resp.get_json()
        assert data["ok"] is True


class TestOperationsBulk:
    def test_bulk_requires_auth(self, client):
        resp = client.post("/operations/bulk")
        assert resp.status_code == 302

    def test_bulk_unknown_action(self, authed_client):
        resp = authed_client.post("/operations/bulk", data={
            "action": "bad_action",
            "tenant_ids": ["fake"],
        })
        data = resp.get_json()
        assert data["ok"] is False

    def test_bulk_no_tenants(self, authed_client):
        resp = authed_client.post("/operations/bulk", data={
            "action": "block",
            "target_user": "user@test.com",
        })
        data = resp.get_json()
        assert data["ok"] is False
        assert "No tenants" in data["error"]

    def test_bulk_missing_target(self, authed_client):
        resp = authed_client.post("/operations/bulk", data={
            "action": "block",
            "tenant_ids": ["fake"],
            "target_user": "",
        })
        data = resp.get_json()
        assert data["ok"] is False
        assert "required" in data["error"].lower()

    @patch("routes.operations.start_job", return_value="bulk-job-1")
    @patch("routes.operations.get_tenant_pfx_password", return_value="testpass")
    @patch("routes.operations.tenant_pfx_path")
    def test_bulk_successful(self, mock_pfx, mock_pass, mock_start, authed_client):
        tid1 = db.create_tenant("Tenant A", "a.onmicrosoft.com")
        tid2 = db.create_tenant("Tenant B", "b.onmicrosoft.com")
        db.update_tenant(tid1, client_id="client-a")
        db.update_tenant(tid2, client_id="client-b")
        mock_pfx.return_value = Path("/fake/path.pfx")

        resp = authed_client.post("/operations/bulk", data={
            "action": "revoke_sessions",
            "tenant_ids": [tid1, tid2],
            "target_user": "user@test.com",
        })
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["job_ids"]) == 2
        assert len(data["tenant_jobs"]) == 2
        assert "Tenant A" in data["tenant_jobs"][0]["tenant_name"]

    @patch("routes.operations.start_job", return_value="bulk-job-2")
    @patch("routes.operations.get_tenant_pfx_password", return_value="testpass")
    @patch("routes.operations.tenant_pfx_path")
    def test_bulk_skips_unconfigured(self, mock_pfx, mock_pass, mock_start, authed_client):
        tid1 = db.create_tenant("Configured", "c.onmicrosoft.com")
        tid2 = db.create_tenant("Not Configured", "nc.onmicrosoft.com")
        db.update_tenant(tid1, client_id="client-c")
        # tid2 has no client_id
        mock_pfx.return_value = Path("/fake/path.pfx")

        resp = authed_client.post("/operations/bulk", data={
            "action": "block",
            "tenant_ids": [tid1, tid2],
            "target_user": "user@test.com",
        })
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["job_ids"]) == 1  # Only configured tenant


class TestOperationMetadata:
    def test_all_operations_have_required_fields(self):
        from routes.operations import OPERATIONS
        required_keys = {"tier", "name", "icon", "color", "script", "desc", "fields"}
        for key, op in OPERATIONS.items():
            assert required_keys.issubset(op.keys()), f"Operation {key} missing keys"

    def test_all_tiers_present(self):
        from routes.operations import OPERATIONS
        tiers = {op["tier"] for op in OPERATIONS.values()}
        assert 1 in tiers
        assert 2 in tiers
        assert 3 in tiers

    def test_valid_script_types(self):
        from routes.operations import OPERATIONS
        valid = {"user", "mailbox", "report", "offboard"}
        for key, op in OPERATIONS.items():
            assert op["script"] in valid, f"Operation {key} has invalid script type"
