"""The worker's own nightly database backup (DEPLOY.md, "Backups").

Every install backs itself up without a crontab line: at ``cfg.backup_cron`` (``OFFICESENTRY_BACKUP_CRON``, 03:30
in the install's timezone unless set; ``off`` turns it off) the worker queues a ``backup`` task, which makes
the same copy as the ``backup`` command into ``<data dir>/backups``, keeping ``cfg.backup_keep``
(``OFFICESENTRY_BACKUP_KEEP``) of its own copies, and records how it went for System status and the "backup
failed" alert. It never deletes a copy it didn't make: a server's own job in that folder keeps its own number.

Many servers already make a nightly copy themselves (``deploy/jobs.sh``, a crontab line writing to a folder off
the data volume, the cloud-init file, the Kubernetes CronJob, a NAS's scheduled task), so a night is left to a
copy something else made in the last ``RECENT``: the ``backup`` command's own record says when, wherever it
wrote, and a copy in the backups folder counts too. The worker remembers that copy's time, and if nothing newer
comes by ``LATE`` after it (a copy made once by hand, or a server job that stopped), makes one then, before
System status would call the backup overdue. So there's one copy a night either way, and never a gap.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from . import backups, schedule, tasks
from .devices import parse_ts

log = logging.getLogger(__name__)

LAST_SLOT = "backup.last_slot"  # the latest slot seen to (queued or skipped)
MADE = "backup.made"  # the name of the last copy the worker made
MINE = "backup.mine"  # the names of the copies the worker made and still keeps, oldest first (JSON)
OTHER = "backup.other"  # when something else last made a copy, as the worker last saw it
WAITING = "backup.waiting"  # the time of the copy a skipped night was left to
RECENT = timedelta(hours=25)  # a copy made elsewhere this recently stands in for tonight's...
LATE = timedelta(hours=25)  # ...as long as a newer one comes by then (System status: overdue after 26)


def folder(cfg):
    return cfg.data_dir / "backups"


def _setting(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _store(conn, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
        return
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def made_elsewhere(conn, cfg) -> datetime | None:
    """When the newest copy something other than the worker made was made, or None."""
    mine = _setting(conn, MADE)
    found = [t for t in (parse_ts(_setting(conn, OTHER)),) if t]
    local = backups.read_status(cfg.data_dir).get("local") or {}
    if (done := parse_ts(local.get("last_success"))) and str(local.get("detail") or "").split(",")[0] != mine:
        found.append(done)  # the backup command, wherever it wrote
    newest = backups.newest_backup(folder(cfg))
    if newest is not None and newest.name != mine:
        try:
            found.append(datetime.fromtimestamp(newest.stat().st_mtime, UTC))
        except OSError:
            pass
    return max(found, default=None)


@schedule.periodic("nightly_backup")
def _queue(ctx) -> None:
    if not ctx.cfg.backup_cron:
        return
    late = False
    if (waiting := parse_ts(_setting(ctx.conn, WAITING))) and ctx.now >= waiting + LATE:
        _store(ctx.conn, WAITING, None)
        if late := (other := made_elsewhere(ctx.conn, ctx.cfg)) is None or other <= waiting:
            log.info("Backing up now: nothing newer came after the copy made at %s", _iso(waiting))
            tasks.enqueue(ctx.conn, "backup", key=f"backup:late:{_iso(waiting)}")
    slot = schedule.due_slot(ctx.cfg.backup_cron, ctx.cfg.timezone, ctx.now, last_slot=_setting(ctx.conn, LAST_SLOT))
    if slot is None:
        return
    if (other := made_elsewhere(ctx.conn, ctx.cfg)) and ctx.now - other < RECENT:
        log.info("No backup by the worker tonight: something else made one at %s", _iso(other))
        _store(ctx.conn, WAITING, _iso(other))
    elif not late:
        tasks.enqueue(ctx.conn, "backup", key=f"backup:{schedule.iso(slot)}")
    _store(ctx.conn, LAST_SLOT, schedule.iso(slot))


@tasks.handler("backup")
def _backup(ctx) -> dict:
    other = made_elsewhere(ctx.conn, ctx.cfg)  # before this copy's record replaces the command's
    try:
        # Kept 0: the command's rotation would trim every copy in the folder, a server job's too (_rotate).
        dest, _ = backups.backup_and_record(ctx.cfg.data_dir, ctx.cfg.db_path, folder(ctx.cfg), 0)
    except backups.BackupFailed as exc:
        raise tasks.TaskFailed(str(exc)) from exc
    old = _rotate(ctx.conn, ctx.cfg, dest.name)
    if other:
        _store(ctx.conn, OTHER, _iso(other))
    _store(ctx.conn, MADE, dest.name)
    log.info("Saved %s%s", dest.name, f" (removed {len(old)} older)" if old else "")
    return {"file": dest.name, "removed": len(old)}


def _rotate(conn, cfg, made: str) -> list:
    """Delete the worker's own copies beyond ``cfg.backup_keep``, oldest first, and never a copy something else
    made: a server job writing to the same folder may keep more (``backup --keep 14``), and the night the
    worker stands in for it mustn't trim that job's copies to the worker's number. Returns the deleted files."""
    names = json.loads(_setting(conn, MINE) or "[]") or [n for n in (_setting(conn, MADE),) if n]
    # dict.fromkeys: each name once, in order. A copy that's gone was rotated by a server job, or deleted by hand.
    there = [folder(cfg) / n for n in dict.fromkeys([*names, made]) if (folder(cfg) / n).exists()]
    old, kept = there[: -cfg.backup_keep], there[-cfg.backup_keep :]
    for path in old:
        path.unlink(missing_ok=True)
    _store(conn, MINE, json.dumps([p.name for p in kept]))
    return old
