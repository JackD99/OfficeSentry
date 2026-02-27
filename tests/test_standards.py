"""
tests/test_standards.py — Tests for new CIPP-inspired features:
tenant groups, standards templates, BPA, alerts, and secure scores.
"""

import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


class TestTenantGroups:
    def test_create_and_get_group(self):
        gid = db.create_tenant_group("Healthcare", description="HIPAA clients", color="success")
        group = db.get_tenant_group(gid)
        assert group is not None
        assert group["name"] == "Healthcare"
        assert group["description"] == "HIPAA clients"
        assert group["color"] == "success"

    def test_list_groups(self):
        db.create_tenant_group("Alpha Group")
        db.create_tenant_group("Beta Group")
        groups = db.list_tenant_groups()
        assert len(groups) == 2
        names = [g["name"] for g in groups]
        assert "Alpha Group" in names
        assert "Beta Group" in names

    def test_group_member_count(self):
        gid = db.create_tenant_group("Test Group")
        tid1 = db.create_tenant("T1", "t1.com")
        tid2 = db.create_tenant("T2", "t2.com")
        db.add_tenant_to_group(gid, tid1)
        db.add_tenant_to_group(gid, tid2)
        groups = db.list_tenant_groups()
        assert groups[0]["member_count"] == 2

    def test_update_group(self):
        gid = db.create_tenant_group("Old Name")
        db.update_tenant_group(gid, name="New Name", color="danger")
        group = db.get_tenant_group(gid)
        assert group["name"] == "New Name"
        assert group["color"] == "danger"

    def test_delete_group(self):
        gid = db.create_tenant_group("Delete Me")
        assert db.delete_tenant_group(gid) is True
        assert db.get_tenant_group(gid) is None

    def test_add_remove_tenant(self):
        gid = db.create_tenant_group("Group")
        tid = db.create_tenant("Tenant", "t.com")
        db.add_tenant_to_group(gid, tid)
        members = db.get_group_tenants(gid)
        assert len(members) == 1
        assert members[0]["name"] == "Tenant"

        db.remove_tenant_from_group(gid, tid)
        members = db.get_group_tenants(gid)
        assert len(members) == 0

    def test_set_group_members(self):
        gid = db.create_tenant_group("Group")
        t1 = db.create_tenant("A", "a.com")
        t2 = db.create_tenant("B", "b.com")
        t3 = db.create_tenant("C", "c.com")

        db.set_group_members(gid, [t1, t2])
        assert len(db.get_group_tenants(gid)) == 2

        db.set_group_members(gid, [t3])
        members = db.get_group_tenants(gid)
        assert len(members) == 1
        assert members[0]["name"] == "C"

    def test_get_tenant_groups(self):
        g1 = db.create_tenant_group("G1")
        g2 = db.create_tenant_group("G2")
        tid = db.create_tenant("T", "t.com")
        db.add_tenant_to_group(g1, tid)
        db.add_tenant_to_group(g2, tid)
        groups = db.get_tenant_groups_for_tenant(tid)
        assert len(groups) == 2

    def test_duplicate_add_ignored(self):
        gid = db.create_tenant_group("G")
        tid = db.create_tenant("T", "t.com")
        db.add_tenant_to_group(gid, tid)
        db.add_tenant_to_group(gid, tid)  # Should not raise
        assert len(db.get_group_tenants(gid)) == 1


class TestStandardsTemplates:
    def test_create_and_get_template(self):
        tid = db.create_standards_template(
            "Security Baseline", description="Core checks",
            checks=["unified_audit_log", "security_defaults"],
            created_by="admin"
        )
        tmpl = db.get_standards_template(tid)
        assert tmpl is not None
        assert tmpl["name"] == "Security Baseline"
        assert len(tmpl["checks"]) == 2
        assert "unified_audit_log" in tmpl["checks"]

    def test_list_templates(self):
        db.create_standards_template("A", checks=["a"], created_by="admin")
        db.create_standards_template("B", checks=["b"], created_by="admin")
        templates = db.list_standards_templates()
        assert len(templates) == 2

    def test_update_template(self):
        tid = db.create_standards_template("Old", checks=["a"], created_by="admin")
        db.update_standards_template(tid, name="New", checks=["a", "b", "c"])
        tmpl = db.get_standards_template(tid)
        assert tmpl["name"] == "New"
        assert len(tmpl["checks"]) == 3

    def test_delete_template(self):
        tid = db.create_standards_template("X", checks=[], created_by="admin")
        assert db.delete_standards_template(tid) is True
        assert db.get_standards_template(tid) is None


