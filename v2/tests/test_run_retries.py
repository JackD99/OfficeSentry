"""A collection that failed for a reason waiting fixes is tried again by itself (store.plan_retry)."""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from officesentry import home, schedule, store, system_health, tenant_health
from officesentry.exchange import ExchangeAccessDenied, ExchangeError
from officesentry.graph import GraphError, PermissionMissing
from officesentry.web.tenants import data_health
from officesentry.worker import NotConnected, execute_run, housekeeping, passing
from tests.conftest import login, make_user


def _raises(exc):
    def factory(tenant, connection):
        raise exc

    return factory


def _collect(db, factory):
    run = store.claim_next_run(db)
    status = execute_run(db, run, factory)
    return db.execute("SELECT * FROM runs WHERE id = ?", (run["id"],)).fetchone(), status


def _when(row) -> datetime:
    return datetime.strptime(row["retry_at"], store.ISO).replace(tzinfo=UTC)


BUSY = GraphError(503, "serviceNotAvailable", "The service is temporarily unavailable.")


def test_a_run_microsoft_was_too_busy_for_is_tried_twice_more_then_left(db, two_tenants):
    alpha, _ = two_tenants
    store.enqueue_run(db, alpha, "users", trigger="schedule", scheduled_for="2026-10-15T02:00:00Z")
    first, status = _collect(db, _raises(BUSY))
    assert status == "failed" and "Graph 503" in first["error"]
    now = datetime.now(UTC)
    assert timedelta(minutes=19) < _when(first) - now <= timedelta(minutes=20)
    log = [r["message"] for r in db.execute("SELECT message FROM run_events WHERE run_id = ?", (first["id"],))]
    assert log[-1].startswith("Trying again by itself at ")

    assert store.queue_retries(db, now) == 0  # not due yet
    assert store.queue_retries(db, now + timedelta(days=2)) == 0  # planned over a day before: the night's run has it
    assert store.queue_retries(db, now + timedelta(minutes=21)) == 1
    assert store.queue_retries(db, now + timedelta(minutes=22)) == 0  # queued once
    second, _ = _collect(db, _raises(BUSY))
    # The retry is the same night's run, behind anyone waiting on a page.
    assert (second["retry_of"], second["trigger"], second["scheduled_for"]) == (
        first["id"],
        "schedule",
        first["scheduled_for"],
    )
    assert second["priority"] == store.PRIORITY_NIGHTLY
    assert timedelta(hours=1, minutes=59) < _when(second) - now <= timedelta(hours=2)

    assert store.queue_retries(db, now + timedelta(hours=3)) == 1
    third, _ = _collect(db, _raises(BUSY))
    assert third["retry_of"] == first["id"]  # every try counts against the first failure
    assert third["retry_at"] is None  # that was the last try: it waits for the next night now
    assert store.queue_retries(db, now + timedelta(hours=6)) == 0


@pytest.mark.parametrize(
    "exc",
    [
        BUSY,
        GraphError(429, "TooManyRequests", "Too many requests"),
        GraphError(0, "network_error", "Couldn't reach Microsoft's sign-in service: Network is unreachable"),
        ExchangeError(502, "BadGateway", ""),
        sqlite3.OperationalError("database is locked"),
    ],
)
def test_failures_that_pass_by_waiting(exc):
    assert passing(exc)


@pytest.mark.parametrize(
    "exc",
    [
        GraphError(403, "Authorization_RequestDenied", "Insufficient privileges"),
        GraphError(401, "invalid_client", "AADSTS7000215: Invalid client secret provided."),
        PermissionMissing(403, "Authorization_RequestDenied", "Missing User.Read.All"),
        NotConnected("tenant has no app connection with a client ID"),
        ExchangeAccessDenied(403, "Forbidden", "The role assigned to application isn't supported"),
        sqlite3.OperationalError("no such column: nope"),
        KeyError("displayName"),
    ],
)
def test_failures_that_need_someone_are_not_tried_again(db, two_tenants, exc):
    alpha, _ = two_tenants
    assert not passing(exc)
    store.enqueue_run(db, alpha, "users")
    run, status = _collect(db, _raises(exc))
    assert status == "failed" and run["retry_at"] is None


