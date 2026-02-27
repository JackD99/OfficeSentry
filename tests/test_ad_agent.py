"""
tests/test_ad_agent.py — Tests for the AD Agent feature:
  - Database CRUD for all 5 new tables
  - Operator route access control and response formats
  - Agent API enrollment, heartbeat, poll, result
  - HMAC authentication validation
  - Tenant isolation
  - Download endpoint access control and bundle contents
"""

import hashlib
import hmac
import io
import json
import secrets
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


# ---------------------------------------------------------------------------
# Database tests — tenant_ad_config
# ---------------------------------------------------------------------------

class TestTenantAdConfigDB:

    def test_upsert_creates_config(self):
        tid = db.create_tenant("Test", "test.com")
        row_id = db.upsert_tenant_ad_config(tid, hybrid_detected=1,
                                              sync_server_names=["DC01", "DC02"])
        config = db.get_tenant_ad_config(tid)
        assert config is not None
        assert config["hybrid_detected"] == 1
        assert config["sync_server_names"] == ["DC01", "DC02"]
        assert config["tenant_id"] == tid

    def test_upsert_updates_existing(self):
        tid = db.create_tenant("Test", "test.com")
        db.upsert_tenant_ad_config(tid, hybrid_detected=0)
        db.upsert_tenant_ad_config(tid, hybrid_detected=1,
                                    sync_server_names=["DC03"])
        config = db.get_tenant_ad_config(tid)
        assert config["hybrid_detected"] == 1
        assert config["sync_server_names"] == ["DC03"]

    def test_get_nonexistent_returns_none(self):
        assert db.get_tenant_ad_config("nonexistent") is None


# ---------------------------------------------------------------------------
# Database tests — ad_agents
# ---------------------------------------------------------------------------

class TestAdAgentsDB:

    def test_create_and_get_agent(self):
        tid = db.create_tenant("Test", "test.com")
        agent_id = db.create_ad_agent(tid, "Test Agent", "encrypted_secret_data")
        agent = db.get_ad_agent(agent_id)
        assert agent is not None
        assert agent["tenant_id"] == tid
        assert agent["agent_name"] == "Test Agent"
        assert agent["status"] == "active"
        assert agent["server_names"] == []

    def test_get_active_agent_for_tenant(self):
        tid = db.create_tenant("Test", "test.com")
        agent1_id = db.create_ad_agent(tid, "Agent 1", "secret1")
        agent2_id = db.create_ad_agent(tid, "Agent 2", "secret2")
        # Should return one of the active agents
        agent = db.get_ad_agent_for_tenant(tid)
        assert agent is not None
        assert agent["id"] in (agent1_id, agent2_id)
        assert agent["status"] == "active"

    def test_get_active_agent_excludes_revoked(self):
        tid = db.create_tenant("Test", "test.com")
        agent_id = db.create_ad_agent(tid, "Agent", "secret")
        db.revoke_ad_agent(agent_id)
        agent = db.get_ad_agent_for_tenant(tid)
        assert agent is None

    def test_list_agents(self):
        tid = db.create_tenant("Test", "test.com")
        db.create_ad_agent(tid, "A1", "s1")
        db.create_ad_agent(tid, "A2", "s2")
        agents = db.list_ad_agents(tid)
        assert len(agents) == 2

    def test_update_agent(self):
        tid = db.create_tenant("Test", "test.com")
        agent_id = db.create_ad_agent(tid, "Agent", "secret")
        db.update_ad_agent(agent_id, last_seen_at="2025-01-01T00:00:00",
                           last_ip="10.0.0.1",
                           server_names=["DC01"])
        agent = db.get_ad_agent(agent_id)
        assert agent["last_seen_at"] == "2025-01-01T00:00:00"
        assert agent["last_ip"] == "10.0.0.1"
        assert agent["server_names"] == ["DC01"]

    def test_update_agent_ignores_disallowed_fields(self):
        tid = db.create_tenant("Test", "test.com")
        agent_id = db.create_ad_agent(tid, "Agent", "secret")
        db.update_ad_agent(agent_id, tenant_id="hacked", id="hacked")
        agent = db.get_ad_agent(agent_id)
        assert agent["tenant_id"] == tid
        assert agent["id"] == agent_id

    def test_revoke_agent(self):
        tid = db.create_tenant("Test", "test.com")
        agent_id = db.create_ad_agent(tid, "Agent", "secret")
        db.revoke_ad_agent(agent_id)
        agent = db.get_ad_agent(agent_id)
        assert agent["status"] == "revoked"
        assert agent["revoked_at"] is not None

    def test_delete_agent(self):
        tid = db.create_tenant("Test", "test.com")
        agent_id = db.create_ad_agent(tid, "Agent", "secret")
        assert db.delete_ad_agent(agent_id) is True
        assert db.get_ad_agent(agent_id) is None
        assert db.delete_ad_agent(agent_id) is False


