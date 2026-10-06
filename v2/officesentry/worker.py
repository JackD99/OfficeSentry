"""Background worker: runs queued collector runs, the collection schedule,
emailed reports and snapshot retention.

One process, no broker. Run it with ``python -m officesentry worker``. Inside
it, ``cfg.worker_concurrency`` collector threads each claim and execute runs
(one tenant per thread at a time, see ``store.claim_next_run``), while the main
thread does the once-a-minute housekeeping, so a long collection doesn't hold
up the schedule or emails. Every thread has its own SQLite connection. Only
run one worker process: at start it marks any run left 'running' as failed.
It records a heartbeat so the portal and ``healthcheck`` can tell it is running.
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

import requests

from . import alerts, clouds, findings, lifecycle, retention, schedule, store, throttle
from .collectors import REGISTRY, RunContext
from .config import Config
from .connection_check import explain
from .crypto import Vault
from .db import connect, migrate, prune_audit, utcnow
from .exchange import ExchangeClient, ExchangeError
from .graph import GraphClient, GraphError, LicenseRequired, PermissionMissing
from .reports import monthly_review
from .text import plural

log = logging.getLogger(__name__)


class NotConnected(Exception):
    pass


_local = threading.local()


def _session() -> requests.Session:
    """One HTTP session per thread, so its connections to Microsoft are reused from run to run.

    Sessions aren't shared between threads; each collector thread has its own.
    """
    if getattr(_local, "session", None) is None:
        _local.session = requests.Session()
    return _local.session


def connection_cloud(tenant: dict, connection: dict | None) -> str:
    """The cloud to sign in to, once the tenant's app connection is known to be usable there.

    An app registration only works in the cloud it was registered in, so a tenant in
    GCC High, DoD or China needs an app connection set up for that same cloud.
    """
    cloud = clouds.of(tenant)
    if not connection or not connection["client_id"]:
        if not cloud.is_default:
            raise NotConnected(clouds.app_needed(cloud))
        raise NotConnected("tenant has no app connection with a client ID")
    if clouds.of(connection).key != cloud.key:
        raise NotConnected(clouds.app_needed(cloud))
    return cloud.key


def default_graph_factory(vault: Vault):
    def factory(tenant: dict, connection: dict | None) -> GraphClient:
        cloud = connection_cloud(tenant, connection)
        return GraphClient.from_certificate(
            tenant["tenant_ref"], connection["client_id"],
            vault.decrypt(connection["key_enc"]), connection["thumbprint"], cloud=cloud, session=_session(),
        )
    return factory


def default_exchange_factory(vault: Vault):
    def factory(tenant: dict, connection: dict | None) -> ExchangeClient:
        cloud = connection_cloud(tenant, connection)
        return ExchangeClient.from_certificate(
            tenant["tenant_ref"], connection["client_id"],
            vault.decrypt(connection["key_enc"]), connection["thumbprint"], cloud=cloud, session=_session(),
        )
    return factory


def execute_run(conn, run, graph_factory, exchange_factory=None, alerting: alerts.Alerting | None = None,
                client_id: str | None = None) -> str:
    """Execute one claimed run. Returns the final status.

    ``client_id`` is the app the graph client signs in as, when that isn't the
    tenant's app connection (``live-test`` signs in with a separate test app).

    Findings are synced (and alerted on) once the tenant has nothing else queued
    or running, so a night's collection evaluates the checks once on complete
    data rather than after each of its runs.
    """
    status = _collect(conn, run, graph_factory, exchange_factory, client_id)
    tenant = store.get_tenant(conn, run["tenant_id"])
    if tenant is not None and not conn.execute(
            "SELECT 1 FROM runs WHERE tenant_id = ? AND status IN ('queued', 'running')", (tenant["id"],)).fetchone():
        _sync(conn, tenant, run["id"], alerting)
        # Rebuild the tenant's dashboard row now rather than on the next page load.
        warm_rollup(conn, store.get_tenant(conn, tenant["id"]))
    return status


def _sync(conn, tenant, run_id: int, alerting: alerts.Alerting | None) -> None:
    if not conn.execute("SELECT 1 FROM runs WHERE tenant_id = ? AND status IN ('succeeded', 'partial') LIMIT 1",
                        (tenant["id"],)).fetchone():
        return  # nothing collected yet
    try:
        stats = findings.sync(conn, tenant)
        if any(stats.values()):
            store.log_event(conn, run_id, "info", "Findings: {new} new, {reopened} reopened, {resolved} resolved"
                            .format(**stats))
        alerts.after_sync(conn, alerting, tenant, stats, run_id)
    except Exception:  # a rule bug must not mark a good collection as failed
        store.log_event(conn, run_id, "error", "Updating findings failed:\n" + traceback.format_exc())


def _collect(conn, run, graph_factory, exchange_factory, client_id: str | None) -> str:
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
                         capabilities=store.tenant_capabilities(tenant),
                         client_id=client_id or (connection["client_id"] if connection else None))
        if exchange_factory is not None:
            ctx.exchange_factory = lambda: exchange_factory(dict(tenant), dict(connection) if connection else None)
        if why := clouds.missing(clouds.of(tenant), collector_cls.key):
            ctx.warn(why)  # Microsoft doesn't offer it in this national cloud: partial, not failed
        else:
            collector_cls().collect(ctx)
    except (NotConnected, PermissionMissing, LicenseRequired, GraphError, ExchangeError) as exc:
        store.log_event(conn, run_id, "error", str(exc))
        # Sign-in failures get the plain explanation; the raw message stays in the run log.
        problem = explain(getattr(exc, "message", ""), getattr(exc, "code", ""))
        store.finish_run(conn, run_id, "failed", f"Sign-in failed: {problem}" if problem else str(exc))
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
    # Data came back, so consent exists even if it was granted outside Office Sentry's consent link.
    conn.execute("UPDATE tenants SET consented_at = COALESCE(consented_at, ?) WHERE id = ?",
                 (utcnow(), run["tenant_id"]))
    if tenant["consented_at"] is None:
        # Consent was given outside the link (say the client's admin opened it): collect the rest now
        # rather than at the next nightly slot.
        queued = lifecycle.queue_first_collection(conn, run["tenant_id"])
        if queued:
            store.log_event(conn, run_id, "info", f"Consent confirmed; queued {plural(queued, 'data source')}")
    return status


def warm_rollup(conn, tenant) -> None:
    try:
        monthly_review.rollup_row(conn, tenant, cached=True)
    except Exception:  # the page builds it again, and says what's wrong
        log.exception("Building the dashboard row for tenant %s failed", tenant["id"])


def warm_rollups(conn) -> None:
    """Rebuild every dashboard row that's out of date, once a day as "new" and the 4-week comparison move."""
    for tenant in conn.execute("SELECT * FROM tenants WHERE archived_at IS NULL ORDER BY id").fetchall():
        warm_rollup(conn, tenant)


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
        # Nothing is scheduled before consent: every run would fail to sign in.
        # Paused and archived tenants keep their data but aren't collected.
        "SELECT id FROM tenants WHERE enabled = 1 AND archived_at IS NULL AND connection_id IS NOT NULL"
        " AND consented_at IS NOT NULL ORDER BY id"
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


