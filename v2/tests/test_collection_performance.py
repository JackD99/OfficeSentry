"""Collection at scale: queue order, parallel workers, the dashboard cache and run log retention."""

import threading
import time
from datetime import UTC, datetime, timedelta

from officesentry import findings, schedule, store, worker
from officesentry.reports import monthly_review
from tests.conftest import login, make_user
from tests.seed import seed_tenant


def _plan(db, sql, *args) -> str:
    return " ".join(r["detail"] for r in db.execute("EXPLAIN QUERY PLAN " + sql, args))


# -- Queue ---------------------------------------------------------------------------------


def test_run_now_goes_ahead_of_the_nightly_backlog(db, two_tenants):
    alpha, bravo = two_tenants
    nightly = store.enqueue_run(db, alpha, "users", trigger="schedule")
    manual = store.enqueue_run(db, bravo, "users", requested_by="admin")
    assert store.claim_next_run(db)["id"] == manual
    assert store.claim_next_run(db)["id"] == nightly


def test_asking_for_a_queued_run_again_raises_its_priority(db, two_tenants):
    alpha, bravo = two_tenants
    store.enqueue_run(db, bravo, "users", trigger="schedule")
    waiting = store.enqueue_run(db, alpha, "users", trigger="schedule")
    assert store.enqueue_run(db, alpha, "users", requested_by="admin") == waiting
    assert store.claim_next_run(db)["id"] == waiting


def test_report_refreshes_are_waited_for(db, two_tenants):
    alpha, bravo = two_tenants
    store.enqueue_run(db, bravo, "users", trigger="schedule")
    slot = datetime.now(UTC)
    assert not schedule.refresh_for_report(db, alpha, slot, 12, now=slot)
    assert store.claim_next_run(db)["tenant_id"] == alpha


def test_a_tenant_runs_one_collector_at_a_time(db, two_tenants):
    alpha, bravo = two_tenants
    first = store.enqueue_run(db, alpha, "users")
    store.enqueue_run(db, alpha, "groups")
    other = store.enqueue_run(db, bravo, "users")
    assert store.claim_next_run(db)["id"] == first
    assert store.claim_next_run(db)["id"] == other  # alpha's second run waits
    assert store.claim_next_run(db) is None
    store.finish_run(db, first, "succeeded")
    assert store.claim_next_run(db)["collector"] == "groups"


def test_collector_threads_share_the_queue(cfg, db, two_tenants, monkeypatch):
    alpha, bravo = two_tenants
    keys = ["users", "groups", "domains"]
    for tenant in (alpha, bravo):
        for key in keys:
            store.enqueue_run(db, tenant, key, trigger="schedule")
    lock = threading.Lock()
    running: dict[int, int] = {}
    seen = []

    def fake_execute(conn, run, *args, **kwargs):
        with lock:
            running[run["tenant_id"]] = running.get(run["tenant_id"], 0) + 1
            seen.append((run["tenant_id"], run["collector"], dict(running)))
        time.sleep(0.05)
        with lock:
            running[run["tenant_id"]] -= 1
        store.finish_run(conn, run["id"], "succeeded")
        return "succeeded"

    monkeypatch.setattr(worker, "execute_run", fake_execute)
    stop = threading.Event()
    threads = [worker._collector_thread(cfg, n, stop, None, None, None) for n in range(3)]
    deadline = time.monotonic() + 20
    while len(seen) < 6 and time.monotonic() < deadline:
        time.sleep(0.05)
    stop.set()
    for t in threads:
        t.join(10)
    assert sorted((t, k) for t, k, _ in seen) == sorted((t, k) for t in (alpha, bravo) for k in keys)
    assert all(count <= 1 for *_, now in seen for count in now.values())  # never two runs for one tenant
    assert any(len([c for c in now.values() if c]) == 2 for *_, now in seen)  # both tenants at once
    assert db.execute("SELECT COUNT(*) FROM runs WHERE status != 'succeeded'").fetchone()[0] == 0


