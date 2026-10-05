"""Backups before upgrades, and restoring a backup."""

import os
import sqlite3

import pytest

from officesentry import backups, store, worker
from officesentry import db as db_module
from officesentry.__main__ import main
from officesentry.db import connect, migrate


@pytest.fixture
def env(cfg, monkeypatch):
    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(cfg.data_dir))
    return cfg


def _tenants(path):
    conn = connect(path)
    try:
        return [r["name"] for r in conn.execute("SELECT name FROM tenants ORDER BY id")]
    finally:
        conn.close()


def test_upgrading_a_database_backs_it_up_first(cfg, monkeypatch):
    every = db_module.migration_files()
    monkeypatch.setattr(db_module, "migration_files", lambda: [m for m in every if m[0] <= 4])
    conn = connect(cfg.db_path)
    migrate(conn)
    conn.execute("INSERT INTO tenants (name, tenant_ref, created_at) VALUES ('Old', 'old.onmicrosoft.com', 'x')")
    monkeypatch.setattr(db_module, "migration_files", lambda: every)
    assert migrate(conn) == every[-1][0]
    saved = sorted((cfg.data_dir / "backups").glob("pre-upgrade-*.db"))
    assert len(saved) == 1 and saved[0].name.startswith(f"pre-upgrade-v4-to-v{every[-1][0]}-")
    copy = connect(saved[0])
    assert copy.execute("PRAGMA user_version").fetchone()[0] == 4
    assert copy.execute("SELECT name FROM tenants").fetchone()[0] == "Old"
    if os.name != "nt":
        assert oct(saved[0].stat().st_mode & 0o777) == "0o600"
    # Nothing to upgrade, nothing copied.
    migrate(conn)
    assert len(list((cfg.data_dir / "backups").glob("pre-upgrade-*.db"))) == 1


def test_a_fresh_database_is_not_backed_up(cfg):
    migrate(connect(cfg.db_path))
    assert not (cfg.data_dir / "backups").exists()


def test_only_the_newest_pre_upgrade_backups_are_kept(cfg, monkeypatch):
    folder = cfg.data_dir / "backups"
    folder.mkdir(parents=True)
    for i in range(5):
        (folder / f"pre-upgrade-v1-to-v2-2026010{i}-000000.db").write_bytes(b"")
    (folder / "officesentry-20260101-000000.db").write_bytes(b"")  # a manual backup is never rotated here
    every = db_module.migration_files()
    monkeypatch.setattr(db_module, "migration_files", lambda: every[:-1])
    conn = connect(cfg.db_path)
    migrate(conn)
    monkeypatch.setattr(db_module, "migration_files", lambda: every)
    migrate(conn)
    kept = sorted(p.name for p in folder.glob("pre-upgrade-*.db"))
    assert len(kept) == backups.KEEP_PRE_UPGRADE and kept[-1].startswith(f"pre-upgrade-v{every[-2][0]}-")
    assert (folder / "officesentry-20260101-000000.db").exists()


def test_a_failed_backup_stops_the_upgrade(cfg, monkeypatch):
    every = db_module.migration_files()
    monkeypatch.setattr(db_module, "migration_files", lambda: every[:-1])
    conn = connect(cfg.db_path)
    migrate(conn)

    def full_disk(*_a):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(backups, "copy_database", full_disk)
    monkeypatch.setattr(db_module, "migration_files", lambda: every)
    with pytest.raises(RuntimeError, match="wasn't upgraded"):
        migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == every[-2][0]


def test_backup_and_restore(env, db, two_tenants, capsys):
    main(["backup"])
    saved = next((env.data_dir / "backups").glob("officesentry-*.db"))
    store.create_tenant(db, "Charlie Co", "charlie.onmicrosoft.com", None)
    db.execute("DELETE FROM tenants WHERE name = 'Alpha Ltd'")
    assert _tenants(env.db_path) == ["Bravo Inc", "Charlie Co"]

    main(["restore", str(saved)])
    out = capsys.readouterr().out
    assert "Restored" in out and "secrets.json" in out
    assert _tenants(env.db_path) == ["Alpha Ltd", "Bravo Inc"]
    # The database it replaced is kept, in case the wrong file was restored.
    before = next((env.data_dir / "backups").glob("pre-restore-*.db"))
    assert _tenants(before) == ["Bravo Inc", "Charlie Co"]
    # An open connection sees the restored data too: nothing was copied behind SQLite's back.
    assert [r["name"] for r in db.execute("SELECT name FROM tenants ORDER BY id")] == ["Alpha Ltd", "Bravo Inc"]
    assert connect(env.db_path).execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_restore_refuses_while_the_worker_runs(env, db, two_tenants):
    main(["backup"])
    saved = next((env.data_dir / "backups").glob("officesentry-*.db"))
    worker.beat(db)
    with pytest.raises(SystemExit, match="worker is running"):
        main(["restore", str(saved)])
    main(["restore", str(saved), "--force"])


@pytest.mark.parametrize("content", [b"", b"not a database at all, just some text" * 200])
def test_restore_refuses_files_that_are_not_databases(env, db, tmp_path, content):
    bogus = tmp_path / "bogus.db"
    bogus.write_bytes(content)
    with pytest.raises(SystemExit, match="Not restored"):
        main(["restore", str(bogus)])


def test_restore_refuses_other_sqlite_databases_and_newer_versions(env, db, tmp_path):
    other = tmp_path / "other.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE notes (x)")
    conn.close()
    with pytest.raises(SystemExit, match="isn't an Office Sentry database"):
        main(["restore", str(other)])

    newer = tmp_path / "newer.db"
    conn = connect(newer)
    migrate(conn)
    conn.execute("PRAGMA user_version = 999")
    conn.close()
    with pytest.raises(SystemExit, match="newer Office Sentry"):
        main(["restore", str(newer)])
    with pytest.raises(SystemExit, match="live database"):
        main(["restore", str(env.db_path)])


def test_restore_onto_a_new_server_clears_a_stale_wal(env, db, two_tenants, tmp_path):
    main(["backup", "--dir", str(tmp_path / "offsite")])
    saved = next((tmp_path / "offsite").glob("officesentry-*.db"))
    db.close()
    env.db_path.unlink()
    for suffix in ("-wal", "-shm"):
        path = env.db_path.with_name(env.db_path.name + suffix)
        path.unlink(missing_ok=True)
        path.write_bytes(b"left over from a database that's gone")
    main(["restore", str(saved)])
    assert _tenants(env.db_path) == ["Alpha Ltd", "Bravo Inc"]
    assert not list((env.data_dir / "backups").glob("pre-restore-*.db"))


def test_an_older_backup_is_upgraded_on_the_next_start(env, monkeypatch, tmp_path, capsys):
    every = db_module.migration_files()
    old = tmp_path / "old.db"
    monkeypatch.setattr(db_module, "migration_files", lambda: [m for m in every if m[0] <= 4])
    conn = connect(old)
    migrate(conn)
    conn.execute("INSERT INTO tenants (name, tenant_ref, created_at) VALUES ('Old', 'old.onmicrosoft.com', 'x')")
    conn.close()
    monkeypatch.setattr(db_module, "migration_files", lambda: every)
    main(["restore", str(old)])
    assert "upgraded to version" in capsys.readouterr().out
    conn = connect(env.db_path)
    assert migrate(conn) == every[-1][0]
    assert conn.execute("SELECT name FROM tenants").fetchone()[0] == "Old"