def clear_heartbeat(conn) -> None:
    """A worker that stopped cleanly says so at once, rather than looking alive for HEARTBEAT_STALE."""
    conn.execute("DELETE FROM settings WHERE key = ?", (HEARTBEAT_KEY,))


def last_seen(conn) -> datetime | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (HEARTBEAT_KEY,)).fetchone()
    return datetime.fromisoformat(row["value"].replace("Z", "+00:00")) if row else None


def is_alive(conn, now: datetime | None = None) -> bool:
    seen = last_seen(conn)
    return seen is not None and (now or datetime.now(UTC)) - seen < HEARTBEAT_STALE


# The worker counts as stuck when its main loop (housekeeping) hasn't come
# round for this long, or a single collection has run for longer than RUN_STALE.
LOOP_STALE = timedelta(minutes=10)
RUN_STALE = timedelta(hours=4)


class Liveness:
    """What the worker's threads are doing, shared with the heartbeat thread.

    The heartbeat is only written while the worker makes progress, so a
    wedged main loop or a collector thread stuck in one run (a deadlocked
    write, a hung request) stops the heartbeat and shows as unhealthy in the
    portal banner and ``healthcheck worker``. Collector threads record their
    runs with ``start_run``/``end_run``; the main loop calls ``tick``.
    """

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.loop_at = clock()
        self.runs: dict[int, tuple[int, float]] = {}  # thread ident -> (run id, started)
        self._lock = threading.Lock()

    def tick(self) -> None:
        with self._lock:
            self.loop_at = self.clock()

    def start_run(self, run_id: int) -> None:
        with self._lock:
            self.runs[threading.get_ident()] = (run_id, self.clock())

    def end_run(self) -> None:
        with self._lock:
            self.runs.pop(threading.get_ident(), None)

    def problem(self) -> str | None:
        """Why the worker looks stuck, or None while it's making progress."""
        with self._lock:
            now = self.clock()
            for run_id, started in self.runs.values():
                if now - started > RUN_STALE.total_seconds():
                    return f"run {run_id} has been going for {int((now - started) // 60)} minutes"
            if now - self.loop_at > LOOP_STALE.total_seconds():
                return f"the main loop hasn't moved for {int((now - self.loop_at) // 60)} minutes"
            return None


def heartbeat_step(conn, liveness: Liveness | None) -> bool:
    """Record the heartbeat if the main loop is making progress. Returns whether it did."""
    problem = liveness.problem() if liveness else None
    if problem:
        log.error("Worker looks stuck (%s); not recording a heartbeat", problem)
        return False
    beat(conn)
    return True


