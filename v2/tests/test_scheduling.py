"""Cron schedule, freshness, retention, worker heartbeat and deployment commands."""

import gzip
import os
from contextlib import closing
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from officesentry import delivery, schedule, store, worker
from officesentry.__main__ import main
from officesentry.collectors.base import Collector
from officesentry.config import load_config
from officesentry.db import connect

from .conftest import login, make_user


def finished_run(db, tenant_id, collector, finished_at, status="succeeded", objects=1):
    rid = store.enqueue_run(db, tenant_id, collector)
    store.claim_next_run(db)
    store.add_objects(db, rid, "item", [(f"o{i}", {"i": i}) for i in range(objects)])
    store.add_metric(db, rid, tenant_id, f"{collector}.total", objects)
    store.finish_run(db, rid, status)
    db.execute("UPDATE runs SET created_at = ?, finished_at = ? WHERE id = ?", (finished_at, finished_at, rid))
    return rid


def has_objects(db, run_id):
    return bool(store.run_objects(db, run_id))


# -- Cron --------------------------------------------------------------------


def test_previous_fire_is_in_the_configured_timezone():
    now = datetime(2026, 10, 2, 1, 30, tzinfo=UTC)
    assert schedule.previous_fire("0 2 * * *", "UTC", now) == datetime(2026, 10, 1, 2, 0, tzinfo=UTC)
    # 02:00 in London during BST is 01:00 UTC.
    assert schedule.previous_fire("0 2 * * *", "Europe/London", now) == datetime(2026, 10, 2, 1, 0, tzinfo=UTC)
    # A slot exactly at "now" counts as passed.
    exact = datetime(2026, 10, 2, 2, 0, tzinfo=UTC)
    assert schedule.previous_fire("0 2 * * *", "UTC", exact) == exact
    # After the clocks go back, 02:00 London is 02:00 UTC.
    assert schedule.previous_fire("0 2 * * *", "Europe/London", datetime(2026, 11, 2, 3, 0, tzinfo=UTC)) == datetime(
        2026, 11, 2, 2, 0, tzinfo=UTC
    )
    assert schedule.next_fire("0 2 * * 1-5", "UTC", datetime(2026, 10, 2, 3, 0, tzinfo=UTC)) == datetime(
        2026, 10, 5, 2, 0, tzinfo=UTC
    )  # Friday -> Monday


def test_config_reads_cron_and_rejects_bad_values(tmp_path, monkeypatch):
    monkeypatch.setenv("OFFICESENTRY_NIGHTLY_HOUR", "4")
    assert load_config(data_dir=tmp_path).collect_cron == "0 4 * * *"
    monkeypatch.setenv("OFFICESENTRY_COLLECT_CRON", "30 1 * * 1-5")
    assert load_config(data_dir=tmp_path).collect_cron == "30 1 * * 1-5"
    monkeypatch.setenv("OFFICESENTRY_COLLECT_CRON", "every night")
    with pytest.raises(ValueError, match="COLLECT_CRON"):
        load_config(data_dir=tmp_path)
    monkeypatch.delenv("OFFICESENTRY_COLLECT_CRON")
    monkeypatch.setenv("OFFICESENTRY_TIMEZONE", "Mars/Olympus")
    with pytest.raises(ValueError, match="TIMEZONE"):
        load_config(data_dir=tmp_path)


def test_schedule_follows_cron(db, cfg, two_tenants):
    cfg.collect_cron = "0 6 * * 1-5"  # weekday mornings
    saturday = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    assert worker.schedule_tick(db, cfg, saturday) > 0
    slots = {r[0] for r in db.execute("SELECT scheduled_for FROM runs")}
    assert slots == {"2026-10-02T06:00:00Z"}  # Friday's slot, caught up once
    assert worker.schedule_tick(db, cfg, saturday + timedelta(days=1)) == 0


