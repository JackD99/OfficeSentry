"""A client tenant's lifecycle in Office Sentry: first collection, pause, archive, delete.

* **Paused** (``tenants.enabled = 0``): no scheduled or manual collection, no
  emailed reports or alert emails. Everything collected stays and reports still
  open. Resume picks up where it left off.
* **Archived** (``archived_at``): paused, and hidden from tenant lists,
  dashboards, rollups, digests and clients. Admins can restore it.
* **Deleted**: the tenant and everything stored for it, in one transaction with
  foreign keys on, checked afterwards for anything left behind. Activity log
  lines stay (they record who did what).

None of this touches the client's Microsoft 365 tenant; removing the app there
is done by the client (see docs/what-office-sentry-reads.md).
"""

from __future__ import annotations

import json
import sqlite3

from . import events
from .db import audit, transaction, utcnow
from .text import plural

# Tables with a tenant_id that are kept on purpose when a tenant is deleted.
KEPT = {"audit_log"}


def queue_first_collection(conn, tenant_id: int, requested_by: str = "Onboarding") -> int:
    """Queue every data source that has never succeeded for the tenant. Returns how many."""
    from . import store
    from .collectors import REGISTRY

    done = {r["collector"] for r in conn.execute(
        "SELECT DISTINCT collector FROM runs WHERE tenant_id = ? AND status IN ('succeeded', 'partial')",
        (tenant_id,))}
    keys = [k for k in REGISTRY if k not in done]
    for key in keys:
        store.enqueue_run(conn, tenant_id, key, requested_by=requested_by)
    return len(keys)


def _drop_queued(conn, tenant_id: int) -> int:
    return conn.execute("DELETE FROM runs WHERE tenant_id = ? AND status = 'queued'", (tenant_id,)).rowcount


def pause(conn, tenant, user: str) -> int:
    """Stop collection and emails. Returns the queued runs dropped."""
    with transaction(conn, immediate=True):
        conn.execute("UPDATE tenants SET enabled = 0, paused_at = COALESCE(paused_at, ?) WHERE id = ?",
                     (utcnow(), tenant["id"]))
        dropped = _drop_queued(conn, tenant["id"])
        audit(conn, user, "tenant_paused", tenant["id"], tenant["name"])
    return dropped


def resume(conn, tenant, user: str) -> None:
    if tenant["archived_at"]:
        raise ValueError("restore the tenant before resuming it")
    with transaction(conn, immediate=True):
        conn.execute("UPDATE tenants SET enabled = 1, paused_at = NULL WHERE id = ?", (tenant["id"],))
        audit(conn, user, "tenant_resumed", tenant["id"], tenant["name"])


def archive(conn, tenant, user: str) -> None:
    with transaction(conn, immediate=True):
        now = utcnow()
        conn.execute("UPDATE tenants SET enabled = 0, paused_at = COALESCE(paused_at, ?), archived_at = ?"
                     " WHERE id = ?", (now, now, tenant["id"]))
        _drop_queued(conn, tenant["id"])
        audit(conn, user, "tenant_archived", tenant["id"], tenant["name"])


def restore(conn, tenant, user: str) -> None:
    """Bring an archived tenant back. It stays paused until someone resumes it."""
    with transaction(conn, immediate=True):
        conn.execute("UPDATE tenants SET archived_at = NULL WHERE id = ?", (tenant["id"],))
        audit(conn, user, "tenant_restored", tenant["id"], tenant["name"])


# -- Offboarding -----------------------------------------------------------------------------


def client_only_users(conn, tenant_id: int) -> list:
    """Client accounts whose only tenant is this one: they can't see anything once it's gone."""
    return conn.execute(
        "SELECT u.* FROM users u JOIN user_tenants ut ON ut.user_id = u.id AND ut.tenant_id = ?"
        " WHERE u.role = 'client' AND NOT EXISTS"
        " (SELECT 1 FROM user_tenants o WHERE o.user_id = u.id AND o.tenant_id <> ?)"
        " ORDER BY u.username COLLATE NOCASE", (tenant_id, tenant_id)).fetchall()


def _tenant_tables(conn) -> list[str]:
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return [t for t in tables
            if any(c["name"] == "tenant_id" for c in conn.execute(f"PRAGMA table_info({t})"))]


def stored(conn, tenant_id: int) -> dict[str, int]:
    """Everything stored for a tenant, by what people call it. Used before and after deleting."""
    one = lambda sql: conn.execute(sql, (tenant_id,)).fetchone()[0]  # noqa: E731
    return {
        "runs": one("SELECT COUNT(*) FROM runs WHERE tenant_id = ?"),
        "objects": one("SELECT COALESCE(SUM(items), 0) FROM objects"
                       " WHERE run_id IN (SELECT id FROM runs WHERE tenant_id = ?)"),
        "metrics": one("SELECT COUNT(*) FROM metrics WHERE tenant_id = ?"),
        "findings": one("SELECT COUNT(*) FROM findings WHERE tenant_id = ?"),
        "open_findings": one("SELECT COUNT(*) FROM findings WHERE tenant_id = ? AND resolved_at IS NULL"),
        "schedules": one("SELECT COUNT(*) FROM email_schedules WHERE tenant_id = ?"),
        "alerts": one("SELECT COUNT(*) FROM email_alerts WHERE tenant_id = ?"),
        "contacts": one("SELECT COUNT(*) FROM contacts WHERE tenant_id = ?"),
        "deliveries": one("SELECT COUNT(*) FROM email_deliveries WHERE tenant_id = ?"),
        "access": one("SELECT COUNT(*) FROM user_tenants WHERE tenant_id = ?"),
        **{f"history_{k}": n for k, n in events.counts(conn, tenant_id).items()},
    }


