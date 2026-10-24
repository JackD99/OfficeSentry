"""Background worker: runs queued collector runs, background tasks, the
collection schedule, emailed reports and snapshot retention.

One process, no broker. Run it with ``python -m officesentry worker``. Inside
it, ``cfg.worker_concurrency`` collector threads each claim and execute runs
(one tenant per thread at a time, see ``store.claim_next_run``) and
``TASK_THREADS`` task threads run ``tasks.py`` work (month-end approvals,
due report emails and each tenant's precomputed cross-tenant rows), while the main thread does the once-a-minute
housekeeping, so a long collection or a month-end of PDFs doesn't hold up the
schedule. Every thread has its own SQLite connection. Claimed runs and tasks
carry a lease the heartbeat thread renews; one whose worker died is failed
(a run) or queued again (a task) once its lease runs out. It records a
heartbeat so the portal and ``healthcheck`` can tell it is running.
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
from functools import partial

import requests

from . import (
    activity_alerts,
    alerts,
    app_setup,
    clouds,
    findings,
    lifecycle,
    logs,
    ratelimit,
    retention,
    schedule,
    state_history,
    store,
    tasks,
    throttle,
)
from .auditlog import addon
from .auditlog.feed import FeedClient, FeedError
from .autodesk import setup as autodesk_setup
from .autodesk.api import AutodeskClient, AutodeskError
from .collectors import REGISTRY, RunContext
from .collectors.base import NothingToRead
from .config import Config
from .connection_check import explain
from .crypto import Vault
from .db import connect, migrate, prune_audit, utcnow
from .exchange import ExchangeClient, ExchangeError
from .graph import GraphClient, GraphError, LicenseRequired, PermissionMissing
from .reports import monthly_review, precomputed
from .sharepoint_api import SharePointError
from .text import plural

log = logging.getLogger(__name__)


class NotConnected(Exception):
    pass


# Microsoft's answers that waiting fixes, once the client's own retries of the request have run out: too many
# requests, a server error or outage, or no answer at all (status 0: the network, or the sign-in service).
PASSING_STATUSES = {0, 429, 500, 502, 503, 504}


def passing(exc: BaseException) -> bool:
    """Whether a run that failed with ``exc`` may work if tried again later, with nobody changing anything
    (``store.plan_retry``). A permission, a licence, a sign-in Microsoft refused or a bug needs someone first."""
    if isinstance(exc, GraphError | ExchangeError | FeedError | SharePointError | AutodeskError):
        return exc.status in PASSING_STATUSES
    if isinstance(exc, sqlite3.OperationalError):  # the database stayed locked past the busy timeout
        return any(word in str(exc) for word in ("locked", "busy"))
    return False


def _failed(conn, run_id: int, error: str, exc: BaseException) -> None:
    """Record the failure, and plan the run's retry when waiting may fix it."""
    with store.atomic(conn):
        store.finish_run(conn, run_id, "failed", error)
        when = store.plan_retry(conn, run_id) if passing(exc) else None
        if when:
            store.log_event(conn, run_id, "info", f"Trying again by itself at {when[11:16]} UTC")


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
        raise NotConnected("client has no app connection with a client ID")
    if clouds.of(connection).key != cloud.key:
        raise NotConnected(clouds.app_needed(cloud))
    return cloud.key


def default_graph_factory(vault: Vault):
    def factory(tenant: dict, connection: dict | None) -> GraphClient:
        cloud = connection_cloud(tenant, connection)
        return GraphClient.from_certificate(
            tenant["tenant_ref"],
            connection["client_id"],
            vault.decrypt(connection["key_enc"]),
            connection["thumbprint"],
            cloud=cloud,
            session=_session(),
            limiter=ratelimit.LIMITER,
        )

    return factory