def test_schedule_skips_collectors_that_are_already_fresh(db, cfg, two_tenants):
    alpha, _ = two_tenants
    cfg.max_age_hours = 12
    finished_run(db, alpha, "users", "2026-10-01T20:00:00Z")  # a manual run that evening
    finished_run(db, alpha, "secure_score", "2026-09-30T20:00:00Z")  # too old
    worker.schedule_tick(db, cfg, datetime(2026, 10, 2, 3, 0, tzinfo=UTC))
    queued = {
        r[0] for r in db.execute("SELECT collector FROM runs WHERE tenant_id = ? AND status = 'queued'", (alpha,))
    }
    assert "users" not in queued and "secure_score" in queued
    # Later ticks for the same slot queue nothing more.
    assert worker.schedule_tick(db, cfg, datetime(2026, 10, 2, 13, 0, tzinfo=UTC)) == 0


# -- Emailed reports refresh stale data first --------------------------------


def _monthly_report(db, tenant_id):
    sid = db.execute(
        "INSERT INTO email_schedules (kind, tenant_id, reports, frequency, day_of_month, hour, timezone,"
        " created_at) VALUES ('report', ?, '[\"review\"]', 'monthly', 1, 8, 'UTC',"
        " '2026-01-01T00:00:00Z')",
        (tenant_id,),
    ).lastrowid
    delivery.set_recipients(db, "schedule", sid, [delivery.add_contact(db, tenant_id, "", "a@x.com")])
    return sid


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(delivery, "send_schedule", lambda app, conn, vault, s, **k: calls.append(s["tenant_id"]))
    return calls


def test_report_waits_for_a_refresh_of_stale_data(app, db, cfg, two_tenants, vault, sent):
    alpha, _ = two_tenants
    _monthly_report(db, alpha)
    for key in schedule.nightly_collectors():
        finished_run(db, alpha, key, "2026-09-29T02:00:00Z")  # days old
    now = datetime(2026, 10, 1, 8, 1, tzinfo=UTC)

    assert delivery.tick(app, db, vault, cfg, now) == 0
    queued = db.execute("SELECT COUNT(*) FROM runs WHERE status = 'queued'").fetchone()[0]
    assert queued == len(schedule.nightly_collectors())
    assert delivery.tick(app, db, vault, cfg, now + timedelta(minutes=1)) == 0
    assert sent == []

    # One refresh fails, the rest succeed: the report goes out anyway.
    db.execute("UPDATE runs SET status = 'succeeded', finished_at = '2026-10-01T08:05:00Z' WHERE status = 'queued'")
    db.execute("UPDATE runs SET status = 'failed' WHERE id = (SELECT MAX(id) FROM runs)")
    assert delivery.tick(app, db, vault, cfg, now + timedelta(minutes=6)) == 1
    assert sent == [alpha]
    # The failed collector isn't retried for this report.
    assert db.execute("SELECT COUNT(*) FROM runs WHERE status = 'queued'").fetchone()[0] == 0


def test_report_stops_waiting_after_a_while(app, db, cfg, two_tenants, vault, sent):
    alpha, _ = two_tenants
    _monthly_report(db, alpha)
    slot = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    assert delivery.tick(app, db, vault, cfg, slot) == 0
    # The worker is stuck on something else; the refresh runs never start.
    assert delivery.tick(app, db, vault, cfg, slot + schedule.REFRESH_WAIT + timedelta(minutes=1)) == 1


# -- Retention ---------------------------------------------------------------


def test_retention_keeps_30_dailies_and_12_monthlies(db, two_tenants):
    alpha, bravo = two_tenants
    start = datetime(2025, 1, 1, 2, 0, tzinfo=UTC)
    ids = {}
    for day in range(600):  # nightly runs for well over a year
        when = start + timedelta(days=day)
        ids[when] = finished_run(db, alpha, "users", schedule.iso(when))
    # An earlier run on the latest day, a failed run, and another tenant's only run.
    extra = finished_run(db, alpha, "users", schedule.iso(start + timedelta(days=599, hours=-1)))
    failed = finished_run(db, alpha, "users", schedule.iso(start + timedelta(days=100)), status="failed")
    other = finished_run(db, bravo, "users", schedule.iso(start))

    keep = store.snapshots_to_keep(db, keep_daily=30, keep_monthly=12)
    latest = max(ids)
    dailies = {ids[latest - timedelta(days=d)] for d in range(30)}
    assert dailies <= keep
    month_ends = {}
    for when, rid in ids.items():
        month_ends[(when.year, when.month)] = rid
    monthlies = {month_ends[m] for m in sorted(month_ends)[-12:]}
    assert monthlies <= keep
    assert keep == dailies | monthlies | {other}

    removed = store.prune_snapshots(db, keep_daily=30, keep_monthly=12)
    assert removed == 600 + 2 - len(dailies | monthlies)
    assert has_objects(db, ids[latest]) and has_objects(db, other)
    assert not has_objects(db, extra) and not has_objects(db, failed)
    assert not has_objects(db, ids[start])
    # Metrics, run rows and logs are kept.
    assert len(store.metric_history(db, alpha, "users.total")) == 602
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 603
    assert store.prune_snapshots(db, keep_daily=30, keep_monthly=12) == 0


