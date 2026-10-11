"""Background tasks: long work handed to the worker instead of done in a web request or in the
worker's once-a-minute loop.

A task is a row in ``tasks`` (kind, payload, status, priority, lease, progress, result). The
worker's task threads (``worker.TASK_THREADS``) claim the next one, highest priority first, and
run the handler registered for its kind with ``@handler(kind)``:

* ``approve`` (``month_end.py``): freeze one client's month and send or publish it.
* ``send_schedule`` (``delivery.py``): one scheduled report or digest email that is due.
* ``precompute`` (``reports/precomputed.py``): one tenant's rows on What changed and the check
  matrix, after its collection and once a day.

**Exactly once.** At most one task per ``key`` is queued or running (a partial unique index),
so approving a client's month twice queues it once. A claimed task carries a lease the worker
renews while it runs; when a worker dies its lease runs out and ``expire_leases`` queues the
task again, up to ``MAX_ATTEMPTS`` times. A handler that does a step it mustn't repeat saves
``ctx.checkpoint(...)`` in the same transaction as that step, and on a retry reads
``ctx.done`` to skip it. A handler's own failure is not retried: it raises ``TaskFailed`` with
words for people, or any other exception, and the task is marked failed with that message.

Pages show progress from the rows (``batch_progress``, ``latest``); nothing here talks to
Microsoft 365.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import store
from .db import transaction, utcnow

log = logging.getLogger(__name__)

PRIORITY_WAITING = store.PRIORITY_WAITING  # someone is watching the page for it
PRIORITY_BACKGROUND = 0
MAX_ATTEMPTS = 3  # claims, counting retries after a worker died mid-task
KEEP = timedelta(days=30)  # finished tasks are pruned after this (``prune``)
PENDING = ("queued", "running")

HANDLERS: dict[str, Callable[[TaskContext], dict | None]] = {}


class TaskFailed(Exception):
    """A failure to show people as it is ("The client is paused"), rather than as a crash."""


def handler(kind: str):
    """Register the function that runs tasks of ``kind``. It returns a small dict (the result) or None."""

    def decorator(fn):
        HANDLERS[kind] = fn
        return fn

    return decorator


@dataclass
class TaskContext:
    app: object
    conn: object
    vault: object
    cfg: object
    task: object
    payload: dict = field(default_factory=dict)

    @property
    def id(self) -> int:
        return self.task["id"]

    @property
    def done(self) -> str | None:
        """The last checkpoint an earlier attempt saved, or None."""
        return self.task["checkpoint"]

    @property
    def tenant(self):
        return store.get_tenant(self.conn, self.task["tenant_id"]) if self.task["tenant_id"] else None

    def progress(self, text: str) -> None:
        """What the task is doing now, for the page that's waiting on it."""
        self.conn.execute("UPDATE tasks SET progress = ? WHERE id = ?", (text[:200], self.id))

    def checkpoint(self, name: str) -> None:
        """Record that a step is done. Call it inside the transaction that does the step."""
        self.conn.execute("UPDATE tasks SET checkpoint = ? WHERE id = ?", (name, self.id))


# -- Queueing --------------------------------------------------------------------------------


def enqueue(
    conn,
    kind: str,
    *,
    key: str | None = None,
    tenant_id: int | None = None,
    payload: dict | None = None,
    batch: str | None = None,
    priority: int = PRIORITY_BACKGROUND,
    user: str | None = None,
) -> int:
    """Queue a task and return its id. With ``key``, a task already queued or running for that key is
    returned instead (its priority raised if this one's is higher). Works inside a caller's transaction."""
    if kind not in HANDLERS:
        raise ValueError(f"no handler for task kind {kind!r}")
    with store.atomic(conn):
        if key is not None:
            existing = conn.execute(
                "SELECT id, status, priority FROM tasks WHERE key = ? AND status IN ('queued', 'running')", (key,)
            ).fetchone()
            if existing:
                if existing["status"] == "queued" and existing["priority"] < priority:
                    conn.execute("UPDATE tasks SET priority = ? WHERE id = ?", (priority, existing["id"]))
                return existing["id"]
        return conn.execute(
            "INSERT INTO tasks (kind, key, batch, tenant_id, payload, priority, requested_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, key, batch, tenant_id, json.dumps(payload or {}), priority, user, utcnow()),
        ).lastrowid


def pending(conn, key: str):
    """The task queued or running for ``key``, or None."""
    return conn.execute("SELECT * FROM tasks WHERE key = ? AND status IN ('queued', 'running')", (key,)).fetchone()


def latest(conn, key: str):
    """The newest task for ``key`` in any state, or None."""
    return conn.execute("SELECT * FROM tasks WHERE key = ? ORDER BY id DESC LIMIT 1", (key,)).fetchone()


def claim_next(conn):
    """Atomically move the next queued task to 'running' with a fresh lease, and return it."""
    with transaction(conn, immediate=True):
        row = conn.execute(
            "SELECT id FROM tasks INDEXED BY idx_tasks_status WHERE status = 'queued'"
            " ORDER BY priority DESC, created_at, id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE tasks SET status = 'running', started_at = ?, lease_until = ?, attempts = attempts + 1,"
            " progress = NULL WHERE id = ?",
            (utcnow(), store.lease_until(), row["id"]),
        )
        return conn.execute("SELECT * FROM tasks WHERE id = ?", (row["id"],)).fetchone()


