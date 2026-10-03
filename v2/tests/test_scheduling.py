"""Cron schedule, freshness, retention, worker heartbeat and deployment commands."""

from datetime import UTC, datetime, timedelta

import pytest

from officesentry import mailer, schedule, store, worker
from officesentry.__main__ import main
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
    assert schedule.previous_fire("0 2 * * *", "Europe/London", datetime(2026, 11, 2, 3, 0, tzinfo=UTC)) \
        == datetime(2026, 11, 2, 2, 0, tzinfo=UTC)
    assert schedule.next_fire("0 2 * * 1-5", "UTC", datetime(2026, 10, 2, 3, 0, tzinfo=UTC)) \
        == datetime(2026, 10, 5, 2, 0, tzinfo=UTC)  # Friday -> Monday


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
    queued = {r[0] for r in db.execute("SELECT collector FROM runs WHERE tenant_id = ? AND status = 'queued'",
                                       (alpha,))}
    assert "users" not in queued and "secure_score" in queued
    # Later ticks for the same slot queue nothing more.
    assert worker.schedule_tick(db, cfg, datetime(2026, 10, 2, 13, 0, tzinfo=UTC)) == 0


# -- Emailed reports refresh stale data first --------------------------------


def test_report_waits_for_a_refresh_of_stale_data(app, db, two_tenants, vault, monkeypatch):
    alpha, _ = two_tenants
    sent = []
    monkeypatch.setattr(mailer, "deliver", lambda *a, **k: sent.append(a[3]["id"]))
    db.execute("INSERT INTO report_schedules (tenant_id, recipients, day_of_month, hour, created_at)"
               " VALUES (?, 'a@x.com', 1, 8, '2026-01-01T00:00:00Z')", (alpha,))
    for key in schedule.nightly_collectors():
        finished_run(db, alpha, key, "2026-09-29T02:00:00Z")  # days old
    now = datetime(2026, 10, 1, 8, 1, tzinfo=UTC)

    assert mailer.run_due(app, db, vault, "UTC", now, max_age_hours=12) == 0
    queued = db.execute("SELECT COUNT(*) FROM runs WHERE status = 'queued'").fetchone()[0]
    assert queued == len(schedule.nightly_collectors())
    assert mailer.run_due(app, db, vault, "UTC", now + timedelta(minutes=1), max_age_hours=12) == 0
    assert sent == []

    # One refresh fails, the rest succeed: the report goes out anyway.
    db.execute("UPDATE runs SET status = 'succeeded', finished_at = '2026-10-01T08:05:00Z' WHERE status = 'queued'")
    db.execute("UPDATE runs SET status = 'failed' WHERE id = (SELECT MAX(id) FROM runs)")
    assert mailer.run_due(app, db, vault, "UTC", now + timedelta(minutes=6), max_age_hours=12) == 1
    assert sent == [alpha]
    # The failed collector isn't retried for this report.
    assert db.execute("SELECT COUNT(*) FROM runs WHERE status = 'queued'").fetchone()[0] == 0


def test_report_stops_waiting_after_a_while(app, db, two_tenants, vault, monkeypatch):
    alpha, _ = two_tenants
    monkeypatch.setattr(mailer, "deliver", lambda *a, **k: None)
    db.execute("INSERT INTO report_schedules (tenant_id, recipients, day_of_month, hour, created_at)"
               " VALUES (?, 'a@x.com', 1, 8, '2026-01-01T00:00:00Z')", (alpha,))
    slot = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
    assert mailer.run_due(app, db, vault, "UTC", slot, max_age_hours=12) == 0
    # The worker is stuck on something else; the refresh runs never start.
    assert mailer.run_due(app, db, vault, "UTC", slot + schedule.REFRESH_WAIT + timedelta(minutes=1),
                          max_age_hours=12) == 1


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
    client.delete_cookie("session")
    login(client, "analyst")
    assert b"background worker isn" not in client.get("/").data


def test_worker_once_queues_the_slot_and_drains_it(cfg, db, two_tenants, monkeypatch):
    calls = []
    monkeypatch.setattr(worker, "execute_run", lambda conn, run, *a: (calls.append(run["collector"]),
                                                                        store.finish_run(conn, run["id"], "failed")))
    worker.run_worker(cfg, once=True, graph_factory=lambda *a: None, exchange_factory=lambda *a: None)
    assert len(calls) == 2 * len(schedule.nightly_collectors())


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
    files = sorted(out.glob("officesentry-*.db"))
    assert 1 <= len(files) <= 2
    copy = connect(files[-1])
    assert copy.execute("SELECT COUNT(*) FROM tenants").fetchone()[0] == 2
    assert oct(files[-1].stat().st_mode & 0o777) == "0o600"