# ---------------------------------------------------------------------------
# Database tests — enrollment tokens
# ---------------------------------------------------------------------------

class TestEnrollmentTokensDB:

    def test_create_and_validate_token(self):
        tid = db.create_tenant("Test", "test.com")
        raw = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        token_id = db.create_enrollment_token(tid, token_hash, expires, "admin")

        result = db.validate_enrollment_token(tid, token_hash)
        assert result is not None
        assert result["id"] == token_id
        assert result["used_at"] is None

    def test_validate_wrong_tenant(self):
        tid1 = db.create_tenant("Test1", "test1.com")
        tid2 = db.create_tenant("Test2", "test2.com")
        raw = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        db.create_enrollment_token(tid1, token_hash, expires, "admin")
        # Cannot use token for different tenant
        assert db.validate_enrollment_token(tid2, token_hash) is None

    def test_validate_expired_token(self):
        tid = db.create_tenant("Test", "test.com")
        raw = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        # Use SQLite-compatible datetime format (no timezone suffix)
        expires = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        db.create_enrollment_token(tid, token_hash, expires, "admin")
        assert db.validate_enrollment_token(tid, token_hash) is None

    def test_validate_consumed_token(self):
        tid = db.create_tenant("Test", "test.com")
        raw = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(raw.encode()).hexdigest()
        expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        token_id = db.create_enrollment_token(tid, token_hash, expires, "admin")
        db.consume_enrollment_token(token_id, "agent123")
        assert db.validate_enrollment_token(tid, token_hash) is None

    def test_list_tokens(self):
        tid = db.create_tenant("Test", "test.com")
        db.create_enrollment_token(tid, "hash1",
                                    (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
                                    "admin")
        db.create_enrollment_token(tid, "hash2",
                                    (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
                                    "admin")
        tokens = db.list_enrollment_tokens(tid)
        assert len(tokens) == 2
        # token_hash should not be in the list output (only id, dates, etc.)
        assert "token_hash" not in tokens[0]


# ---------------------------------------------------------------------------
# Database tests — agent commands
# ---------------------------------------------------------------------------

class TestAgentCommandsDB:

    def test_create_and_get_command(self):
        tid = db.create_tenant("Test", "test.com")
        cmd_id = db.create_agent_command(tid, "lookup_user",
                                          {"identity": "jdoe"}, "admin")
        cmd = db.get_agent_command(cmd_id)
        assert cmd is not None
        assert cmd["command_type"] == "lookup_user"
        assert cmd["parameters"] == {"identity": "jdoe"}
        assert cmd["status"] == "pending"
        assert cmd["result"] is None

    def test_poll_pending_commands(self):
        tid = db.create_tenant("Test", "test.com")
        agent_id = db.create_ad_agent(tid, "Agent", "secret")
        db.create_agent_command(tid, "ping", {}, "admin")
        db.create_agent_command(tid, "lookup_user", {"identity": "x"}, "admin")

        commands = db.poll_pending_commands(tid, agent_id)
        assert len(commands) == 2
        # Commands should now be marked as running
        for c in commands:
            updated = db.get_agent_command(c["id"])
            assert updated["status"] == "running"
            assert updated["agent_id"] == agent_id

    def test_poll_commands_tenant_isolation(self):
        tid1 = db.create_tenant("T1", "t1.com")
        tid2 = db.create_tenant("T2", "t2.com")
        agent1 = db.create_ad_agent(tid1, "A1", "s1")
        agent2 = db.create_ad_agent(tid2, "A2", "s2")
        db.create_agent_command(tid1, "ping", {}, "admin")
        db.create_agent_command(tid2, "ping", {}, "admin")

        cmds1 = db.poll_pending_commands(tid1, agent1)
        cmds2 = db.poll_pending_commands(tid2, agent2)
        assert len(cmds1) == 1
        assert len(cmds2) == 1
        assert cmds1[0]["tenant_id"] == tid1
        assert cmds2[0]["tenant_id"] == tid2

    def test_complete_command(self):
        tid = db.create_tenant("Test", "test.com")
        cmd_id = db.create_agent_command(tid, "ping", {}, "admin")
        db.complete_agent_command(cmd_id, {"message": "pong"}, "completed")
        cmd = db.get_agent_command(cmd_id)
        assert cmd["status"] == "completed"
        assert cmd["result"] == {"message": "pong"}
        assert cmd["completed_at"] is not None

    def test_list_commands_with_filter(self):
        tid = db.create_tenant("Test", "test.com")
        cmd1 = db.create_agent_command(tid, "ping", {}, "admin")
        cmd2 = db.create_agent_command(tid, "ping", {}, "admin")
        db.complete_agent_command(cmd1, {"ok": True}, "completed")

        all_cmds = db.list_agent_commands(tid)
        assert len(all_cmds) == 2
        pending = db.list_agent_commands(tid, status="pending")
        assert len(pending) == 1
        completed = db.list_agent_commands(tid, status="completed")
        assert len(completed) == 1


# ---------------------------------------------------------------------------
# Database tests — agent templates
# ---------------------------------------------------------------------------

class TestAgentTemplatesDB:

    def test_create_and_get_template(self):
        tid = db.create_tenant("Test", "test.com")
        tmpl_id = db.create_agent_template(
            tid, "New Hire", "Standard new hire setup",
            {"groups": ["IT-Users"], "attributes": {"department": "IT"}},
            "admin"
        )
        tmpl = db.get_agent_template(tmpl_id)
        assert tmpl is not None
        assert tmpl["name"] == "New Hire"
        assert tmpl["template_data"]["groups"] == ["IT-Users"]
        assert tmpl["tenant_id"] == tid

    def test_list_templates(self):
        tid = db.create_tenant("Test", "test.com")
        db.create_agent_template(tid, "T1", "", {}, "admin")
        db.create_agent_template(tid, "T2", "", {}, "admin")
        templates = db.list_agent_templates(tid)
        assert len(templates) == 2

    def test_update_template(self):
        tid = db.create_tenant("Test", "test.com")
        tmpl_id = db.create_agent_template(tid, "Old", "", {}, "admin")
        db.update_agent_template(tmpl_id, name="New",
                                  template_data={"groups": ["G1"]})
        tmpl = db.get_agent_template(tmpl_id)
        assert tmpl["name"] == "New"
        assert tmpl["template_data"]["groups"] == ["G1"]

    def test_delete_template(self):
        tid = db.create_tenant("Test", "test.com")
        tmpl_id = db.create_agent_template(tid, "Del", "", {}, "admin")
        assert db.delete_agent_template(tmpl_id) is True
        assert db.get_agent_template(tmpl_id) is None


# ---------------------------------------------------------------------------
# Route tests — operator endpoints
# ---------------------------------------------------------------------------

class TestAdAgentOperatorRoutes:

    def test_overview_page(self, authed_client):
        resp = authed_client.get("/ad-agent")
        assert resp.status_code == 200
        assert b"AD Agent" in resp.data

    def test_overview_requires_login(self, client):
        resp = client.get("/ad-agent")
        assert resp.status_code == 302
        assert "/login" in resp.headers.get("Location", "")

    def test_agent_status_returns_json(self, authed_client):
        tid = db.create_tenant("Test", "test.com")
        resp = authed_client.get(f"/tenants/{tid}/ad-agent/status")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["agent"] is None  # No agent yet
        assert data["config"] is None

    def test_agent_status_with_agent(self, authed_client):
        tid = db.create_tenant("Test", "test.com")
        db.create_ad_agent(tid, "MyAgent", "enc_secret")
        resp = authed_client.get(f"/tenants/{tid}/ad-agent/status")
        data = resp.get_json()
        assert data["ok"] is True
        assert data["agent"] is not None
        assert data["agent"]["agent_name"] == "MyAgent"

    def test_agent_status_404_bad_tenant(self, authed_client):
        resp = authed_client.get("/tenants/badid/ad-agent/status")
        assert resp.status_code == 404

    def test_generate_enrollment_token(self, authed_client):
        tid = db.create_tenant("Test", "test.com")
        resp = authed_client.post(f"/tenants/{tid}/ad-agent/enroll")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert "enrollment_token" in data
        assert "expires_at" in data
        assert len(data["enrollment_token"]) > 20

    def test_generate_enrollment_requires_admin(self, client):
        tid = db.create_tenant("Test", "test.com")
        resp = client.post(f"/tenants/{tid}/ad-agent/enroll")
        assert resp.status_code == 302  # Redirect to login

    def test_revoke_agent(self, authed_client):
        tid = db.create_tenant("Test", "test.com")
        agent_id = db.create_ad_agent(tid, "Agent", "secret")
        resp = authed_client.post(
            f"/tenants/{tid}/ad-agent/revoke",
            data=json.dumps({"agent_id": agent_id}),
            content_type="application/json"
        )
        data = resp.get_json()
        assert data["ok"] is True
        agent = db.get_ad_agent(agent_id)
        assert agent["status"] == "revoked"

    def test_revoke_no_agent(self, authed_client):
        tid = db.create_tenant("Test", "test.com")
        resp = authed_client.post(
            f"/tenants/{tid}/ad-agent/revoke",
            data=json.dumps({}),
            content_type="application/json"
        )
        data = resp.get_json()
        assert data["ok"] is False

    def test_rotate_agent_credentials(self, authed_client, app):
        tid = db.create_tenant("Test", "test.com")
        old_enc = db.encrypt_value("old_secret", app.secret_key)
        agent_id = db.create_ad_agent(tid, "Agent", old_enc)
        resp = authed_client.post(
            f"/tenants/{tid}/ad-agent/rotate",
            data=json.dumps({}),
            content_type="application/json"
        )
        data = resp.get_json()
        assert data["ok"] is True
        agent = db.get_ad_agent(agent_id)
        assert agent["agent_secret_enc"] != old_enc

    def test_dispatch_command(self, authed_client):
        tid = db.create_tenant("Test", "test.com")
        db.create_ad_agent(tid, "Agent", "secret")
        resp = authed_client.post(
            f"/tenants/{tid}/ad-agent/command",
            data=json.dumps({"command_type": "ping", "parameters": {}}),
            content_type="application/json"
        )
        data = resp.get_json()
        assert data["ok"] is True
        assert "command_id" in data

    def test_dispatch_invalid_command_type(self, authed_client):
        tid = db.create_tenant("Test", "test.com")
        db.create_ad_agent(tid, "Agent", "secret")
        resp = authed_client.post(
            f"/tenants/{tid}/ad-agent/command",
            data=json.dumps({"command_type": "drop_database", "parameters": {}}),
            content_type="application/json"
        )
        data = resp.get_json()
        assert data["ok"] is False
        assert "Invalid command type" in data["error"]

    def test_dispatch_no_agent(self, authed_client):
        tid = db.create_tenant("Test", "test.com")
        resp = authed_client.post(
            f"/tenants/{tid}/ad-agent/command",
            data=json.dumps({"command_type": "ping", "parameters": {}}),
            content_type="application/json"
        )
        data = resp.get_json()
        assert data["ok"] is False

    def test_list_commands(self, authed_client):
        tid = db.create_tenant("Test", "test.com")
        db.create_agent_command(tid, "ping", {}, "admin")
        resp = authed_client.get(f"/tenants/{tid}/ad-agent/commands")
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["commands"]) == 1

    def test_update_server_names(self, authed_client):
        tid = db.create_tenant("Test", "test.com")
        resp = authed_client.post(
            f"/tenants/{tid}/ad-agent/server-names",
            data=json.dumps({"server_names": ["DC01", "DC02"]}),
            content_type="application/json"
        )
        data = resp.get_json()
        assert data["ok"] is True
        assert data["server_names"] == ["DC01", "DC02"]
        config = db.get_tenant_ad_config(tid)
        assert config["sync_server_names"] == ["DC01", "DC02"]

    def test_hybrid_status(self, authed_client):
        tid = db.create_tenant("Test", "test.com")
        db.upsert_tenant_ad_config(tid, hybrid_detected=1,
                                    sync_server_names=["DC01"])
        resp = authed_client.get(f"/tenants/{tid}/ad-agent/hybrid-status")
        data = resp.get_json()
        assert data["ok"] is True
        assert data["hybrid_detected"] is True
        assert data["sync_server_names"] == ["DC01"]


# ---------------------------------------------------------------------------
# Route tests — templates CRUD
# ---------------------------------------------------------------------------

class TestAdAgentTemplateRoutes:

    def test_list_templates(self, authed_client):
        tid = db.create_tenant("Test", "test.com")
        db.create_agent_template(tid, "T1", "desc", {}, "admin")
        resp = authed_client.get(f"/tenants/{tid}/ad-agent/templates")
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["templates"]) == 1

    def test_create_template(self, authed_client):
        tid = db.create_tenant("Test", "test.com")
        resp = authed_client.post(
            f"/tenants/{tid}/ad-agent/templates",
            data=json.dumps({
                "name": "New Hire",
                "description": "Standard setup",
                "template_data": {"groups": ["IT"], "attributes": {"dept": "IT"}}
            }),
            content_type="application/json"
        )
        data = resp.get_json()
        assert data["ok"] is True
        assert "template_id" in data

    def test_create_template_no_name(self, authed_client):
        tid = db.create_tenant("Test", "test.com")
        resp = authed_client.post(
            f"/tenants/{tid}/ad-agent/templates",
            data=json.dumps({"name": "", "template_data": {}}),
            content_type="application/json"
        )
        data = resp.get_json()
        assert data["ok"] is False

    def test_update_template(self, authed_client):
        tid = db.create_tenant("Test", "test.com")
        tmpl_id = db.create_agent_template(tid, "Old", "", {}, "admin")
        resp = authed_client.put(
            f"/tenants/{tid}/ad-agent/templates/{tmpl_id}",
            data=json.dumps({"name": "Updated"}),
            content_type="application/json"
        )
        data = resp.get_json()
        assert data["ok"] is True

    def test_update_template_wrong_tenant(self, authed_client):
        tid1 = db.create_tenant("T1", "t1.com")
        tid2 = db.create_tenant("T2", "t2.com")
        tmpl_id = db.create_agent_template(tid1, "T", "", {}, "admin")
        resp = authed_client.put(
            f"/tenants/{tid2}/ad-agent/templates/{tmpl_id}",
            data=json.dumps({"name": "Hacked"}),
            content_type="application/json"
        )
        data = resp.get_json()
        assert data["ok"] is False

    def test_delete_template(self, authed_client):
        tid = db.create_tenant("Test", "test.com")
        tmpl_id = db.create_agent_template(tid, "Del", "", {}, "admin")
        resp = authed_client.delete(f"/tenants/{tid}/ad-agent/templates/{tmpl_id}")
        data = resp.get_json()
        assert data["ok"] is True
        assert db.get_agent_template(tmpl_id) is None


