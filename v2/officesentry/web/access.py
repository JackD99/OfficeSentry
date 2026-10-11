"""Authentication and authorization. Deny by default.

* Every view needs ``@login_required`` or ``@roles_required(...)``; there is
  no anonymous access except login, first-run setup, /health and the MSP's
  brand colours and logo (``/brand.css``, ``/brand/logo``) for the sign-in page,
  and the thank-you page a client's administrator reaches through a signed
  consent link (``/consent/callback``, see ``onboarding.client_returned``).
  On the public demo site (``public_demo.py``) every visitor is the read-only
  demo user, which is never an admin.
* Any view that touches a tenant must load it through ``tenant_or_404()``,
  which returns 404 for tenants the user may not see (no existence leak).

Roles:
  admin    everything, including users and app connections
  analyst  view and refresh tenants they are granted (or all, if all_tenants), one by one or
           through a tenant group (every tenant in the group, including ones added later)
  client   view their granted tenants only (never through a group)

Grants are read here and nowhere else: ``visible_tenants`` and ``can_see_tenant``
share ``_GRANTED``. Tenant groups otherwise only narrow lists of visible tenants.
"""

from __future__ import annotations

from functools import wraps
from urllib.parse import urlsplit

from flask import abort, g, redirect, request, url_for

from ..db import audit_counted
from . import public_demo, sessions

ROLES = ("admin", "analyst", "client")


def load_user() -> None:
    # Sessions live server-side (see sessions.py): a deleted or disabled user, a
    # password, role or two-step change, sign-out and idle time all end them.
    g.session_row, g.user = sessions.current()
    public_demo.sign_in_visitor()


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
                refused()
                abort(403)
            return view(*args, **kwargs)

        return wrapper

    return decorator


def sees_all_tenants(user) -> bool:
    return user["role"] == "admin" or (user["role"] == "analyst" and bool(user["all_tenants"]))


# Tenant ids granted to user :uid. Analysts also get every tenant in a group they were granted;
# clients never do, so a tenant added to a group can't reach a client account by accident.
_GRANTED = (
    "SELECT tenant_id FROM user_tenants WHERE user_id = :uid"
    " UNION SELECT m.tenant_id FROM user_tenant_groups ug"
    " JOIN tenant_group_members m ON m.group_id = ug.group_id"
    " WHERE ug.user_id = :uid AND :role = 'analyst'"
)


def visible_tenants(conn, user, archived: bool = False):
    """Tenants the user may see. Archived tenants are left out (only admins list them, with ``archived``)."""
    where = "" if archived and user["role"] == "admin" else " AND t.archived_at IS NULL"
    if sees_all_tenants(user):
        return conn.execute(f"SELECT t.* FROM tenants t WHERE 1 = 1{where} ORDER BY t.name COLLATE NOCASE").fetchall()
    return conn.execute(
        f"SELECT t.* FROM tenants t WHERE t.id IN ({_GRANTED}){where} ORDER BY t.name COLLATE NOCASE",
        {"uid": user["id"], "role": user["role"]},
    ).fetchall()


def can_see_tenant(conn, user, tenant_id: int) -> bool:
    if user is None:
        return False
    if sees_all_tenants(user):
        return True
    return (
        conn.execute(
            f"SELECT 1 WHERE :tid IN ({_GRANTED})", {"uid": user["id"], "role": user["role"], "tid": tenant_id}
        ).fetchone()
        is not None
    )


def tenant_or_404(tenant_id: int):
    """The tenant, if this user may see it. An archived tenant is only there for admins (to restore or delete)."""
    tenant = g.db.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    if tenant is None or not can_see_tenant(g.db, g.user, tenant_id):
        refused(f"tenant {tenant_id}")
        abort(404)
    if tenant["archived_at"] and g.user["role"] != "admin":
        refused(f"tenant {tenant_id} (archived)")
        abort(404)
    return tenant


def refused(what: str = "") -> None:
    """Count a request the signed-in person wasn't allowed (a tenant they weren't granted, a staff page) in
    the activity log: one ``access_refused`` row per person, address and hour, naming the latest route.
    A stale bookmark can't flood the log, and a client probing other clients' tenants is visible to admins.
    The public demo's shared visitor is refused plenty by design and isn't recorded."""
    user = g.get("user")
    if user is None or "db" not in g or g.get("public_demo") or g.get("refusal_counted"):
        return
    g.refusal_counted = True  # once per request, however many layers refuse it
    detail = f"{request.method} {request.path}" + (f" ({what})" if what else "")
    audit_counted(g.db, "access_refused", (request.remote_addr or "")[:64], user["username"], detail)


def safe_next(target: str | None, fallback: str | None = None) -> str:
    """Only allow same-site, path-only redirects (after sign-in, and back to the page a form came from).

    Browsers drop tabs and newlines from URLs and treat a backslash as a slash,
    so "/\t/evil.example" or "/\\evil.example" would leave the site; any control
    character, backslash, scheme or host is refused, and ``fallback`` (the
    dashboard by default) is used instead.
    """
    fallback = fallback or url_for("tenants.dashboard")
    if not target or len(target) > 2000 or not target.startswith("/") or target.startswith("//"):
        return fallback
    if "\\" in target or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in target):
        return fallback
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return fallback
    return target
