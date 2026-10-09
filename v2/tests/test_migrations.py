"""Guards around database upgrades: numbering, and older code meeting a newer database."""

import re
import sqlite3

import pytest

from officesentry import db as db_module
from officesentry.__main__ import main
from officesentry.db import connect, migrate, schema_version
from officesentry.web import create_app


@pytest.fixture
def env(cfg, monkeypatch):
    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(cfg.data_dir))
    return cfg


def test_migration_files_have_unique_four_digit_numbers():
    # Two threads can each add "the next" migration. If both land, the second is skipped for good on every
    # database the first reached; renumber the newer one above main's highest when merging main in.
    names = sorted(p.name for p in db_module.MIGRATIONS_DIR.glob("*.sql"))
    assert names
    bad = [n for n in names if not re.fullmatch(r"\d{4}_[a-z0-9_]+\.sql", n)]
    assert not bad, f"migration files must be named NNNN_description.sql: {bad}"
    numbers = [n[:4] for n in names]
    clashes = sorted({n for n in names if numbers.count(n[:4]) > 1})
    assert not clashes, f"migrations share a number, renumber the newer one: {clashes}"


def _write(folder, files):
    folder.mkdir()
    for name, sql in files.items():
        (folder / name).write_text(sql, encoding="utf-8")


def test_two_migrations_with_one_number_stop_the_upgrade(cfg, tmp_path, monkeypatch):
    folder = tmp_path / "migrations"
    _write(folder, {"0001_a.sql": "CREATE TABLE a (x);", "0002_b.sql": "CREATE TABLE b (x);",
                    "0002_c.sql": "CREATE TABLE c (x);"})
    monkeypatch.setattr(db_module, "MIGRATIONS_DIR", folder)
    conn = connect(cfg.db_path)
    with pytest.raises(RuntimeError, match=r"share the number 0002 \(0002_b\.sql, 0002_c\.sql\)"):
        migrate(conn)
    # Nothing was applied, rather than 0002_c.sql being skipped without a word.
    assert schema_version(conn) == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall() == []


def _upgraded_then_rolled_back(cfg, monkeypatch):
    """A database upgraded by the newest code, then opened by the release before it."""
    every = db_module.migration_files()
    older = every[:-1]
    monkeypatch.setattr(db_module, "migration_files", lambda: older)
    conn = connect(cfg.db_path)
    migrate(conn)
    conn.execute("INSERT INTO tenants (name, tenant_ref, created_at) VALUES ('Old', 'old.onmicrosoft.com', 'x')")
    monkeypatch.setattr(db_module, "migration_files", lambda: every)
    assert migrate(conn) == every[-1][0]
    monkeypatch.setattr(db_module, "migration_files", lambda: older)
    return conn, older[-1][0], every[-1][0]


def test_older_code_refuses_a_newer_database(cfg, monkeypatch):
    conn, old, new = _upgraded_then_rolled_back(cfg, monkeypatch)
    saved = next((cfg.data_dir / "backups").glob(f"pre-upgrade-v{old}-to-v{new}-*.db"))
    with pytest.raises(db_module.DatabaseTooNew) as caught:
        migrate(conn)
    message = str(caught.value)
    assert f"database version {new}; this version knows up to {old}" in message
    assert f"python -m officesentry restore {saved}" in message
    assert "Anything collected since the upgrade is lost" in message
    assert schema_version(conn) == new
    # The portal and the worker both migrate when they start, so neither runs old code on the new schema.
    with pytest.raises(db_module.DatabaseTooNew):
        create_app(cfg)


def test_the_refusal_points_at_the_backups_folder_when_no_copy_fits(cfg, monkeypatch):
    conn = connect(cfg.db_path)
    migrate(conn)
    latest = schema_version(conn)
    conn.execute(f"PRAGMA user_version = {latest + 1}")
    with pytest.raises(db_module.DatabaseTooNew, match=re.escape(str(cfg.data_dir / "backups"))):
        migrate(conn)

    memory = sqlite3.connect(":memory:")
    memory.execute(f"PRAGMA user_version = {latest + 1}")
    with pytest.raises(db_module.DatabaseTooNew, match="pre-upgrade"):
        migrate(memory)


def test_restoring_the_pre_upgrade_copy_lets_older_code_start(env, monkeypatch, capsys):
    conn, old, new = _upgraded_then_rolled_back(env, monkeypatch)
    conn.close()
    saved = next((env.data_dir / "backups").glob(f"pre-upgrade-v{old}-to-v{new}-*.db"))
    main(["restore", str(saved)])
    assert "Restored" in capsys.readouterr().out
    conn = connect(env.db_path)
    assert migrate(conn) == old
    assert conn.execute("SELECT name FROM tenants").fetchone()[0] == "Old"
