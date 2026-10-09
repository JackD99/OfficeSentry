"""Tenant groups: the MSP's own slices of its clients (tier, region, service level).

Schema in ``migrations/0029_tenant_groups.sql``. A tenant is in any number of
groups. Groups filter lists of tenants someone already sees (``only``) and
scope a digest; they never add tenants to a list. The one grant is an admin
giving an analyst a whole group (``user_tenant_groups``), which
``access.visible_tenants()`` reads; nothing else here decides access.
"""

from __future__ import annotations

from dataclasses import dataclass

from .db import utcnow

NAME_MAX = 80
CATEGORY_MAX = 40
DESCRIPTION_MAX = 300
# Offered when naming a category; any other text works too.
SUGGESTED_CATEGORIES = ("Tier", "Region", "Service level")


@dataclass
class Option:
    """A group as a picker shows it to one person: only the tenants they see are counted."""
    id: int
    name: str
    category: str
    tenants: int


def get(conn, group_id: int | None):
    if group_id is None:
        return None
    return conn.execute("SELECT * FROM tenant_groups WHERE id = ?", (group_id,)).fetchone()


def all_groups(conn) -> list:
    """Every group with its member count, by category then name (admin pages)."""
    return conn.execute(
        "SELECT g.*, (SELECT COUNT(*) FROM tenant_group_members m JOIN tenants t ON t.id = m.tenant_id"
        "             WHERE m.group_id = g.id AND t.archived_at IS NULL) AS tenants"
        " FROM tenant_groups g ORDER BY g.category = '', g.category COLLATE NOCASE, g.name COLLATE NOCASE"
    ).fetchall()


def members(conn, group_id: int) -> set[int]:
    return {r[0] for r in conn.execute("SELECT tenant_id FROM tenant_group_members WHERE group_id = ?",
                                       (group_id,))}


def only(conn, tenants, group_id: int | None) -> list:
    """``tenants`` (rows someone may see) narrowed to one group, in the same order. None keeps them all."""
    if group_id is None:
        return list(tenants)
    keep = members(conn, group_id)
    return [t for t in tenants if t["id"] in keep]


def options(conn, tenants) -> list[Option]:
    """The groups worth offering someone who sees ``tenants``: those holding at least one of them."""
    ids = {t["id"] for t in tenants}
    counts: dict[int, int] = {}
    for group_id, tenant_id in conn.execute("SELECT group_id, tenant_id FROM tenant_group_members"):
        if tenant_id in ids:
            counts[group_id] = counts.get(group_id, 0) + 1
    return [Option(g["id"], g["name"], g["category"], counts[g["id"]]) for g in all_groups(conn) if g["id"] in counts]


def by_category(opts: list[Option]) -> list[tuple[str, list[Option]]]:
    """Options under their category heading, uncategorised last (pickers use an optgroup per heading)."""
    out: dict[str, list[Option]] = {}
    for o in opts:
        out.setdefault(o.category, []).append(o)
    return sorted(out.items(), key=lambda kv: (kv[0] == "", kv[0].lower()))


def of_tenants(conn, tenant_ids) -> dict[int, list]:
    """Each tenant's groups (id, name, category), by name."""
    wanted = set(tenant_ids)
    out: dict[int, list] = {t: [] for t in wanted}
    for r in conn.execute("SELECT m.tenant_id, g.id, g.name, g.category FROM tenant_group_members m"
                          " JOIN tenant_groups g ON g.id = m.group_id ORDER BY g.name COLLATE NOCASE"):
        if r["tenant_id"] in wanted:
            out[r["tenant_id"]].append(r)
    return out


def clean(name: str, category: str, description: str) -> tuple[dict, str | None]:
    """Form values tidied, and what's wrong with them in plain words (or None)."""
    values = {"name": " ".join(name.split())[:NAME_MAX], "category": " ".join(category.split())[:CATEGORY_MAX],
              "description": description.strip()[:DESCRIPTION_MAX]}
    if not values["name"]:
        return values, "Give the group a name."
    return values, None


def name_taken(conn, name: str, except_id: int | None = None) -> bool:
    return conn.execute("SELECT 1 FROM tenant_groups WHERE name = ? AND id IS NOT ?", (name, except_id)).fetchone() \
        is not None


def create(conn, values: dict, user: str) -> int:
    return conn.execute(
        "INSERT INTO tenant_groups (name, category, description, created_by, created_at) VALUES (?, ?, ?, ?, ?)",
        (values["name"], values["category"], values["description"], user, utcnow())).lastrowid


def update(conn, group_id: int, values: dict) -> None:
    conn.execute("UPDATE tenant_groups SET name = ?, category = ?, description = ? WHERE id = ?",
                 (values["name"], values["category"], values["description"], group_id))


def set_members(conn, group_id: int, tenant_ids) -> tuple[int, int]:
    """Make the group hold exactly ``tenant_ids``. Returns (added, removed)."""
    before = members(conn, group_id)
    after = set(tenant_ids)
    conn.executemany("DELETE FROM tenant_group_members WHERE group_id = ? AND tenant_id = ?",
                     [(group_id, t) for t in before - after])
    conn.executemany("INSERT INTO tenant_group_members (group_id, tenant_id) VALUES (?, ?)",
                     [(group_id, t) for t in sorted(after - before)])
    return len(after - before), len(before - after)


def set_tenant_groups(conn, tenant_id: int, group_ids) -> tuple[int, int]:
    """Put one tenant in exactly ``group_ids``. Returns (added, removed)."""
    before = {r[0] for r in conn.execute("SELECT group_id FROM tenant_group_members WHERE tenant_id = ?",
                                         (tenant_id,))}
    valid = {r[0] for r in conn.execute("SELECT id FROM tenant_groups")}
    after = set(group_ids) & valid
    conn.executemany("DELETE FROM tenant_group_members WHERE group_id = ? AND tenant_id = ?",
                     [(g, tenant_id) for g in before - after])
    conn.executemany("INSERT INTO tenant_group_members (group_id, tenant_id) VALUES (?, ?)",
                     [(g, tenant_id) for g in sorted(after - before)])
    return len(after - before), len(before - after)


def usage(conn, group_id: int) -> dict:
    """What depends on a group, for its delete page."""
    one = lambda sql: conn.execute(sql, (group_id,)).fetchone()[0]  # noqa: E731
    return {
        "digests": one("SELECT COUNT(*) FROM email_schedules WHERE group_id = ?"),
        "analysts": one("SELECT COUNT(*) FROM user_tenant_groups ug JOIN users u ON u.id = ug.user_id"
                        " WHERE ug.group_id = ? AND u.role = 'analyst'"),
    }


def remove(conn, group_id: int) -> None:
    """Remove a group. Its tenants stay; analysts granted it lose those tenants; digests on it cover everyone."""
    conn.execute("DELETE FROM tenant_groups WHERE id = ?", (group_id,))


def user_groups(conn, user_id: int) -> set[int]:
    return {r[0] for r in conn.execute("SELECT group_id FROM user_tenant_groups WHERE user_id = ?", (user_id,))}


def set_user_groups(conn, user_id: int, group_ids) -> None:
    conn.execute("DELETE FROM user_tenant_groups WHERE user_id = ?", (user_id,))
    conn.executemany("INSERT INTO user_tenant_groups (user_id, group_id) VALUES (?, ?)",
                     [(user_id, g) for g in sorted(set(group_ids))])