def test_retention_leaves_recent_and_running_runs_alone(db, two_tenants):
    alpha, _ = two_tenants
    now = datetime.now(UTC)
    first = finished_run(db, alpha, "users", schedule.iso(now - timedelta(hours=3)))
    finished_run(db, alpha, "users", schedule.iso(now - timedelta(hours=1)))
    running = store.enqueue_run(db, alpha, "secure_score")
    store.claim_next_run(db)
    store.add_objects(db, running, "item", [("x", {})])
    assert store.prune_snapshots(db) == 0
    assert has_objects(db, first) and has_objects(db, running)


def test_change_baselines_skip_pruned_runs(db, two_tenants):
    from officesentry.changes import _baseline_run

    alpha, _ = two_tenants
    kept = finished_run(db, alpha, "users", "2026-09-01T02:00:00Z")
    pruned = finished_run(db, alpha, "users", "2026-09-01T15:00:00Z")
    db.execute("DELETE FROM objects WHERE run_id = ?", (pruned,))
    assert _baseline_run(db, alpha, "users", "2026-09-02T00:00:00Z")["id"] == kept


def test_run_page_says_when_a_snapshot_was_pruned(client, db, two_tenants):
    alpha, _ = two_tenants
    rid = finished_run(db, alpha, "users", "2025-01-01T02:00:00Z", objects=3)
    db.execute("DELETE FROM objects WHERE run_id = ?", (rid,))
    make_user(db, "admin")
    login(client, "admin")
    assert b"3 items were collected" in client.get(f"/runs/{rid}").data


# -- Heartbeat and deployment commands ----------------------------------------


def test_admins_see_when_the_worker_is_not_running(client, db, two_tenants):
    make_user(db, "admin")
    make_user(db, "analyst", role="analyst", all_tenants=True)
    login(client, "admin")
    assert b"background worker isn" in client.get("/").data
    worker.beat(db)
    assert worker.is_alive(db)
    assert b"background worker isn" not in client.get("/").data
    db.execute("UPDATE settings SET value = '2026-01-01T00:00:00Z' WHERE key = ?", (worker.HEARTBEAT_KEY,))
    client.post("/logout")
    login(client, "analyst")
    assert b"background worker isn" not in client.get("/").data


def test_worker_once_queues_the_slot_and_drains_it(cfg, db, two_tenants, monkeypatch):
    calls = []
    monkeypatch.setattr(
        worker,
        "execute_run",
        lambda conn, run, *a: (calls.append(run["collector"]), store.finish_run(conn, run["id"], "failed")),
    )
    worker.run_worker(cfg, once=True, graph_factory=lambda *a: None, exchange_factory=lambda *a: None)
    assert len(calls) == 2 * len(schedule.nightly_collectors())


def test_worker_survives_a_failing_housekeeping_pass(cfg, db, two_tenants, monkeypatch, caplog):
    """ "database is locked" in the once-a-minute work is logged and retried, not fatal."""
    import sqlite3
    import threading

    calls, stop = [], threading.Event()

    def housekeeping(app, conn, *_a):
        calls.append(conn.in_transaction)
        if len(calls) == 1:
            conn.execute("BEGIN IMMEDIATE")  # left open by the failure, as a half-done write would be
            raise sqlite3.OperationalError("database is locked")
        stop.set()

    monkeypatch.setattr(worker, "housekeeping", housekeeping)
    monkeypatch.setattr(worker, "TICK_EVERY", 0)
    monkeypatch.setattr(worker, "IDLE_WAIT", 0.01)
    thread = threading.Thread(
        target=worker.run_worker, args=(cfg,), kwargs={"graph_factory": lambda *a: None, "stop": stop}
    )
    thread.start()
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert calls == [False, False]  # retried on the next pass, with the failed transaction rolled back
    assert "Housekeeping failed" in caplog.text
    # Stopping cleanly clears the heartbeat, so a restore straight after "docker compose stop" isn't refused.
    assert worker.last_seen(db) is None