class TestTenantStandards:
    def test_assign_and_get(self):
        tenant_id = db.create_tenant("T", "t.com")
        tmpl_id = db.create_standards_template("S", checks=["a"], created_by="admin")
        db.assign_standard_to_tenant(tenant_id, tmpl_id, action="report", applied_by="admin")
        standards = db.get_tenant_standards(tenant_id)
        assert len(standards) == 1
        assert standards[0]["template_name"] == "S"
        assert standards[0]["action"] == "report"

    def test_remove_assignment(self):
        tenant_id = db.create_tenant("T", "t.com")
        tmpl_id = db.create_standards_template("S", checks=["a"], created_by="admin")
        db.assign_standard_to_tenant(tenant_id, tmpl_id, applied_by="admin")
        db.remove_tenant_standard(tenant_id, tmpl_id)
        assert len(db.get_tenant_standards(tenant_id)) == 0


class TestBPAResults:
    def test_save_and_get_results(self):
        tid = db.create_tenant("T", "t.com")
        db.save_bpa_result(tid, "audit_log", "Unified Audit Log",
                           status="pass", category="Security",
                           detail="Enabled", impact="Critical")
        results = db.get_bpa_results(tenant_id=tid)
        assert len(results) == 1
        assert results[0]["check_key"] == "audit_log"
        assert results[0]["status"] == "pass"

    def test_save_replaces_old(self):
        tid = db.create_tenant("T", "t.com")
        db.save_bpa_result(tid, "mfa", "MFA", status="fail", category="Identity")
        db.save_bpa_result(tid, "mfa", "MFA", status="pass", category="Identity")
        results = db.get_bpa_results(tenant_id=tid)
        assert len(results) == 1
        assert results[0]["status"] == "pass"

    def test_bpa_summary(self):
        tid = db.create_tenant("T", "t.com")
        db.save_bpa_result(tid, "a", "A", status="pass", category="S")
        db.save_bpa_result(tid, "b", "B", status="fail", category="S")
        db.save_bpa_result(tid, "c", "C", status="warn", category="S")
        summary = db.get_bpa_summary()
        assert len(summary) == 1
        assert summary[0]["pass_count"] == 1
        assert summary[0]["fail_count"] == 1
        assert summary[0]["warn_count"] == 1

    def test_clear_results(self):
        tid = db.create_tenant("T", "t.com")
        db.save_bpa_result(tid, "a", "A", status="pass", category="S")
        db.clear_bpa_results(tid)
        assert len(db.get_bpa_results(tenant_id=tid)) == 0


class TestAlertRules:
    def test_create_and_get_rule(self):
        rid = db.create_alert_rule(
            "Test Rule", alert_type="audit_log",
            conditions={"type": "audit_log", "action_pattern": "breach_"},
            actions=[{"type": "webhook", "url": "https://example.com"}],
            created_by="admin"
        )
        rule = db.get_alert_rule(rid)
        assert rule is not None
        assert rule["name"] == "Test Rule"
        assert rule["conditions"]["action_pattern"] == "breach_"
        assert len(rule["actions"]) == 1

    def test_list_rules(self):
        db.create_alert_rule("R1", created_by="admin")
        db.create_alert_rule("R2", created_by="admin")
        rules = db.list_alert_rules()
        assert len(rules) == 2

    def test_update_rule(self):
        rid = db.create_alert_rule("Old", created_by="admin")
        db.update_alert_rule(rid, name="New", enabled=0)
        rule = db.get_alert_rule(rid)
        assert rule["name"] == "New"
        assert rule["enabled"] == 0

    def test_delete_rule(self):
        rid = db.create_alert_rule("X", created_by="admin")
        assert db.delete_alert_rule(rid) is True
        assert db.get_alert_rule(rid) is None


class TestAlertHistory:
    def test_create_and_list_alerts(self):
        rid = db.create_alert_rule("Rule", created_by="admin")
        db.create_alert(rid, "Test Alert", detail="Something happened",
                        severity="high", tenant_name="Contoso")
        alerts = db.list_alerts()
        assert len(alerts) == 1
        assert alerts[0]["title"] == "Test Alert"
        assert alerts[0]["severity"] == "high"

    def test_update_alert_status(self):
        rid = db.create_alert_rule("Rule", created_by="admin")
        aid = db.create_alert(rid, "Alert")
        db.update_alert_status(aid, "acknowledged")
        alerts = db.list_alerts()
        assert alerts[0]["status"] == "acknowledged"

    def test_resolve_alert(self):
        rid = db.create_alert_rule("Rule", created_by="admin")
        aid = db.create_alert(rid, "Alert")
        db.update_alert_status(aid, "resolved")
        alerts = db.list_alerts()
        assert alerts[0]["status"] == "resolved"
        assert alerts[0]["resolved_at"] is not None

    def test_alert_counts(self):
        rid = db.create_alert_rule("Rule", created_by="admin")
        db.create_alert(rid, "A1", severity="high")
        db.create_alert(rid, "A2", severity="critical")
        aid3 = db.create_alert(rid, "A3", severity="low")
        db.update_alert_status(aid3, "resolved")
        counts = db.get_alert_counts()
        assert counts["total_active"] == 2
        assert counts["by_severity"]["high"] == 1
        assert counts["by_severity"]["critical"] == 1
        assert counts["by_status"]["resolved"] == 1

    def test_filter_alerts_by_status(self):
        rid = db.create_alert_rule("Rule", created_by="admin")
        db.create_alert(rid, "New")
        aid2 = db.create_alert(rid, "Old")
        db.update_alert_status(aid2, "resolved")
        active = db.list_alerts(status="new")
        assert len(active) == 1


