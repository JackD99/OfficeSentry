"""Backups before upgrades, and restoring a backup."""

import gzip
import os
import shutil
import sqlite3
from contextlib import closing

import pytest

from officesentry import backups, store, worker
from officesentry import db as db_module
from officesentry.__main__ import main
from officesentry.config import SETUP_TOKEN_FILE, setup_token
from officesentry.db import connect, migrate
from tests.conftest import make_user


@pytest.fixture
def env(cfg, monkeypatch):
    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(cfg.data_dir))
    return cfg


def _unzip(path, tmp_path):
    """A gzipped backup as a plain database file to open."""
    plain = tmp_path / (path.name + ".db")
    with gzip.open(path, "rb") as zipped, open(plain, "wb") as out:
        shutil.copyfileobj(zipped, out)
    return plain


def _tenants(path):
    if path.name.endswith(".gz"):
        path = _unzip(path, path.parent)
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
    # Not compressed: the older release a rollback goes back to restores it, and may not read gzip.
    assert saved[0].name.endswith(".db") and saved[0].read_bytes()[:6] == b"SQLite"
    with closing(connect(saved[0])) as copy:
        assert copy.execute("PRAGMA user_version").fetchone()[0] == 4
        assert copy.execute("SELECT name FROM tenants").fetchone()[0] == "Old"
    if os.name != "nt":
        assert oct(saved[0].stat().st_mode & 0o777) == "0o600"
    # Nothing to upgrade, nothing copied.
    migrate(conn)
    assert len(list((cfg.data_dir / "backups").glob("pre-upgrade-*.db"))) == 1
    conn.close()


def test_a_fresh_database_is_not_backed_up(cfg):
    with closing(connect(cfg.db_path)) as conn:
        migrate(conn)
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
    conn.close()


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
    conn.close()


def test_backup_and_restore(env, db, two_tenants, capsys):
    main(["backup"])
    saved = next((env.data_dir / "backups").glob("officesentry-*.db.gz"))
    store.create_tenant(db, "Charlie Co", "charlie.onmicrosoft.com", None)
    db.execute("DELETE FROM tenants WHERE name = 'Alpha Ltd'")
    assert _tenants(env.db_path) == ["Bravo Inc", "Charlie Co"]

    main(["restore", str(saved)])
    out = capsys.readouterr().out
    assert "Restored" in out and "secrets.json" in out
    assert _tenants(env.db_path) == ["Alpha Ltd", "Bravo Inc"]
    # The database it replaced is kept, in case the wrong file was restored.
    before = next((env.data_dir / "backups").glob("pre-restore-*.db.gz"))
    assert _tenants(before) == ["Bravo Inc", "Charlie Co"]
    # An open connection sees the restored data too: nothing was copied behind SQLite's back.
    assert [r["name"] for r in db.execute("SELECT name FROM tenants ORDER BY id")] == ["Alpha Ltd", "Bravo Inc"]
    with closing(connect(env.db_path)) as fresh:
        assert fresh.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # Nothing is left of the unpacked copy, SQLite's -shm and -wal beside it included.
    assert not list(env.data_dir.glob("restore-*"))
    main(["check-backup", str(saved)])
    assert sorted(p.name for p in saved.parent.iterdir() if p.name.startswith("officesentry-")) == [saved.name]


