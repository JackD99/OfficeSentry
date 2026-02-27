"""
tests/test_rbac.py — Tests for Role-Based Access Control (RBAC) system.
"""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "portal"))

import database as db


# ---------------------------------------------------------------------------
# Permissions module unit tests
# ---------------------------------------------------------------------------

class TestRoleHierarchy:
    """Test RBAC role hierarchy and has_role()."""

    def test_super_admin_has_all_roles(self):
        from permissions import has_role
        user = {"role": "super_admin"}
        assert has_role(user, "super_admin")
        assert has_role(user, "admin")
        assert has_role(user, "operator")
        assert has_role(user, "viewer")

    def test_admin_has_admin_and_below(self):
        from permissions import has_role
        user = {"role": "admin"}
        assert not has_role(user, "super_admin")
        assert has_role(user, "admin")
        assert has_role(user, "operator")
        assert has_role(user, "viewer")

    def test_operator_has_operator_and_below(self):
        from permissions import has_role
        user = {"role": "operator"}
        assert not has_role(user, "super_admin")
        assert not has_role(user, "admin")
        assert has_role(user, "operator")
        assert has_role(user, "viewer")

    def test_viewer_has_viewer_only(self):
        from permissions import has_role
        user = {"role": "viewer"}
        assert not has_role(user, "super_admin")
        assert not has_role(user, "admin")
        assert not has_role(user, "operator")
        assert has_role(user, "viewer")

    def test_missing_role_defaults_to_viewer(self):
        from permissions import has_role
        user = {}
        # Missing role defaults to "viewer" level, which meets viewer requirement
        assert has_role(user, "viewer")
        assert not has_role(user, "operator")

    def test_unknown_min_role_returns_false(self):
        from permissions import has_role
        user = {"role": "super_admin"}
        assert not has_role(user, "nonexistent_role")

    def test_role_labels_exist_for_all_roles(self):
        from permissions import ROLE_HIERARCHY, ROLE_LABELS
        for role in ROLE_HIERARCHY:
            assert role in ROLE_LABELS


class TestCanRunOperation:
    """Test operation tier restrictions."""

    def test_super_admin_all_tiers(self):
        from permissions import can_run_operation
        user = {"role": "super_admin"}
        assert can_run_operation(user, 1)
        assert can_run_operation(user, 2)
        assert can_run_operation(user, 3)

    def test_admin_tier_1_and_2(self):
        from permissions import can_run_operation
        user = {"role": "admin"}
        assert can_run_operation(user, 1)
        assert can_run_operation(user, 2)
        assert not can_run_operation(user, 3)

    def test_operator_tier_1_only(self):
        from permissions import can_run_operation
        user = {"role": "operator"}
        assert can_run_operation(user, 1)
        assert not can_run_operation(user, 2)
        assert not can_run_operation(user, 3)

    def test_viewer_no_tiers(self):
        from permissions import can_run_operation
        user = {"role": "viewer"}
        assert not can_run_operation(user, 1)
        assert not can_run_operation(user, 2)
        assert not can_run_operation(user, 3)


class TestCanRunAudit:
    """Test audit permission checks."""

    def test_operator_can_run_audit(self):
        from permissions import can_run_audit
        assert can_run_audit({"role": "operator"})

    def test_admin_can_run_audit(self):
        from permissions import can_run_audit
        assert can_run_audit({"role": "admin"})

    def test_viewer_cannot_run_audit(self):
        from permissions import can_run_audit
        assert not can_run_audit({"role": "viewer"})


class TestCanAccessTenant:
    """Test tenant access filtering."""

    def test_super_admin_can_access_any_tenant(self):
        from permissions import can_access_tenant
        user = {"role": "super_admin", "allowed_tenant_groups": "[]"}
        assert can_access_tenant(user, "any-tenant-id")

    def test_no_group_restriction_allows_all(self):
        from permissions import can_access_tenant
        user = {"role": "operator", "allowed_tenant_groups": "[]"}
        assert can_access_tenant(user, "any-tenant-id")

    def test_user_with_groups_can_access_grouped_tenant(self):
        from permissions import can_access_tenant
        group_id = db.create_tenant_group("TestGroup")
        tenant_id = db.create_tenant("Test", "test.com", tenant_id="tid-1")
        db.add_tenant_to_group(group_id, tenant_id)

        user = {"role": "operator", "allowed_tenant_groups": json.dumps([str(group_id)])}
        assert can_access_tenant(user, tenant_id)

    def test_user_with_groups_cannot_access_ungrouped_tenant(self):
        from permissions import can_access_tenant
        group_id = db.create_tenant_group("TestGroup")
        tenant_id = db.create_tenant("Test", "test.com", tenant_id="tid-1")
        # Don't add tenant to group

        user = {"role": "operator", "allowed_tenant_groups": json.dumps([str(group_id)])}
        assert not can_access_tenant(user, tenant_id)


class TestFilterTenantsForUser:
    """Test tenant list filtering."""

    def test_super_admin_sees_all(self):
        from permissions import filter_tenants_for_user
        tenants = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
        user = {"role": "super_admin"}
        result = filter_tenants_for_user(user, tenants)
        assert len(result) == 3

    def test_no_groups_sees_all(self):
        from permissions import filter_tenants_for_user
        tenants = [{"id": "1"}, {"id": "2"}]
        user = {"role": "operator", "allowed_tenant_groups": "[]"}
        result = filter_tenants_for_user(user, tenants)
        assert len(result) == 2

    def test_grouped_user_sees_only_assigned(self):
        from permissions import filter_tenants_for_user
        group_id = db.create_tenant_group("GroupA")
        t1 = db.create_tenant("A", "a.com", tenant_id="ta")
        t2 = db.create_tenant("B", "b.com", tenant_id="tb")
        db.add_tenant_to_group(group_id, t1)
        # t2 is NOT in the group

        tenants = [{"id": t1}, {"id": t2}]
        user = {"role": "operator", "allowed_tenant_groups": json.dumps([str(group_id)])}
        result = filter_tenants_for_user(user, tenants)
        assert len(result) == 1
        assert result[0]["id"] == t1