# -- Indexes -------------------------------------------------------------------------------


def test_hot_run_lookups_use_an_index(db, two_tenants):
    db.execute("ANALYZE")
    for sql, args in [
        (
            "SELECT * FROM runs WHERE tenant_id = ? AND collector = ? AND status IN ('succeeded', 'partial')"
            " ORDER BY finished_at DESC, id DESC LIMIT 1",
            (1, "users"),
        ),
        ("SELECT 1 FROM runs WHERE tenant_id = ? AND collector = ? AND scheduled_for = ?", (1, "users", "x")),
        (
            "SELECT finished_at, id FROM runs WHERE tenant_id = ? AND finished_at IS NOT NULL"
            " ORDER BY finished_at DESC, id DESC LIMIT 1",
            (1,),
        ),
    ]:
        assert "SCAN runs" not in _plan(db, sql, *args), sql


# -- Dashboard cache -----------------------------------------------------------------------


def _same(a: dict, b: dict) -> bool:
    def norm(row):
        return {**row, "kpis": {k: vars(v) for k, v in row["kpis"].items()}}

    return norm(a) == norm(b)


def test_cached_rollup_row_matches_the_full_build(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    tenant = store.get_tenant(db, alpha)
    first = monthly_review.rollup_row(db, tenant, cached=True)
    assert db.execute("SELECT COUNT(*) FROM rollup_cache").fetchone()[0] == 1
    again = monthly_review.rollup_row(db, tenant, cached=True)
    assert _same(first, again)
    assert _same(again, monthly_review.rollup_row(db, tenant))


def test_cached_rollup_row_follows_accepted_risks_runs_and_prices(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    tenant = store.get_tenant(db, alpha)

    def row():
        return monthly_review.rollup_row(db, store.get_tenant(db, alpha), cached=True)

    before = row()
    target = db.execute(
        "SELECT * FROM findings WHERE tenant_id = ? AND resolved_at IS NULL ORDER BY id LIMIT 1", (alpha,)
    ).fetchone()
    findings.accept(db, target["id"], "admin", "known", None)
    accepted = row()
    assert sum(accepted["counts"].values()) == sum(before["counts"].values()) - 1
    findings.unaccept(db, target["id"])
    assert _same(row(), before)

    db.execute(
        "INSERT INTO sku_prices (sku_part_number, monthly_price, updated_at) VALUES ('SPB', 18.1, ?)",
        (datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),),
    )
    assert _same(row(), monthly_review.rollup_row(db, tenant))

    seed_tenant(db, alpha, users=[])  # a new collection changes the figures
    assert _same(row(), monthly_review.rollup_row(db, store.get_tenant(db, alpha)))


def test_cached_rollup_row_works_out_freshness_now(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    tenant = store.get_tenant(db, alpha)
    assert monthly_review.rollup_row(db, tenant, cached=True)["stale_data"] is False
    old = (datetime.now(UTC) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("UPDATE runs SET finished_at = ? WHERE tenant_id = ? AND collector = 'users'", (old, alpha))
    assert monthly_review.rollup_row(db, tenant, cached=True)["stale_data"] is True


def test_worker_builds_the_row_after_a_tenants_last_run(db, two_tenants):
    alpha, _ = two_tenants
    seed_tenant(db, alpha)
    assert db.execute("SELECT tenant_id FROM rollup_cache").fetchall()[0][0] == alpha


def test_dashboard_and_all_tenants_use_the_cache(client, db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    db.execute("DELETE FROM rollup_cache")
    make_user(db, "admin")
    login(client, "admin")
    assert client.get("/").status_code == 200
    assert {r[0] for r in db.execute("SELECT tenant_id FROM rollup_cache")} == {alpha, bravo}
    db.execute("UPDATE rollup_cache SET data = json_set(data, '$.failing', 4242)")
    assert client.get("/across/review").status_code == 200  # served from the cache, without errors


# -- Run log retention ---------------------------------------------------------------------


def test_old_run_logs_keep_only_warnings_and_errors(db, two_tenants):
    alpha, _ = two_tenants
    old, recent = store.enqueue_run(db, alpha, "users"), None
    for level in ("info", "warning", "error", "info"):
        store.log_event(db, old, level, f"{level} line")
    store.finish_run(db, old, "succeeded")
    long_ago = (datetime.now(UTC) - timedelta(days=200)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("UPDATE runs SET created_at = ? WHERE id = ?", (long_ago, old))
    recent = store.enqueue_run(db, alpha, "users")
    store.log_event(db, recent, "info", "today")

    assert store.prune_run_logs(db) == 2
    assert [r[0] for r in db.execute("SELECT level FROM run_events WHERE run_id = ? ORDER BY id", (old,))] == [
        "warning",
        "error",
    ]
    assert db.execute("SELECT COUNT(*) FROM run_events WHERE run_id = ?", (recent,)).fetchone()[0] == 1
    assert store.prune_run_logs(db) == 0  # carries on from where it stopped


# -- One sync per tenant, shared sign-ins ---------------------------------------------------


def test_findings_sync_once_when_a_tenants_queue_drains(cfg, db, two_tenants, monkeypatch):
    synced = []

    def fake_collect(conn, run, *args):
        store.finish_run(conn, run["id"], "succeeded")
        return "succeeded"

    monkeypatch.setattr(worker, "_collect", fake_collect)
    monkeypatch.setattr(
        worker.findings,
        "sync",
        lambda conn, tenant: synced.append(tenant["id"]) or {"new": 0, "reopened": 0, "resolved": 0, "opened": []},
    )
    worker.run_worker(cfg, once=True, graph_factory=lambda *a: None, exchange_factory=lambda *a: None)
    assert sorted(synced) == sorted(two_tenants)  # not once per collector


def test_failed_runs_alone_dont_sync(db, two_tenants, monkeypatch):
    alpha, _ = two_tenants
    synced = []
    monkeypatch.setattr(worker.findings, "sync", lambda conn, tenant: synced.append(tenant["id"]))
    store.enqueue_run(db, alpha, "users")
    run = store.claim_next_run(db)
    assert worker.execute_run(db, run, lambda *a: (_ for _ in ()).throw(worker.NotConnected("no"))) == "failed"
    assert synced == []


def test_token_providers_are_reused_per_tenant_and_certificate(monkeypatch):
    from officesentry import msauth

    made = []
    monkeypatch.setattr(msauth, "certificate_token_provider", lambda *a: made.append(a) or (lambda: "tok"))
    monkeypatch.setattr(msauth, "_PROVIDERS", type(msauth._PROVIDERS)())
    args = ("contoso.onmicrosoft.com", "app", "KEY", "thumb", "https://graph.microsoft.com")
    first = msauth.shared_certificate_token_provider(*args)
    assert msauth.shared_certificate_token_provider(*args) is first
    msauth.shared_certificate_token_provider(*args[:2], "NEWKEY", "thumb2", args[4])
    msauth.shared_certificate_token_provider(*args[:4], "https://outlook.office365.com")
    assert len(made) == 3
    monkeypatch.setattr(msauth, "MAX_PROVIDERS", 2)
    msauth.shared_certificate_token_provider("other.onmicrosoft.com", *args[1:])
    assert len(msauth._PROVIDERS) == 2


def test_each_collector_thread_has_its_own_http_session():
    sessions = []
    threads = [
        threading.Thread(target=lambda: sessions.append((worker._session(), worker._session()))) for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(a is b for a, b in sessions) and sessions[0][0] is not sessions[1][0]


def test_a_wedged_main_loop_shows_even_while_runs_go_on():
    now = [0.0]
    live = worker.Liveness(clock=lambda: now[0])
    live.start_run(3)
    now[0] += worker.LOOP_STALE.total_seconds() + 1
    assert "main loop" in live.problem()