def _heartbeat_thread(db_path, stop: threading.Event, liveness: Liveness | None = None) -> threading.Thread:
    """Report liveness from a side thread, so a long collection doesn't look like a dead worker."""
    def loop():
        conn = connect(db_path)
        try:
            while not stop.is_set():
                try:
                    heartbeat_step(conn, liveness)
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
        log.info("Queued %s", plural(queued, "scheduled run"))
    schedule.run_periodic(schedule.TickContext(app=app, conn=conn, vault=vault, cfg=cfg, now=now))


def prune(conn, cfg: Config) -> int:
    """The daily retention pass: the admin's settings (``retention.apply``), then sign-in noise."""
    removed = retention.apply(conn, cfg)["snapshots"]
    throttle.prune(conn)
    if noise := prune_audit(conn):
        log.info("Retention: removed %s", plural(noise, "old failed sign-in record"))
    conn.execute("PRAGMA optimize")  # keeps the query planner's statistics current
    return removed


TICK_EVERY = 60  # seconds between housekeeping passes
IDLE_WAIT = 2  # seconds to wait when the queue is empty


def _rollback(conn) -> None:
    if conn.in_transaction:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            log.warning("Rollback after a failed housekeeping pass failed", exc_info=True)


def _execute(conn, run, graph_factory, exchange_factory, alerting, liveness: Liveness) -> None:
    log.info("Run %s: %s for tenant %s", run["id"], run["collector"], run["tenant_id"])
    liveness.start_run(run["id"])
    try:
        status = execute_run(conn, run, graph_factory, exchange_factory, alerting)
        alerts.after_run(conn, alerting, run["id"], status)
    finally:
        liveness.end_run()


def _claim(conn):
    try:
        return store.claim_next_run(conn)
    except sqlite3.OperationalError:
        log.exception("Could not claim the next run; trying again shortly")
        _rollback(conn)
        return None


def _collector_thread(cfg: Config, number: int, stop: threading.Event, graph_factory, exchange_factory,
                      alerting, liveness: Liveness | None = None) -> threading.Thread:
    """Claim and execute queued runs until ``stop`` is set, then finish the current run."""
    liveness = liveness or Liveness()

    def loop():
        conn = connect(cfg.db_path)
        try:
            while not stop.is_set():
                run = _claim(conn)
                if run is None:
                    stop.wait(IDLE_WAIT)
                    continue
                try:
                    _execute(conn, run, graph_factory, exchange_factory, alerting, liveness)
                except Exception:  # say, the database stayed locked: keep the thread alive
                    log.exception("Run %s could not be finished", run["id"])
                    _rollback(conn)
        finally:
            conn.close()
    thread = threading.Thread(target=loop, name=f"collector-{number}", daemon=True)
    thread.start()
    return thread


def _housekeeping(app, conn, vault, cfg: Config) -> None:
    try:
        housekeeping(app, conn, vault, cfg)
    except Exception:  # say "database is locked": try again next minute rather than exit
        log.exception("Housekeeping failed; trying again in a minute")
        _rollback(conn)


def _daily(conn, cfg: Config) -> None:
    try:
        prune(conn, cfg)
    except sqlite3.Error:
        log.exception("Pruning old snapshots failed")
        _rollback(conn)
    warm_rollups(conn)


def run_worker(cfg: Config, *, once: bool = False, graph_factory=None, exchange_factory=None,
               stop: threading.Event | None = None) -> None:
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
        log.warning("Marked %s as failed", plural(recovered, "orphaned run"))

    stop = stop or threading.Event()
    liveness = Liveness()
    if once:
        # Tests and one-off use: housekeeping, then drain the queue in this thread.
        _housekeeping(app, conn, vault, cfg)
        _daily(conn, cfg)
        while (run := _claim(conn)) is not None:
            _execute(conn, run, graph_factory, exchange_factory, alerting, liveness)
        return

    if threading.current_thread() is threading.main_thread():
        # Finish the current runs on "docker stop" rather than leave them half done.
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
    heartbeat = _heartbeat_thread(cfg.db_path, stop, liveness)
    log.info("Worker started with %s. Collecting on '%s' (%s); next slot %s",
             plural(cfg.worker_concurrency, "collector thread"), cfg.collect_cron, cfg.timezone,
             schedule.next_fire(cfg.collect_cron, cfg.timezone, datetime.now(UTC)).isoformat())
    collectors = [_collector_thread(cfg, n + 1, stop, graph_factory, exchange_factory, alerting, liveness)
                  for n in range(cfg.worker_concurrency)]

    last_day = None
    while not stop.is_set():
        started = time.monotonic()
        liveness.tick()
        _housekeeping(app, conn, vault, cfg)
        today = datetime.now(UTC).date()
        if today != last_day:
            _daily(conn, cfg)
            last_day = today
        liveness.tick()
        stop.wait(max(IDLE_WAIT, TICK_EVERY - (time.monotonic() - started)))
    for thread in collectors:
        thread.join()
    heartbeat.join(timeout=10)
    try:
        clear_heartbeat(conn)
    except sqlite3.Error:
        log.warning("Could not clear the worker heartbeat", exc_info=True)
    log.info("Worker stopped")