def leftovers(conn, tenant_id: int, run_ids: list[int] = ()) -> dict[str, int]:
    """Rows still referring to the tenant (or its runs), by table. Empty once it is fully deleted."""
    left = {}
    for table in _tenant_tables(conn):
        if table in KEPT:
            continue
        n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]
        if n:
            left[table] = n
    if run_ids:
        ids = json.dumps(list(run_ids))
        for table in ("objects", "run_events", "metrics"):
            n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE run_id IN (SELECT value FROM json_each(?))",
                             (ids,)).fetchone()[0]
            if n:
                left[table] = left.get(table, 0) + n
    return left


class DeleteError(Exception):
    pass


def delete_tenant(conn, tenant, user: str, delete_users: list[int] = ()) -> dict:
    """Delete the tenant and everything stored for it. Returns what was removed.

    ``delete_users`` may only name client accounts whose sole tenant is this one.
    Runs in one transaction: if anything tenant-scoped would be left behind,
    nothing is deleted.
    """
    if not conn.execute("PRAGMA foreign_keys").fetchone()[0]:
        raise DeleteError("foreign keys are off on this database connection")
    allowed = {u["id"]: u["username"] for u in client_only_users(conn, tenant["id"])}
    unknown = set(delete_users) - set(allowed)
    if unknown:
        raise DeleteError("only client accounts with no other tenant can be deleted with it")
    # The activity history is in its own files: its key goes with the tenant (ON DELETE CASCADE), and its rows
    # are deleted right after. If that stops part-way, the daily retention pass sweeps up rows with no key.
    key, base = events.tenant_key(conn, tenant["id"]), events.root(conn)
    # Overwrite deleted content in the file instead of leaving it in free pages (see erase_freed_pages).
    previous = conn.execute("PRAGMA secure_delete").fetchone()[0]
    conn.execute("PRAGMA secure_delete = ON")
    try:
        removed = _delete(conn, tenant, user, delete_users, allowed)
    finally:
        conn.execute(f"PRAGMA secure_delete = {('OFF', 'ON', 'FAST')[int(previous)]}")
    if key is not None:
        events.delete_tenant(base, key)
    erase_freed_pages(conn)
    return removed


# Audit actions whose detail can name the client's people (UPNs, email addresses, delivery errors).
PERSONAL_DETAIL = ("finding_accepted", "finding_unaccepted", "contact_added", "contact_removed", "email_retried",
                   "alert_sent", "alert_failed")
REDACTED = "(removed when the tenant was deleted)"


def _scrub_audit(conn, tenant_id: int, deleted_users: dict[int, str]) -> None:
    """Keep who did what and when for the deleted tenant, but not the client's people in the detail.

    Rows by client accounts deleted with it lose the account name and address (they are usually
    the client's own email addresses), and so do admin rows about those accounts.
    """
    conn.execute(f"UPDATE audit_log SET detail = ? WHERE tenant_id = ? AND action IN"
                 f" ({','.join('?' * len(PERSONAL_DETAIL))})", (REDACTED, tenant_id, *PERSONAL_DETAIL))
    for uid, name in deleted_users.items():
        conn.execute("UPDATE audit_log SET username = ?, ip = NULL WHERE username = ?",
                     (f"deleted client account #{uid}", name))
        # "name", "name (client)", "name: role=..." in user_created, user_updated, user_signed_out...
        conn.execute("UPDATE audit_log SET detail = ? WHERE substr(action, 1, 5) = 'user_'"
                     " AND substr(detail, 1, ?) = ? AND substr(detail, ? + 1, 1) IN ('', ' ', ':')",
                     (f"deleted client account #{uid}", len(name), name, len(name)))


def erase_freed_pages(conn) -> bool:
    """Best effort, after a delete: rebuild the file and empty the write-ahead log, so deleted rows
    can't be read from free pages or old WAL frames. Backups made before keep the data until they
    rotate out. Returns False if another connection kept it from finishing (try again later)."""
    try:
        conn.execute("VACUUM")
        busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
    except sqlite3.Error:
        return False
    return not busy


def _delete(conn, tenant, user: str, delete_users, allowed: dict[int, str]) -> dict:
    with transaction(conn, immediate=True):
        removed = stored(conn, tenant["id"])
        run_ids = [r["id"] for r in conn.execute("SELECT id FROM runs WHERE tenant_id = ?", (tenant["id"],))]
        for uid in delete_users:
            conn.execute("DELETE FROM users WHERE id = ?", (uid,))
        conn.execute("DELETE FROM tenants WHERE id = ?", (tenant["id"],))
        left = leftovers(conn, tenant["id"], run_ids)
        if left:
            raise DeleteError("these would be left behind: " + ", ".join(f"{t} ({n})" for t, n in left.items()))
        _scrub_audit(conn, tenant["id"], {uid: allowed[uid] for uid in delete_users})
        names = sorted(allowed[u] for u in delete_users)
        removed["users"] = len(names)
        detail = (f"{tenant['name']} ({tenant['tenant_ref']}): {plural(removed['runs'], 'run')}, "
                  f"{plural(removed['objects'], 'stored object')}, {plural(removed['findings'], 'finding')}, "
                  f"{plural(removed['schedules'], 'emailed report')}, {plural(removed['contacts'], 'contact')}")
        if names:
            detail += "; client accounts deleted: " + ", ".join(names)
        audit(conn, user, "tenant_deleted", tenant["id"], detail)
    return removed
