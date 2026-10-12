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
A client account that signs in with a client's own Microsoft account sees only clients in that
organisation (``_organisation``), whatever it was granted.

``roles_required`` is kept for code written before capabilities: ``"admin"`` alone means
``needs("settings.manage")`` and ``("admin", "analyst")`` means any staff role. New code names a capability.
"""

from __future__ import annotations

from functools import wraps
from urllib.parse import urlsplit

from flask import abort, g, has_request_context, redirect, request, url_for

from .. import permissions, roles, sso
from ..db import audit_counted
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
            return redirect(url_for("auth.login", next=_return_to()))
        return view(*args, **kwargs)

    return wrapper


def _return_to() -> str | None:
    """Where sign-in brings the person back to: this page, or for a form posted after the session ended
    (whose address only answers POST, so a GET of it would be a 405), the page the form was on, if the
    browser said which. None opens their home page."""
    if request.method in ("GET", "HEAD"):
        return request.full_path.rstrip("?")
    ref = urlsplit(request.referrer or "")
    if not ref.path or (ref.netloc and ref.netloc != request.host):
        return None
    return ref.path + (f"?{ref.query}" if ref.query else "")


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
            if (kind == "staff" and not a.is_staff) or (kind == "admin" and not a.is_admin):
                refused()
                abort(403)
            g.needed = capabilities
            for cap in capabilities:
                if not a.can(cap):
                    refused()
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


def _organisation(user, a: roles.Access) -> str | None:
    """The Microsoft organisation (tenant ID) a client's person signs in from, or None. Such an account only
    ever sees clients in that organisation (``sso.directory_id``), whatever it was granted, so a grant made
    by mistake never shows one company's people another company's data."""
    return user["entra_tid"] if a.kind == permissions.CLIENT and user["entra_tid"] else None


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
    if (tid := _organisation(user, a)) is not None:
        rows = [t for t in rows if sso.directory_id(conn, t) == tid]
    return _needed_here(a, rows)


def tenant_ids(conn, user) -> set[int] | None:
    """The ids of the tenants this person's scope covers, whatever the current view needs (``None``: every
    client). For comparing two people's reach; pages list tenants with ``visible_tenants``. A grant their
    organisation hides from them now (``_organisation``) still counts: it would show again if they were
    disconnected from Microsoft."""
    a = access_of(user, conn)
    return None if a.sees_all else {r[0] for r in conn.execute(_GRANTED, _params(user, a))}


def unseen_client_accounts(conn, user) -> dict[int, str]:
    """Client accounts (id -> username) whose every client is outside this person's scope. Their names are the
    client's own people (jane@bravo.example), so pages that list accounts leave them out for whoever doesn't
    see every client."""
    mine = tenant_ids(conn, user)
    if mine is None:
        return {}
    grants: dict[int, set[int]] = {}
    names: dict[int, str] = {}
    for row in conn.execute(
        "SELECT u.id, u.username, ut.tenant_id FROM users u JOIN user_tenants ut ON ut.user_id = u.id"
        " WHERE u.role = 'client'"
    ):
        grants.setdefault(row["id"], set()).add(row["tenant_id"])
        names[row["id"]] = row["username"]
    return {uid: names[uid] for uid, tids in grants.items() if not tids & mine}


def can_see_tenant(conn, user, tenant_id: int) -> bool:
    if user is None:
        return False
    a = access_of(user, conn)
    if (tid := _organisation(user, a)) is not None:
        tenant = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
        if tenant is None or sso.directory_id(conn, tenant) != tid:
            return False
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
        refused(f"tenant {tenant_id}")
        abort(404)
    a = current()
    if tenant["archived_at"] and not a.can("clients.manage"):
        refused(f"tenant {tenant_id} (archived)")
        abort(404)
    needed = g.get("needed") or ()
    if not a.can_all(needed, tenant_id):
        refused(f"tenant {tenant_id}")
        abort(403)
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
