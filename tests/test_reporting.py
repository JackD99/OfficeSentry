"""
tests/test_reporting.py — Tests for Phase 3: Reporting features.

Covers: PDF report generation, report comparison, client dashboard,
SMTP configuration, branding, and tenant_branding database operations.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import database as db


# ---------------------------------------------------------------------------
# Database: tenant_branding table and CRUD
# ---------------------------------------------------------------------------

class TestTenantBranding:
    def test_get_branding_returns_none_when_empty(self):
        db.create_tenant("Test Corp", "test.onmicrosoft.com")
        tenants = db.list_tenants()
        tid = tenants[0]["id"]
        assert db.get_tenant_branding(tid) is None

    def test_upsert_and_get_branding(self):
        db.create_tenant("Test Corp", "test.onmicrosoft.com")
        tid = db.list_tenants()[0]["id"]
        db.upsert_tenant_branding(tid, company_name="Acme MSP", accent_color="#ff0000")
        branding = db.get_tenant_branding(tid)
        assert branding is not None
        assert branding["company_name"] == "Acme MSP"
        assert branding["accent_color"] == "#ff0000"

    def test_upsert_updates_existing(self):
        db.create_tenant("Test Corp", "test.onmicrosoft.com")
        tid = db.list_tenants()[0]["id"]
        db.upsert_tenant_branding(tid, company_name="Old Name")
        db.upsert_tenant_branding(tid, company_name="New Name")
        branding = db.get_tenant_branding(tid)
        assert branding["company_name"] == "New Name"

    def test_upsert_ignores_invalid_fields(self):
        db.create_tenant("Test Corp", "test.onmicrosoft.com")
        tid = db.list_tenants()[0]["id"]
        db.upsert_tenant_branding(tid, evil_field="hack")
        branding = db.get_tenant_branding(tid)
        assert branding is None  # No valid fields were set

    def test_get_default_branding(self):
        defaults = db.get_default_branding()
        assert "company_name" in defaults
        assert "accent_color" in defaults


class TestSchemaMigration:
    def test_schema_version_is_9(self):
        assert db.SCHEMA_VERSION == 9

    def test_report_schedules_has_recipients_column(self):
        with db.get_db() as conn:
            cursor = conn.execute("PRAGMA table_info(report_schedules)")
            columns = {row[1] for row in cursor.fetchall()}
        assert "recipients" in columns
        assert "email_on_completion" in columns

    def test_tenant_branding_table_exists(self):
        with db.get_db() as conn:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='tenant_branding'"
            )
            assert cursor.fetchone() is not None


# ---------------------------------------------------------------------------
# Report Engine
# ---------------------------------------------------------------------------

class TestReportEngine:
    def test_report_types_defined(self):
        from report_engine import REPORT_TYPES
        assert "executive_summary" in REPORT_TYPES
        assert "security_posture" in REPORT_TYPES
        assert "bpa_compliance" in REPORT_TYPES
        assert "identity_risk" in REPORT_TYPES
        assert "license_optimization" in REPORT_TYPES
        assert "breach_investigation" in REPORT_TYPES

    def test_weasyprint_availability_flag_exists(self):
        from report_engine import WEASYPRINT_AVAILABLE
        assert isinstance(WEASYPRINT_AVAILABLE, bool)

    def test_get_report_data_returns_none_for_missing_tenant(self):
        from report_engine import get_report_data
        assert get_report_data("executive_summary", "nonexistent") is None

    def test_get_report_data_executive_summary(self):
        from report_engine import get_report_data
        db.create_tenant("Test", "test.onmicrosoft.com")
        tid = db.list_tenants()[0]["id"]
        data = get_report_data("executive_summary", tid)
        assert data is not None
        assert data["tenant"]["name"] == "Test"
        assert "bpa_summary" in data
        assert "generated_at" in data

    def test_get_report_data_bpa_compliance(self):
        from report_engine import get_report_data
        db.create_tenant("Test", "test.onmicrosoft.com")
        tid = db.list_tenants()[0]["id"]
        # Add some BPA results
        db.save_bpa_result(tid, "check1", "MFA Check", status="pass",
                           category="Identity", impact="High")
        db.save_bpa_result(tid, "check2", "Admin MFA", status="fail",
                           category="Identity", impact="Critical")
        data = get_report_data("bpa_compliance", tid)
        assert data is not None
        assert "categories" in data
        assert "Identity" in data["categories"]
        assert len(data["categories"]["Identity"]) == 2

    def test_get_report_data_breach_investigation(self):
        from report_engine import get_report_data
        db.create_tenant("Test", "test.onmicrosoft.com")
        tid = db.list_tenants()[0]["id"]
        data = get_report_data("breach_investigation", tid)
        assert data is not None
        assert "cases" in data

    def test_generate_pdf_raises_without_weasyprint(self):
        from report_engine import generate_pdf, WEASYPRINT_AVAILABLE
        if not WEASYPRINT_AVAILABLE:
            db.create_tenant("Test", "test.onmicrosoft.com")
            tid = db.list_tenants()[0]["id"]
            with pytest.raises(RuntimeError, match="WeasyPrint"):
                generate_pdf("executive_summary", tid)

    def test_generate_pdf_bad_report_type(self):
        from report_engine import generate_pdf, WEASYPRINT_AVAILABLE
        if WEASYPRINT_AVAILABLE:
            with pytest.raises(ValueError, match="Unknown report type"):
                generate_pdf("nonexistent_type", "fake-id")

    def test_generate_pdf_missing_tenant(self):
        from report_engine import generate_pdf, WEASYPRINT_AVAILABLE
        if WEASYPRINT_AVAILABLE:
            with pytest.raises(ValueError, match="Tenant not found"):
                generate_pdf("executive_summary", "nonexistent-tenant")


# ---------------------------------------------------------------------------
# PDF Routes
# ---------------------------------------------------------------------------

class TestPDFRoutes:
    def test_pdf_types_endpoint(self, authed_client):
        resp = authed_client.get("/reports/pdf-types")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "types" in data
        assert "executive_summary" in data["types"]

    def test_pdf_download_bad_type(self, authed_client):
        db.create_tenant("Test", "test.onmicrosoft.com")
        tid = db.list_tenants()[0]["id"]
        resp = authed_client.get(f"/reports/{tid}/pdf/invalid_type")
        assert resp.status_code in (400, 501)

    def test_pdf_download_missing_tenant(self, authed_client):
        resp = authed_client.get("/reports/nonexistent/pdf/executive_summary")
        assert resp.status_code in (404, 501)

    def test_pdf_download_requires_auth(self, client):
        resp = client.get("/reports/fake/pdf/executive_summary")
        assert resp.status_code == 302  # redirects to login


# ---------------------------------------------------------------------------
# Report Comparison
# ---------------------------------------------------------------------------

class TestReportComparison:
    def test_compare_page_loads(self, authed_client):
        resp = authed_client.get("/reports/compare")
        assert resp.status_code == 200

    def test_compare_requires_auth(self, client):
        resp = client.get("/reports/compare")
        assert resp.status_code == 302

    def test_compare_with_csv_files(self, authed_client, tmp_path):
        """Compare two CSV files and check that differences are detected."""
        # Create tenant and output dir
        db.create_tenant("CompTest", "comp.onmicrosoft.com")
        tid = db.list_tenants()[0]["id"]

        from helpers import tenant_output_dir
        out_dir = tenant_output_dir(tid)
        out_dir.mkdir(parents=True, exist_ok=True)

        # Write two CSV files
        csv_a = out_dir / "report_a.csv"
        csv_b = out_dir / "report_b.csv"
        csv_a.write_text("Name,Status\nAlice,Active\nBob,Active\n")
        csv_b.write_text("Name,Status\nAlice,Active\nCharlie,Active\n")

        resp = authed_client.get(f"/reports/compare?a={csv_a}&b={csv_b}")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Added" in html or "Removed" in html

    def test_compare_path_traversal_blocked(self, authed_client):
        resp = authed_client.get(
            "/reports/compare?a=../../etc/passwd&b=../../etc/shadow"
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Client Dashboard
# ---------------------------------------------------------------------------

class TestClientDashboard:
    def test_client_dashboard_requires_auth(self, client):
        resp = client.get("/client-dashboard")
        assert resp.status_code == 302

    def test_client_dashboard_with_tenant_id(self, authed_client):
        db.create_tenant("ClientTest", "client.onmicrosoft.com")
        tid = db.list_tenants()[0]["id"]
        resp = authed_client.get(f"/client-dashboard?tenant_id={tid}")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "ClientTest" in html

    def test_client_dashboard_missing_tenant(self, authed_client):
        resp = authed_client.get("/client-dashboard")
        assert resp.status_code == 404

    def test_client_dashboard_nonexistent_tenant(self, authed_client):
        resp = authed_client.get("/client-dashboard?tenant_id=nonexistent")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Branding Routes
# ---------------------------------------------------------------------------

class TestBrandingRoutes:
    def test_get_branding(self, authed_client):
        db.create_tenant("BrandTest", "brand.onmicrosoft.com")
        tid = db.list_tenants()[0]["id"]
        resp = authed_client.get(f"/reports/branding/{tid}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "branding" in data

    def test_set_branding(self, authed_client):
        db.create_tenant("BrandTest", "brand.onmicrosoft.com")
        tid = db.list_tenants()[0]["id"]
        resp = authed_client.post(f"/reports/branding/{tid}", data={
            "company_name": "New MSP",
            "accent_color": "#123456",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

        # Verify it was saved
        branding = db.get_tenant_branding(tid)
        assert branding["company_name"] == "New MSP"
        assert branding["accent_color"] == "#123456"

    def test_set_default_branding(self, authed_client):
        resp = authed_client.post("/reports/branding/default", data={
            "company_name": "Global MSP",
            "accent_color": "#abcdef",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_branding_requires_auth(self, client):
        resp = client.get("/reports/branding/fake")
        assert resp.status_code == 302


# ---------------------------------------------------------------------------
# SMTP Configuration Routes
# ---------------------------------------------------------------------------

class TestSMTPRoutes:
    def test_get_smtp_config(self, authed_client):
        resp = authed_client.get("/reports/smtp-config")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "smtp" in data

    def test_set_smtp_config(self, authed_client):
        resp = authed_client.post("/reports/smtp-config", data={
            "smtp_host": "smtp.example.com",
            "smtp_port": "587",
            "smtp_username": "user@example.com",
            "smtp_password": "secret123",
            "smtp_sender": "noreply@example.com",
            "smtp_use_tls": "true",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True

    def test_smtp_test_requires_recipient(self, authed_client):
        resp = authed_client.post("/reports/smtp-test", data={})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is False
        assert "Recipient" in data["error"]

    def test_smtp_test_without_config(self, authed_client):
        resp = authed_client.post("/reports/smtp-test", data={
            "recipient": "test@example.com",
        })
        assert resp.status_code == 200
        data = resp.get_json()
        # Either SMTP not configured or connection failed
        assert data["ok"] is False

    def test_smtp_config_requires_auth(self, client):
        resp = client.get("/reports/smtp-config")
        assert resp.status_code == 302


# ---------------------------------------------------------------------------
# Permissions: Client Role
# ---------------------------------------------------------------------------

class TestClientRole:
    def test_client_in_role_hierarchy(self):
        from permissions import ROLE_HIERARCHY
        assert "client" in ROLE_HIERARCHY
        assert ROLE_HIERARCHY["client"] == 0

    def test_client_in_role_labels(self):
        from permissions import ROLE_LABELS
        assert "client" in ROLE_LABELS
        assert ROLE_LABELS["client"] == "Client"

    def test_is_client_user(self):
        from permissions import is_client_user
        assert is_client_user({"role": "client"}) is True
        assert is_client_user({"role": "viewer"}) is False
        assert is_client_user({"role": "admin"}) is False

    def test_client_cannot_run_operations(self):
        from permissions import can_run_operation
        user = {"role": "client"}
        assert can_run_operation(user, 1) is False
        assert can_run_operation(user, 2) is False
        assert can_run_operation(user, 3) is False

    def test_client_cannot_run_audits(self):
        from permissions import can_run_audit
        user = {"role": "client"}
        assert can_run_audit(user) is False

    def test_has_role_client_below_viewer(self):
        from permissions import has_role
        user = {"role": "client"}
        assert has_role(user, "client") is True
        assert has_role(user, "viewer") is False
        assert has_role(user, "operator") is False

    def test_get_client_tenant_id(self):
        from permissions import get_client_tenant_id
        # Non-client users return None
        assert get_client_tenant_id({"role": "admin"}) is None
        # Client with no groups returns None
        assert get_client_tenant_id({"role": "client", "allowed_tenant_groups": "[]"}) is None


# ---------------------------------------------------------------------------
# Existing reports routes still work
# ---------------------------------------------------------------------------

class TestExistingReportRoutes:
    def test_reports_page_loads(self, authed_client):
        resp = authed_client.get("/reports")
        assert resp.status_code == 200

    def test_schedules_page_loads(self, authed_client):
        resp = authed_client.get("/reports/schedules")
        assert resp.status_code == 200

    def test_audit_trail_loads(self, authed_client):
        resp = authed_client.get("/audit-log")
        assert resp.status_code == 200

    def test_schedule_create_empty(self, authed_client):
        resp = authed_client.post("/reports/schedules/create", data={})
        data = resp.get_json()
        assert data["ok"] is False

    def test_saved_view_empty(self, authed_client):
        resp = authed_client.post("/reports/views", data={})
        data = resp.get_json()
        assert data["ok"] is False
