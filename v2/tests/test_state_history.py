"""State history: each object stored once per version, recorded as runs finish and backfilled from the snapshots
retention kept, reading back exactly as the snapshots do (shadow mode)."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from officesentry import changes, lifecycle, retention, schedule, state_history, store, system_health, worker
from officesentry.__main__ import main
from officesentry.certs import generate_certificate
from officesentry.config import load_config
from officesentry.crypto import Vault
from officesentry.db import connect, migrate
from officesentry.reports import data_reports, month_changes, rollups, user_profile
from officesentry.rules import evaluate
from officesentry.snapshot import TenantSnapshot
from tests import graphdata as gd
from tests.seed import seed_tenant
from tests.test_collectors import USERS

from .conftest import login, make_user

NOW = datetime.now(UTC)


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _collect(
    db, tenant_id, objects: dict[str, list[tuple[str, dict]]], collector="users", days_ago=None, status="succeeded"
) -> int:
    """One run of ``collector`` storing ``objects`` ({kind: [(id, data)]}), finished as the worker finishes runs."""
    rid = store.enqueue_run(db, tenant_id, collector)
    db.execute("UPDATE runs SET status = 'running' WHERE id = ?", (rid,))
    for kind, items in objects.items():
        store.add_objects(db, rid, kind, items)
    store.finish_run(db, rid, status)
    if days_ago is not None:
        db.execute("UPDATE runs SET finished_at = ? WHERE id = ?", (iso(days_ago), rid))
    state_history.record_finished(db, rid)
    return rid


def _versions(db, kind="user"):
    return db.execute(
        "SELECT v.object_id, v.from_run, v.to_run FROM state_versions v JOIN state_series s"
        " ON s.id = v.series_id WHERE s.kind = ? ORDER BY v.object_id, v.from_run",
        (kind,),
    ).fetchall()


def _same_reads(db, run_ids):
    for rid in run_ids:
        assert state_history.run_objects(db, rid) == store.run_objects(db, rid), rid
        assert state_history.verify_run(db, rid) == []


def person(name, signed_in="2026-10-01T09:00:00Z", **extra):
    return {"displayName": name, "lastSignInDateTime": signed_in, "accountEnabled": True, **extra}


# -- Versions --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        {"displayName": "Ann", "lastSignInDateTime": "2026-10-01T09:00:00Z", "z": 1},
        {"displayName": "Ann", "lastSignInDateTime": None},
        {"displayName": "Ann"},
        {"lastSignInDateTime": "x", "lastNonInteractiveSignInDateTime": "y"},
        ["not", "a", "dict"],
        "text",
        None,
    ],
)
def test_fast_changing_fields_split_off_and_merge_back_exactly(data):
    stable, moving = state_history.split("user", data)
    assert store._line("u1", state_history.merge(stable, moving)) == store._line("u1", data)
    if isinstance(data, dict) and "lastSignInDateTime" in data:
        assert "lastSignInDateTime" not in stable and moving["lastSignInDateTime"] == data["lastSignInDateTime"]


def test_each_version_is_stored_once_and_every_run_reads_back(db, two_tenants):
    alpha = two_tenants[0]
    first = _collect(
        db,
        alpha,
        {
            "user": [("a", person("Ann")), ("b", person("Bob")), ("c", person("Cat"))],
            "group": [("g", {"name": "Staff"})],
        },
    )
    # Only sign-in times moved: no new versions, just the night's values.
    second = _collect(
        db,
        alpha,
        {
            "user": [("a", person("Ann", "2026-10-02T09:00:00Z")), ("b", person("Bob")), ("c", person("Cat", None))],
            "group": [("g", {"name": "Staff"})],
        },
    )
    # Bob renamed, Cat gone, Dan new, the group kind gone altogether.
    third = _collect(
        db,
        alpha,
        {"user": [("a", person("Ann", "2026-10-03T09:00:00Z")), ("b", person("Robert")), ("d", person("Dan"))]},
    )
    assert [tuple(v) for v in _versions(db)] == [
        ("a", first, None),
        ("b", first, third),
        ("b", third, None),
        ("c", first, third),
        ("d", third, None),
    ]
    assert [tuple(v) for v in _versions(db, "group")] == [("g", first, third)]
    _same_reads(db, [first, second, third])
    assert state_history.find_object(db, second, "user", "a")["lastSignInDateTime"] == "2026-10-02T09:00:00Z"
    assert state_history.find_object(db, second, "user", "c")["lastSignInDateTime"] is None
    assert state_history.find_object(db, third, "user", "c") is None
    assert state_history.find_object(db, first, "user", "zz") is None
    assert state_history.run_objects(db, third, "user", limit=2) == store.run_objects(db, third, "user", limit=2)
    for oid in "abcd":
        assert state_history.object_copies(db, alpha, "users", "user", oid) == store.object_copies(
            db, alpha, "users", "user", oid
        )
    assert state_history.matching_objects(db, alpha, "users", "user", "displayName", "Bob") == store.matching_objects(
        db, alpha, "users", "user", "displayName", "Bob"
    )
    # The other tenant has nothing, and a kind a run never stored reads as empty.
    assert state_history.run_objects(db, third, "device") == []
    assert db.execute("SELECT COUNT(*) FROM state_series WHERE tenant_id = ?", (two_tenants[1],)).fetchone()[0] == 0


def test_fast_changing_fields_store_only_what_changed_with_a_full_row_every_few(db, two_tenants):
    alpha = two_tenants[0]
    people = {oid: person(name) for oid, name in (("a", "Ann"), ("b", "Bob"), ("c", "Cat"), ("d", "Dan"))}
    runs = []
    for night in range(20):
        if night % 3 == 0:
            people["a"] = dict(people["a"], lastSignInDateTime=f"2026-10-{night + 1:02}T09:00:00Z")
        if night == 5:
            people["b"] = {k: v for k, v in people["b"].items() if k != "lastSignInDateTime"}  # no time any more
        if night == 8:
            del people["c"]
        if night == 12:
            people["c"] = person("Cat")  # back, with the time it had before
        if night == 15:
            people = {oid: dict(p, lastSignInDateTime=f"night {night}") for oid, p in people.items()}  # all moved
        runs.append(_collect(db, alpha, {"user": sorted(people.items())}))
    _same_reads(db, runs)
    rows = db.execute("SELECT run_id, full FROM state_volatile ORDER BY run_id").fetchall()
    assert len(rows) < len(runs)  # nights where nothing moved store nothing
    assert rows[0]["full"] and sum(r["full"] for r in rows) >= 3
    gaps, since = [], 0
    for row in rows:
        since = 0 if row["full"] else since + 1
        gaps.append(since)
    assert max(gaps) < state_history.KEYFRAME_ROWS
    assert len(_versions(db)) == 5  # sign-in times never made a version; Cat's return did


def test_a_run_that_stored_nothing_ends_every_version(db, two_tenants):
    alpha = two_tenants[0]
    first = _collect(db, alpha, {"user": [("a", person("Ann"))]})
    empty = _collect(db, alpha, {})
    again = _collect(db, alpha, {"user": [("a", person("Ann"))]})
    assert [tuple(v) for v in _versions(db)] == [("a", first, empty), ("a", again, None)]
    _same_reads(db, [first, empty, again])


def test_runs_go_in_in_order_and_only_once(db, two_tenants):
    alpha = two_tenants[0]
    first = _collect(db, alpha, {"user": [("a", person("Ann"))]})
    second = _collect(db, alpha, {"user": [("a", person("Ann B"))]})
    run = db.execute("SELECT * FROM runs WHERE id = ?", (first,)).fetchone()
    assert not state_history.record_run(db, run)  # older than what's recorded
    assert not state_history.record_run(db, db.execute("SELECT * FROM runs WHERE id = ?", (second,)).fetchone())
    assert db.execute("SELECT COUNT(*) FROM state_runs").fetchone()[0] == 2


def test_failed_runs_and_sign_in_detail_are_not_recorded(db, two_tenants):
    alpha = two_tenants[0]
    failed = _collect(db, alpha, {"user": [("a", person("Ann"))]}, status="failed")
    signins = _collect(
        db,
        alpha,
        {"sign_in_account": [("ann@x.com", {"count": 3})], "sign_in_note": [("n", {"x": 1})]},
        collector="sign_ins",
    )
    assert state_history.recorded(db, failed) is None
    assert {r[0] for r in db.execute("SELECT kind FROM state_series")} == {"sign_in_note"}
    # Reading still answers for them, from the snapshot.
    assert state_history.run_objects(db, signins) == store.run_objects(db, signins)
    assert state_history.find_object(db, signins, "sign_in_account", "ann@x.com") == {"count": 3}
    assert state_history.verify_run(db, signins) == []


def test_ids_and_values_that_need_escaping_survive(db, two_tenants):
    odd = ['quote"d', "back\\slash", "new\nline", "café", "日本", "", "a]b, c"]
    rid = _collect(db, two_tenants[0], {"thing": [(i, {"name": i, "n": [i, 1.5, True, None]}) for i in odd]})
    _same_reads(db, [rid])
    assert state_history.find_object(db, rid, "thing", "日本")["name"] == "日本"


def test_large_kinds_split_into_packs(db, two_tenants, monkeypatch):
    monkeypatch.setattr(store, "PACK_BYTES", 2000)
    users = [(f"u{i:03}", person(f"Person {i}", pad="x" * 100)) for i in range(100)]
    first = _collect(db, two_tenants[0], {"user": users})
    users[37] = ("u037", person("Renamed", pad="x" * 100))
    second = _collect(db, two_tenants[0], {"user": users})
    assert db.execute("SELECT COUNT(*) FROM state_packs WHERE run_id = ?", (first,)).fetchone()[0] > 5
    assert db.execute("SELECT COUNT(*) FROM state_packs WHERE run_id = ?", (second,)).fetchone()[0] == 1
    _same_reads(db, [first, second])


# -- Recording and the backfill --------------------------------------------------------------------


def test_every_collection_is_recorded_as_it_finishes(db, two_tenants):
    seed_tenant(db, two_tenants[0])
    runs = [r["id"] for r in db.execute("SELECT id FROM runs WHERE status IN ('succeeded', 'partial') ORDER BY id")]
    assert len(runs) > 20
    assert [r[0] for r in db.execute("SELECT run_id FROM state_runs ORDER BY run_id")] == runs
    _same_reads(db, runs)
    assert state_history.status(db)["mismatches"] == 0


def _before_history(db, tenant_id, nights=3):
    """Runs collected before the state history existed: snapshots only."""
    ids = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(state_history, "record_finished", lambda conn, run_id: False)
        for n in range(nights):
            ids.append(
                _collect(
                    db,
                    tenant_id,
                    {
                        "user": [
                            ("a", person("Ann", f"2026-10-0{n + 1}T09:00:00Z")),
                            ("b", person("Bob" if n < 2 else "Robert")),
                        ]
                    },
                )
            )
            ids.append(_collect(db, tenant_id, {"group": [("g", {"name": f"Staff {n // 2}"})]}, collector="groups"))
    return ids


def test_the_backfill_records_old_runs_oldest_first_a_little_at_a_time(db, two_tenants, monkeypatch):
    ids = _before_history(db, two_tenants[0])
    ticks = iter([0.0] + [0.0] * 2 + [10.0] * 50)  # time for two runs, then out of time
    assert state_history.catch_up(db, seconds=5, clock=lambda: next(ticks)) == (2, False)
    assert [r[0] for r in db.execute("SELECT run_id FROM state_runs ORDER BY run_id")] == ids[:2]
    assert db.execute("SELECT value FROM settings WHERE key = ?", (state_history.WATERMARK,)).fetchone()[0] == str(
        ids[1]
    )
    # A run finishing now waits for the backfill to reach its data source's earlier runs, then goes in as usual.
    late = _collect(db, two_tenants[0], {"user": [("a", person("Ann"))]})
    assert state_history.recorded(db, late) is None
    assert state_history.catch_up(db, seconds=None) == (5, True)
    _same_reads(db, [*ids, late])
    assert state_history.catch_up(db, seconds=None) == (0, True)
    newest = _collect(db, two_tenants[0], {"user": [("a", person("Ann"))]})
    assert state_history.recorded(db, newest) is not None


def test_the_backfill_passes_over_pruned_runs_and_waits_for_running_ones(db, two_tenants, monkeypatch):
    ids = _before_history(db, two_tenants[0])
    db.execute("DELETE FROM objects WHERE run_id = ?", (ids[2],))  # retention removed the second night's users
    running = store.enqueue_run(db, two_tenants[1], "users")
    db.execute("UPDATE runs SET status = 'running' WHERE id = ?", (running,))
    later = _collect(db, two_tenants[0], {"user": [("a", person("Ann"))]})
    assert state_history.catch_up(db, seconds=None) == (6, False)  # every kept run, and the newest
    assert state_history.recorded(db, ids[2]) is None
    assert state_history.recorded(db, later) is not None
    # The watermark stays below the run still going, so it's recorded once it finishes.
    assert (
        int(db.execute("SELECT value FROM settings WHERE key = ?", (state_history.WATERMARK,)).fetchone()[0])
        == running - 1
    )
    store.add_objects(db, running, "user", [("z", person("Zed"))])
    store.finish_run(db, running, "succeeded")
    assert state_history.catch_up(db, seconds=None) == (1, True)
    _same_reads(db, [r for r in (*ids, running, later) if r != ids[2]])
    # Reading the pruned night falls back to its (empty) snapshot, as the snapshot store answers.
    assert state_history.run_objects(db, ids[2]) == store.run_objects(db, ids[2]) == []


def test_the_worker_backfills_every_minute(db, two_tenants, monkeypatch):
    for module in worker.JOB_MODULES:
        __import__(module)
    assert schedule.PERIODIC["state_history"] is state_history._history_job
    ids = _before_history(db, two_tenants[0], nights=1)
    state_history._history_job(schedule.TickContext(None, db, None, None, NOW))
    assert all(state_history.recorded(db, rid) for rid in ids)


def test_nothing_is_recorded_while_the_disk_is_nearly_full(db, two_tenants, monkeypatch, caplog):
    ids = _before_history(db, two_tenants[0], nights=1)
    monkeypatch.setattr(system_health, "disk_usage", lambda path: (90.0, 10 * 2**30, 100 * 2**30))
    assert state_history.catch_up(db, seconds=None) == (0, False)
    rid = _collect(db, two_tenants[1], {"user": [("a", person("Ann"))]})
    assert state_history.recorded(db, rid) is None
    with caplog.at_level("WARNING"):
        state_history._history_job(schedule.TickContext(None, db, None, None, NOW))
        state_history._history_job(schedule.TickContext(None, db, None, None, NOW))
    assert caplog.text.count("paused while the disk is nearly full") == 1
    assert db.execute("SELECT COUNT(*) FROM state_runs").fetchone()[0] == 0
    monkeypatch.setattr(system_health, "disk_usage", lambda path: (40.0, 60 * 2**30, 100 * 2**30))
    assert state_history.catch_up(db, seconds=None) == (3, True)
    _same_reads(db, [*ids, rid])


def test_a_failure_while_recording_never_fails_the_collection(db, two_tenants, monkeypatch):
    def broken(conn, run, objects=None):
        raise sqlite3.OperationalError("database or disk is full")

    monkeypatch.setattr(state_history, "record_run", broken)
    seed_tenant(db, two_tenants[0])
    assert not db.execute("SELECT 1 FROM runs WHERE status = 'failed'").fetchone()
    assert db.execute("SELECT COUNT(*) FROM state_runs").fetchone()[0] == 0
    monkeypatch.undo()
    assert state_history.catch_up(db, seconds=None)[1]
    _same_reads(db, [r["id"] for r in db.execute("SELECT id FROM runs")])


def test_a_run_that_reads_back_differently_is_logged_and_counted(db, two_tenants, monkeypatch, caplog):
    real = state_history._version

    def lossy(kind, object_id, data):  # a bug that drops a field
        return real(kind, object_id, {k: v for k, v in data.items() if k != "accountEnabled"})

    monkeypatch.setattr(state_history, "_version", lossy)
    with caplog.at_level("ERROR"):
        rid = _collect(db, two_tenants[0], {"user": [("a", person("Ann"))], "group": [("g", {"name": "x"})]})
    assert f"run {rid} reads back differently from its snapshot (user)" in caplog.text
    status = state_history.status(db)
    assert (status["mismatches"], status["last_mismatch"]) == (1, f"run {rid}: user")
    assert state_history.verify_run(db, rid) == ["user"]


# -- Retention and tenant deletion -----------------------------------------------------------------


def test_retention_forgets_old_state_but_keeps_each_sources_latest(db, two_tenants, cfg):
    alpha = two_tenants[0]
    old = _collect(db, alpha, {"user": [("a", person("Ann")), ("b", person("Bob"))]}, days_ago=900)
    mid = _collect(db, alpha, {"user": [("a", person("Ann")), ("c", person("Cat"))]}, days_ago=800)
    recent = _collect(
        db, alpha, {"user": [("a", person("Ann", "2026-10-09T09:00:00Z")), ("c", person("Cat"))]}, days_ago=10
    )
    only = _collect(db, alpha, {"group": [("g", {"name": "Staff"})]}, collector="groups", days_ago=1000)
    expected = {rid: state_history.run_objects(db, rid) for rid in (recent, only)}
    assert retention.dry_run(db, retention.Policy())["state_months"] == 2
    assert db.execute("SELECT COUNT(*) FROM state_runs").fetchone()[0] == 4  # the dry run deleted nothing
    assert retention.apply(db, cfg)["state_months"] == 2
    assert [r[0] for r in db.execute("SELECT run_id FROM state_runs ORDER BY run_id")] == [recent, only]
    # Bob ended before the cutoff and went; Ann and Cat run on from the older runs they started in.
    assert [tuple(v) for v in _versions(db)] == [("a", old, None), ("c", mid, None)]
    assert {rid: state_history.run_objects(db, rid) for rid in (recent, only)} == expected
    assert not db.execute("SELECT 1 FROM state_volatile WHERE run_id IN (?, ?)", (old, mid)).fetchone()
    # No pack is left that no version points at.
    assert not db.execute("SELECT 1 FROM state_packs WHERE id NOT IN (SELECT pack_id FROM state_versions)").fetchone()
    assert retention.prune_state(db, None) == 0  # kept forever


def test_deleting_a_tenant_deletes_its_history(db, two_tenants):
    alpha, bravo = two_tenants
    seed_tenant(db, alpha)
    seed_tenant(db, bravo)
    lifecycle.delete_tenant(db, store.get_tenant(db, alpha), "admin")
    for table in ("state_series", "state_runs", "state_packs"):
        assert not db.execute(f"SELECT 1 FROM {table} WHERE tenant_id = ?", (alpha,)).fetchone(), table
    for table in ("state_versions", "state_volatile"):
        assert not db.execute(
            f"SELECT 1 FROM {table} WHERE series_id NOT IN (SELECT id FROM state_series)"
        ).fetchone(), table
    assert db.execute("SELECT COUNT(*) FROM state_runs WHERE tenant_id = ?", (bravo,)).fetchone()[0] > 20


# -- The upgrade, the dry run and the command ------------------------------------------------------


def test_the_upgrade_adds_empty_tables_and_changes_no_rows(cfg, monkeypatch):
    from officesentry import db as db_module

    every = db_module.migration_files()
    first = next(n for n, path in every if "state_history" in str(path))
    before = [m for m in every if m[0] < first]  # a database from before the daily history (and anything since)
    monkeypatch.setattr(db_module, "migration_files", lambda: before)
    conn = connect(cfg.db_path)
    migrate(conn)
    tid = store.create_tenant(conn, "Old", "old.onmicrosoft.com", None)
    rid = store.enqueue_run(conn, tid, "users")
    store.add_objects(conn, rid, "user", [("a", person("Ann"))])
    store.finish_run(conn, rid, "succeeded")
    rows = conn.execute("SELECT * FROM objects").fetchall()
    monkeypatch.setattr(db_module, "migration_files", lambda: every)
    migrate(conn)
    assert conn.execute("SELECT * FROM objects").fetchall() == rows
    assert conn.execute("SELECT COUNT(*) FROM state_runs").fetchone()[0] == 0
    assert list(cfg.db_path.parent.glob("backups/pre-upgrade-v*-to-v*.db"))  # copied first, as every upgrade
    assert state_history.catch_up(conn, seconds=None) == (1, True)
    _same_reads(conn, [rid])
    conn.close()


def test_the_dry_run_estimates_without_writing(db, two_tenants, monkeypatch):
    seed_tenant(db, two_tenants[0])
    db.execute("DELETE FROM state_versions")
    db.execute("DELETE FROM state_series")
    for table in ("state_packs", "state_volatile", "state_runs"):
        db.execute(f"DELETE FROM {table}")
    changes_before = db.total_changes
    est = state_history.estimate(db, seconds=60)
    assert db.total_changes == changes_before
    assert est.pending == est.sampled_runs > 20 and est.share == 1.0
    state_history.catch_up(db, seconds=None)
    stored = state_history.status(db)
    assert est.versions == stored["versions"]
    assert 0.6 < est.total_bytes / stored["bytes"] < 2.5, (est.total_bytes, stored["bytes"])


def test_the_history_command(cfg, db, two_tenants, monkeypatch, capsys):
    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(cfg.data_dir))
    _before_history(db, two_tenants[0], nights=2)
    main(["history"])
    out = capsys.readouterr().out
    assert "0 runs recorded" in out and "4 runs still to record" in out, out
    main(["history", "--dry-run", "--seconds", "5"])
    out = capsys.readouterr().out
    assert "Dry run, nothing written. 4 runs" in out and "Room needed: about" in out
    assert db.execute("SELECT COUNT(*) FROM state_runs").fetchone()[0] == 0
    main(["history", "--backfill"])
    assert "Recorded 4 runs" in capsys.readouterr().out
    main(["history", "--verify"])
    assert "Checked 4 runs that still have their snapshot: every one reads back exactly." in capsys.readouterr().out
    main(["history"])
    out = capsys.readouterr().out
    assert "4 runs recorded" in out and "0 runs still to record" in out
    main(["history", "--dry-run"])
    assert "Nothing to record" in capsys.readouterr().out
    # A pack damaged on disk is found.
    db.execute(
        "UPDATE state_packs SET data = ? WHERE id = (SELECT MIN(id) FROM state_packs)",
        (store.PACK_ZLIB + __import__("zlib").compress(b'["a", {"displayName": "Mallory"}]'),),
    )
    with pytest.raises(SystemExit):
        main(["history", "--verify"])
    assert "differ from the snapshot" in capsys.readouterr().out


def test_retention_page_has_the_daily_history(client, db):
    make_user(db, "admin")
    login(client, "admin")
    page = client.get("/admin/retention").get_data(as_text=True)
    assert "Daily history" in page and 'name="state_months"' in page and 'value="25"' in page


# -- The history reads exactly as the snapshots: every report, check, change feed and page --------


@pytest.fixture(scope="module")
def recorded(tmp_path_factory):
    """Two tenants with two collections each, a month apart, recorded as they finished."""
    with pytest.MonkeyPatch.context() as mp:
        for var in ("SECRET_KEY", "ENCRYPTION_KEYS", "DATA_DIR"):
            mp.delenv("OFFICESENTRY_" + var, raising=False)
        cfg = load_config(data_dir=tmp_path_factory.mktemp("history"), testing=True, secure_cookies=False)
    conn = connect(cfg.db_path)
    migrate(conn)
    cid = store.create_connection(
        conn,
        Vault(cfg.encryption_keys),
        "Test app",
        generate_certificate("test"),
        client_id="11111111-2222-3333-4444-555555555555",
    )
    tenants = [store.create_tenant(conn, name, f"{name.lower()}.onmicrosoft.com", cid) for name in ("Alpha", "Bravo")]
    for tid in tenants:
        seed_tenant(conn, tid, users=[*USERS, gd.INFO_USER], inbox_rules={})
    old = iso(40)
    conn.execute("UPDATE runs SET finished_at = ?, started_at = ?, created_at = ?", (old, old, old))
    conn.execute("UPDATE state_runs SET finished_at = ?", (old,))
    conn.execute("UPDATE metrics SET captured_at = ?", (old,))
    moved = dict(USERS[1], signInActivity={"lastSignInDateTime": "2026-10-08T07:00:00Z"})
    changed = [dict(USERS[0], accountEnabled=False, assignedLicenses=[]), moved, *USERS[2:], gd.INFO_USER]
    for tid in tenants:
        seed_tenant(conn, tid, users=changed)
    runs = [r["id"] for r in conn.execute("SELECT id FROM runs WHERE status IN ('succeeded', 'partial')")]
    assert [r[0] for r in conn.execute("SELECT run_id FROM state_runs ORDER BY run_id")] == runs
    yield conn, tenants
    conn.close()


def _same(value) -> str:
    return re.sub(r" at 0x[0-9a-f]+", "", repr(value))


READERS = ("run_objects", "find_object", "object_copies", "matching_objects")


def _both(recorded, fn, monkeypatch):
    """``fn`` over each tenant reading the snapshots, then reading only the history."""
    conn, tenants = recorded
    a = _same([fn(conn, store.get_tenant(conn, tid)) for tid in tenants])
    with monkeypatch.context() as mp:
        for name in READERS:
            mp.setattr(store, name, getattr(state_history, name))
        b = _same([fn(conn, store.get_tenant(conn, tid)) for tid in tenants])
    return a, b


def test_every_recorded_run_reads_back_as_its_snapshot(recorded):
    conn, _ = recorded
    for (rid,) in conn.execute("SELECT run_id FROM state_runs").fetchall():
        assert state_history.verify_run(conn, rid) == [], rid
    # The second night's sign-in time moved without a new version of the account.
    kinds = {r[0] for r in conn.execute("SELECT DISTINCT kind FROM state_volatile JOIN state_series ON id = series_id")}
    assert "user" in kinds


@pytest.mark.parametrize("key", list(data_reports.REPORTS))
def test_every_report_is_identical_from_the_history(recorded, key, monkeypatch):
    a, b = _both(recorded, lambda conn, t: data_reports.build(conn, t, key, NOW), monkeypatch)
    assert a == b


def test_checks_and_change_feeds_are_identical_from_the_history(recorded, monkeypatch):
    a, b = _both(recorded, lambda conn, t: evaluate(TenantSnapshot(conn, t), NOW), monkeypatch)
    assert a == b and "pass" in a
    a, b = _both(
        recorded, lambda conn, t: evaluate(TenantSnapshot(conn, t, as_of=NOW - timedelta(days=20)), NOW), monkeypatch
    )
    assert a == b
    a, b = _both(recorded, lambda conn, t: changes.all_changes(conn, t["id"], iso(20)), monkeypatch)
    assert a == b and "Change(" in a
    a, b = _both(recorded, lambda conn, t: month_changes.build(conn, t, now=NOW), monkeypatch)
    assert a == b


def test_account_pages_and_their_history_are_identical_from_the_history(recorded, monkeypatch):
    ids = [u["id"] for u in USERS] + [gd.INFO_USER["id"]]
    a, b = _both(recorded, lambda conn, t: [user_profile.build(conn, t, uid, NOW) for uid in ids], monkeypatch)
    assert a == b and "Sign-in blocked" in a


def test_cross_tenant_pages_are_identical_from_the_history(recorded, monkeypatch):
    conn, tenants = recorded
    rows = [store.get_tenant(conn, t) for t in tenants]
    for fn in (
        lambda: rollups.check_matrix(conn, rows, NOW),
        lambda: month_changes.across(conn, rows, now=NOW),
        lambda: [rollups.build(conn, rows, key, NOW) for key in data_reports.REPORTS],
    ):
        a = _same(fn())
        with monkeypatch.context() as mp:
            for name in READERS:
                mp.setattr(store, name, getattr(state_history, name))
            assert _same(fn()) == a