def test_worker_once_survives_a_failing_housekeeping_pass(cfg, db, two_tenants, monkeypatch):
    import sqlite3

    def broken(*_a):
        raise sqlite3.OperationalError("database is locked")

    ran = []
    monkeypatch.setattr(worker, "housekeeping", broken)
    monkeypatch.setattr(
        worker,
        "execute_run",
        lambda conn, run, *a: (ran.append(run["id"]), store.finish_run(conn, run["id"], "failed")),
    )
    store.enqueue_run(db, two_tenants[0], "users")
    worker.run_worker(cfg, once=True, graph_factory=lambda *a: None, exchange_factory=lambda *a: None)
    assert len(ran) == 1


def test_a_stuck_main_loop_stops_the_heartbeat(db):
    now = [0.0]
    live = worker.Liveness(clock=lambda: now[0])
    assert worker.heartbeat_step(db, live) and worker.is_alive(db)
    db.execute("DELETE FROM settings WHERE key = ?", (worker.HEARTBEAT_KEY,))
    now[0] += worker.LOOP_STALE.total_seconds() + 1
    assert live.problem() and not worker.heartbeat_step(db, live)
    assert not worker.is_alive(db)
    live.tick()
    assert worker.heartbeat_step(db, live)
    # A long collection is progress, up to a ceiling, while the main loop keeps coming round.
    live.start_run(7)
    for _ in range(3):
        now[0] += worker.LOOP_STALE.total_seconds()
        live.tick()
    assert live.problem() is None
    now[0] += worker.RUN_STALE.total_seconds()
    live.tick()
    assert "run 7" in live.problem()
    live.end_run()
    assert live.problem() is None


def test_a_finished_run_is_recorded_once_the_lock_clears(cfg, db, two_tenants, monkeypatch, caplog):
    """A collector thread that finds the database locked as it records its run keeps trying, rather than leave
    the run to be failed as "worker stopped" when its lease runs out."""
    monkeypatch.setattr(worker, "FINISH_WAIT", 0.01)
    db.execute("PRAGMA busy_timeout = 20")
    other = connect(cfg.db_path)
    rollbacks = []
    rollback = worker._rollback

    def release_on_the_second_try(conn):
        rollbacks.append(conn)
        if len(rollbacks) == 2:
            other.rollback()
        rollback(conn)

    monkeypatch.setattr(worker, "_rollback", release_on_the_second_try)

    class Quiet(Collector):
        key = "quiet"
        title = "Quiet"

        def collect(self, ctx):
            ctx.metric("quiet", 1)
            other.execute("BEGIN IMMEDIATE")  # another writer takes the lock as the run finishes

    monkeypatch.setitem(worker.REGISTRY, "quiet", Quiet)
    store.enqueue_run(db, two_tenants[0], "quiet")
    assert worker.execute_run(db, store.claim_next_run(db), lambda *a: SimpleNamespace()) == "succeeded"
    run = store.latest_run(db, two_tenants[0], "quiet")
    assert run["status"] == "succeeded" and run["finished_at"]
    assert len(rollbacks) == 2 and "The database is busy" in caplog.text
    other.close()


def test_a_lock_that_never_clears_still_fails_the_finish(cfg, db, monkeypatch):
    monkeypatch.setattr(worker, "FINISH_WAIT", 0.01)
    monkeypatch.setattr(worker, "FINISH_TRIES", 3)
    db.execute("PRAGMA busy_timeout = 20")
    other = connect(cfg.db_path)
    other.execute("BEGIN IMMEDIATE")
    tries = []

    def step():
        tries.append(1)
        with store.atomic(db):
            db.execute("UPDATE settings SET value = value WHERE key = 'none'")

    with pytest.raises(sqlite3.OperationalError):
        worker._while_locked(db, step)
    assert len(tries) == 3
    other.rollback()
    worker._while_locked(db, step)  # free again: first time
    assert len(tries) == 4

    def broken():
        raise sqlite3.OperationalError("no such table: nothing")

    with pytest.raises(sqlite3.OperationalError):  # only a lock is tried again
        worker._while_locked(db, broken)
    other.close()


