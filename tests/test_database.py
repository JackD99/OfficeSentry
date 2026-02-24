"""
tests/test_database.py — Database layer tests: CRUD, encryption, schema.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


class TestUsers:
    def test_create_and_get_user(self):
        db.create_user("testuser", "testpass", display_name="Test User", is_admin=0)
        user = db.get_user("testuser")
        assert user is not None
        assert user["username"] == "testuser"
        assert user["display_name"] == "Test User"
        assert user["is_admin"] == 0

    def test_user_count(self):
        assert db.user_count() == 0
        db.create_user("a", "pass1234")
        assert db.user_count() == 1
        db.create_user("b", "pass1234")
        assert db.user_count() == 2

    def test_password_hashing(self):
        db.create_user("hashtest", "mypassword")
        user = db.get_user("hashtest")
        # The stored hash should NOT be the plaintext
        assert user["password_hash"] != "mypassword"
        assert db.check_password(user["password_hash"], "mypassword")
        assert not db.check_password(user["password_hash"], "wrongpassword")

    def test_update_password(self):
        db.create_user("pwuser", "oldpass1")
        db.update_password("pwuser", "newpass1")
        user = db.get_user("pwuser")
        assert db.check_password(user["password_hash"], "newpass1")
        assert not db.check_password(user["password_hash"], "oldpass1")

    def test_get_nonexistent_user(self):
        assert db.get_user("nobody") is None


class TestTenants:
    def test_create_and_get_tenant(self):
        tid = db.create_tenant("Test Co", "test.onmicrosoft.com",
                               tenant_id="abc-123", notes="Test tenant")
        t = db.get_tenant(tid)
        assert t is not None
        assert t["name"] == "Test Co"
        assert t["domain"] == "test.onmicrosoft.com"
        assert t["tenant_id"] == "abc-123"
        assert t["notes"] == "Test tenant"

    def test_list_tenants(self):
        db.create_tenant("Alpha", "alpha.com")
        db.create_tenant("Beta", "beta.com")
        tenants = db.list_tenants()
        assert len(tenants) == 2
        names = [t["name"] for t in tenants]
        assert "Alpha" in names
        assert "Beta" in names

    def test_update_tenant(self):
        tid = db.create_tenant("Old Name", "old.com")
        db.update_tenant(tid, name="New Name", domain="new.com")
        t = db.get_tenant(tid)
        assert t["name"] == "New Name"
        assert t["domain"] == "new.com"

    def test_delete_tenant(self):
        tid = db.create_tenant("Delete Me", "del.com")
        db.delete_tenant(tid)
        assert db.get_tenant(tid) is None

    def test_touch_tenant_audit(self):
        tid = db.create_tenant("Audit Me", "aud.com")
        t = db.get_tenant(tid)
        assert t["last_audit_at"] is None
        db.touch_tenant_audit(tid)
        t = db.get_tenant(tid)
        assert t["last_audit_at"] is not None


class TestEncryption:
    def test_encrypt_decrypt_roundtrip(self):
        secret = "my-flask-secret-key"
        plaintext = "SuperSecretPfxPassword!"
        encrypted = db.encrypt_value(plaintext, secret)
        assert encrypted != plaintext
        decrypted = db.decrypt_value(encrypted, secret)
        assert decrypted == plaintext

    def test_different_secrets_fail(self):
        encrypted = db.encrypt_value("test", "secret1")
        import pytest
        with pytest.raises(Exception):
            db.decrypt_value(encrypted, "secret2")


class TestJobs:
    def test_create_and_get_job(self):
        tid = db.create_tenant("T", "t.com")
        jid = db.create_job("Test Job", "spo", tenant_id=tid, operator="admin")
        job = db.get_job(jid)
        assert job is not None
        assert job["label"] == "Test Job"
        assert job["status"] == "running"
        assert job["output"] == []

    def test_append_and_finish_job(self):
        jid = db.create_job("Runner", "security")
        db.append_job_output(jid, "line 1")
        db.append_job_output(jid, "line 2")
        job = db.get_job(jid)
        assert job["output"] == ["line 1", "line 2"]

        db.finish_job(jid, "done")
        job = db.get_job(jid)
        assert job["status"] == "done"
        assert job["ended_at"] is not None

    def test_list_running_jobs(self):
        jid1 = db.create_job("Running", "spo")
        jid2 = db.create_job("Done", "spo")
        db.finish_job(jid2, "done")

        running = db.list_running_jobs()
        assert len(running) == 1
        assert running[0]["id"] == jid1


class TestAuditLog:
    def test_log_and_retrieve(self):
        db.log_action("admin", "test_action", details="something happened")
        entries = db.get_audit_log()
        assert len(entries) == 1
        assert entries[0]["action"] == "test_action"
        assert entries[0]["operator"] == "admin"

    def test_filter_by_tenant(self):
        tid = db.create_tenant("Filtered", "f.com")
        db.log_action("admin", "action1", tenant_id=tid)
        db.log_action("admin", "action2")  # no tenant
        entries = db.get_audit_log(tenant_id=tid)
        assert len(entries) == 1
        assert entries[0]["action"] == "action1"


class TestSchemaVersion:
    def test_schema_version_initialized(self):
        """After init_db, schema_version table exists with version 1."""
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()
            assert row is not None
            assert row["version"] == db.SCHEMA_VERSION