def default_exchange_factory(vault: Vault):
    def factory(tenant: dict, connection: dict | None) -> ExchangeClient:
        cloud = connection_cloud(tenant, connection)
        return ExchangeClient.from_certificate(
            tenant["tenant_ref"],
            connection["client_id"],
            vault.decrypt(connection["key_enc"]),
            connection["thumbprint"],
            cloud=cloud,
            session=_session(),
            limiter=ratelimit.LIMITER,
        )

    return factory


def default_feed_factory(vault: Vault):
    """The audit log add-on's feed client for a tenant, signed in as the add-on's own app in its cloud."""

    def factory(conn, tenant: dict) -> FeedClient:
        return addon.client_for(conn, vault, tenant, session=_session(), shared=True, limiter=ratelimit.LIMITER)

    return factory


def default_autodesk_factory(vault: Vault):
    """An Autodesk client for one Autodesk sign-in, as the MSP's Autodesk app (``autodesk/setup.py``)."""

    def factory(conn, sign_in) -> AutodeskClient:
        return autodesk_setup.client_for(conn, vault, sign_in, session=_session())

    return factory


def execute_run(
    conn,
    run,
    graph_factory,
    exchange_factory=None,
    alerting: alerts.Alerting | None = None,
    client_id: str | None = None,
    feed_factory=None,
    autodesk_factory=None,
) -> str:
    """Execute one claimed run. Returns the final status.

    ``client_id`` is the app the graph client signs in as, when that isn't the
    tenant's app connection (``live-test`` signs in with a separate test app).
    ``feed_factory`` makes the audit log add-on's feed client (``default_feed_factory``), and
    ``autodesk_factory`` an Autodesk sign-in's client (``default_autodesk_factory``).

    Findings are synced (and alerted on) once the tenant has nothing else queued
    or running, so a night's collection evaluates the checks once on complete
    data rather than after each of its runs.
    """
    status = _collect(conn, run, graph_factory, exchange_factory, client_id, feed_factory, autodesk_factory)
    tenant = store.get_tenant(conn, run["tenant_id"])
    if (
        tenant is not None
        and not conn.execute(
            "SELECT 1 FROM runs WHERE tenant_id = ? AND status IN ('queued', 'running')", (tenant["id"],)
        ).fetchone()
    ):
        _sync(conn, tenant, run["id"], alerting)
        # Rebuild the tenant's dashboard row now rather than on the next page load, and its rows on
        # What changed and the check matrix in a background task.
        warm_rollup(conn, store.get_tenant(conn, tenant["id"]))
        _queue_precompute(conn, tenant["id"])
    return status


def _sync(conn, tenant, run_id: int, alerting: alerts.Alerting | None) -> None:
    if not conn.execute(
        "SELECT 1 FROM runs WHERE tenant_id = ? AND status IN ('succeeded', 'partial') LIMIT 1", (tenant["id"],)
    ).fetchone():
        return  # nothing collected yet
    try:
        stats = findings.sync(conn, tenant)
        if any(stats.values()):
            store.log_event(
                conn, run_id, "info", "Findings: {new} new, {reopened} reopened, {resolved} resolved".format(**stats)
            )
        alerts.after_sync(conn, alerting, tenant, stats, run_id)
    except Exception:  # a rule bug must not mark a good collection as failed
        store.log_event(conn, run_id, "error", "Updating findings failed:\n" + traceback.format_exc())


def _collect(
    conn, run, graph_factory, exchange_factory, client_id: str | None, feed_factory=None, autodesk_factory=None
) -> str:
    """Run the collector, then store how many requests it made to Microsoft (whatever the outcome)."""
    telemetry = ratelimit.Telemetry()
    try:
        return _run_collector(
            conn, run, graph_factory, exchange_factory, client_id, telemetry, feed_factory, autodesk_factory
        )
    finally:
        try:
            ratelimit.save(conn, run["id"], telemetry)
        except Exception:  # counting must never fail a collection
            log.exception("Storing request telemetry for run %s failed", run["id"])


