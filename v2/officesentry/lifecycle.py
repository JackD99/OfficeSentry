"""A client tenant's lifecycle in Office Sentry: first collection, pause, archive, delete.

* **Paused** (``tenants.enabled = 0``): no scheduled or manual collection, no
  emailed reports or alert emails. Everything collected stays and reports still
  open. Resume picks up where it left off.
* **Archived** (``archived_at``): paused, and hidden from tenant lists,
  dashboards, rollups, digests and clients. Admins can restore it.
* **Deleted**: the tenant and everything stored for it, in one transaction with
  foreign keys on, checked afterwards for anything left behind. Activity log
  lines stay (they record who did what). The ``erase`` task then removes its
  activity history and the deleted pages' last copies from the files on disk
  (``erase``); a failure shows on System status, is alerted and audited, and
  is tried again every hour.

None of this touches the client's Microsoft 365 tenant; removing the app there
is done by the client (see docs/what-office-sentry-reads.md).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import UTC, datetime, timedelta

from . import events, packing, schedule, tasks
from .auditlog import addon
from .backups import database_file
from .db import audit, transaction, utcnow
from .devices import parse_ts
from .text import plural

log = logging.getLogger(__name__)

# Tables with a tenant_id that are kept on purpose when a tenant is deleted.
KEPT = {"audit_log"}


def queue_first_collection(conn, tenant_id: int, requested_by: str = "Onboarding") -> int:
    """Queue every data source that has never succeeded for the tenant. Returns how many."""
    from . import store
    from .collectors import core

    done = {
        r["collector"]
        for r in conn.execute(
            "SELECT DISTINCT collector FROM runs WHERE tenant_id = ? AND status IN ('succeeded', 'partial')",
            (tenant_id,),
        )
    }
    keys = [k for k in core() if k not in done]
    for key in keys:
        store.enqueue_run(conn, tenant_id, key, requested_by=requested_by)
    return len(keys)


def _drop_queued(conn, tenant_id: int) -> int:
    return conn.execute("DELETE FROM runs WHERE tenant_id = ? AND status = 'queued'", (tenant_id,)).rowcount


def pause(conn, tenant, user: str) -> int:
    """Stop collection and emails. Returns the queued runs dropped."""
    with transaction(conn, immediate=True):
        conn.execute(
            "UPDATE tenants SET enabled = 0, paused_at = COALESCE(paused_at, ?) WHERE id = ?", (utcnow(), tenant["id"])
        )
        dropped = _drop_queued(conn, tenant["id"])
        audit(conn, user, "tenant_paused", tenant["id"], tenant["name"])
    return dropped


def resume(conn, tenant, user: str) -> None:
    if tenant["archived_at"]:
        raise ValueError("restore the client before resuming it")
    with transaction(conn, immediate=True):
        conn.execute("UPDATE tenants SET enabled = 1, paused_at = NULL WHERE id = ?", (tenant["id"],))
        audit(conn, user, "tenant_resumed", tenant["id"], tenant["name"])


def archive(conn, tenant, user: str) -> None:
    with transaction(conn, immediate=True):
        now = utcnow()
        conn.execute(
            "UPDATE tenants SET enabled = 0, paused_at = COALESCE(paused_at, ?), archived_at = ? WHERE id = ?",
            (now, now, tenant["id"]),
        )
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
        " ORDER BY u.username COLLATE NOCASE",
        (tenant_id, tenant_id),
    ).fetchall()


def _tenant_tables(conn) -> list[str]:
    tables = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return [t for t in tables if any(c["name"] == "tenant_id" for c in conn.execute(f"PRAGMA table_info({t})"))]


def stored(conn, tenant_id: int) -> dict[str, int]:
    """Everything stored for a tenant, by what people call it. Used before and after deleting."""
    one = lambda sql: conn.execute(sql, (tenant_id,)).fetchone()[0]  # noqa: E731
    return {
        "runs": one("SELECT COUNT(*) FROM runs WHERE tenant_id = ?"),
        "objects": one(
            "SELECT COALESCE(SUM(items), 0) FROM objects WHERE run_id IN (SELECT id FROM runs WHERE tenant_id = ?)"
        ),
        "metrics": one("SELECT COUNT(*) FROM metrics WHERE tenant_id = ?"),
        "findings": one("SELECT COUNT(*) FROM findings WHERE tenant_id = ?"),
        "open_findings": one("SELECT COUNT(*) FROM findings WHERE tenant_id = ? AND resolved_at IS NULL"),
        "schedules": one("SELECT COUNT(*) FROM email_schedules WHERE tenant_id = ?"),
        "alerts": one("SELECT COUNT(*) FROM email_alerts WHERE tenant_id = ?"),
        "contacts": one("SELECT COUNT(*) FROM contacts WHERE tenant_id = ?"),
        "deliveries": one("SELECT COUNT(*) FROM email_deliveries WHERE tenant_id = ?"),
        "issued": one("SELECT COUNT(*) FROM issued_reports WHERE tenant_id = ?"),  # the Report history's files
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
            n = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE run_id IN (SELECT value FROM json_each(?))", (ids,)
            ).fetchone()[0]
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
        raise DeleteError("only client accounts with no other client can be deleted with it")
    # Overwrite deleted content with zeros instead of leaving it in free pages. The old pages stay in the file
    # until the write-ahead log is written back, which the erase task does (see ``erase``).
    previous = conn.execute("PRAGMA secure_delete").fetchone()[0]
    conn.execute("PRAGMA secure_delete = ON")
    try:
        return _delete(conn, tenant, user, delete_users, allowed)
    finally:
        conn.execute(f"PRAGMA secure_delete = {('OFF', 'ON', 'FAST')[int(previous)]}")


# Audit actions whose detail can name the client's people (UPNs, email addresses, delivery errors).
PERSONAL_DETAIL = (
    "finding_accepted",
    "finding_unaccepted",
    "contact_added",
    "contact_removed",
    "email_retried",
    "alert_sent",
    "alert_failed",
)
REDACTED = "(removed when the client was deleted)"


def _scrub_audit(conn, tenant_id: int, deleted_users: dict[int, str]) -> None:
    """Keep who did what and when for the deleted tenant, but not the client's people in the detail.

    Rows by client accounts deleted with it lose the account name and address (they are usually
    the client's own email addresses), and so do admin rows about those accounts.
    """
    conn.execute(
        f"UPDATE audit_log SET detail = ? WHERE tenant_id = ? AND action IN ({','.join('?' * len(PERSONAL_DETAIL))})",
        (REDACTED, tenant_id, *PERSONAL_DETAIL),
    )
    for uid, name in deleted_users.items():
        conn.execute(
            "UPDATE audit_log SET username = ?, ip = NULL WHERE username = ?", (f"deleted client account #{uid}", name)
        )
        # "name", "name (client)", "name: role=..." in user_created, user_updated, user_signed_out...
        conn.execute(
            "UPDATE audit_log SET detail = ? WHERE substr(action, 1, 5) = 'user_'"
            " AND substr(detail, 1, ?) = ? AND substr(detail, ? + 1, 1) IN ('', ' ', ':')",
            (f"deleted client account #{uid}", len(name), name, len(name)),
        )


def _delete(conn, tenant, user: str, delete_users, allowed: dict[int, str]) -> dict:
    with transaction(conn, immediate=True):
        removed = stored(conn, tenant["id"])
        run_ids = [r["id"] for r in conn.execute("SELECT id FROM runs WHERE tenant_id = ?", (tenant["id"],))]
        for uid in delete_users:
            conn.execute("DELETE FROM users WHERE id = ?", (uid,))
        addon.on_delete(conn, tenant, user)  # stops its audit log feed at Microsoft afterwards, if it's on
        conn.execute("DELETE FROM tenants WHERE id = ?", (tenant["id"],))
        left = leftovers(conn, tenant["id"], run_ids)
        if left:
            raise DeleteError("these would be left behind: " + ", ".join(f"{t} ({n})" for t, n in left.items()))
        _scrub_audit(conn, tenant["id"], {uid: allowed[uid] for uid in delete_users})
        names = sorted(allowed[u] for u in delete_users)
        removed["users"] = len(names)
        detail = (
            f"{tenant['name']} ({tenant['tenant_ref']}): {plural(removed['runs'], 'run')}, "
            f"{plural(removed['objects'], 'stored object')}, {plural(removed['findings'], 'finding')}, "
            f"{plural(removed['schedules'], 'emailed report')}, {plural(removed['contacts'], 'contact')}"
        )
        if names:
            detail += "; client accounts deleted: " + ", ".join(names)
        audit(conn, user, "tenant_deleted", tenant["id"], detail)
        request_erase(conn, user)
    return removed


# -- Erasing what a delete leaves on disk ------------------------------------------------------------
#
# The delete itself is one quick transaction. What it leaves is slow to clear, so it's the ``erase`` task's
# job, never the request's: the tenant's activity history rows (its key went with the tenant, so
# ``events.sweep`` finds them), the deleted rows' old pages, which stay in the database file until the
# write-ahead log is written back, and the free pages, which VACUUM gives back to the disk when there's room
# for its copy. The settings row ``erase.state`` says a delete is waiting, and how the last try went:
#
#   {"requested": "<oldest delete waiting>", "deletes": <deletes so far>, "tried": <deletes when the last try
#    started>, "attempted": "<when it started>", "error": "<why it failed>" or null}
#
# It's removed once a try that started after the last delete finishes (counted, not timed, so two in one
# second can't be confused). The ``erase`` periodic job queues the task again when that hasn't happened: a
# delete during a try, a worker that died, or a failure an hour ago.
# Backups made before the delete keep the data until they rotate out (DEPLOY.md, "Backups").

ERASE_STATE = "erase.state"
ERASE_TASK = "erase"
ERASE_RETRY = timedelta(hours=1)
CHECKPOINT_TRIES = 6  # a reader holding the write-ahead log stops it being emptied: wait for it this many times
CHECKPOINT_WAIT = 10  # seconds


class EraseError(Exception):
    pass


def erase_state(conn) -> dict | None:
    """The waiting erase (see above), or None when everything deleted has been erased."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (ERASE_STATE,)).fetchone()
    try:
        state = json.loads(row[0]) if row else None
    except ValueError:
        return None
    return state if isinstance(state, dict) and state.get("requested") else None


def _save_state(conn, state: dict) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (ERASE_STATE, json.dumps(state)),
    )


