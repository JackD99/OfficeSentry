"""The audit log add-on's periodic job (registered through ``worker.JOB_MODULES``): it queues an ``audit_check``
task for each client still waiting for its approval to be confirmed, or approved but with a feed that hasn't
started yet (``addon.due``), and queues the ``audit_stop`` task again for a client whose stop was refused at the
MSP's end (``addon.stops_due``). The task threads do the signing in. The add-on's nightly collection is queued
with everything else (``worker.schedule_tick``)."""

from __future__ import annotations

from datetime import UTC

from ..schedule import periodic
from . import addon


@periodic("audit_checks")
def _check_job(ctx) -> None:
    now = ctx.now if ctx.now.tzinfo else ctx.now.replace(tzinfo=UTC)
    for tenant_id in addon.due(ctx.conn, now):
        addon.queue_check(ctx.conn, tenant_id)
    for tenant_id in addon.stops_due(ctx.conn, now):
        addon.retry_stop(ctx.conn, tenant_id)