def _run_collector(
    conn,
    run,
    graph_factory,
    exchange_factory,
    client_id: str | None,
    telemetry: ratelimit.Telemetry,
    feed_factory=None,
    autodesk_factory=None,
) -> str:
    run_id = run["id"]
    tenant = store.get_tenant(conn, run["tenant_id"])
    collector_cls = REGISTRY.get(run["collector"])
    if tenant is None or collector_cls is None:
        store.finish_run(conn, run_id, "failed", "unknown client or collector")
        return "failed"

    connection = store.get_connection(conn, tenant["connection_id"]) if tenant["connection_id"] else None
    store.log_event(conn, run_id, "info", f"Starting {collector_cls.title} for {tenant['name']}")
    try:
        graph = graph_factory(dict(tenant), dict(connection) if connection else None)
        graph.telemetry = telemetry
        ctx = RunContext(
            conn=conn,
            run_id=run_id,
            tenant=dict(tenant),
            graph=graph,
            capabilities=store.tenant_capabilities(tenant),
            client_id=client_id or (connection["client_id"] if connection else None),
        )
        if exchange_factory is not None:

            def exchange() -> ExchangeClient:
                client = exchange_factory(dict(tenant), dict(connection) if connection else None)
                client.telemetry = telemetry
                return client

            ctx.exchange_factory = exchange
        if feed_factory is not None:

            def feed() -> FeedClient:
                client = feed_factory(conn, dict(tenant))
                client.telemetry = telemetry
                return client

            ctx.feed_factory = feed
        if autodesk_factory is not None:

            def autodesk(sign_in) -> AutodeskClient:
                client = autodesk_factory(conn, sign_in)
                client.telemetry = telemetry
                return client

            ctx.autodesk_factory = autodesk
        if why := clouds.missing(clouds.of(tenant), collector_cls.key):
            ctx.warn(why)  # Microsoft doesn't offer it in this national cloud: partial, not failed
        else:
            collector_cls().collect(ctx)
            if failure := ctx.sign_in_failure():
                raise failure  # what it read isn't the tenant's data: keep the last good run
            ctx.note_exchange_cuts()
    except (
        NotConnected,
        NothingToRead,
        PermissionMissing,
        LicenseRequired,
        GraphError,
        ExchangeError,
        FeedError,
        SharePointError,
        addon.AddonError,
        AutodeskError,
        autodesk_setup.SetupError,
    ) as exc:
        _rollback(conn)  # whatever the collector left half-written isn't wanted, and mustn't hold the next write
        store.log_event(conn, run_id, "error", str(exc))
        # Sign-in failures get the plain explanation; the raw message stays in the run log.
        problem = explain(getattr(exc, "message", ""), getattr(exc, "code", ""))
        error = f"Sign-in failed: {problem}" if problem else str(exc)
        log.error("Run failed: %s", error.splitlines()[0][:300] if error else type(exc).__name__)
        _while_locked(conn, partial(_failed, conn, run_id, error, exc))
        return "failed"
    except Exception as exc:  # a collector bug must not kill the worker
        _rollback(conn)
        store.log_event(conn, run_id, "error", traceback.format_exc())
        log.exception("Run failed on a bug (%s)", type(exc).__name__)
        _while_locked(conn, partial(_failed, conn, run_id, f"{type(exc).__name__}: {exc}", exc))
        return "failed"

    status = "partial" if ctx.warnings else "succeeded"

    def finish() -> None:
        with store.atomic(conn):
            if ctx.truncated:
                conn.execute("UPDATE runs SET truncated = 1 WHERE id = ?", (run_id,))
            store.log_event(conn, run_id, "info", f"Finished: {status}")
            store.finish_run(conn, run_id, status, "; ".join(ctx.warnings) or None)

    # The data is stored by now: a lock here mustn't leave the run to be failed later as "worker stopped".
    _while_locked(conn, finish)
    try:
        state_history.record_finished(conn, run_id)
    except Exception:  # the history is a second copy, written in shadow: it must never fail a collection
        log.exception("Recording run %s in the state history failed", run_id)
    if collector_cls.addon:
        return status  # read as an add-on's own app, so it says nothing about the shared app's consent
    # Data came back, so consent exists even if it was granted outside Office Sentry's consent link.
    conn.execute(
        "UPDATE tenants SET consented_at = COALESCE(consented_at, ?) WHERE id = ?", (utcnow(), run["tenant_id"])
    )
    if tenant["consented_at"] is None:
        # Consent was given outside the link (say the client's admin opened it): collect the rest now
        # rather than at the next nightly slot.
        queued = lifecycle.queue_first_collection(conn, run["tenant_id"])
        if queued:
            store.log_event(conn, run_id, "info", f"Consent confirmed; queued {plural(queued, 'data source')}")
    return status