def test_a_retry_gives_way_to_a_newer_run_and_a_paused_client(db, two_tenants):
    alpha, bravo = two_tenants
    for tenant in two_tenants:
        store.enqueue_run(db, tenant, "users")
        _collect(db, _raises(BUSY))
    later = datetime.now(UTC) + timedelta(minutes=21)
    store.enqueue_run(db, alpha, "users")  # someone pressed Refresh meanwhile
    db.execute("UPDATE tenants SET enabled = 0 WHERE id = ?", (bravo,))  # paused
    assert store.queue_retries(db, later) == 0
    db.execute("UPDATE tenants SET enabled = 1 WHERE id = ?", (bravo,))
    assert store.queue_retries(db, later) == 1


def test_a_run_the_worker_stopped_during_is_tried_again(app, db, vault, cfg, two_tenants):
    alpha, _ = two_tenants
    now = datetime.now(UTC)
    due = now + store.RETRY_DELAYS[0] + timedelta(minutes=1)
    # The night's run is still the latest slot's then, so the schedule queues no newer one for the retry to give way to.
    slot = schedule.iso(schedule.previous_fire(cfg.collect_cron, cfg.timezone, due))
    store.enqueue_run(db, alpha, "users", trigger="schedule", scheduled_for=slot)
    store.claim_next_run(db)
    assert store.expire_run_leases(db, now + store.LEASE + timedelta(seconds=1)) == 1  # an update restarted it
    run = db.execute("SELECT * FROM runs").fetchone()
    assert run["error"] == store.WORKER_STOPPED and store.retry_pending(run) and _when(run) <= due
    # The worker's housekeeping queues it once it's due.
    housekeeping(app, db, vault, cfg, due)
    retry = db.execute("SELECT * FROM runs WHERE retry_of = ?", (run["id"],)).fetchone()
    assert retry["status"] == "queued" and retry["collector"] == "users"


def test_a_night_that_is_being_tried_again_isnt_called_failed(db, cfg, two_tenants):
    slot = "2026-10-15T02:00:00Z"
    for tenant in two_tenants:
        store.enqueue_run(db, tenant, "users", trigger="schedule", scheduled_for=slot)
        _collect(db, _raises(BUSY))
    assert system_health._slot(db, slot).pending == 2  # both still to be tried again: not failed yet
    store.queue_retries(db, datetime.now(UTC) + timedelta(minutes=21))
    for _ in two_tenants:
        run = store.claim_next_run(db)
        store.finish_run(db, run["id"], "succeeded")
    slot_now = system_health._slot(db, slot)
    assert (slot_now.tenants, slot_now.failed, slot_now.pending) == (2, 0, 0)  # each counted by its last try


def test_pages_say_a_failure_is_being_tried_again(client, db, two_tenants):
    alpha, _ = two_tenants
    store.enqueue_run(db, alpha, "users")
    first, _ = _collect(db, _raises(BUSY))
    latest = tenant_health.latest_runs(db, alpha)
    tenant = store.get_tenant(db, alpha)
    assert data_health(tenant, latest)[:2] == ("warn", "1 failed, trying again")
    reason = home.today(db, [tenant]).rows[0].reasons[0]
    assert (reason.tone, reason.text) == ("warn", "1 collection failed and will be tried again")
    make_user(db, "admin")
    login(client, "admin")
    for page in (f"/tenants/{alpha}/collection", f"/runs/{first['id']}"):
        assert "Office Sentry tries again by itself in about 20 minutes." in client.get(page).get_data(as_text=True)
    # Once its tries are used up it's a failure like any other, and the retry says what it was.
    store.queue_retries(db, datetime.now(UTC) + timedelta(minutes=21))
    retry = store.claim_next_run(db)
    store.finish_run(db, retry["id"], "failed", "Graph 503 serviceNotAvailable: busy")
    assert data_health(tenant, tenant_health.latest_runs(db, alpha))[:2] == ("bad", "1 failed")
    page = client.get(f"/runs/{retry['id']}").get_data(as_text=True)
    assert f'tried again after <a href="/runs/{first["id"]}">run {first["id"]}</a> failed' in page
    assert "Tried again" in client.get(f"/tenants/{alpha}/collection").get_data(as_text=True)