def test_a_backup_the_portal_cant_read_says_so(env, db, two_tenants, monkeypatch):
    main(["backup"])
    saved = next((env.data_dir / "backups").glob("officesentry-*.db.gz"))

    def denied(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(backups, "_is_gzip", denied)  # as for a file copied in as root
    with pytest.raises(SystemExit, match=r"can't be read \(Permission denied\)\. The portal's user"):
        main(["restore", str(saved)])
    assert _tenants(env.db_path) == ["Alpha Ltd", "Bravo Inc"]


def test_restore_refuses_while_the_worker_runs(env, db, two_tenants):
    main(["backup"])
    saved = next((env.data_dir / "backups").glob("officesentry-*.db.gz"))
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
    saved = next((tmp_path / "offsite").glob("officesentry-*.db.gz"))
    db.close()
    env.db_path.unlink()
    for suffix in ("-wal", "-shm"):
        path = env.db_path.with_name(env.db_path.name + suffix)
        path.unlink(missing_ok=True)
        path.write_bytes(b"left over from a database that's gone")
    main(["restore", str(saved)])
    assert _tenants(env.db_path) == ["Alpha Ltd", "Bravo Inc"]
    assert not list((env.data_dir / "backups").glob("pre-restore-*"))


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
    with closing(connect(env.db_path)) as conn:
        assert migrate(conn) == every[-1][0]
        assert conn.execute("SELECT name FROM tenants").fetchone()[0] == "Old"


def test_backups_are_compressed_private_and_rotate_with_older_plain_ones(env, db, two_tenants):
    folder = env.data_dir / "backups"
    folder.mkdir(parents=True)
    (folder / "officesentry-20260101-000000.db").write_bytes(b"")  # from before backups were compressed
    main(["backup", "--keep", "1"])
    (saved,) = folder.iterdir()  # the older plain one rotated out
    assert saved.name.endswith(".db.gz")
    assert saved.read_bytes()[:2] == b"\x1f\x8b"
    assert saved.stat().st_size < backups.database_bytes(db) / 3
    if os.name != "nt":
        assert oct(saved.stat().st_mode & 0o777) == "0o600"
    assert not list(folder.glob("*.part"))


def test_a_plain_backup_still_restores(env, db, two_tenants, tmp_path):
    plain = tmp_path / "copies" / "officesentry-20260101-000000.db"
    backups.copy_database(db, plain)
    assert plain.read_bytes()[:6] == b"SQLite"
    main(["check-backup", str(plain)])
    assert [p.name for p in plain.parent.iterdir()] == [plain.name]  # no -shm or -wal left beside it
    db.execute("DELETE FROM tenants WHERE name = 'Alpha Ltd'")
    main(["restore", str(plain)])
    assert _tenants(env.db_path) == ["Alpha Ltd", "Bravo Inc"]
    assert [p.name for p in plain.parent.iterdir()] == [plain.name]


def test_a_restore_onto_a_new_server_forgets_its_setup_link(env, db, tmp_path):
    make_user(db, "admin")
    saved = backups.copy_database(db, tmp_path / "copies" / "officesentry-20260101-000000.db")
    db.execute("DELETE FROM users")  # a new server, waiting for its first admin
    setup_token(env.data_dir)
    main(["restore", str(saved)])
    assert not (env.data_dir / SETUP_TOKEN_FILE).exists()


def test_restore_refuses_a_damaged_compressed_backup(env, db, two_tenants):
    main(["backup"])
    saved = next((env.data_dir / "backups").glob("officesentry-*.db.gz"))
    saved.write_bytes(saved.read_bytes()[:200])  # cut short
    with pytest.raises(SystemExit, match="couldn't be unpacked"):
        main(["restore", str(saved)])
    assert _tenants(env.db_path) == ["Alpha Ltd", "Bravo Inc"]
    assert not list(env.data_dir.glob("restore-*"))


def test_backup_refuses_without_room_for_the_copy(env, db, two_tenants, monkeypatch):
    monkeypatch.setattr(backups.shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(10**9, 10**9, 1000))
    with pytest.raises(SystemExit, match="No backup made: a copy of the database needs about"):
        main(["backup"])
    assert not list((env.data_dir / "backups").glob("*"))


def test_the_pre_upgrade_copy_is_made_before_the_write_lock(cfg, monkeypatch):
    every = db_module.migration_files()
    monkeypatch.setattr(db_module, "migration_files", lambda: every[:-1])
    conn = connect(cfg.db_path)
    migrate(conn)
    other = connect(cfg.db_path)
    seen = []

    def copy(src, dest):
        # Another connection can still write while the copy is made: nothing holds the write lock yet.
        other.execute("INSERT INTO settings (key, value) VALUES ('written_during_copy', '1')")
        seen.append(dest.name)
        return dest

    monkeypatch.setattr(backups, "copy_database", copy)
    monkeypatch.setattr(db_module, "migration_files", lambda: every)
    assert migrate(conn) == every[-1][0]
    assert len(seen) == 1 and seen[0].startswith(f"pre-upgrade-v{every[-2][0]}-to-v{every[-1][0]}-")
    conn.close()
    other.close()


def test_a_second_process_upgrading_at_once_does_not_copy_again(cfg, monkeypatch):
    every = db_module.migration_files()
    monkeypatch.setattr(db_module, "migration_files", lambda: every[:-1])
    conn = connect(cfg.db_path)
    migrate(conn)
    first = backups.before_upgrade(cfg.db_path, every[-2][0], every[-1][0])
    assert first is not None and first.exists()
    assert backups.before_upgrade(cfg.db_path, every[-2][0], every[-1][0]) is None
    conn.close()


def test_a_retry_after_going_back_gets_a_copy_of_its_own(cfg, monkeypatch):
    """An upgrade rolled back and tried again within the hour: the first attempt's copy is missing what was
    written since, so going back with it would lose that."""
    every = db_module.migration_files()
    monkeypatch.setattr(db_module, "migration_files", lambda: every[:-1])
    conn = connect(cfg.db_path)
    migrate(conn)
    monkeypatch.setattr(backups, "_stamp", lambda: "20261102-090000")
    first = backups.before_upgrade(cfg.db_path, every[-2][0], every[-1][0])
    conn.execute("INSERT INTO settings (key, value) VALUES ('written_after_going_back', '1')")
    monkeypatch.setattr(backups, "_stamp", lambda: "20261102-090500")
    second = backups.before_upgrade(cfg.db_path, every[-2][0], every[-1][0])
    assert second is not None and second != first
    with closing(sqlite3.connect(second)) as copy:
        assert copy.execute("SELECT value FROM settings WHERE key = 'written_after_going_back'").fetchone() == ("1",)
    conn.close()


def test_no_copy_once_another_process_has_upgraded(cfg, monkeypatch):
    every = db_module.migration_files()
    conn = connect(cfg.db_path)
    migrate(conn)  # the other process got there first
    assert backups.before_upgrade(cfg.db_path, every[-2][0], every[-1][0]) is None
    assert not list((cfg.data_dir / "backups").glob(f"{backups.PRE_UPGRADE_PREFIX}*"))
    conn.close()


def test_reading_a_copys_version_leaves_nothing_beside_it(db, tmp_path):
    copy = backups.copy_database(db, tmp_path / "copies" / "pre-upgrade-v1-to-v2-20260101-000000.db")
    assert backups.copy_version(copy) == db.execute("PRAGMA user_version").fetchone()[0]
    assert [p.name for p in copy.parent.iterdir()] == [copy.name]
    assert backups.copy_version(copy.parent / "missing.db") is None


def _corrupt(db, path, how):
    """Damage the live database the way a failing disk or a half-copied file does."""
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()
    data = bytearray(path.read_bytes())
    if how == "header":
        data[:100] = b"\0" * 100  # "file is not a database"
    else:
        page = int.from_bytes(data[16:18], "big")
        data[page:] = bytes((i * 7 + 3) % 251 for i in range(len(data) - page))  # every page but the first
    path.write_bytes(bytes(data))
    for suffix in ("-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    check = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            assert check.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        check.close()
    return bytes(data)


@pytest.mark.parametrize("how", ["header", "pages"])
def test_restore_replaces_a_damaged_database(env, db, two_tenants, capsys, how):
    main(["backup"])
    saved = next((env.data_dir / "backups").glob("officesentry-*.db.gz"))
    damaged = _corrupt(db, env.db_path, how)
    capsys.readouterr()

    main(["restore", str(saved)])
    out = capsys.readouterr().out
    assert "Restored" in out and "was damaged" in out
    assert _tenants(env.db_path) == ["Alpha Ltd", "Bravo Inc"]
    with closing(connect(env.db_path)) as restored:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    # The damaged file is kept whole, not copied through SQLite, in case anything can still be saved from it.
    (aside,) = (env.data_dir / "backups").glob("pre-restore-*")
    assert aside.name.endswith("-damaged.db") and aside.name in out
    assert aside.read_bytes() == damaged


def test_rotation_takes_a_moved_aside_database_with_its_wal(tmp_path):
    old = tmp_path / "pre-restore-20260101-000000-damaged.db"
    old.write_bytes(b"damaged")
    old.with_name(old.name + "-wal").write_bytes(b"its last changes")
    for day in range(2, 5):
        (tmp_path / f"pre-restore-2026010{day}-000000.db.gz").write_bytes(b"")
    assert backups.rotate(tmp_path, backups.PRE_RESTORE_PREFIX, backups.KEEP_PRE_RESTORE) == [old]
    assert not list(tmp_path.glob("*damaged*"))


def test_restore_without_room_for_the_current_database_says_so(env, db, two_tenants, monkeypatch):
    main(["backup"])
    saved = next((env.data_dir / "backups").glob("officesentry-*.db.gz"))
    db.execute("DELETE FROM tenants WHERE name = 'Alpha Ltd'")
    monkeypatch.setattr(backups.shutil, "disk_usage", lambda _p: shutil._ntuple_diskusage(10**9, 10**9, 1000))
    with pytest.raises(SystemExit, match="Not restored: There's no room to keep a copy of the current database"):
        main(["restore", str(saved)])
    assert _tenants(env.db_path) == ["Bravo Inc"]  # left as it was


def test_a_backup_whose_activity_history_fails_says_what_was_saved(env, db, two_tenants):
    folder = env.data_dir / "backups"
    folder.mkdir(parents=True)
    for day in range(1, 4):
        (folder / f"officesentry-2026010{day}-000000.db.gz").write_bytes(b"")
    (folder / "events").write_text("a file where the activity history's folder goes", encoding="utf-8")
    with pytest.raises(SystemExit, match=r"The database was copied to officesentry-.*\.db\.gz, but the activity"):
        main(["backup", "--keep", "2"])
    saved = backups.backup_files(folder, backups.BACKUP_PREFIX)
    assert len(saved) == 2 and _tenants(saved[-1]) == ["Alpha Ltd", "Bravo Inc"]  # rotation still happened
    status = backups.read_status(env.data_dir)["local"]
    assert status["ok"] is False and status["error"].startswith("The database was copied to officesentry-")


def test_a_locked_database_is_not_mistaken_for_a_damaged_one(env, db, two_tenants, monkeypatch):
    main(["backup"])
    saved = next((env.data_dir / "backups").glob("officesentry-*.db.gz"))

    def locked(*_args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(backups, "copy_database", locked)
    with pytest.raises(SystemExit, match="Not restored: The current database couldn't be copied first"):
        main(["restore", str(saved), "--force"])
    assert env.db_path.exists() and not list((env.data_dir / "backups").glob("pre-restore-*"))


# -- The worker's nightly backup (nightly_backup.py) -----------------------------------------------


def _tick(cfg, db, now):
    from officesentry import nightly_backup, schedule

    nightly_backup._queue(schedule.TickContext(app=None, conn=db, vault=None, cfg=cfg, now=now))
    return [t["key"] for t in db.execute("SELECT key FROM tasks WHERE kind = 'backup' AND status = 'queued'")]


def test_the_worker_backs_up_every_night_by_itself(cfg, db, two_tenants, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from officesentry import system_health, tasks

    stamps = iter(f"20261015-0330{n:02}" for n in range(60))  # a name each, however fast they're made
    monkeypatch.setattr(backups, "_stamp", lambda: next(stamps))
    cfg.backup_cron, cfg.backup_keep = "30 3 * * *", 2
    night = datetime.now(UTC).replace(hour=3, minute=31, second=0, microsecond=0)
    assert _tick(cfg, db, night) == [f"backup:{night:%Y-%m-%d}T03:30:00Z"]
    assert _tick(cfg, db, night + timedelta(minutes=1)) == [f"backup:{night:%Y-%m-%d}T03:30:00Z"]  # once a night
    assert tasks.drain(None, db, None, cfg) == 1
    saved = list((cfg.data_dir / "backups").glob("officesentry-*.db.gz"))
    assert len(saved) == 1 and _tenants(saved[0]) == ["Alpha Ltd", "Bravo Inc"]
    assert backups.read_status(cfg.data_dir)["local"]["ok"] is True
    assert system_health.check_backup(db, cfg, datetime.now(UTC)).status == "ok"
    # Its own copy from last night doesn't stop tonight's, and it keeps cfg.backup_keep of them.
    for n in range(1, 4):
        os.utime(sorted(saved[0].parent.glob("officesentry-*.db.gz"))[-1], (0, (night - timedelta(days=1)).timestamp()))
        assert len(_tick(cfg, db, night + timedelta(days=n))) == 1
        tasks.drain(None, db, None, cfg)
    assert len(list(saved[0].parent.glob("officesentry-*.db.gz"))) == 2


def test_the_worker_leaves_the_night_to_a_server_that_backs_up_itself(cfg, db, two_tenants, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from officesentry import nightly_backup, tasks

    stamps = iter(f"20261015-0330{n:02}" for n in range(60))  # a name each, however fast they're made
    monkeypatch.setattr(backups, "_stamp", lambda: next(stamps))
    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(cfg.data_dir))
    cfg.backup_cron = "30 3 * * *"
    night = datetime.now(UTC).replace(hour=3, minute=31, second=0, microsecond=0)
    elsewhere = cfg.data_dir.parent / "off-volume"

    def server_backup(at, *args):  # deploy/jobs.sh, a crontab line, the Kubernetes CronJob...
        main(["backup", *args])
        newest = backups.newest_backup(elsewhere if "--dir" in args else cfg.data_dir / "backups")
        os.utime(newest, (0, at.timestamp()))
        backups.record_status(cfg.data_dir, "local", True, file=newest, now=at)

    # A copy in a folder off the data volume counts, from what the backup command recorded.
    server_backup(night - timedelta(hours=23, minutes=45), "--dir", str(elsewhere), "--keep", "14")
    assert _tick(cfg, db, night) == []
    server_backup(night + timedelta(minutes=15), "--dir", str(elsewhere), "--keep", "14")
    assert _tick(cfg, db, night + timedelta(hours=25)) == []  # it came, so nothing to catch up
    # So does one in the backups folder, even when the status file says nothing about it.
    server_backup(night + timedelta(days=1, minutes=14), "--keep", "14")
    (cfg.data_dir / backups.STATUS_FILE).unlink()
    assert _tick(cfg, db, night + timedelta(days=2)) == []
    # The server's job stopped: an hour after it would have run, the worker makes the copy, then every night.
    late = night + timedelta(days=1, minutes=14) + nightly_backup.LATE
    assert _tick(cfg, db, late - timedelta(minutes=1)) == []
    assert _tick(cfg, db, late) == [f"backup:late:{night + timedelta(days=1, minutes=14):%Y-%m-%dT%H:%M:%SZ}"]
    assert tasks.drain(None, db, None, cfg) == 1
    assert len(_tick(cfg, db, night + timedelta(days=3))) == 1


def test_a_copy_made_once_by_hand_never_leaves_a_gap(cfg, db, two_tenants, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from officesentry import nightly_backup, system_health, tasks

    stamps = iter(f"20261015-0330{n:02}" for n in range(60))
    monkeypatch.setattr(backups, "_stamp", lambda: next(stamps))
    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(cfg.data_dir))
    cfg.backup_cron = "30 3 * * *"
    night = datetime.now(UTC).replace(hour=3, minute=31, second=0, microsecond=0)
    assert len(_tick(cfg, db, night)) == 1
    tasks.drain(None, db, None, cfg)
    by_hand = night + timedelta(hours=11)  # before an update, say
    main(["backup"])
    backups.record_status(
        cfg.data_dir, "local", True, file=backups.newest_backup(cfg.data_dir / "backups"), now=by_hand
    )
    os.utime(backups.newest_backup(cfg.data_dir / "backups"), (0, by_hand.timestamp()))
    assert _tick(cfg, db, night + timedelta(days=1)) == []  # it stands in for tonight's...
    assert len(_tick(cfg, db, by_hand + timedelta(hours=25))) == 1  # ...until it's 25 hours old
    tasks.drain(None, db, None, cfg)
    # The task records its copy at the time it really ran, so System status is asked at that time too.
    local = backups.read_status(cfg.data_dir)["local"]
    assert local["ok"] and local["detail"].startswith(nightly_backup._setting(db, nightly_backup.MADE))
    caught_up = datetime.fromisoformat(local["last_success"].replace("Z", "+00:00"))
    assert system_health.check_backup(db, cfg, caught_up + timedelta(minutes=2)).status == "ok"
    assert len(_tick(cfg, db, night + timedelta(days=2))) == 1  # then back to every night


def test_the_nightly_backup_can_be_turned_off_and_says_what_went_wrong(cfg, db, monkeypatch):
    from datetime import UTC, datetime

    from officesentry import system_health, tasks
    from officesentry.config import load_config

    monkeypatch.setenv("OFFICESENTRY_BACKUP_CRON", "off")
    assert load_config(data_dir=cfg.data_dir).backup_cron == ""
    for name, value in (("BACKUP_CRON", "every night"), ("BACKUP_KEEP", "0")):
        monkeypatch.setenv(f"OFFICESENTRY_{name}", value)
        with pytest.raises(ValueError, match=f"OFFICESENTRY_{name}"):
            load_config(data_dir=cfg.data_dir)
        monkeypatch.delenv(f"OFFICESENTRY_{name}")
    assert _tick(cfg, db, datetime.now(UTC)) == []  # off (cfg.backup_cron is "" here)
    assert "OFFICESENTRY_BACKUP_CRON=off" in system_health.check_backup(db, cfg, datetime.now(UTC)).fix

    cfg.backup_cron = "30 3 * * *"
    assert "every night at 03:30 (UTC)" in system_health.check_backup(db, cfg, datetime.now(UTC)).fix

    def full(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(backups, "backup", full)
    assert len(_tick(cfg, db, datetime.now(UTC))) == 1
    tasks.drain(None, db, None, cfg)
    task = db.execute("SELECT status, error FROM tasks WHERE kind = 'backup'").fetchone()
    assert (task["status"], task["error"]) == ("failed", "No backup made: No space left on device.")
    check = system_health.check_backup(db, cfg, datetime.now(UTC))
    assert check.summary == "The last backup failed" and check.fix.startswith("No backup made: No space left")