FINISH_TRIES = 10  # each waits out the 30-second busy timeout first, so about 5 minutes in all
FINISH_WAIT = 2  # seconds between tries


def _while_locked(conn, step) -> None:
    """Run ``step`` (one transaction), trying again while the database stays locked by another writer."""
    for attempt in range(FINISH_TRIES):
        try:
            step()
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc) or attempt + 1 == FINISH_TRIES:
                raise
            log.warning("The database is busy; trying again to record a finished run")
            _rollback(conn)
            time.sleep(FINISH_WAIT)


def warm_rollup(conn, tenant) -> None:
    try:
        monthly_review.rollup_row(conn, tenant, cached=True)
    except Exception:  # the page builds it again, and says what's wrong
        log.exception("Building the dashboard row for tenant %s failed", tenant["id"])


def _queue_precompute(conn, tenant_id: int | None = None, rebuild: bool = False) -> None:
    """Queue one tenant's precomputed rows (or every tenant's). Pages build a missing row themselves."""
    try:
        if tenant_id is None:
            precomputed.queue_all(conn, rebuild)
        else:
            precomputed.queue(conn, tenant_id, rebuild)
    except sqlite3.Error:
        log.exception("Queueing the precomputed rows failed")
        _rollback(conn)


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
        "SELECT * FROM tenants WHERE enabled = 1 AND archived_at IS NULL AND connection_id IS NOT NULL"
        " AND consented_at IS NOT NULL ORDER BY id"
    ).fetchall()
    for tenant in tenants:
        for key in schedule.nightly_collectors(conn, tenant):
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
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (HEARTBEAT_KEY, utcnow()),
    )


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
# round for this long, a single collection has run for longer than RUN_STALE,
# or a single task (one client's month-end, one email) for longer than TASK_STALE.
LOOP_STALE = timedelta(minutes=10)
RUN_STALE = timedelta(hours=4)
TASK_STALE = timedelta(hours=1)
DAILY_STALE = timedelta(hours=4)  # the daily pass (retention, every dashboard row), in its own thread
STALE = {"run": RUN_STALE, "task": TASK_STALE, "daily": DAILY_STALE}


