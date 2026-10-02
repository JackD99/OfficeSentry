"""Background worker: runs queued collector runs and the nightly schedule.

One process, no broker. Run it with ``python -m officesentry worker``.
"""

from __future__ import annotations

import logging
import time
import traceback
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from . import alerts, findings, store
from .collectors import REGISTRY, RunContext
from .config import Config
from .crypto import Vault
from .db import connect, migrate
from .exchange import ExchangeClient, ExchangeError
from .graph import GraphClient, GraphError, LicenseRequired, PermissionMissing

log = logging.getLogger(__name__)


class NotConnected(Exception):
    pass


def default_graph_factory(vault: Vault):
    def factory(tenant: dict, connection: dict | None) -> GraphClient:
        if not connection or not connection["client_id"]:
            raise NotConnected("tenant has no app connection with a client ID")
        return GraphClient.from_certificate(
            tenant["tenant_ref"], connection["client_id"],
            vault.decrypt(connection["key_enc"]), connection["thumbprint"],
        )
    return factory


def default_exchange_factory(vault: Vault):
    def factory(tenant: dict, connection: dict | None) -> ExchangeClient:
        if not connection or not connection["client_id"]:
            raise NotConnected("tenant has no app connection with a client ID")
        return ExchangeClient.from_certificate(
            tenant["tenant_ref"], connection["client_id"],
            vault.decrypt(connection["key_enc"]), connection["thumbprint"],
        )
    return factory


def execute_run(conn, run, graph_factory, exchange_factory=None, alerting: alerts.Alerting | None = None) -> str:
    """Execute one claimed run. Returns the final status."""
    run_id = run["id"]
    tenant = store.get_tenant(conn, run["tenant_id"])
    collector_cls = REGISTRY.get(run["collector"])
    if tenant is None or collector_cls is None:
        store.finish_run(conn, run_id, "failed", "unknown tenant or collector")
        return "failed"

    connection = store.get_connection(conn, tenant["connection_id"]) if tenant["connection_id"] else None
    store.log_event(conn, run_id, "info", f"Starting {collector_cls.title} for {tenant['name']}")
    try:
        graph = graph_factory(dict(tenant), dict(connection) if connection else None)
        ctx = RunContext(conn=conn, run_id=run_id, tenant=dict(tenant), graph=graph,
                         capabilities=store.tenant_capabilities(tenant))
        if exchange_factory is not None:
            ctx.exchange_factory = lambda: exchange_factory(dict(tenant), dict(connection) if connection else None)
        collector_cls().collect(ctx)
    except (NotConnected, PermissionMissing, LicenseRequired, GraphError, ExchangeError) as exc:
        store.log_event(conn, run_id, "error", str(exc))
        store.finish_run(conn, run_id, "failed", str(exc))
        return "failed"
    except Exception as exc:  # a collector bug must not kill the worker
        store.log_event(conn, run_id, "error", traceback.format_exc())
        store.finish_run(conn, run_id, "failed", f"{type(exc).__name__}: {exc}")
        return "failed"

    if ctx.truncated:
        conn.execute("UPDATE runs SET truncated = 1 WHERE id = ?", (run_id,))
    status = "partial" if ctx.warnings else "succeeded"
    store.log_event(conn, run_id, "info", f"Finished: {status}")
    store.finish_run(conn, run_id, status, "; ".join(ctx.warnings) or None)
    try:
        stats = findings.sync(conn, store.get_tenant(conn, run["tenant_id"]))
        if any(stats.values()):
            store.log_event(conn, run_id, "info", "Findings: {new} new, {reopened} reopened, {resolved} resolved"
                            .format(**stats))
        alerts.after_sync(conn, alerting, tenant, stats, run_id)
    except Exception:  # a rule bug must not mark a good collection as failed
        store.log_event(conn, run_id, "error", "Updating findings failed:\n" + traceback.format_exc())
    return status


def current_slot(now: datetime, tz: str, hour: int) -> datetime:
    """The most recent nightly slot at or before ``now`` (returned in UTC)."""
    local = now.astimezone(ZoneInfo(tz))
    slot = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if slot > local:
        slot -= timedelta(days=1)
    return slot.astimezone(UTC)


def schedule_tick(conn, cfg: Config, now: datetime | None = None) -> int:
    """Queue the nightly runs for the current slot that are not queued yet."""
    now = now or datetime.now(UTC)
    slot = current_slot(now, cfg.timezone, cfg.nightly_hour).strftime("%Y-%m-%dT%H:%M:%SZ")
    queued = 0
    tenants = conn.execute(
        "SELECT id FROM tenants WHERE enabled = 1 AND connection_id IS NOT NULL ORDER BY id"
    ).fetchall()
    for tenant in tenants:
        for key, cls in REGISTRY.items():
            if not cls.nightly:
                continue
            done = conn.execute(
                "SELECT 1 FROM runs WHERE tenant_id = ? AND collector = ? AND scheduled_for = ?",
                (tenant["id"], key, slot),
            ).fetchone()
            if not done:
                store.enqueue_run(conn, tenant["id"], key, trigger="schedule", scheduled_for=slot)
                queued += 1
    return queued


def run_worker(cfg: Config, *, once: bool = False, graph_factory=None, exchange_factory=None) -> None:
    conn = connect(cfg.db_path)
    migrate(conn)
    vault = Vault(cfg.encryption_keys)
    graph_factory = graph_factory or default_graph_factory(vault)
    exchange_factory = exchange_factory or default_exchange_factory(vault)
    alerting = alerts.Alerting(cfg.base_url, vault)

    from . import mailer
    from .web import create_app

    app = create_app(cfg)  # for rendering emailed reports
    recovered = store.recover_orphaned_runs(conn)
    if recovered:
        log.warning("Marked %s orphaned run(s) as failed", recovered)

    last_tick = 0.0
    last_prune_day = None
    while True:
        if time.monotonic() - last_tick > 60:
            schedule_tick(conn, cfg)
            last_tick = time.monotonic()
            try:
                mailer.run_due(app, conn, vault, cfg.timezone)
            except Exception:  # never let email problems stop collections
                log.exception("Sending scheduled reports failed")
            today = datetime.now(UTC).date()
            if today != last_prune_day:
                removed = store.prune_snapshots(conn, cfg.retention_days)
                if removed:
                    log.info("Pruned %s snapshot object(s)", removed)
                last_prune_day = today

        run = store.claim_next_run(conn)
        if run is not None:
            log.info("Run %s: %s for tenant %s", run["id"], run["collector"], run["tenant_id"])
            status = execute_run(conn, run, graph_factory, exchange_factory, alerting)
            alerts.after_run(conn, alerting, run["id"], status)
            continue
        if once:
            return
        time.sleep(2)

