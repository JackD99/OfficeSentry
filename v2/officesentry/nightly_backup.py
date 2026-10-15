"""The worker's own nightly database backup (DEPLOY.md, "Backups").

Every install backs itself up without a crontab line: at ``cfg.backup_cron`` (``OFFICESENTRY_BACKUP_CRON``, 03:30
in the install's timezone unless set; ``off`` turns it off) the worker queues a ``backup`` task, which makes
the same copy as the ``backup`` command into ``<data dir>/backups``, keeping ``cfg.backup_keep``
(``OFFICESENTRY_BACKUP_KEEP``), and records how it went for System status and the "backup failed" alert.

Many servers already make a nightly copy there themselves (``deploy/jobs.sh``, the cloud-init file, the
Kubernetes CronJob, a NAS's scheduled task), so a night is skipped when the newest copy in the folder is one the
worker didn't make, from the last day. Whichever runs first each day then settles which one keeps making the
copies, and there's one a night either way.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from . import backups, schedule, tasks

log = logging.getLogger(__name__)

LAST_SLOT = "backup.last_slot"  # the latest slot seen to (queued or skipped)
MADE = "backup.made"  # the name of the last copy the worker made
RECENT = timedelta(days=1)


def folder(cfg):
    return cfg.data_dir / "backups"


def _setting(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _store(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def made_elsewhere(conn, cfg, now: datetime) -> bool:
    """Whether the newest copy in the backups folder was made in the last day by something else."""
    newest = backups.newest_backup(folder(cfg))
    if newest is None or newest.name == _setting(conn, MADE):
        return False
    try:
        return now.timestamp() - newest.stat().st_mtime < RECENT.total_seconds()
    except OSError:
        return False


@schedule.periodic("nightly_backup")
def _queue(ctx) -> None:
    if not ctx.cfg.backup_cron:
        return
    slot = schedule.due_slot(ctx.cfg.backup_cron, ctx.cfg.timezone, ctx.now, last_slot=_setting(ctx.conn, LAST_SLOT))
    if slot is None:
        return
    if made_elsewhere(ctx.conn, ctx.cfg, ctx.now):
        log.info("No backup by the worker tonight: the server made one in the last day")
    else:
        tasks.enqueue(ctx.conn, "backup", key=f"backup:{schedule.iso(slot)}")
    _store(ctx.conn, LAST_SLOT, schedule.iso(slot))


@tasks.handler("backup")
def _backup(ctx) -> dict:
    try:
        dest, old = backups.backup_and_record(ctx.cfg.data_dir, ctx.cfg.db_path, folder(ctx.cfg), ctx.cfg.backup_keep)
    except backups.BackupFailed as exc:
        raise tasks.TaskFailed(str(exc)) from exc
    _store(ctx.conn, MADE, dest.name)
    log.info("Saved %s%s", dest.name, f" (removed {len(old)} older)" if old else "")
    return {"file": dest.name, "removed": len(old)}