class TestSecureScores:
    def test_save_and_get_score(self):
        tid = db.create_tenant("T", "t.com")
        db.save_secure_score(tid, 45.5, 80.0, average_comparative=55.0,
                             details={"services": ["AAD", "EXO"]})
        score = db.get_latest_secure_score(tid)
        assert score is not None
        assert score["current_score"] == 45.5
        assert score["max_score"] == 80.0
        assert score["average_comparative_score"] == 55.0
        assert score["details"]["services"] == ["AAD", "EXO"]

    def test_get_latest_score(self):
        tid = db.create_tenant("T", "t.com")
        db.save_secure_score(tid, 40.0, 80.0)
        db.save_secure_score(tid, 50.0, 80.0)
        score = db.get_latest_secure_score(tid)
        assert score["current_score"] == 50.0

    def test_get_all_latest(self):
        t1 = db.create_tenant("A", "a.com")
        t2 = db.create_tenant("B", "b.com")
        db.save_secure_score(t1, 40.0, 80.0)
        db.save_secure_score(t2, 60.0, 80.0)
        scores = db.get_all_latest_secure_scores()
        assert len(scores) == 2

    def test_no_score_returns_none(self):
        tid = db.create_tenant("T", "t.com")
        assert db.get_latest_secure_score(tid) is None


class TestStandardsRoutes:
    """Test route accessibility for new features."""

    def test_bpa_page_requires_auth(self, client):
        resp = client.get("/bpa")
        assert resp.status_code == 302

    def test_bpa_page_accessible(self, authed_client):
        resp = authed_client.get("/bpa")
        assert resp.status_code == 200

    def test_standards_page_accessible(self, authed_client):
        resp = authed_client.get("/standards")
        assert resp.status_code == 200

    def test_alerts_page_accessible(self, authed_client):
        resp = authed_client.get("/alerts")
        assert resp.status_code == 200

    def test_secure_scores_page_accessible(self, authed_client):
        resp = authed_client.get("/secure-scores")
        assert resp.status_code == 200

    def test_groups_page_accessible(self, authed_client):
        resp = authed_client.get("/groups")
        assert resp.status_code == 200

    def test_create_group_via_api(self, authed_client):
        resp = authed_client.post("/groups/new", data={
            "name": "Test Group",
            "description": "Test",
            "color": "primary",
        })
        data = resp.get_json()
        assert data["ok"] is True
        assert "group_id" in data

    def test_create_group_empty_name(self, authed_client):
        resp = authed_client.post("/groups/new", data={"name": ""})
        data = resp.get_json()
        assert data["ok"] is False

    def test_create_template_via_api(self, authed_client):
        resp = authed_client.post("/standards/templates/new", data={
            "name": "My Baseline",
            "description": "Core checks",
            "checks": ["unified_audit_log", "security_defaults"],
        })
        data = resp.get_json()
        assert data["ok"] is True
        assert "template_id" in data

    def test_create_template_no_checks(self, authed_client):
        resp = authed_client.post("/standards/templates/new", data={
            "name": "Empty",
        })
        data = resp.get_json()
        assert data["ok"] is False

    def test_create_alert_rule_via_api(self, authed_client):
        resp = authed_client.post("/alerts/rules/new", data={
            "name": "Breach Alert",
            "alert_type": "audit_log",
            "action_pattern": "breach_",
            "severity": "high",
            "webhook_url": "https://hooks.example.com/test",
        })
        data = resp.get_json()
        assert data["ok"] is True
        assert "rule_id" in data

    def test_alert_counts_api(self, authed_client):
        resp = authed_client.get("/api/alerts/counts")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "total_active" in data

    def test_apply_standards(self, authed_client):
        # Create a template and tenant first
        tid = db.create_tenant("T", "t.com")
        db.update_tenant(tid, client_id="test-client-id")
        tmpl_id = db.create_standards_template("S", checks=["a"], created_by="admin")
        resp = authed_client.post("/standards/apply", data={
            "template_id": tmpl_id,
            "tenant_ids": [tid],
            "action": "report",
        })
        data = resp.get_json()
        assert data["ok"] is True

    def test_bpa_scan_no_tenants(self, authed_client):
        resp = authed_client.post("/bpa/scan", data={})
        data = resp.get_json()
        assert data["ok"] is False


class TestSchemaVersion:
    def test_schema_version_matches(self):
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()
            assert row is not None
            assert row["version"] == db.SCHEMA_VERSION