def test_the_daily_pass_runs_beside_the_main_loop(cfg, db, monkeypatch, caplog):
    """The daily pass (retention, every dashboard row) can take longer than the main loop is allowed to be
    away, so it runs in its own thread, counted as progress for up to four hours."""
    now = [0.0]
    live = worker.Liveness(clock=lambda: now[0])
    hold = threading.Event()
    seen = []

    def slow_daily(conn, cfg):
        seen.append((threading.current_thread().name, conn is not db))
        hold.wait(10)

    monkeypatch.setattr(worker, "_daily", slow_daily)
    thread = worker._daily_thread(cfg, live)
    for _ in range(50):
        if seen:
            break
        hold.wait(0.05)
    assert seen == [("daily", True)]  # its own thread, its own connection
    assert live.held("daily") == [0] and live.problem() is None
    now[0] += worker.LOOP_STALE.total_seconds() * 3
    live.tick()
    assert live.problem() is None  # longer than the loop may be away, fine for the daily pass
    now[0] += worker.DAILY_STALE.total_seconds()
    live.tick()
    assert "daily 0 has been going" in live.problem()
    hold.set()
    thread.join(5)
    assert not thread.is_alive() and live.problem() is None

    def broken(conn, cfg):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(worker, "_daily", broken)
    worker._daily_thread(cfg, live).join(5)
    assert "The daily pass failed" in caplog.text and live.problem() is None


def test_healthcheck_command(cfg, db, monkeypatch):
    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(cfg.data_dir))
    with pytest.raises(SystemExit) as exc:
        main(["healthcheck", "worker"])
    assert "never" in str(exc.value.code)
    worker.beat(db)
    main(["healthcheck", "worker"])  # healthy: returns normally


def test_backup_command(cfg, db, two_tenants, monkeypatch, tmp_path):
    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(cfg.data_dir))
    out = tmp_path / "backups"
    for _ in range(3):
        main(["backup", "--dir", str(out), "--keep", "2"])
    files = sorted(out.glob("officesentry-*.db.gz"))
    assert 1 <= len(files) <= 2
    plain = tmp_path / "copy.db"
    plain.write_bytes(gzip.decompress(files[-1].read_bytes()))
    with closing(connect(plain)) as copy:
        assert copy.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 2
    if os.name != "nt":  # Windows has no Unix file modes
        assert oct(files[-1].stat().st_mode & 0o777) == "0o600"


def test_due_slot():
    now = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    slot = schedule.due_slot("0 8 * * *", "UTC", now)
    assert slot == datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
    assert schedule.due_slot("0 8 * * *", "UTC", now, last_slot=schedule.iso(slot)) is None
    assert schedule.due_slot("0 8 * * *", "UTC", now, not_before="2026-10-05T08:30:00Z") is None
    # Days of downtime catch up once, with the latest slot.
    assert schedule.due_slot("0 8 * * *", "UTC", now, last_slot="2026-10-01T08:00:00Z") == slot


def test_periodic_jobs_run_each_tick_and_are_isolated(app, db, cfg, vault, monkeypatch):
    seen = []
    monkeypatch.setattr(schedule, "PERIODIC", {})

    @schedule.periodic("broken")
    def broken(ctx):
        raise RuntimeError("boom")

    @schedule.periodic("probe")
    def probe(ctx):
        seen.append((ctx.cfg.timezone, ctx.now))

    with pytest.raises(ValueError):
        schedule.periodic("probe")(probe)
    now = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    worker.housekeeping(app, db, vault, cfg, now)
    assert seen == [(cfg.timezone, now)]


def test_report_emails_are_a_periodic_job():
    for module in worker.JOB_MODULES:
        __import__(module)
    assert "email_delivery" in schedule.PERIODIC