# ---------------------------------------------------------------------------
# Route tests — agent download
# ---------------------------------------------------------------------------

class TestAgentDownload:

    @patch("routes.ad_agent.SCRIPTS_DIR", Path(__file__).parent.parent / "scripts")
    def test_download_returns_zip(self, authed_client):
        tid = db.create_tenant("Test Corp", "test.com")
        resp = authed_client.get(f"/tenants/{tid}/ad-agent/download")
        assert resp.status_code == 200
        assert resp.content_type == "application/zip"
        assert "OfficeSentry-AD-Agent" in resp.headers.get("Content-Disposition", "")

    @patch("routes.ad_agent.SCRIPTS_DIR", Path(__file__).parent.parent / "scripts")
    def test_download_contains_expected_files(self, authed_client):
        tid = db.create_tenant("Test Corp", "test.com")
        resp = authed_client.get(f"/tenants/{tid}/ad-agent/download")
        buf = io.BytesIO(resp.data)
        with zipfile.ZipFile(buf) as zf:
            names = zf.namelist()
            assert "OfficeSentry-AD-Agent/AD-Agent.ps1" in names
            assert "OfficeSentry-AD-Agent/enrollment.json" in names
            assert "OfficeSentry-AD-Agent/README.txt" in names

    @patch("routes.ad_agent.SCRIPTS_DIR", Path(__file__).parent.parent / "scripts")
    def test_download_enrollment_json_has_tenant_binding(self, authed_client):
        tid = db.create_tenant("Test Corp", "test.com")
        resp = authed_client.get(f"/tenants/{tid}/ad-agent/download")
        buf = io.BytesIO(resp.data)
        with zipfile.ZipFile(buf) as zf:
            enrollment = json.loads(zf.read("OfficeSentry-AD-Agent/enrollment.json"))
            assert enrollment["tenant_id"] == tid
            assert enrollment["tenant_name"] == "Test Corp"
            assert "enrollment_token" in enrollment
            assert "portal_url" in enrollment
            # Enrollment token should be substantial
            assert len(enrollment["enrollment_token"]) > 20

    @patch("routes.ad_agent.SCRIPTS_DIR", Path(__file__).parent.parent / "scripts")
    def test_download_creates_enrollment_token_in_db(self, authed_client):
        tid = db.create_tenant("Test Corp", "test.com")
        authed_client.get(f"/tenants/{tid}/ad-agent/download")
        tokens = db.list_enrollment_tokens(tid)
        assert len(tokens) == 1

    def test_download_requires_login(self, client):
        tid = db.create_tenant("Test", "test.com")
        resp = client.get(f"/tenants/{tid}/ad-agent/download")
        assert resp.status_code == 302

    @patch("routes.ad_agent.SCRIPTS_DIR", Path(__file__).parent.parent / "scripts")
    def test_download_enrollment_token_no_secrets_leaked(self, authed_client):
        """Verify the download does not contain agent_secret or portal secret_key."""
        tid = db.create_tenant("Test Corp", "test.com")
        resp = authed_client.get(f"/tenants/{tid}/ad-agent/download")
        buf = io.BytesIO(resp.data)
        with zipfile.ZipFile(buf) as zf:
            enrollment = json.loads(zf.read("OfficeSentry-AD-Agent/enrollment.json"))
            # Should NOT contain agent_secret (that's only returned after enrollment)
            assert "agent_secret" not in enrollment
            # Should NOT contain the Flask secret key
            assert "secret_key" not in enrollment


