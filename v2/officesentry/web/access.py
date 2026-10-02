"""Authentication and authorization. Deny by default.

* Every view needs ``@login_required`` or ``@roles_required(...)``; there is
  no anonymous access except login, first-run setup and /health.
* Any view that touches a tenant must load it through ``tenant_or_404()``,
  which returns 404 for tenants the user may not see (no existence leak).

Roles:
  admin    everything, including users and app connections
  analyst  view and refresh tenants they are granted (or all, if all_tenants)
  client   view their granted tenants only
"""

from __future__ import annotations

from functools import wraps

from flask import abort, g, redirect, request, session, url_for

ROLES = ("admin", "analyst", "client")


def load_user() -> None:
    g.user = None
    user_id = session.get("uid")
    if not user_id:
        return
    user = g.db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    # A deleted or disabled user, or a password change, ends existing sessions.
    if user is None or user["disabled"] or user["session_version"] != session.get("sv"):
        session.clear()
        return
    g.user = user


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.get("user") is None:
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)
    return wrapper


def roles_required(*roles: str):
    for role in roles:
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}")

    def decorator(view):
        @wraps(view)
        @login_required
        def wrapper(*args, **kwargs):
            if g.user["role"] not in roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapper
    return decorator


def sees_all_tenants(user) -> bool:
    return user["role"] == "admin" or (user["role"] == "analyst" and bool(user["all_tenants"]))


def visible_tenants(conn, user):
    if sees_all_tenants(user):
        return conn.execute("SELECT * FROM tenants ORDER BY name COLLATE NOCASE").fetchall()
    return conn.execute(
        "SELECT t.* FROM tenants t JOIN user_tenants ut ON ut.tenant_id = t.id"
        " WHERE ut.user_id = ? ORDER BY t.name COLLATE NOCASE",
        (user["id"],),
    ).fetchall()


def can_see_tenant(conn, user, tenant_id: int) -> bool:
    if user is None:
        return False
    if sees_all_tenants(user):
        return True
    return conn.execute(
        "SELECT 1 FROM user_tenants WHERE user_id = ? AND tenant_id = ?", (user["id"], tenant_id)
    ).fetchone() is not None


def tenant_or_404(tenant_id: int):
    tenant = g.db.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    if tenant is None or not can_see_tenant(g.db, g.user, tenant_id):
        abort(404)
    return tenant


def safe_next(target: str | None) -> str:
    """Only allow same-site relative redirects after login."""
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return url_for("tenants.dashboard")
    return target
