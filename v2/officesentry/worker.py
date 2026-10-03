"""Background worker: runs queued collector runs, the collection schedule,
emailed reports and snapshot retention.

One process, no broker. Run it with ``python -m officesentry worker``. It
records a heartbeat so the portal and ``healthcheck`` can tell it is running.
"""

from __future__ import annotations

import importlib
import logging
import signal
import sqlite3
import threading
import time
import traceback
from datetime import UTC, datetime, timedelta

from . import alerts, findings, schedule, store
from .collectors import REGISTRY, RunContext
from .config import Config
from .crypto import Vault
from .db import connect, migrate, utcnow
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


def schedule_tick(conn, cfg: Config, now: datetime | None = None) -> int:
    """Queue the collection runs for the latest cron slot that are not queued yet.

    A collector that already succeeded within ``cfg.max_age_hours`` before the
    slot (say, a manual run that evening) is skipped for that slot.
    """
    now = now or datetime.now(UTC)
    slot_dt = schedule.previous_fire(cfg.collect_cron, cfg.timezone, now)
    slot = schedule.iso(slot_dt)
    queued = 0
    tenants = conn.execute(
        "SELECT id FROM tenants WHERE enabled = 1 AND connection_id IS NOT NULL ORDER BY id"
    ).fetchall()
    for tenant in tenants:
        for key in schedule.nightly_collectors():
            done = conn.execute(
                "SELECT 1 FROM runs WHERE tenant_id = ? AND collector = ? AND scheduled_for = ?",
                (tenant["id"], key, slot),
            ).fetchone()
            if done or schedule.is_fresh(conn, tenant["id"], key, slot_dt, cfg.max_age_hours):
                continue
            store.enqueue_run(conn, tenant["id"], key, trigger="schedule", scheduled_for=slot)
            queued += 1
    return queued


# -- Heartbeat ---------------------------------------------------------------

HEARTBEAT_KEY = "worker_seen_at"
HEARTBEAT_EVERY = 30  # seconds
HEARTBEAT_STALE = timedelta(minutes=5)


def beat(conn) -> None:
    conn.execute("INSERT INTO settings (key, value) VALUES (?, ?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (HEARTBEAT_KEY, utcnow()))


def last_seen(conn) -> datetime | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (HEARTBEAT_KEY,)).fetchone()
    return datetime.fromisoformat(row["value"].replace("Z", "+00:00")) if row else None


def is_alive(conn, now: datetime | None = None) -> bool:
    seen = last_seen(conn)
    return seen is not None and (now or datetime.now(UTC)) - seen < HEARTBEAT_STALE


def _heartbeat_thread(db_path, stop: threading.Event) -> threading.Thread:
    """Report liveness from a side thread, so a long collection doesn't look like a dead worker."""
    def loop():
        conn = connect(db_path)
        try:
            while not stop.is_set():
                try:
                    beat(conn)
                except sqlite3.Error:
                    log.warning("Could not record worker heartbeat", exc_info=True)
                stop.wait(HEARTBEAT_EVERY)
        finally:
            conn.close()
    thread = threading.Thread(target=loop, name="heartbeat", daemon=True)
    thread.start()
    return thread


# -- Main loop ---------------------------------------------------------------


# Modules that register ``schedule.periodic`` jobs. Add yours here.
JOB_MODULES = ("officesentry.delivery",)


def housekeeping(app, conn, vault, cfg: Config, now: datetime | None = None) -> None:
    """The once-a-minute work: queue scheduled collections, then run the periodic jobs."""
    for module in JOB_MODULES:
        importlib.import_module(module)
    now = now or datetime.now(UTC)
    queued = schedule_tick(conn, cfg, now)
    if queued:
        log.info("Queued %s scheduled run(s)", queued)
    schedule.run_periodic(schedule.TickContext(app=app, conn=conn, vault=vault, cfg=cfg, now=now))


def prune(conn, cfg: Config) -> int:
    removed = store.prune_snapshots(conn, cfg.keep_daily, cfg.keep_monthly, cfg.timezone)
    if removed:
        log.info("Retention: removed the stored objects of %s older run(s)", removed)
    return removed


def run_worker(cfg: Config, *, once: bool = False, graph_factory=None, exchange_factory=None) -> None:
    conn = connect(cfg.db_path)
    migrate(conn)
    vault = Vault(cfg.encryption_keys)
    graph_factory = graph_factory or default_graph_factory(vault)
    exchange_factory = exchange_factory or default_exchange_factory(vault)
    from .web import create_app

    app = create_app(cfg)  # for rendering emailed reports and alert emails
    alerting = alerts.Alerting(cfg.base_url, vault, app)
    recovered = store.recover_orphaned_runs(conn)
    if recovered:
        log.warning("Marked %s orphaned run(s) as failed", recovered)

    stop = threading.Event()
    if not once:
        # Finish the current run on "docker stop" rather than leave it half done.
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        _heartbeat_thread(cfg.db_path, stop)
    log.info("Worker started. Collecting on '%s' (%s); next slot %s", cfg.collect_cron, cfg.timezone,
             schedule.next_fire(cfg.collect_cron, cfg.timezone, datetime.now(UTC)).isoformat())

    last_tick = None
    last_prune_day = None
    while not stop.is_set():
        if last_tick is None or time.monotonic() - last_tick >= 60:
            last_tick = time.monotonic()
            housekeeping(app, conn, vault, cfg)
            today = datetime.now(UTC).date()
            if today != last_prune_day:
                try:
                    prune(conn, cfg)
                except sqlite3.Error:
                    log.exception("Pruning old snapshots failed")
                last_prune_day = today

        run = store.claim_next_run(conn)
        if run is not None:
            log.info("Run %s: %s for tenant %s", run["id"], run["collector"], run["tenant_id"])
            status = execute_run(conn, run, graph_factory, exchange_factory, alerting)
            alerts.after_run(conn, alerting, run["id"], status)
            continue
        if once:
            break
        stop.wait(2)
    stop.set()
    log.info("Worker stopped")