def request_erase(conn, user: str | None = None) -> None:
    """Note that a delete left something to erase, and queue the task. Works inside the delete's transaction."""
    state = erase_state(conn) or {"requested": utcnow()}
    _save_state(conn, {**state, "deletes": int(state.get("deletes") or 0) + 1})
    tasks.enqueue(conn, ERASE_TASK, key=ERASE_TASK, user=user)


def erase(conn, progress=lambda _text: None) -> str:
    """Remove what deleted tenants left on disk. Returns a note for the activity log ("" when there's nothing
    to say); raises EraseError, with words for people, when deleted data may still be readable."""
    progress("Removing the activity history of deleted clients")
    try:
        rows = events.sweep(conn)
    except (OSError, sqlite3.Error) as exc:
        raise EraseError(f"the activity history couldn't be cleaned: {exc}") from exc
    notes = [f"{plural(rows, 'activity history row')} removed"] if rows else []
    db_file = database_file(conn)
    if db_file is not None:
        # Deleted rows are already zeros in the database (secure_delete), so a VACUUM only gives the free
        # pages back to the disk: skipped, and said, when there's no room for its copy.
        needed, free = packing.vacuum_room(conn, db_file)
        if free < needed:
            notes.append(
                f"the file wasn't shrunk: that needs about {needed // 2**20:,} MB free and there's {free // 2**20:,} MB"
            )
        else:
            progress("Rebuilding the database file")
            try:
                packing.vacuum(conn, db_file)
            except sqlite3.OperationalError as exc:
                notes.append(f"the file wasn't shrunk: {exc}")
    progress("Writing the database's log back")
    for attempt in range(CHECKPOINT_TRIES):
        if not conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]:
            break
        if attempt + 1 < CHECKPOINT_TRIES:
            time.sleep(CHECKPOINT_WAIT)
    else:
        raise EraseError(
            "the portal or worker kept the database busy, so the deleted data's last copies couldn't be "
            "cleared from its log file"
        )
    return "; ".join(notes)