class Liveness:
    """What the worker's threads are doing, shared with the heartbeat thread.

    The heartbeat is only written while the worker makes progress, so a
    wedged main loop or a collector thread stuck in one run (a deadlocked
    write, a hung request) stops the heartbeat and shows as unhealthy in the
    portal banner and ``healthcheck worker``. Collector threads record their
    runs with ``start_run``/``end_run``, task threads their tasks with
    ``start_task``/``end_task`` and the daily pass ``start_run(0, "daily")``;
    the main loop calls ``tick``. ``held`` is what this process has claimed,
    whose leases the heartbeat thread renews.
    """

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.loop_at = clock()
        self.runs: dict[int, tuple[str, int, float]] = {}  # thread ident -> ("run" or "task", id, started)
        self._lock = threading.Lock()

    def tick(self) -> None:
        with self._lock:
            self.loop_at = self.clock()

    def start_run(self, run_id: int, kind: str = "run") -> None:
        with self._lock:
            self.runs[threading.get_ident()] = (kind, run_id, self.clock())

    def end_run(self) -> None:
        with self._lock:
            self.runs.pop(threading.get_ident(), None)

    def start_task(self, task_id: int) -> None:
        self.start_run(task_id, "task")

    end_task = end_run

    def held(self, kind: str) -> list[int]:
        """The runs (``"run"``) or tasks (``"task"``) this process is working on."""
        with self._lock:
            return [item for k, item, _ in self.runs.values() if k == kind]

    def problem(self) -> str | None:
        """Why the worker looks stuck, or None while it's making progress."""
        with self._lock:
            now = self.clock()
            for kind, item, started in self.runs.values():
                if now - started > STALE.get(kind, TASK_STALE).total_seconds():
                    return f"{kind} {item} has been going for {int((now - started) // 60)} minutes"
            if now - self.loop_at > LOOP_STALE.total_seconds():
                return f"the main loop hasn't moved for {int((now - self.loop_at) // 60)} minutes"
            return None


def renew_leases(conn, liveness: Liveness | None) -> None:
    """Keep what this worker is running claimed. Done even when it looks stuck: a lease only says the
    process is alive, so another worker never takes over a run or task that is still going."""
    if liveness is None:
        return
    store.renew_run_leases(conn, liveness.held("run"))
    tasks.renew(conn, liveness.held("task"))


def expire_leases(conn, liveness: Liveness | None = None) -> None:
    """Fail runs and queue again tasks whose worker stopped without finishing them."""
    failed = store.expire_run_leases(conn, held=liveness.held("run") if liveness else ())
    if failed:
        log.warning("Marked %s as failed: the worker running it stopped", plural(failed, "run"))
    requeued, gave_up = tasks.expire_leases(conn, held=liveness.held("task") if liveness else ())
    if requeued or gave_up:
        log.warning("Tasks whose worker stopped: %s queued again, %s failed", requeued, gave_up)


def heartbeat_step(conn, liveness: Liveness | None) -> bool:
    """Record the heartbeat if the main loop is making progress. Returns whether it did."""
    try:
        renew_leases(conn, liveness)
    except sqlite3.Error:
        log.warning("Could not renew the leases of running work", exc_info=True)
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


# Modules that register ``schedule.periodic`` jobs, and ``tasks.handler`` kinds. Add yours here.
JOB_MODULES = (
    "officesentry.delivery",
    "officesentry.cert_lifecycle",
    "officesentry.packing",
    "officesentry.state_history",
    "officesentry.system_alerts",
    "officesentry.onboarding",
    "officesentry.lifecycle",
    "officesentry.updates",
    "officesentry.auditlog.jobs",
    "officesentry.nightly_backup",
)
TASK_MODULES = (
    "officesentry.delivery",
    "officesentry.month_end",
    "officesentry.reports.precomputed",
    "officesentry.onboarding",
    "officesentry.lifecycle",
    "officesentry.auditlog.addon",
    "officesentry.nightly_backup",
)
TASK_THREADS = 2  # rendering is CPU-bound: two let one client's approval pass a long run of others


def _load_modules() -> None:
    for module in (*JOB_MODULES, *TASK_MODULES):
        importlib.import_module(module)


def housekeeping(app, conn, vault, cfg: Config, now: datetime | None = None) -> None:
    """The once-a-minute work: queue scheduled collections, then run the periodic jobs."""
    _load_modules()
    now = now or datetime.now(UTC)
    queued = schedule_tick(conn, cfg, now)
    if queued:
        log.info("Queued %s", plural(queued, "scheduled run"))
    if retries := store.queue_retries(conn, now):
        log.info("Queued %s of runs that failed for a passing reason", plural(retries, "retry", "retries"))
    schedule.run_periodic(schedule.TickContext(app=app, conn=conn, vault=vault, cfg=cfg, now=now))