# ---------------------------------------------------------------------------
# Agent API tests — enrollment
# ---------------------------------------------------------------------------

class TestAgentAPIEnrollment:

    def test_enroll_with_valid_token(self, client, app):
        tid = db.create_tenant("Test", "test.com")
        raw_token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        db.create_enrollment_token(tid, token_hash, expires, "admin")

        resp = client.post("/api/agent/enroll",
                           data=json.dumps({
                               "tenant_id": tid,
                               "enrollment_token": raw_token,
                               "agent_name": "Test Agent",
                           }),
                           content_type="application/json")
        data = resp.get_json()
        assert data["ok"] is True
        assert "agent_id" in data
        assert "agent_secret" in data
        assert len(data["agent_secret"]) > 20

    def test_enroll_consumes_token(self, client, app):
        tid = db.create_tenant("Test", "test.com")
        raw_token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        db.create_enrollment_token(tid, token_hash, expires, "admin")

        # First enrollment succeeds
        resp1 = client.post("/api/agent/enroll",
                            data=json.dumps({
                                "tenant_id": tid,
                                "enrollment_token": raw_token,
                            }),
                            content_type="application/json")
        assert resp1.get_json()["ok"] is True

        # Second enrollment fails (token consumed)
        resp2 = client.post("/api/agent/enroll",
                            data=json.dumps({
                                "tenant_id": tid,
                                "enrollment_token": raw_token,
                            }),
                            content_type="application/json")
        assert resp2.get_json()["ok"] is False

    def test_enroll_invalid_token(self, client):
        tid = db.create_tenant("Test", "test.com")
        resp = client.post("/api/agent/enroll",
                           data=json.dumps({
                               "tenant_id": tid,
                               "enrollment_token": "invalid_token_here",
                           }),
                           content_type="application/json")
        data = resp.get_json()
        assert data["ok"] is False

    def test_enroll_wrong_tenant(self, client):
        tid1 = db.create_tenant("T1", "t1.com")
        tid2 = db.create_tenant("T2", "t2.com")
        raw_token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        expires = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        db.create_enrollment_token(tid1, token_hash, expires, "admin")

        # Try to use token for different tenant
        resp = client.post("/api/agent/enroll",
                           data=json.dumps({
                               "tenant_id": tid2,
                               "enrollment_token": raw_token,
                           }),
                           content_type="application/json")
        assert resp.get_json()["ok"] is False

    def test_enroll_missing_fields(self, client):
        resp = client.post("/api/agent/enroll",
                           data=json.dumps({"tenant_id": ""}),
                           content_type="application/json")
        data = resp.get_json()
        assert data["ok"] is False


