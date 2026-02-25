"""
tests/test_tenant_admin.py — Tests for tenant administration, conditional access,
GDAP management, tools, and backup features.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


# ---------------------------------------------------------------------------
# Database CRUD tests for new tables
# ---------------------------------------------------------------------------

class TestTenantVariables:
    def test_set_and_get_variable(self):
        tid = db.create_tenant("VarTenant", "var.com")
        db.set_tenant_variable(tid, "site_url", "https://example.com", "admin")
        variables = db.get_tenant_variables(tid)
        assert len(variables) == 1
        assert variables[0]["var_key"] == "site_url"
        assert variables[0]["var_value"] == "https://example.com"

    def test_update_existing_variable(self):
        tid = db.create_tenant("VarTenant2", "var2.com")
        db.set_tenant_variable(tid, "region", "US", "admin")
        db.set_tenant_variable(tid, "region", "EU", "admin")
        variables = db.get_tenant_variables(tid)
        assert len(variables) == 1
        assert variables[0]["var_value"] == "EU"

    def test_delete_variable(self):
        tid = db.create_tenant("VarTenant3", "var3.com")
        db.set_tenant_variable(tid, "key1", "val1", "admin")
        db.set_tenant_variable(tid, "key2", "val2", "admin")
        db.delete_tenant_variable(tid, "key1")
        variables = db.get_tenant_variables(tid)
        assert len(variables) == 1
        assert variables[0]["var_key"] == "key2"


class TestCachedTenantData:
    def test_save_and_get_cached_data(self):
        tid = db.create_tenant("CacheTenant", "cache.com")
        data = {"response": {"value": [{"name": "test"}]}}
        db.save_cached_data(tid, "domains", data)
        cached = db.get_cached_data(tid, "domains")
        assert cached is not None
        assert cached["data"]["response"]["value"][0]["name"] == "test"

    def test_overwrite_cached_data(self):
        tid = db.create_tenant("CacheTenant2", "cache2.com")
        db.save_cached_data(tid, "domains", {"v": 1})
        db.save_cached_data(tid, "domains", {"v": 2})
        cached = db.get_cached_data(tid, "domains")
        assert cached["data"]["v"] == 2

    def test_list_cached_data(self):
        tid = db.create_tenant("CacheTenant3", "cache3.com")
        db.save_cached_data(tid, "domains", {"d": 1})
        db.save_cached_data(tid, "licences", {"l": 1})
        items = db.list_cached_data(tid)
        assert len(items) == 2
        types = [i["data_type"] for i in items]
        assert "domains" in types
        assert "licences" in types


class TestGDAPRoleTemplates:
    def test_create_and_get_template(self):
        tmpl_id = db.create_gdap_role_template(
            "Standard MSP", "Standard role set",
            ["role-1", "role-2"], "admin"
        )
        tmpl = db.get_gdap_role_template(tmpl_id)
        assert tmpl is not None
        assert tmpl["name"] == "Standard MSP"
        assert tmpl["roles"] == ["role-1", "role-2"]

    def test_list_templates(self):
        db.create_gdap_role_template("T1", "", [], "admin")
        db.create_gdap_role_template("T2", "", [], "admin")
        templates = db.list_gdap_role_templates()
        assert len(templates) == 2

    def test_update_template(self):
        tmpl_id = db.create_gdap_role_template("Before", "desc", [], "admin")
        db.update_gdap_role_template(tmpl_id, name="After", roles=["r1"])
        tmpl = db.get_gdap_role_template(tmpl_id)
        assert tmpl["name"] == "After"
        assert tmpl["roles"] == ["r1"]

    def test_delete_template(self):
        tmpl_id = db.create_gdap_role_template("Del", "", [], "admin")
        db.delete_gdap_role_template(tmpl_id)
        assert db.get_gdap_role_template(tmpl_id) is None


class TestTenantBackups:
    def test_create_and_get_backup(self):
        tid = db.create_tenant("BackupTenant", "backup.com")
        bid = db.create_tenant_backup(
            tid, "full", ["conditionalAccess", "domains"], "admin"
        )
        backup = db.get_tenant_backup(bid)
        assert backup is not None
        assert backup["tenant_id"] == tid
        assert backup["backup_type"] == "full"
        assert backup["components"] == ["conditionalAccess", "domains"]
        assert backup["status"] == "pending"

    def test_update_backup(self):
        tid = db.create_tenant("BackupTenant2", "backup2.com")
        bid = db.create_tenant_backup(tid, "full", [], "admin")
        db.update_tenant_backup(bid, status="done", data={"config": "data"})
        backup = db.get_tenant_backup(bid)
        assert backup["status"] == "done"
        assert backup["data"]["config"] == "data"

    def test_list_backups(self):
        tid = db.create_tenant("BackupTenant3", "backup3.com")
        db.create_tenant_backup(tid, "full", [], "admin")
        db.create_tenant_backup(tid, "partial", [], "admin")
        backups = db.list_tenant_backups()
        assert len(backups) == 2

    def test_list_backups_by_tenant(self):
        tid1 = db.create_tenant("BT1", "bt1.com")
        tid2 = db.create_tenant("BT2", "bt2.com")
        db.create_tenant_backup(tid1, "full", [], "admin")
        db.create_tenant_backup(tid2, "full", [], "admin")
        backups = db.list_tenant_backups(tenant_id=tid1)
        assert len(backups) == 1

    def test_delete_backup(self):
        tid = db.create_tenant("BackupDel", "bdel.com")
        bid = db.create_tenant_backup(tid, "full", [], "admin")
        db.delete_tenant_backup(bid)
        assert db.get_tenant_backup(bid) is None


class TestCATemplates:
    def test_create_and_get_template(self):
        tmpl_id = db.create_ca_template(
            "Block Legacy", "Block legacy auth",
            {"state": "enabled"}, "security", "admin"
        )
        tmpl = db.get_ca_template(tmpl_id)
        assert tmpl is not None
        assert tmpl["name"] == "Block Legacy"
        assert tmpl["policy_json"]["state"] == "enabled"

    def test_list_templates(self):
        db.create_ca_template("T1", "", {}, "general", "admin")
        db.create_ca_template("T2", "", {}, "general", "admin")
        templates = db.list_ca_templates()
        assert len(templates) == 2

    def test_update_template(self):
        tmpl_id = db.create_ca_template("Old", "desc", {}, "general", "admin")
        db.update_ca_template(tmpl_id, name="New", category="security")
        tmpl = db.get_ca_template(tmpl_id)
        assert tmpl["name"] == "New"
        assert tmpl["category"] == "security"

    def test_delete_template(self):
        tmpl_id = db.create_ca_template("Del", "", {}, "general", "admin")
        db.delete_ca_template(tmpl_id)
        assert db.get_ca_template(tmpl_id) is None


# ---------------------------------------------------------------------------
# Route access control tests
# ---------------------------------------------------------------------------

class TestTenantAdminRoutes:
    def test_tenant_lookup_accessible(self, authed_client):
        resp = authed_client.get("/tenant-lookup")
        assert resp.status_code == 200
        assert b"Tenant Lookup" in resp.data

    def test_tenant_variables_page(self, authed_client):
        resp = authed_client.get("/tenant-variables")
        assert resp.status_code == 200
        assert b"Variables" in resp.data

    def test_tenant_domains_page(self, authed_client):
        tid = db.create_tenant("DomainTest", "dom.com")
        resp = authed_client.get(f"/tenants/{tid}/domains")
        assert resp.status_code == 200

    def test_tenant_auth_methods_page(self, authed_client):
        tid = db.create_tenant("AuthTest", "auth.com")
        resp = authed_client.get(f"/tenants/{tid}/auth-methods")
        assert resp.status_code == 200

    def test_tenant_partner_relationships_page(self, authed_client):
        tid = db.create_tenant("PartnerTest", "partner.com")
        resp = authed_client.get(f"/tenants/{tid}/partner-relationships")
        assert resp.status_code == 200

    def test_tenant_app_consent_page(self, authed_client):
        tid = db.create_tenant("ConsentTest", "consent.com")
        resp = authed_client.get(f"/tenants/{tid}/app-consent")
        assert resp.status_code == 200

    def test_tenant_applications_page(self, authed_client):
        tid = db.create_tenant("AppsTest", "apps.com")
        resp = authed_client.get(f"/tenants/{tid}/applications")
        assert resp.status_code == 200

    def test_tenant_directory_audits_page(self, authed_client):
        tid = db.create_tenant("AuditLogTest", "auditlog.com")
        resp = authed_client.get(f"/tenants/{tid}/directory-audits")
        assert resp.status_code == 200

    def test_tenant_licences_page(self, authed_client):
        tid = db.create_tenant("LicTest", "lic.com")
        resp = authed_client.get(f"/tenants/{tid}/licences")
        assert resp.status_code == 200

    def test_tenant_variables_set(self, authed_client):
        tid = db.create_tenant("VarSet", "varset.com")
        resp = authed_client.post(f"/tenants/{tid}/variables/set", data={
            "var_key": "mykey",
            "var_value": "myval",
        })
        data = resp.get_json()
        assert data["ok"] is True
        variables = db.get_tenant_variables(tid)
        assert len(variables) == 1
        assert variables[0]["var_key"] == "mykey"

    def test_tenant_variables_delete(self, authed_client):
        tid = db.create_tenant("VarDel", "vardel.com")
        db.set_tenant_variable(tid, "delme", "val", "admin")
        resp = authed_client.post(f"/tenants/{tid}/variables/delete", data={
            "var_key": "delme",
        })
        data = resp.get_json()
        assert data["ok"] is True
        variables = db.get_tenant_variables(tid)
        assert len(variables) == 0

    def test_cache_import(self, authed_client):
        tid = db.create_tenant("CacheImp", "cacheimp.com")
        resp = authed_client.post(f"/tenants/{tid}/cache-import", data={
            "data_type": "domains",
            "data": json.dumps({"response": {"value": []}}),
        })
        data = resp.get_json()
        assert data["ok"] is True
        cached = db.get_cached_data(tid, "domains")
        assert cached is not None

    def test_nonexistent_tenant_domains_404(self, authed_client):
        resp = authed_client.get("/tenants/nonexistent/domains")
        assert resp.status_code == 404

    def test_unauthenticated_access_redirects(self, client):
        resp = client.get("/tenant-lookup")
        assert resp.status_code == 302


class TestConditionalAccessRoutes:
    def test_ca_policies_page(self, authed_client):
        resp = authed_client.get("/conditional-access")
        assert resp.status_code == 200
        assert b"Conditional Access" in resp.data

    def test_tenant_ca_policies_page(self, authed_client):
        tid = db.create_tenant("CATest", "ca.com")
        resp = authed_client.get(f"/tenants/{tid}/ca-policies")
        assert resp.status_code == 200

    def test_tenant_named_locations_page(self, authed_client):
        tid = db.create_tenant("NLTest", "nl.com")
        resp = authed_client.get(f"/tenants/{tid}/named-locations")
        assert resp.status_code == 200

    def test_ca_templates_page(self, authed_client):
        resp = authed_client.get("/ca-templates")
        assert resp.status_code == 200
        assert b"Templates" in resp.data

    def test_ca_template_create(self, authed_client):
        resp = authed_client.post("/ca-templates/create", data={
            "name": "Block Legacy Auth",
            "description": "Blocks legacy authentication",
            "policy_json": '{"state": "enabled"}',
            "category": "security",
        })
        data = resp.get_json()
        assert data["ok"] is True
        templates = db.list_ca_templates()
        assert len(templates) == 1
        assert templates[0]["name"] == "Block Legacy Auth"

    def test_ca_template_update(self, authed_client):
        tmpl_id = db.create_ca_template("Old", "old desc", {}, "general", "admin")
        resp = authed_client.post(f"/ca-templates/{tmpl_id}/update", data={
            "name": "New Name",
            "description": "new desc",
            "policy_json": '{"state": "disabled"}',
            "category": "security",
        })
        data = resp.get_json()
        assert data["ok"] is True
        tmpl = db.get_ca_template(tmpl_id)
        assert tmpl["name"] == "New Name"

    def test_ca_template_delete(self, authed_client):
        tmpl_id = db.create_ca_template("Del", "", {}, "general", "admin")
        resp = authed_client.post(f"/ca-templates/{tmpl_id}/delete")
        data = resp.get_json()
        assert data["ok"] is True
        assert db.get_ca_template(tmpl_id) is None

    def test_tenant_vacation_mode_page(self, authed_client):
        tid = db.create_tenant("VacTest", "vac.com")
        resp = authed_client.get(f"/tenants/{tid}/vacation-mode")
        assert resp.status_code == 200

    def test_unauthenticated_ca_redirects(self, client):
        resp = client.get("/conditional-access")
        assert resp.status_code == 302


class TestGDAPRoutes:
    def test_gdap_relationships_page(self, authed_client):
        resp = authed_client.get("/gdap")
        assert resp.status_code == 200
        assert b"GDAP" in resp.data

    def test_gdap_role_templates_page(self, authed_client):
        resp = authed_client.get("/gdap/role-templates")
        assert resp.status_code == 200

    def test_gdap_role_template_create(self, authed_client):
        # Use a well-known GDAP role ID from the route's GDAP_DIRECTORY_ROLES
        resp = authed_client.post("/gdap/role-templates/create", data={
            "name": "Basic Roles",
            "description": "Basic set",
            "role_ids": "62e90394-69f5-4237-9190-012177145e10",
        })
        data = resp.get_json()
        assert data["ok"] is True
        templates = db.list_gdap_role_templates()
        assert len(templates) == 1

    def test_gdap_role_template_update(self, authed_client):
        tmpl_id = db.create_gdap_role_template("Old", "", [], "admin")
        resp = authed_client.post(f"/gdap/role-templates/{tmpl_id}/update", data={
            "name": "New",
            "description": "updated",
            "roles": "role-a",
        })
        data = resp.get_json()
        assert data["ok"] is True
        tmpl = db.get_gdap_role_template(tmpl_id)
        assert tmpl["name"] == "New"

    def test_gdap_role_template_delete(self, authed_client):
        tmpl_id = db.create_gdap_role_template("Del", "", [], "admin")
        resp = authed_client.post(f"/gdap/role-templates/{tmpl_id}/delete")
        data = resp.get_json()
        assert data["ok"] is True
        assert db.get_gdap_role_template(tmpl_id) is None

    def test_gdap_onboarding_page(self, authed_client):
        resp = authed_client.get("/gdap/onboarding")
        assert resp.status_code == 200

    def test_gdap_offboarding_page(self, authed_client):
        resp = authed_client.get("/gdap/offboarding")
        assert resp.status_code == 200

    def test_tenant_gdap_relationships_page(self, authed_client):
        tid = db.create_tenant("GDAPTenant", "gdap.com")
        resp = authed_client.get(f"/tenants/{tid}/gdap-relationships")
        assert resp.status_code == 200

    def test_unauthenticated_gdap_redirects(self, client):
        resp = client.get("/gdap")
        assert resp.status_code == 302


class TestToolsRoutes:
    def test_graph_explorer_page(self, authed_client):
        resp = authed_client.get("/graph-explorer")
        assert resp.status_code == 200
        assert b"Graph Explorer" in resp.data

    def test_geoip_lookup_page(self, authed_client):
        resp = authed_client.get("/geoip-lookup")
        assert resp.status_code == 200
        assert b"GeoIP" in resp.data

    def test_domain_health_page(self, authed_client):
        resp = authed_client.get("/domain-health")
        assert resp.status_code == 200
        assert b"Domain Health" in resp.data

    def test_backup_page(self, authed_client):
        resp = authed_client.get("/backup")
        assert resp.status_code == 200
        assert b"Backup" in resp.data

    def test_backup_detail(self, authed_client):
        tid = db.create_tenant("BackupDetailTest", "bdt.com")
        bid = db.create_tenant_backup(tid, "full", ["domains"], "admin")
        db.update_tenant_backup(bid, status="done", data={"test": True})
        resp = authed_client.get(f"/backup/{bid}")
        assert resp.status_code == 200

    def test_backup_detail_404(self, authed_client):
        resp = authed_client.get("/backup/nonexistent")
        assert resp.status_code == 404

    def test_backup_create_returns_json(self, authed_client):
        tid = db.create_tenant("BackupCreate", "bc.com",
                               tenant_id="test-tid")
        # Backup creation requires a configured tenant; we verify JSON response
        resp = authed_client.post("/backup/create", data={
            "tenant_id": tid,
            "components": "domains",
        })
        data = resp.get_json()
        assert "ok" in data

    def test_backup_delete(self, authed_client):
        tid = db.create_tenant("BackupDel2", "bdel2.com")
        bid = db.create_tenant_backup(tid, "full", [], "admin")
        resp = authed_client.post(f"/backup/{bid}/delete")
        data = resp.get_json()
        assert data["ok"] is True
        assert db.get_tenant_backup(bid) is None

    def test_unauthenticated_tools_redirects(self, client):
        resp = client.get("/graph-explorer")
        assert resp.status_code == 302
        resp = client.get("/domain-health")
        assert resp.status_code == 302
        resp = client.get("/backup")
        assert resp.status_code == 302


class TestTenantLookupAPI:
    def test_tenant_lookup_query_missing_domain(self, authed_client):
        resp = authed_client.post("/tenant-lookup/query", data={})
        data = resp.get_json()
        assert data["ok"] is False
        assert "domain" in data["error"].lower()


class TestSchemaV4:
    def test_new_tables_exist(self):
        """Verify that all v4 migration tables were created."""
        with db.get_db() as conn:
            tables = [r["name"] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
            assert "tenant_custom_variables" in tables
            assert "cached_tenant_data" in tables
            assert "gdap_role_templates" in tables
            assert "tenant_backups" in tables
            assert "ca_policy_templates" in tables