def prune(conn, cfg: Config) -> int:
    """The daily retention pass: the admin's settings (``retention.apply``), then sign-in noise.
    Returns how many old runs' objects and sign-in detail rows it removed."""
    done = retention.apply(conn, cfg)
    removed = done["snapshots"] + done["signin_days"]
    throttle.prune(conn)
    tasks.prune(conn)
    app_setup.prune(conn)
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


def _execute(
    conn, run, graph_factory, exchange_factory, alerting, liveness: Liveness, feed_factory=None, autodesk_factory=None
) -> None:
    # Every line logged until the run ends names it (logs.py), so an error can be matched to the run.
    with logs.context(run=run["id"], tenant=run["tenant_id"], collector=run["collector"]):
        log.info("Run started")
        liveness.start_run(run["id"])
        try:
            status = execute_run(
                conn,
                run,
                graph_factory,
                exchange_factory,
                alerting,
                feed_factory=feed_factory,
                autodesk_factory=autodesk_factory,
            )
            alerts.after_run(conn, alerting, run["id"], status)
            activity_alerts.after_run(conn, alerting, run["id"], status)
        finally:
            liveness.end_run()


def _claim(conn):
    try:
        return store.claim_next_run(conn)
    except sqlite3.OperationalError:
        log.exception("Could not claim the next run; trying again shortly")
        _rollback(conn)
        return None


def _collector_thread(
    cfg: Config,
    number: int,
    stop: threading.Event,
    graph_factory,
    exchange_factory,
    alerting,
    liveness: Liveness | None = None,
    feed_factory=None,
    autodesk_factory=None,
) -> threading.Thread:
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
                    _execute(
                        conn, run, graph_factory, exchange_factory, alerting, liveness, feed_factory, autodesk_factory
                    )
                except Exception:  # say, the database stayed locked: keep the thread alive
                    log.exception("Run %s could not be finished", run["id"])
                    _rollback(conn)
        finally:
            conn.close()

    thread = threading.Thread(target=loop, name=f"collector-{number}", daemon=True)
    thread.start()
    return thread


def _task_thread(app, vault, cfg: Config, number: int, stop: threading.Event, liveness: Liveness) -> threading.Thread:
    """Claim and run background tasks until ``stop`` is set, then finish the current one."""

    def loop():
        conn = connect(cfg.db_path)
        try:
            while not stop.is_set():
                try:
                    task = tasks.claim_next(conn)
                except sqlite3.OperationalError:
                    log.exception("Could not claim the next task; trying again shortly")
                    _rollback(conn)
                    task = None
                if task is None:
                    stop.wait(IDLE_WAIT)
                    continue
                liveness.start_task(task["id"])
                with logs.context(task=task["id"], kind=task["kind"], tenant=task["tenant_id"]):
                    log.info("Task started")
                    try:
                        tasks.run(app, conn, vault, cfg, task)
                    except Exception:  # say, the database stayed locked: its lease runs out and it's tried again
                        log.exception("Task %s could not be finished", task["id"])
                        _rollback(conn)
                    finally:
                        liveness.end_task()
        finally:
            conn.close()

    thread = threading.Thread(target=loop, name=f"task-{number}", daemon=True)
    thread.start()
    return thread


def _housekeeping(app, conn, vault, cfg: Config, liveness: Liveness | None = None) -> None:
    try:
        expire_leases(conn, liveness)  # take back work a stopped worker left
        housekeeping(app, conn, vault, cfg)
    except Exception:  # say "database is locked": try again next minute rather than exit
        log.exception("Housekeeping failed; trying again in a minute")
        _rollback(conn)


def _daily(conn, cfg: Config) -> None:
    removed = 0
    try:
        removed = prune(conn, cfg)
    except sqlite3.Error:
        log.exception("Pruning old snapshots failed")
        _rollback(conn)
    warm_rollups(conn)
    # The day moved on (what's new, ages, the periods' start), and pruning may have removed a period's
    # starting data, which the stored rows' fingerprints can't see: rebuild them all then.
    _queue_precompute(conn, rebuild=bool(removed))