# ---------------------------------------------------------------------------
# Agent API tests — HMAC authenticated endpoints
# ---------------------------------------------------------------------------

class TestAgentAPIAuthenticated:

    def _make_signed_request(self, client, app, url, body_dict,
                              agent_id, agent_secret):
        """Helper to make a properly HMAC-signed request."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        nonce = secrets.token_hex(16)
        body = json.dumps(body_dict)

        message = f"{agent_id}\n{timestamp}\n{nonce}\n{body}"
        signature = hmac.new(
            agent_secret.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()

        return client.post(url,
                           data=body,
                           content_type="application/json",
                           headers={
                               "X-Agent-Id": agent_id,
                               "X-Agent-Timestamp": timestamp,
                               "X-Agent-Nonce": nonce,
                               "X-Agent-Signature": signature,
                           })

    def _create_enrolled_agent(self, app):
        """Create a tenant + enrolled agent, return (tenant_id, agent_id, secret)."""
        tid = db.create_tenant("Test", "test.com")
        agent_secret = secrets.token_urlsafe(48)
        agent_secret_enc = db.encrypt_value(agent_secret, app.secret_key)
        agent_id = db.create_ad_agent(tid, "Test Agent", agent_secret_enc)
        return tid, agent_id, agent_secret

    def test_heartbeat_success(self, client, app):
        tid, agent_id, secret = self._create_enrolled_agent(app)
        resp = self._make_signed_request(
            client, app, "/api/agent/heartbeat",
            {"agent_version": "1.0.0"}, agent_id, secret
        )
        data = resp.get_json()
        assert data["ok"] is True
        # Verify last_seen updated
        agent = db.get_ad_agent(agent_id)
        assert agent["last_seen_at"] is not None

    def test_heartbeat_missing_headers(self, client):
        resp = client.post("/api/agent/heartbeat",
                           data=json.dumps({}),
                           content_type="application/json")
        assert resp.status_code == 401

    def test_heartbeat_invalid_signature(self, client, app):
        tid, agent_id, secret = self._create_enrolled_agent(app)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        nonce = secrets.token_hex(16)
        resp = client.post("/api/agent/heartbeat",
                           data=json.dumps({}),
                           content_type="application/json",
                           headers={
                               "X-Agent-Id": agent_id,
                               "X-Agent-Timestamp": timestamp,
                               "X-Agent-Nonce": nonce,
                               "X-Agent-Signature": "bad_signature",
                           })
        assert resp.status_code == 401

    def test_heartbeat_revoked_agent(self, client, app):
        tid, agent_id, secret = self._create_enrolled_agent(app)
        db.revoke_ad_agent(agent_id)
        resp = self._make_signed_request(
            client, app, "/api/agent/heartbeat", {}, agent_id, secret
        )
        assert resp.status_code == 403

    def test_heartbeat_expired_timestamp(self, client, app):
        tid, agent_id, secret = self._create_enrolled_agent(app)
        old_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        nonce = secrets.token_hex(16)
        body = json.dumps({})
        message = f"{agent_id}\n{old_time}\n{nonce}\n{body}"
        signature = hmac.new(secret.encode(), message.encode(),
                             hashlib.sha256).hexdigest()
        resp = client.post("/api/agent/heartbeat",
                           data=body,
                           content_type="application/json",
                           headers={
                               "X-Agent-Id": agent_id,
                               "X-Agent-Timestamp": old_time,
                               "X-Agent-Nonce": nonce,
                               "X-Agent-Signature": signature,
                           })
        assert resp.status_code == 401

    def test_poll_returns_commands(self, client, app):
        tid, agent_id, secret = self._create_enrolled_agent(app)
        db.create_agent_command(tid, "ping", {}, "admin")
        resp = self._make_signed_request(
            client, app, "/api/agent/poll", {}, agent_id, secret
        )
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["commands"]) == 1
        assert data["commands"][0]["command_type"] == "ping"

    def test_poll_empty_when_no_commands(self, client, app):
        tid, agent_id, secret = self._create_enrolled_agent(app)
        resp = self._make_signed_request(
            client, app, "/api/agent/poll", {}, agent_id, secret
        )
        data = resp.get_json()
        assert data["ok"] is True
        assert len(data["commands"]) == 0

    def test_result_report(self, client, app):
        tid, agent_id, secret = self._create_enrolled_agent(app)
        cmd_id = db.create_agent_command(tid, "ping", {}, "admin",
                                          agent_id=agent_id)
        # Mark as running first (simulates poll)
        db.poll_pending_commands(tid, agent_id)

        resp = self._make_signed_request(
            client, app, "/api/agent/result",
            {"command_id": cmd_id, "result": {"message": "pong"},
             "status": "completed"},
            agent_id, secret
        )
        data = resp.get_json()
        assert data["ok"] is True
        cmd = db.get_agent_command(cmd_id)
        assert cmd["status"] == "completed"
        assert cmd["result"]["message"] == "pong"

    def test_result_wrong_tenant(self, client, app):
        """Agent cannot report results for commands belonging to another tenant."""
        tid1 = db.create_tenant("T1", "t1.com")
        tid2 = db.create_tenant("T2", "t2.com")
        secret1 = secrets.token_urlsafe(48)
        enc1 = db.encrypt_value(secret1, app.secret_key)
        agent1_id = db.create_ad_agent(tid1, "A1", enc1)

        # Create command for tenant 2
        cmd_id = db.create_agent_command(tid2, "ping", {}, "admin")

        # Agent 1 tries to report result for tenant 2's command
        resp = self._make_signed_request(
            client, app, "/api/agent/result",
            {"command_id": cmd_id, "result": {}, "status": "completed"},
            agent1_id, secret1
        )
        data = resp.get_json()
        assert data["ok"] is False


# ---------------------------------------------------------------------------
# Tenant detail page integration
# ---------------------------------------------------------------------------

class TestTenantDetailAdAgent:

    def test_tenant_detail_has_agent_section(self, authed_client):
        tid = db.create_tenant("Test", "test.com")
        resp = authed_client.get(f"/tenants/{tid}")
        assert resp.status_code == 200
        assert b"ad-agent" in resp.data
        assert b"Office Sentry AD Agent" in resp.data
