"""Roles in the database, and what one person can do once their role and overrides are added up.

See ``permissions.py`` for the capabilities and the built-in roles. Only ``web/access.py`` reads
``Access`` to decide anything; the role pages (``web/roles.py``) and the user page write here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import permissions as p
from . import sso
from .db import audit, utcnow


@dataclass(frozen=True)
class Role:
    id: int
    key: str | None  # built-in roles have one
    name: str
    description: str
    kind: str
    scope: str
    capabilities: frozenset[str]

    @property
    def built_in(self) -> bool:
        return self.key is not None

    @property
    def editable(self) -> bool:
        """Administrator is fixed; everything else can be changed."""
        return self.kind != p.ADMIN

    @property
    def template(self) -> p.Template | None:
        return p.TEMPLATE_BY_KEY.get(self.key or "")

    @property
    def changed_from_template(self) -> bool:
        t = self.template
        return t is not None and (t.capabilities != self.capabilities or t.scope != self.scope)

    @property
    def column(self) -> str:
        """What ``users.role`` says for someone with this role."""
        return p.ROLE_COLUMN[self.kind]


def _row(r) -> Role:
    try:
        caps = json.loads(r["capabilities"] or "[]")
    except ValueError:
        caps = []
    return Role(
        r["id"],
        r["key"],
        r["name"],
        r["description"] or "",
        r["kind"],
        r["scope"] if r["kind"] != p.CLIENT else p.GRANTED,
        frozenset(p.allowed_for(r["kind"], caps if isinstance(caps, list) else [])),
    )


def all_roles(conn) -> list[Role]:
    """Every role: the built-in ones first in their own order, then custom ones by name."""
    rows = conn.execute("SELECT * FROM roles").fetchall()
    order = {t.key: i for i, t in enumerate(p.TEMPLATES)}
    return sorted((_row(r) for r in rows), key=lambda r: (order.get(r.key, len(order)), r.name.lower()))


def get(conn, role_id: int | None) -> Role | None:
    if role_id is None:
        return None
    r = conn.execute("SELECT * FROM roles WHERE id = ?", (role_id,)).fetchone()
    return _row(r) if r else None


def by_key(conn, key: str) -> Role | None:
    r = conn.execute("SELECT * FROM roles WHERE key = ?", (key,)).fetchone()
    return _row(r) if r else None


def _fallback(key: str) -> Role:
    """The built-in role as code knows it, for a database whose row is missing (never in practice)."""
    t = p.TEMPLATE_BY_KEY[key]
    return Role(0, t.key, t.name, t.description, t.kind, t.scope, t.capabilities)


def role_of(conn, user) -> Role:
    """The person's role. ``users.role_id`` when it's set and agrees with the kind ``users.role`` says;
    otherwise the built-in role for ``users.role`` (an account older code made or changed)."""
    column = user["role"]
    try:
        role = get(conn, user["role_id"])
    except (KeyError, IndexError):
        role = None
    if role is not None and role.column == column:
        return role
    key = p.UPGRADE_ROLE.get(column, "client_viewer")
    return by_key(conn, key) or _fallback(key)


def members(conn) -> dict[int, int]:
    """How many enabled accounts hold each role, as ``role_of`` reads it (an account older code made, with no
    ``role_id``, counts for the built-in role its ``users.role`` stands for)."""
    counts: dict[int, int] = {}
    for user in conn.execute("SELECT * FROM users WHERE disabled = 0"):
        role = role_of(conn, user)
        if role.id:  # 0: a built-in role whose row is missing
            counts[role.id] = counts.get(role.id, 0) + 1
    return counts


def assign(conn, user_id: int, role: Role) -> bool:
    """Give an account this role (and the matching ``users.role``). Returns whether anything changed; a
    change signs them out everywhere, since what they may see changed."""
    row = conn.execute("SELECT role, role_id FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None:
        return False
    changed = row["role_id"] != role.id or row["role"] != role.column
    conn.execute(
        "UPDATE users SET role_id = ?, role = ?, session_version = session_version + ? WHERE id = ?",
        (role.id, role.column, int(changed), user_id),
    )
    return changed


def _check(name: str, kind: str, scope: str, capabilities) -> tuple[str, str, list[str]]:
    name = (name or "").strip()
    if not name or len(name) > 60:
        raise ValueError("Give the role a name of up to 60 characters.")
    if kind not in (p.STAFF, p.CLIENT):
        raise ValueError("A role is for staff or for a client's people.")
    scope = scope if kind == p.STAFF and scope in p.SCOPES else p.GRANTED
    return name, scope, sorted(p.allowed_for(kind, capabilities))


def create(conn, actor: str, name: str, description: str, kind: str, scope: str, capabilities) -> int:
    """A custom role. Raises ``ValueError`` with words for the page."""
    name, scope, caps = _check(name, kind, scope, capabilities)
    if conn.execute("SELECT 1 FROM roles WHERE name = ?", (name,)).fetchone():
        raise ValueError("There's already a role with that name.")
    cur = conn.execute(
        "INSERT INTO roles (key, name, description, kind, scope, capabilities, created_at)"
        " VALUES (NULL, ?, ?, ?, ?, ?, ?)",
        (name, (description or "").strip()[:300], kind, scope, json.dumps(caps), utcnow()),
    )
    audit(conn, actor, "role_created", detail=f"{name} ({kind}, {scope}): {', '.join(caps) or 'nothing'}")
    return cur.lastrowid


def update(conn, actor: str, role: Role, name: str, description: str, scope: str, capabilities) -> bool:
    """Change a role. Administrator can't be changed. Everyone holding the role is signed out, since what
    they may see changed. Raises ``ValueError`` with words for the page."""
    if not role.editable:
        raise ValueError("The Administrator role can't be changed.")
    name, scope, caps = _check(name, role.kind, scope, capabilities)
    other = conn.execute("SELECT 1 FROM roles WHERE name = ? AND id <> ?", (name, role.id)).fetchone()
    if other:
        raise ValueError("There's already a role with that name.")
    before = (role.name, role.description, role.scope, sorted(role.capabilities))
    after = (name, (description or "").strip()[:300], scope, caps)
    if before == after:
        return False
    conn.execute(
        "UPDATE roles SET name = ?, description = ?, scope = ?, capabilities = ?, updated_at = ? WHERE id = ?",
        (name, after[1], scope, json.dumps(caps), utcnow(), role.id),
    )
    conn.execute("UPDATE users SET session_version = session_version + 1 WHERE role_id = ?", (role.id,))
    added = sorted(set(caps) - role.capabilities)
    removed = sorted(role.capabilities - set(caps))
    bits = []
    if name != role.name:
        bits.append(f"renamed from {role.name}")
    if scope != role.scope:
        bits.append(f"scope {scope}")
    if added:
        bits.append("added " + ", ".join(added))
    if removed:
        bits.append("removed " + ", ".join(removed))
    audit(conn, actor, "role_updated", detail=f"{name}: {'; '.join(bits) or 'description'}")
    return True


def reset(conn, actor: str, role: Role) -> bool:
    """A built-in role back to what it ships with."""
    t = role.template
    if t is None or not role.editable:
        return False
    return update(conn, actor, role, t.name, t.description, t.scope, t.capabilities)


def remove(conn, actor: str, role: Role) -> None:
    """Remove a custom role nobody holds. Raises ``ValueError`` otherwise."""
    if role.built_in:
        raise ValueError("Built-in roles can't be deleted; change them instead.")
    holders = conn.execute("SELECT COUNT(*) FROM users WHERE role_id = ?", (role.id,)).fetchone()[0]
    if holders:
        noun = "account holds" if holders == 1 else "accounts hold"
        raise ValueError(f"{holders} {noun} this role. Move them to another role first.")
    conn.execute("DELETE FROM roles WHERE id = ?", (role.id,))
    sso.forget_role(conn, role.id)  # so an app role value in Entra never reaches a role made later with its id
    audit(conn, actor, "role_deleted", detail=role.name)


# -- Per-person overrides -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Override:
    capability: str
    tenant_id: int | None  # None: everywhere
    allow: bool


def overrides(conn, user_id: int) -> list[Override]:
    rows = conn.execute(
        "SELECT capability, tenant_id, allow FROM user_access WHERE user_id = ? ORDER BY capability, tenant_id",
        (user_id,),
    ).fetchall()
    return [Override(r["capability"], r["tenant_id"], bool(r["allow"])) for r in rows if r["capability"] in p.BY_KEY]


def set_overrides(conn, actor: str, user, items: list[Override]) -> bool:
    """Replace the person's overrides. A client role only ever gets report capabilities. Returns whether
    anything changed; a change signs them out everywhere."""
    kind = p.KIND_OF_COLUMN.get(user["role"], p.CLIENT)
    allowed = p.allowed_for(kind, p.KEYS)
    wanted = {}
    for o in items:
        if o.capability in allowed:
            wanted[(o.capability, o.tenant_id)] = o.allow
    before = {(o.capability, o.tenant_id): o.allow for o in overrides(conn, user["id"])}
    if before == wanted:
        return False
    now = utcnow()
    conn.execute("DELETE FROM user_access WHERE user_id = ?", (user["id"],))
    conn.executemany(
        "INSERT INTO user_access (user_id, capability, tenant_id, allow, created_at, created_by)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [(user["id"], cap, tid, int(allow), now, actor) for (cap, tid), allow in sorted(wanted.items(), key=str)],
    )
    conn.execute("UPDATE users SET session_version = session_version + 1 WHERE id = ?", (user["id"],))
    words = [
        f"{'+' if allow else '-'}{cap}" + (f"@{tid}" if tid else "")
        for (cap, tid), allow in sorted(wanted.items(), key=str)
    ]
    audit(conn, actor, "user_access_changed", detail=f"{user['username']}: {', '.join(words) or 'none'}")
    return True


# -- Effective access ---------------------------------------------------------------------------------


@dataclass
class Access:
    """What one person may do, worked out once per request from their role and overrides."""

    user: object
    role: Role
    kind: str
    scope: str  # permissions.ALL_CLIENTS or GRANTED
    capabilities: frozenset[str]  # the role's, plus everywhere-allows, minus everywhere-denies
    allows: dict[int, frozenset[str]] = field(default_factory=dict)  # per client
    denies: dict[int, frozenset[str]] = field(default_factory=dict)
    denied_everywhere: frozenset[str] = frozenset()

    @property
    def is_admin(self) -> bool:
        return self.kind == p.ADMIN

    @property
    def is_staff(self) -> bool:
        return self.kind in (p.ADMIN, p.STAFF)

    @property
    def sees_all(self) -> bool:
        return self.scope == p.ALL_CLIENTS

    def can(self, capability: str, tenant_id: int | None = None) -> bool:
        """May they, on this client (or, with no client, anywhere at all)?"""
        if self.is_admin:
            return True
        if capability in self.denied_everywhere:
            return False
        if tenant_id is None:
            return capability in self.capabilities or any(capability in s for s in self.allows.values())
        if capability in self.denies.get(tenant_id, ()):
            return False
        return capability in self.capabilities or capability in self.allows.get(tenant_id, ())

    def can_all(self, capabilities, tenant_id: int | None = None) -> bool:
        return all(self.can(c, tenant_id) for c in capabilities)

    def anywhere(self) -> frozenset[str]:
        """Every capability they hold somewhere, for menus."""
        if self.is_admin:
            return frozenset(p.KEYS)
        extra = frozenset().union(*self.allows.values()) if self.allows else frozenset()
        return (self.capabilities | extra) - self.denied_everywhere

    def everywhere(self) -> frozenset[str]:
        """Every capability they hold on every client they see: ``capabilities`` less anything taken away on one
        client. What a user manager may hand out, and the most an account they change may hold."""
        if self.is_admin:
            return frozenset(p.KEYS)
        taken = frozenset().union(*self.denies.values()) if self.denies else frozenset()
        return self.capabilities - taken


def access_for(conn, user) -> Access:
    role = role_of(conn, user)
    kind = role.kind
    if kind == p.ADMIN:
        return Access(user, role, kind, p.ALL_CLIENTS, frozenset(p.KEYS))
    try:
        all_flag = bool(user["all_tenants"])
    except (KeyError, IndexError):
        all_flag = False
    scope = p.ALL_CLIENTS if kind == p.STAFF and (role.scope == p.ALL_CLIENTS or all_flag) else p.GRANTED
    allowed = p.allowed_for(kind, p.KEYS)
    caps = set(role.capabilities)
    allows: dict[int, set[str]] = {}
    denies: dict[int, set[str]] = {}
    denied_everywhere: set[str] = set()
    for o in overrides(conn, user["id"]):
        if o.capability not in allowed:
            continue
        if o.tenant_id is None:
            if o.allow:
                caps.add(o.capability)
            else:
                denied_everywhere.add(o.capability)
        else:
            (allows if o.allow else denies).setdefault(o.tenant_id, set()).add(o.capability)
    caps -= denied_everywhere
    return Access(
        user,
        role,
        kind,
        scope,
        frozenset(caps),
        {k: frozenset(v) for k, v in allows.items()},
        {k: frozenset(v) for k, v in denies.items()},
        frozenset(denied_everywhere),
    )