def renew(conn, task_ids, now: datetime | None = None) -> None:
    """Keep the tasks this worker is running claimed (``worker._heartbeat_thread``)."""
    ids = list(task_ids)
    if ids:
        conn.execute(
            f"UPDATE tasks SET lease_until = ? WHERE status = 'running' AND id IN ({','.join('?' * len(ids))})",
            (store.lease_until(now), *ids),
        )


def expire_leases(conn, now: datetime | None = None, held=()) -> tuple[int, int]:
    """Tasks whose worker died (lease ran out) are queued again, or failed after ``MAX_ATTEMPTS`` claims.
    ``held`` are tasks this process is running. Returns (queued again, failed)."""
    now_iso = (now or datetime.now(UTC)).strftime(store.ISO)
    held = list(held)
    expired = (
        f"status = 'running' AND (lease_until IS NULL OR lease_until < ?) AND id NOT IN ({','.join('?' * len(held))})"
    )
    with store.atomic(conn):
        failed = conn.execute(
            f"UPDATE tasks SET status = 'failed', finished_at = ?, lease_until = NULL,"
            f" error = 'The worker stopped while doing this, more than once. Try again.'"
            f" WHERE {expired} AND attempts >= ?",
            (utcnow(), now_iso, *held, MAX_ATTEMPTS),
        ).rowcount
        requeued = conn.execute(
            f"UPDATE tasks SET status = 'queued', lease_until = NULL, progress = NULL WHERE {expired}", (now_iso, *held)
        ).rowcount
    return requeued, failed


def finish(conn, task_id: int, status: str, *, result: dict | None = None, error: str | None = None) -> None:
    conn.execute(
        "UPDATE tasks SET status = ?, result = ?, error = ?, finished_at = ?, lease_until = NULL,"
        " progress = NULL WHERE id = ?",
        (
            status,
            json.dumps(result) if result is not None else None,
            error[:1000] if error else None,
            utcnow(),
            task_id,
        ),
    )


# -- Running -----------------------------------------------------------------------------------


def run(app, conn, vault, cfg, task) -> str:
    """Run one claimed task to the end. Returns its final status. Never raises for a handler's failure."""
    fn = HANDLERS.get(task["kind"])
    ctx = TaskContext(app, conn, vault, cfg, task, json.loads(task["payload"] or "{}"))
    try:
        if fn is None:
            raise TaskFailed(f"Unknown task {task['kind']!r}")
        result = fn(ctx)
    except TaskFailed as exc:
        log.warning("Task %s (%s) failed: %s", task["id"], task["kind"], exc)
        _rollback(conn)
        finish(conn, task["id"], "failed", error=str(exc))
        return "failed"
    except Exception as exc:  # a report bug mustn't take the task thread down
        log.exception("Task %s (%s) failed", task["id"], task["kind"])
        _rollback(conn)
        finish(conn, task["id"], "failed", error=f"Something went wrong: {type(exc).__name__}")
        return "failed"
    finish(conn, task["id"], "succeeded", result=result)
    return "succeeded"


def drain(app, conn, vault, cfg, limit: int | None = None) -> int:
    """Run queued tasks in this thread until none are left (``worker --once``, tests). Returns how many ran."""
    count = 0
    while (limit is None or count < limit) and (task := claim_next(conn)) is not None:
        run(app, conn, vault, cfg, task)
        count += 1
    return count


def _rollback(conn) -> None:
    if conn.in_transaction:
        conn.execute("ROLLBACK")


def prune(conn, now: datetime | None = None) -> int:
    """Remove finished tasks older than ``KEEP``. What they did is in the audit log and the email history."""
    cutoff = ((now or datetime.now(UTC)) - KEEP).strftime(store.ISO)
    return conn.execute(
        "DELETE FROM tasks WHERE status IN ('succeeded', 'failed') AND finished_at < ?", (cutoff,)
    ).rowcount


# -- Progress ------------------------------------------------------------------------------------


@dataclass
class Progress:
    total: int = 0
    queued: int = 0
    running: int = 0
    succeeded: int = 0
    failed: int = 0
    failures: list = field(default_factory=list)  # (tenant id, error) for each failed task

    @property
    def done(self) -> int:
        return self.succeeded + self.failed

    @property
    def pending(self) -> int:
        return self.queued + self.running

    @property
    def finished(self) -> bool:
        return self.total > 0 and not self.pending

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "done": self.done,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "pending": self.pending,
            "finished": self.finished,
        }


def batch_progress(conn, batch: str, tenant_ids=None) -> Progress:
    """How far a batch has got. ``tenant_ids`` limits it to the tenants the viewer may see."""
    progress = Progress()
    allowed = None if tenant_ids is None else set(tenant_ids)
    for t in conn.execute("SELECT tenant_id, status, error FROM tasks WHERE batch = ? ORDER BY id", (batch,)):
        if allowed is not None and t["tenant_id"] not in allowed:
            continue
        progress.total += 1
        setattr(progress, t["status"], getattr(progress, t["status"]) + 1)
        if t["status"] == "failed":
            progress.failures.append((t["tenant_id"], t["error"]))
    return progress
