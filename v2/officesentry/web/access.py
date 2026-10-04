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
from urllib.parse import urlsplit

from flask import abort, g, redirect, request, url_for

from . import sessions

ROLES = ("admin", "analyst", "client")


def load_user() -> None:
    # Sessions live server-side (see sessions.py): a deleted or disabled user, a
    # password, role or two-step change, sign-out and idle time all end them.
    g.session_row, g.user = sessions.current()


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


def visible_tenants(conn, user, archived: bool = False):
    """Tenants the user may see. Archived tenants are left out (only admins list them, with ``archived``)."""
    where = "" if archived and user["role"] == "admin" else " AND t.archived_at IS NULL"
    if sees_all_tenants(user):
        return conn.execute(f"SELECT t.* FROM tenants t WHERE 1 = 1{where} ORDER BY t.name COLLATE NOCASE").fetchall()
    return conn.execute(
        "SELECT t.* FROM tenants t JOIN user_tenants ut ON ut.tenant_id = t.id"
        f" WHERE ut.user_id = ?{where} ORDER BY t.name COLLATE NOCASE",
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
    """The tenant, if this user may see it. An archived tenant is only there for admins (to restore or delete)."""
    tenant = g.db.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    if tenant is None or not can_see_tenant(g.db, g.user, tenant_id):
        abort(404)
    if tenant["archived_at"] and g.user["role"] != "admin":
        abort(404)
    return tenant


def safe_next(target: str | None) -> str:
    """Only allow same-site, path-only redirects after login.

    Browsers drop tabs and newlines from URLs and treat a backslash as a slash,
    so "/\t/evil.example" or "/\\evil.example" would leave the site; any control
    character, backslash, scheme or host is refused.
    """
    fallback = url_for("tenants.dashboard")
    if not target or len(target) > 2000 or not target.startswith("/") or target.startswith("//"):
        return fallback
    if "\\" in target or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in target):
        return fallback
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return fallback
    return target
