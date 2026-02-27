"""
portal/permissions.py — Role-Based Access Control (RBAC) for Office Sentry.

Roles (hierarchical):
  super_admin — Full access including user management, settings, all tenants, all tiers
  admin       — Access to assigned tenant groups, all audits, Tier 1-2 operations
  operator    — Access to assigned tenant groups, all audits, Tier 1 operations only
  viewer      — Read-only access to assigned tenant groups, no audits, no operations
  client      — External client, read-only access to a single tenant, self-service downloads
"""

import json

ROLE_HIERARCHY = {
    "super_admin": 4,
    "admin": 3,
    "operator": 2,
    "viewer": 1,
    "client": 0,
}

ROLE_LABELS = {
    "super_admin": "Super Admin",
    "admin": "Admin",
    "operator": "Operator",
    "viewer": "Viewer",
    "client": "Client",
}


def has_role(user: dict, min_role: str) -> bool:
    """Check if a user meets or exceeds the minimum role level."""
    user_level = ROLE_HIERARCHY.get(user.get("role", "viewer"), 0)
    required_level = ROLE_HIERARCHY.get(min_role, 99)
    return user_level >= required_level


def get_user_groups(user: dict) -> list[str]:
    """Return list of tenant group IDs the user is allowed to access."""
    raw = user.get("allowed_tenant_groups", "[]")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    return raw if isinstance(raw, list) else []


def can_access_tenant(user: dict, tenant_id: str) -> bool:
    """Check if a user can access a specific tenant.

    super_admin can access all tenants.
    Others can only access tenants belonging to their allowed groups.
    If allowed_tenant_groups is empty, user can access all tenants (no restriction).
    """
    if user.get("role") == "super_admin":
        return True

    groups = get_user_groups(user)
    if not groups:
        # No group restriction — user can access all tenants
        return True

    # Check if tenant belongs to any of the user's allowed groups
    from database import get_db
    with get_db() as conn:
        placeholders = ",".join("?" * len(groups))
        row = conn.execute(
            f"SELECT 1 FROM tenant_group_members "
            f"WHERE tenant_id = ? AND group_id IN ({placeholders}) LIMIT 1",
            [tenant_id] + groups,
        ).fetchone()
        return row is not None


def can_run_operation(user: dict, tier: int) -> bool:
    """Check if a user can run an operation of the given tier.

    super_admin: all tiers (1, 2, 3)
    admin:       tier <= 2
    operator:    tier <= 1
    viewer:      none
    """
    role = user.get("role", "viewer")
    if role == "super_admin":
        return True
    if role == "admin":
        return tier <= 2
    if role == "operator":
        return tier <= 1
    return False


def can_run_audit(user: dict) -> bool:
    """Check if a user can launch audits. Viewers cannot."""
    return has_role(user, "operator")


def filter_tenants_for_user(user: dict, tenants: list[dict]) -> list[dict]:
    """Filter a tenant list to only those the user can access."""
    if user.get("role") == "super_admin":
        return tenants

    groups = get_user_groups(user)
    if not groups:
        return tenants

    from database import get_db
    with get_db() as conn:
        placeholders = ",".join("?" * len(groups))
        rows = conn.execute(
            f"SELECT DISTINCT tenant_id FROM tenant_group_members "
            f"WHERE group_id IN ({placeholders})",
            groups,
        ).fetchall()
        allowed_ids = {r["tenant_id"] for r in rows}

    return [t for t in tenants if t["id"] in allowed_ids]


def is_client_user(user: dict) -> bool:
    """Check if a user has the client role."""
    return user.get("role") == "client"


def get_client_tenant_id(user: dict) -> str | None:
    """Get the single tenant ID a client user is scoped to.

    Client users have exactly one group containing one tenant.
    Returns the tenant_id or None if not resolvable.
    """
    if not is_client_user(user):
        return None
    groups = get_user_groups(user)
    if not groups:
        return None
    from database import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT tenant_id FROM tenant_group_members WHERE group_id = ? LIMIT 1",
            (groups[0],),
        ).fetchone()
        return row["tenant_id"] if row else None