class TestGetUserGroups:
    """Test get_user_groups() parsing."""

    def test_parses_json_string(self):
        from permissions import get_user_groups
        user = {"allowed_tenant_groups": '["1", "2"]'}
        assert get_user_groups(user) == ["1", "2"]

    def test_returns_list_directly(self):
        from permissions import get_user_groups
        user = {"allowed_tenant_groups": ["1", "2"]}
        assert get_user_groups(user) == ["1", "2"]

    def test_empty_string_returns_empty_list(self):
        from permissions import get_user_groups
        user = {"allowed_tenant_groups": "[]"}
        assert get_user_groups(user) == []

    def test_missing_key_returns_empty_list(self):
        from permissions import get_user_groups
        user = {}
        assert get_user_groups(user) == []

    def test_invalid_json_returns_empty_list(self):
        from permissions import get_user_groups
        user = {"allowed_tenant_groups": "not-json"}
        assert get_user_groups(user) == []


# ---------------------------------------------------------------------------
# Database: RBAC fields
# ---------------------------------------------------------------------------

class TestRBACDatabase:
    """Test RBAC-related database operations."""

    def test_create_admin_user_gets_super_admin_role(self):
        db.create_user("alice", "password123", is_admin=1)
        user = db.get_user("alice")
        assert user["role"] == "super_admin"

    def test_create_non_admin_user_gets_operator_role(self):
        db.create_user("bob", "password123", is_admin=0)
        user = db.get_user("bob")
        assert user["role"] == "operator"

    def test_update_user_role(self):
        db.create_user("carol", "password123")
        db.update_user("carol", role="admin")
        user = db.get_user("carol")
        assert user["role"] == "admin"

    def test_update_user_allowed_tenant_groups(self):
        db.create_user("dave", "password123")
        groups = json.dumps(["g1", "g2"])
        db.update_user("dave", allowed_tenant_groups=groups)
        user = db.get_user("dave")
        assert user["allowed_tenant_groups"] == groups

    def test_list_users_includes_role(self):
        db.create_user("admin1", "password123", is_admin=1)
        db.create_user("op1", "password123")
        users = db.list_users()
        roles = {u["username"]: u["role"] for u in users}
        assert roles["admin1"] == "super_admin"
        assert roles["op1"] == "operator"


# ---------------------------------------------------------------------------
# Route-level RBAC tests
# ---------------------------------------------------------------------------

class TestRBACRoutes:
    """Test RBAC enforcement on routes."""

    def test_admin_required_allows_super_admin(self, authed_client):
        """Admin user (super_admin) can access admin-required pages."""
        resp = authed_client.get("/users")
        assert resp.status_code == 200

    def test_set_role_endpoint(self, authed_client):
        """Admin can set another user's role."""
        db.create_user("testop", "password123")
        resp = authed_client.post("/users/testop/set-role",
                                  data={"role": "admin"})
        data = resp.get_json()
        assert data["ok"]
        user = db.get_user("testop")
        assert user["role"] == "admin"

    def test_set_role_invalid_role(self, authed_client):
        db.create_user("testop2", "password123")
        resp = authed_client.post("/users/testop2/set-role",
                                  data={"role": "nonexistent"})
        data = resp.get_json()
        assert not data["ok"]

    def test_set_role_cannot_change_self(self, authed_client):
        resp = authed_client.post("/users/admin/set-role",
                                  data={"role": "viewer"})
        data = resp.get_json()
        assert not data["ok"]
        assert "own role" in data["error"].lower()

    def test_set_groups_endpoint(self, authed_client):
        db.create_user("groupuser", "password123")
        groups = json.dumps(["g1", "g2"])
        resp = authed_client.post("/users/groupuser/set-groups",
                                  data={"groups": groups})
        data = resp.get_json()
        assert data["ok"]
        user = db.get_user("groupuser")
        assert json.loads(user["allowed_tenant_groups"]) == ["g1", "g2"]

    def test_set_groups_invalid_json(self, authed_client):
        db.create_user("groupuser2", "password123")
        resp = authed_client.post("/users/groupuser2/set-groups",
                                  data={"groups": "not-json"})
        data = resp.get_json()
        assert not data["ok"]

    def test_create_user_with_role(self, authed_client):
        resp = authed_client.post("/users/new", data={
            "username": "newviewer",
            "password": "password123",
            "display_name": "New Viewer",
            "role": "viewer",
        })
        data = resp.get_json()
        assert data["ok"]
        user = db.get_user("newviewer")
        assert user["role"] == "viewer"

    def test_unauthenticated_set_role_redirects(self, client):
        resp = client.post("/users/someone/set-role", data={"role": "admin"})
        assert resp.status_code == 302

    def test_viewer_role_created_via_api(self, authed_client):
        resp = authed_client.post("/users/new", data={
            "username": "v1",
            "password": "password123",
            "role": "viewer",
        })
        assert resp.get_json()["ok"]
        user = db.get_user("v1")
        assert user["role"] == "viewer"
        assert user["is_admin"] == 0
