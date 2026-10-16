"""Compressed snapshot storage: packs, the background converter, and identical output from old and new rows."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from officesentry import changes, packing, retention, schedule, store, worker
from officesentry.__main__ import main
from officesentry.certs import generate_certificate
from officesentry.config import load_config
from officesentry.crypto import Vault
from officesentry.db import connect, migrate, utcnow
from officesentry.reports import data_reports, month_changes, rollups, user_profile
from officesentry.rules import evaluate
from officesentry.snapshot import TenantSnapshot
from tests import graphdata as gd
from tests.seed import seed_tenant
from tests.stored import unpack
from tests.test_collectors import USERS

NOW = datetime.now(UTC)


def _run(db, tenant_id, collector="users"):
    return store.enqueue_run(db, tenant_id, collector)


def _kinds(db, run_id):
    return [
        (r["kind"], r["object_id"], isinstance(r["data"], bytes), r["items"])
        for r in db.execute(
            "SELECT kind, object_id, data, items FROM objects WHERE run_id = ? ORDER BY kind, object_id", (run_id,)
        )
    ]


# -- Packs ---------------------------------------------------------------------------------------


def test_a_second_batch_of_a_kind_replaces_ids_and_keeps_order(db, two_tenants):
    rid = _run(db, two_tenants[0])
    store.add_objects(db, rid, "user", [("b", {"n": 1}), ("a", {"n": 2})])
    store.add_objects(db, rid, "user", [("c", {"n": 3}), ("a", {"n": 4})])
    store.add_objects(db, rid, "user", [])
    store.add_objects(db, rid, "group", [("g", {"x": None})])
    assert [(o["_kind"], o["_id"], o.get("n")) for o in store.run_objects(db, rid)] == [
        ("group", "g", None),
        ("user", "a", 4),
        ("user", "b", 1),
        ("user", "c", 3),
    ]
    assert _kinds(db, rid) == [("group", "g", True, 1), ("user", "a", True, 3)]
    store.finish_run(db, rid, "succeeded")
    assert db.execute("SELECT object_count FROM runs WHERE id = ?", (rid,)).fetchone()[0] == 4


def test_large_kinds_split_into_packs_and_one_object_reads_one_pack(db, two_tenants, monkeypatch):
    monkeypatch.setattr(store, "PACK_BYTES", 2000)
    rid = _run(db, two_tenants[0])
    users = [(f"u{i:03}", {"displayName": f"Person {i}", "pad": "x" * 100, "n": i}) for i in range(100)]
    store.add_objects(db, rid, "user", users)
    packs = _kinds(db, rid)
    assert len(packs) > 5 and sum(p[3] for p in packs) == 100
    assert [p[1] for p in packs] == sorted(p[1] for p in packs)  # keyed by their first object
    assert [o["n"] for o in store.run_objects(db, rid, "user")] == list(range(100))
    assert [o["n"] for o in store.run_objects(db, rid, "user", limit=7)] == list(range(7))
    for i in (0, 37, 99):
        assert store.find_object(db, rid, "user", f"u{i:03}")["n"] == i
    assert store.find_object(db, rid, "user", "u0371") is None  # between two ids
    assert store.find_object(db, rid, "user", "a") is None  # before the first
    assert store.find_object(db, rid, "group", "u001") is None


def test_ids_and_values_that_need_escaping_survive(db, two_tenants):
    rid = _run(db, two_tenants[0])
    odd = ['quote"d', "back\\slash", "new\nline", "café", "日本", "", "a]b, c"]
    store.add_objects(db, rid, "thing", [(i, {"name": i, "nested": {"list": [i, 1.5, True, None]}}) for i in odd])
    got = {o["_id"]: o for o in store.run_objects(db, rid, "thing")}
    assert set(got) == set(odd)
    for i in odd:
        assert got[i]["name"] == i and got[i]["nested"]["list"] == [i, 1.5, True, None]
        assert store.find_object(db, rid, "thing", i)["name"] == i
    assert [o["_id"] for o in store.run_objects(db, rid, "thing")] == sorted(odd)  # SQLite's order: by UTF-8 bytes


def test_matching_objects_reads_old_and_new_rows(db, two_tenants):
    alpha = two_tenants[0]
    old = _run(db, alpha, "admin_roles")
    store.add_objects(
        db,
        old,
        "role_assignment",
        [("r1", {"principalId": "p1"}), ("r2", {"principalId": "p2"}), ("r3", {"principalId": 7}), ("r4", ["p1"])],
    )
    store.finish_run(db, old, "succeeded")
    unpack(db)
    new = _run(db, alpha, "admin_roles")
    store.add_objects(db, new, "role_assignment", [("r5", {"principalId": "p1"}), ("r1", {"principalId": "p1"})])
    store.finish_run(db, new, "succeeded")
    got = store.matching_objects(db, alpha, "admin_roles", "role_assignment", "principalId", "p1")
    assert [(object_id, run_id) for object_id, _, run_id, _ in got] == [("r1", old), ("r1", new), ("r5", new)]
    finished = [
        r["finished_at"] for r in db.execute("SELECT finished_at FROM runs WHERE id IN (?, ?) ORDER BY id", (old, new))
    ]
    assert store.object_copies(db, alpha, "admin_roles", "role_assignment", "r1") == [
        (finished[0], {"principalId": "p1"}),
        (finished[1], {"principalId": "p1"}),
    ]
    assert store.object_copies(db, alpha, "admin_roles", "role_assignment", "r2") == [
        (finished[0], {"principalId": "p2"})
    ]


def test_an_unknown_pack_format_is_refused(db, two_tenants):
    rid = _run(db, two_tenants[0])
    db.execute(
        "INSERT INTO objects (run_id, kind, object_id, data, items) VALUES (?, 'user', 'a', ?, 1)", (rid, b"\x09nope")
    )
    with pytest.raises(ValueError, match="unknown object pack format"):
        store.run_objects(db, rid)


def test_counts_read_objects_not_rows(db, two_tenants):
    from officesentry import lifecycle

    alpha = two_tenants[0]
    rid = _run(db, alpha, "sign_ins")
    store.add_objects(db, rid, "sign_in_account", [(f"a{i}", {"i": i}) for i in range(5)])
    store.add_objects(db, rid, "sign_in_status", [("status", {"available": True})])
    store.finish_run(db, rid, "succeeded")
    assert lifecycle.stored(db, alpha)["objects"] == 6
    old = (NOW - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.execute("UPDATE runs SET finished_at = ? WHERE id = ?", (old, rid))
    marks = ",".join("?" * len(retention.SIGNIN_KINDS))
    expected = db.execute(
        f"SELECT COALESCE(SUM(items), 0) FROM objects WHERE kind IN ({marks})", retention.SIGNIN_KINDS
    ).fetchone()[0]
    assert retention.prune_signin_detail(db, 30) == expected > 0


# -- Converting old rows -------------------------------------------------------------------------


def _legacy(db, tenant_id, runs=3, users=40):
    """Runs stored the way Office Sentry stored them before packing: one row of JSON text per object."""
    ids = []
    for r in range(runs):
        rid = _run(db, tenant_id, ["users", "admin_roles", "groups"][r % 3])
        store.add_objects(db, rid, "user", [(f"u{i}", {"displayName": f"Person {i}", "run": r}) for i in range(users)])
        store.add_objects(db, rid, "status", [("status", {"available": True})])
        store.finish_run(db, rid, "succeeded")
        ids.append(rid)
    unpack(db)
    return ids


def test_converter_packs_old_rows_a_little_at_a_time_and_resumes(db, two_tenants):
    ids = _legacy(db, two_tenants[0])
    before = {rid: store.run_objects(db, rid) for rid in ids}
    assert all(not packed for rid in ids for _, _, packed, _ in _kinds(db, rid))
    ticks = iter([0.0, 0.0, 10.0, 10.0])  # time for one run, then out of time
    assert packing.pack_some(db, seconds=5, clock=lambda: next(ticks)) == (41, False)
    assert db.execute("SELECT value FROM settings WHERE key = ?", (packing.PROGRESS_KEY,)).fetchone()[0] == str(ids[0])
    assert all(packed for _, _, packed, _ in _kinds(db, ids[0]))
    assert not any(packed for _, _, packed, _ in _kinds(db, ids[1]))
    assert packing.pack_some(db, seconds=None) == (82, True)
    assert packing.pack_some(db, seconds=None) == (0, True)
    assert packing.unpacked_objects(db) == 0
    assert {rid: store.run_objects(db, rid) for rid in ids} == before
    assert [r["object_count"] for r in db.execute("SELECT object_count FROM runs ORDER BY id")] == [41, 41, 41]


def test_a_conversion_stopped_part_way_loses_nothing_and_resumes(db, two_tenants, monkeypatch):
    ids = _legacy(db, two_tenants[0])
    before = {rid: store.run_objects(db, rid) for rid in ids}
    real = store.write_packs
    calls = []

    def dies_on_the_third_kind(conn, run_id, kind, lines):
        calls.append(kind)
        if len(calls) == 3:  # the worker stopped (or the disk filled) half way through writing a kind
            conn.execute("DELETE FROM objects WHERE run_id = ? AND kind = ?", (run_id, kind))
            raise sqlite3.OperationalError("database or disk is full")
        real(conn, run_id, kind, lines)

    monkeypatch.setattr(store, "write_packs", dies_on_the_third_kind)
    with pytest.raises(sqlite3.OperationalError):
        packing.pack_some(db, seconds=None)
    # The kind being written was rolled back whole: every run still reads exactly as before.
    assert {rid: store.run_objects(db, rid) for rid in ids} == before
    assert db.execute("SELECT value FROM settings WHERE key = ?", (packing.PROGRESS_KEY,)).fetchone()[0] == str(ids[0])
    monkeypatch.setattr(store, "write_packs", real)
    assert packing.pack_some(db, seconds=None) == (82, True)  # carries on from the run it stopped in
    assert {rid: store.run_objects(db, rid) for rid in ids} == before
    assert packing.unpacked_objects(db) == 0


def test_the_upgrade_itself_is_instant_and_changes_no_rows(cfg, monkeypatch):
    from officesentry import db as db_module

    every = db_module.migration_files()
    monkeypatch.setattr(db_module, "migration_files", lambda: every[:-1])
    conn = connect(cfg.db_path)
    migrate(conn)
    tid = store.create_tenant(conn, "Old", "old.onmicrosoft.com", None)
    # As the older release queued it: store's own code may use columns later migrations add.
    rid = conn.execute(
        "INSERT INTO runs (tenant_id, collector, status, trigger, created_at)"
        " VALUES (?, 'users', 'queued', 'manual', ?)",
        (tid, utcnow()),
    ).lastrowid
    conn.execute("INSERT INTO objects (run_id, kind, object_id, data) VALUES (?, 'user', 'u1', '{\"n\": 1}')", (rid,))
    monkeypatch.setattr(db_module, "migration_files", lambda: every)
    migrate(conn)
    row = conn.execute("SELECT data, items FROM objects").fetchone()
    assert (row["data"], row["items"]) == ('{"n": 1}', 1)  # left as it was, and counted as one object
    assert store.run_objects(conn, rid) == [{"n": 1, "_kind": "user", "_id": "u1"}]
    conn.close()


def test_converter_runs_from_the_worker_every_minute(db, two_tenants):
    for module in worker.JOB_MODULES:
        __import__(module)
    assert schedule.PERIODIC["pack_snapshots"] is packing._pack_job
    _legacy(db, two_tenants[0], runs=1)
    packing._pack_job(schedule.TickContext(None, db, None, None, NOW))
    assert packing.unpacked_objects(db) == 0


def test_compact_packs_everything_and_shrinks_the_file(cfg, db, two_tenants, monkeypatch, capsys):
    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(cfg.data_dir))
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    schema = cfg.db_path.stat().st_size  # every table's empty pages, which compression can't shrink
    _legacy(db, two_tenants[0], runs=6, users=2000)
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    before = cfg.db_path.stat().st_size
    main(["compact"])
    out = capsys.readouterr().out
    assert "Compressed 12006 snapshot objects" in out
    assert "Shrank the database file" in out
    # The snapshot data shrinks about 16 times; new tables in later migrations don't change that.
    assert cfg.db_path.stat().st_size - schema < (before - schema) / 10
    assert packing.unpacked_objects(db) == 0
    assert not list(cfg.data_dir.glob("etilqs_*"))  # VACUUM's temporary file went next to the database, then away


def test_compact_refuses_without_room(cfg, db, two_tenants, monkeypatch):
    import shutil

    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(cfg.data_dir))
    _legacy(db, two_tenants[0], runs=1)
    monkeypatch.setattr(packing.shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(10**9, 10**9, 10))
    with pytest.raises(SystemExit, match="Free some space and run compact again"):
        main(["compact"])
    assert packing.unpacked_objects(db) == 0  # compressing needs no extra room, so that part was done


# -- Identical output from old and new rows ------------------------------------------------------


@pytest.fixture(scope="module")
def twins(tmp_path_factory):
    """Two copies of one database with two tenants and two collections each: one as written now (packs), one
    with every object stored the old way (one row of JSON text each)."""
    with pytest.MonkeyPatch.context() as mp:
        for var in ("SECRET_KEY", "ENCRYPTION_KEYS", "DATA_DIR"):
            mp.delenv("OFFICESENTRY_" + var, raising=False)
        cfg = load_config(data_dir=tmp_path_factory.mktemp("parity"), testing=True, secure_cookies=False)
    packed = connect(cfg.db_path)
    migrate(packed)
    cid = store.create_connection(
        packed,
        Vault(cfg.encryption_keys),
        "Test app",
        generate_certificate("test"),
        client_id="11111111-2222-3333-4444-555555555555",
    )
    tenants = [store.create_tenant(packed, name, f"{name.lower()}.onmicrosoft.com", cid) for name in ("Alpha", "Bravo")]
    for tid in tenants:
        seed_tenant(packed, tid, users=[*USERS, gd.INFO_USER], inbox_rules={})
    old = (NOW - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
    packed.execute("UPDATE runs SET finished_at = ?, started_at = ?, created_at = ?", (old, old, old))
    packed.execute("UPDATE metrics SET captured_at = ?", (old,))
    changed = [dict(USERS[0], accountEnabled=False, assignedLicenses=[]), *USERS[1:], gd.INFO_USER]
    for tid in tenants:
        seed_tenant(packed, tid, users=changed)
    plain_path = cfg.db_path.with_name("plain.db")
    plain = sqlite3.connect(plain_path)
    packed.backup(plain)
    plain.close()
    plain = connect(plain_path)
    unpack(plain)
    assert plain.execute("SELECT COUNT(*) FROM objects WHERE typeof(data) = 'blob'").fetchone()[0] == 0
    assert packed.execute("SELECT COUNT(*) FROM objects WHERE typeof(data) = 'text'").fetchone()[0] == 0
    yield packed, plain, tenants
    packed.close()
    plain.close()


def _same(value) -> str:
    """A comparable form of a result: its repr, without memory addresses."""
    return re.sub(r" at 0x[0-9a-f]+", "", repr(value))


def _both(twins, fn):
    packed, plain, tenants = twins
    a = [fn(packed, store.get_tenant(packed, tid)) for tid in tenants]
    b = [fn(plain, store.get_tenant(plain, tid)) for tid in tenants]
    return _same(a), _same(b)


@pytest.mark.parametrize("key", list(data_reports.REPORTS))
def test_every_report_is_identical_from_old_and_new_rows(twins, key):
    a, b = _both(twins, lambda conn, t: data_reports.build(conn, t, key, NOW))
    assert a == b


def test_checks_and_change_feeds_are_identical(twins):
    a, b = _both(twins, lambda conn, t: evaluate(TenantSnapshot(conn, t), NOW))
    assert a == b and "pass" in a
    before = (NOW - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")  # the first collection is the baseline
    a, b = _both(twins, lambda conn, t: changes.all_changes(conn, t["id"], before))
    assert a == b and "Change(" in a
    a, b = _both(twins, lambda conn, t: month_changes.build(conn, t, now=NOW))
    assert a == b


def test_account_pages_and_their_history_are_identical(twins):
    ids = [u["id"] for u in USERS] + [gd.INFO_USER["id"]]
    a, b = _both(twins, lambda conn, t: [user_profile.build(conn, t, uid, NOW) for uid in ids])
    assert a == b and "Sign-in blocked" in a  # history across the two collections was read


def test_cross_tenant_pages_are_identical(twins):
    packed, plain, tenants = twins
    for fn in (
        lambda conn, ts: rollups.check_matrix(conn, ts, NOW),
        lambda conn, ts: month_changes.across(conn, ts, now=NOW),
        lambda conn, ts: [rollups.build(conn, ts, key, NOW) for key in list(data_reports.REPORTS)[:8]],
    ):
        a = fn(packed, [store.get_tenant(packed, t) for t in tenants])
        b = fn(plain, [store.get_tenant(plain, t) for t in tenants])
        assert _same(a) == _same(b)


def test_packed_storage_is_several_times_smaller(twins):
    packed, plain, _ = twins
    size = {}
    for name, conn in (("packed", packed), ("plain", plain)):
        size[name] = sum(
            len(r[0]) if isinstance(r[0], bytes) else len(r[0].encode()) + len(r[1])
            for r in conn.execute("SELECT data, object_id FROM objects")
        )
    assert size["packed"] * 3 < size["plain"], size