@tasks.handler(ERASE_TASK)
def _erase_task(ctx: tasks.TaskContext) -> dict:
    user = ctx.task["requested_by"] or "worker"
    with transaction(ctx.conn):
        state = erase_state(ctx.conn) or {"requested": utcnow(), "deletes": 0}
        tried = int(state.get("deletes") or 0)
        _save_state(ctx.conn, {**state, "attempted": utcnow(), "tried": tried})
    try:
        note = erase(ctx.conn, ctx.progress)
    except EraseError as exc:
        problem = str(exc)[0].upper() + str(exc)[1:]
        with transaction(ctx.conn):
            if state := erase_state(ctx.conn):
                _save_state(ctx.conn, {**state, "error": problem})
            audit(ctx.conn, user, "tenant_erase_failed", None, problem)
        raise tasks.TaskFailed(problem) from exc
    with transaction(ctx.conn):
        state = erase_state(ctx.conn)
        if state and int(state.get("deletes") or 0) <= tried:
            ctx.conn.execute("DELETE FROM settings WHERE key = ?", (ERASE_STATE,))
        elif state:  # deleted again while this ran: the periodic job queues another try
            _save_state(ctx.conn, {**state, "error": None})
        audit(ctx.conn, user, "tenant_data_erased", None, note or "Deleted data erased from the files on disk")
    return {"note": note}


@schedule.periodic("erase")
def _erase_job(ctx: schedule.TickContext) -> None:
    """Queue the erase task again when a delete is still waiting for one (see above)."""
    state = erase_state(ctx.conn)
    if state is None or tasks.pending(ctx.conn, ERASE_TASK):
        return
    attempted = parse_ts(state.get("attempted"))
    if attempted and state.get("tried") == state.get("deletes"):
        if not state.get("error") or ctx.now - attempted < ERASE_RETRY:
            return
    tasks.enqueue(ctx.conn, ERASE_TASK, key=ERASE_TASK)


def erase_waiting(state: dict | None, now: datetime | None = None) -> timedelta | None:
    """How long the oldest delete has waited to be erased, or None when nothing is waiting."""
    since = parse_ts(state.get("requested")) if state else None
    return (now or datetime.now(UTC)) - since if since else None