def _daily_thread(cfg: Config, liveness: Liveness) -> threading.Thread:
    """The daily pass, beside the main loop: on a large install it takes longer than ``LOOP_STALE``, and the
    main loop has to keep coming round (schedules, alerts, the heartbeat) meanwhile."""

    def work():
        liveness.start_run(0, "daily")
        try:
            conn = connect(cfg.db_path)
            try:
                _daily(conn, cfg)
            finally:
                conn.close()
        except Exception:  # say, the database stayed locked: the next day's pass tries again
            log.exception("The daily pass failed")
        finally:
            liveness.end_run()

    thread = threading.Thread(target=work, name="daily", daemon=True)
    thread.start()
    return thread


def run_worker(
    cfg: Config,
    *,
    once: bool = False,
    graph_factory=None,
    exchange_factory=None,
    stop: threading.Event | None = None,
    feed_factory=None,
    autodesk_factory=None,
) -> None:
    conn = connect(cfg.db_path)
    migrate(conn)
    vault = Vault(cfg.encryption_keys)
    graph_factory = graph_factory or default_graph_factory(vault)
    exchange_factory = exchange_factory or default_exchange_factory(vault)
    feed_factory = feed_factory or default_feed_factory(vault)
    autodesk_factory = autodesk_factory or default_autodesk_factory(vault)
    from .web import create_app

    app = create_app(cfg)  # for rendering emailed reports and alert emails
    alerting = alerts.Alerting(cfg.base_url, vault, app)
    _load_modules()
    expire_leases(conn)  # left by a worker that stopped (and, from before leases, any run left running)

    stop = stop or threading.Event()
    liveness = Liveness()
    if once:
        # Tests and one-off use: housekeeping, then drain the queues in this thread.
        try:
            _housekeeping(app, conn, vault, cfg)
            _daily(conn, cfg)
            while (run := _claim(conn)) is not None:
                _execute(conn, run, graph_factory, exchange_factory, alerting, liveness, feed_factory, autodesk_factory)
            tasks.drain(app, conn, vault, cfg)
        finally:
            conn.close()
        return

    if threading.current_thread() is threading.main_thread():
        # Finish the current runs on "docker stop" rather than leave them half done.
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
    heartbeat = _heartbeat_thread(cfg.db_path, stop, liveness)
    log.info(
        "Worker started with %s. Collecting on '%s' (%s); next slot %s",
        plural(cfg.worker_concurrency, "collector thread"),
        cfg.collect_cron,
        cfg.timezone,
        schedule.next_fire(cfg.collect_cron, cfg.timezone, datetime.now(UTC)).isoformat(),
    )
    collectors = [
        _collector_thread(
            cfg, n + 1, stop, graph_factory, exchange_factory, alerting, liveness, feed_factory, autodesk_factory
        )
        for n in range(cfg.worker_concurrency)
    ]
    collectors += [_task_thread(app, vault, cfg, n + 1, stop, liveness) for n in range(TASK_THREADS)]

    last_day = daily = None
    while not stop.is_set():
        started = time.monotonic()
        liveness.tick()
        _housekeeping(app, conn, vault, cfg, liveness)
        today = datetime.now(UTC).date()
        if today != last_day and (daily is None or not daily.is_alive()):
            daily = _daily_thread(cfg, liveness)
            last_day = today
        liveness.tick()
        stop.wait(max(IDLE_WAIT, TICK_EVERY - (time.monotonic() - started)))
    for thread in collectors + ([daily] if daily else []):
        thread.join()
    heartbeat.join(timeout=10)
    try:
        clear_heartbeat(conn)
    except sqlite3.Error:
        log.warning("Could not clear the worker heartbeat", exc_info=True)
    finally:
        conn.close()
    log.info("Worker stopped")
