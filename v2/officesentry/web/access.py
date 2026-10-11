"""Authentication and authorization. Deny by default.

* Every view needs ``@needs(...)`` (or plain ``@login_required`` for pages anyone signed in may open, such
  as their own account); there is no anonymous access except login, first-run setup, /health and the MSP's
  brand colours and logo (``/brand.css``, ``/brand/logo``) for the sign-in page, and the thank-you page a
  client's administrator reaches through a signed consent link (``/consent/callback``, see
  ``onboarding.client_returned``). On the public demo site (``public_demo.py``) every visitor is the
  read-only demo user, which is never an admin.
* Any view that touches a tenant must load it through ``tenant_or_404()``, which returns 404 for tenants
  the user may not see (no existence leak) and 403 when a capability the view declared is taken away from
  them on that one client.

Access is a role (``roles.py``: a set of capabilities, ``permissions.py``, and a scope) plus per-person
overrides, worked out once per request (``current()``). ``@needs("reports.email", "export")`` says what a
view needs; ``can()`` answers the same question inside views and templates; ``require()`` aborts with 403.

Scope (which tenants) is read here and nowhere else: ``visible_tenants`` and ``can_see_tenant`` share
``_GRANTED``. A role with scope "all", an admin, or a staff account with ``all_tenants`` sees every tenant;
otherwise the tenants granted directly, and for staff the tenants of every group they were granted (a
client never gets tenants through a group). Tenant groups otherwise only narrow lists of visible tenants.

``roles_required`` is kept for code written before capabilities: ``"admin"`` alone means
``needs("settings.manage")`` and ``("admin", "analyst")`` means any staff role. New code names a capability.
"""

from __future__ import annotations

from functools import wraps
from urllib.parse import urlsplit

from flask import abort, g, has_request_context, redirect, request, url_for

from .. import permissions, roles
from . import public_demo, sessions

ROLES = ("admin", "analyst", "client")  # what users.role may say (permissions.ROLE_COLUMN)
STAFF_KINDS = (permissions.ADMIN, permissions.STAFF)


def load_user() -> None:
    # Sessions live server-side (see sessions.py): a deleted or disabled user, a
    # password, role or two-step change, sign-out and idle time all end them.
    g.session_row, g.user = sessions.current()
    public_demo.sign_in_visitor()


def current() -> roles.Access | None:
    """The signed-in person's effective access, worked out once per request."""
    user = g.get("user")
    if user is None:
        return None
    if "access" not in g or g.access.user is not user:
        g.access = roles.access_for(g.db, user)
    return g.access


def can(capability: str, tenant_id: int | None = None) -> bool:
    """May the signed-in person do this, on this tenant (or anywhere, with none)? For views and templates."""
    a = current()
    return a is not None and a.can(capability, tenant_id)


def require(*capabilities: str, tenant_id: int | None = None) -> None:
    """Abort with 403 unless the signed-in person holds every one of these (here, or anywhere)."""
    a = current()
    if a is None:
        abort(403)
    for cap in capabilities:
        if cap not in permissions.BY_KEY:
            raise ValueError(f"unknown capability {cap!r}")
        if not a.can(cap, tenant_id):
            abort(403)


def need_also(*capabilities: str, tenant_id: int | None = None) -> None:
    """A view that only knows what it needs once it has read its URL (which report) says so here: 403
    unless the person holds it, and from now on the tenants listed leave out any client where it was
    taken away (so the group scope worked out before, if any, is dropped and read again)."""
    require(*capabilities, tenant_id=tenant_id)
    g.needed = (*g.get("needed", ()), *capabilities)
    g.pop("group_scope", None)
    g.pop("scope_tenants", None)


def is_staff() -> bool:
    a = current()
    return a is not None and a.is_staff


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if g.get("user") is None:
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapper


def needs(*capabilities: str, kind: str | None = None):
    """The view needs these capabilities (somewhere; ``tenant_or_404`` then checks the tenant it opens) and,
    with ``kind="staff"``, a staff or admin role. The view function keeps them as ``needs`` and ``kind``,
    so tests can walk every route as every role."""
    for cap in capabilities:
        if cap not in permissions.BY_KEY:
            raise ValueError(f"unknown capability {cap!r}")
    if kind not in (None, "staff", "admin"):
        raise ValueError(f"unknown kind {kind!r}")

    def decorator(view):
        @wraps(view)
        @login_required
        def wrapper(*args, **kwargs):
            a = current()
            if kind == "staff" and not a.is_staff:
                abort(403)
            if kind == "admin" and not a.is_admin:
                abort(403)
            g.needed = capabilities
            for cap in capabilities:
                if not a.can(cap):
                    abort(403)
            return view(*args, **kwargs)

        wrapper.needs = capabilities
        wrapper.kind = kind
        return wrapper

    return decorator


def roles_required(*names: str):
    """Before capabilities, views named roles. Kept so older code keeps working: see the module docstring."""
    for name in names:
        if name not in ROLES:
            raise ValueError(f"unknown role {name!r}")
    if "client" in names:
        return needs()
    if "analyst" in names:
        return needs(kind="staff")
    return needs("settings.manage")


def access_of(user, conn=None) -> roles.Access:
    """``current()`` for the signed-in person, or worked out afresh for anyone else (a user being edited,
    someone who just signed in, a test outside a request)."""
    if has_request_context() and user is g.get("user"):
        return current()
    return roles.access_for(conn if conn is not None else g.db, user)


def sees_all_tenants(user, conn=None) -> bool:
    return access_of(user, conn).sees_all


# Tenant ids granted to user :uid. Staff also get every tenant in a group they were granted;
# clients never do, so a tenant added to a group can't reach a client account by accident.
_GRANTED = (
    "SELECT tenant_id FROM user_tenants WHERE user_id = :uid"
    " UNION SELECT m.tenant_id FROM user_tenant_groups ug"
    " JOIN tenant_group_members m ON m.group_id = ug.group_id"
    " WHERE ug.user_id = :uid AND :kind <> 'client'"
)


def _params(user, a: roles.Access) -> dict:
    return {"uid": user["id"], "kind": a.kind}


def _needed_here(a: roles.Access, rows: list) -> list:
    """Leave out tenants where a capability the view declared was taken away from this person."""
    needed = g.get("needed") if has_request_context() else None
    if not needed or a.is_admin or not (a.denies or a.allows):
        return rows
    return [t for t in rows if a.can_all(needed, t["id"])]


def visible_tenants(conn, user, archived: bool = False):
    """Tenants the user may see, within what the current view needs. Archived tenants are left out (only
    someone who manages clients lists them, with ``archived``)."""
    a = access_of(user, conn)
    where = "" if archived and a.can("clients.manage") else " AND t.archived_at IS NULL"
    if a.sees_all:
        rows = conn.execute(f"SELECT t.* FROM tenants t WHERE 1 = 1{where} ORDER BY t.name COLLATE NOCASE").fetchall()
    else:
        rows = conn.execute(
            f"SELECT t.* FROM tenants t WHERE t.id IN ({_GRANTED}){where} ORDER BY t.name COLLATE NOCASE",
            _params(user, a),
        ).fetchall()
    return _needed_here(a, rows)


def can_see_tenant(conn, user, tenant_id: int) -> bool:
    if user is None:
        return False
    a = access_of(user, conn)
    if a.sees_all:
        return True
    return (
        conn.execute(f"SELECT 1 WHERE :tid IN ({_GRANTED})", {**_params(user, a), "tid": tenant_id}).fetchone()
        is not None
    )


def tenant_or_404(tenant_id: int):
    """The tenant, if this user may see it. An archived tenant is only there for someone who manages
    clients (to restore or delete). 403 when a capability the view needs is taken away on this client."""
    tenant = g.db.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    if tenant is None or not can_see_tenant(g.db, g.user, tenant_id):
        abort(404)
    a = current()
    if tenant["archived_at"] and not a.can("clients.manage"):
        abort(404)
    needed = g.get("needed") or ()
    if not a.can_all(needed, tenant_id):
        abort(403)
    return tenant


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
