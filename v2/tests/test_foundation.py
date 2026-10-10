import os
import sqlite3
import stat

import pytest
from cryptography import x509
from cryptography.fernet import Fernet, InvalidToken

from officesentry import db as dbmod
from officesentry.certs import cert_der, generate_certificate, key_credential
from officesentry.config import load_config
from officesentry.crypto import Vault


def test_migrations_are_idempotent(tmp_path):
    conn = dbmod.connect(tmp_path / "x.db")
    v = dbmod.migrate(conn)
    assert v == dbmod.migration_files()[-1][0]
    assert dbmod.migrate(conn) == v
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    bad = tmp_path / "migrations"
    bad.mkdir()
    (bad / "0001_bad.sql").write_text("CREATE TABLE a (id INTEGER);\nCREATE TABLE a (id INTEGER);\n", encoding="utf-8")
    monkeypatch.setattr(dbmod, "MIGRATIONS_DIR", bad)
    conn = dbmod.connect(tmp_path / "x.db")
    with pytest.raises(sqlite3.OperationalError):
        dbmod.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'a'").fetchone() is None


def test_a_transaction_that_reads_first_waits_for_other_writers(tmp_path):
    """Collectors read before they write (store.add_objects) while other threads commit: the write must wait
    for its turn, not fail at once with "database is locked"."""
    import threading
    import time

    path = tmp_path / "x.db"
    dbmod.migrate(dbmod.connect(path))
    mine = dbmod.connect(path)

    def other_writer():
        other = dbmod.connect(path)
        other.execute("INSERT INTO settings VALUES ('b', '2')")
        other.close()

    with dbmod.transaction(mine):
        mine.execute("SELECT COUNT(*) FROM settings").fetchone()
        meanwhile = threading.Thread(target=other_writer)
        meanwhile.start()
        time.sleep(0.2)  # the other connection commits now, unless it has to wait for this one
        mine.execute("INSERT INTO settings VALUES ('a', '1')")
    meanwhile.join(timeout=30)
    assert {r[0] for r in mine.execute("SELECT key FROM settings WHERE key IN ('a', 'b')")} == {"a", "b"}


def _migrate_at(path, barrier, results):
    try:
        barrier.wait(timeout=60)
        results.put(("ok", dbmod.migrate(dbmod.connect(path))))
    except Exception as exc:  # reported to the parent
        results.put(("error", f"{type(exc).__name__}: {exc}"))


def _schema(path):
    conn = dbmod.connect(path)
    try:
        return sorted(tuple(r) for r in conn.execute("SELECT type, name, sql FROM sqlite_master"))
    finally:
        conn.close()


def test_concurrent_first_start_migrates_once(tmp_path):
    """Two gunicorn workers start on a fresh volume at the same moment: both must
    come up, and every migration is applied exactly once."""
    import multiprocessing

    fresh = tmp_path / "fresh.db"
    expected = dbmod.migrate(dbmod.connect(fresh))
    ctx = multiprocessing.get_context("spawn")
    for trial in range(3):
        path = tmp_path / f"race{trial}.db"
        barrier, results = ctx.Barrier(2), ctx.Queue()
        procs = [ctx.Process(target=_migrate_at, args=(path, barrier, results)) for _ in range(2)]
        for proc in procs:
            proc.start()
        outcomes = [results.get(timeout=120) for _ in procs]
        for proc in procs:
            proc.join(timeout=60)
        assert outcomes == [("ok", expected), ("ok", expected)]
        assert _schema(path) == _schema(fresh)


def test_migrate_command(tmp_path, monkeypatch, capsys):
    from officesentry.__main__ import main

    for var in ("SECRET_KEY", "ENCRYPTION_KEYS"):
        monkeypatch.delenv("OFFICESENTRY_" + var, raising=False)
    monkeypatch.setenv("OFFICESENTRY_DATA_DIR", str(tmp_path / "data"))
    main(["migrate"])
    assert "upgraded from 0" in capsys.readouterr().out
    main(["migrate"])
    assert capsys.readouterr().out.strip() == f"Database schema is at version {dbmod.latest_version()}."


def test_secrets_file_is_private_and_keys_are_separate(tmp_path, monkeypatch):
    for var in ("SECRET_KEY", "ENCRYPTION_KEYS"):
        monkeypatch.delenv("OFFICESENTRY_" + var, raising=False)
    cfg = load_config(data_dir=tmp_path / "d")
    if os.name != "nt":  # Windows has no POSIX file modes; access is controlled by ACLs
        mode = stat.S_IMODE(os.stat(tmp_path / "d" / "secrets.json").st_mode)
        assert mode == 0o600
    assert cfg.secret_key not in cfg.encryption_keys
    assert load_config(data_dir=tmp_path / "d").secret_key == cfg.secret_key


def test_key_rotation():
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    token = Vault([old]).encrypt("pem")
    rotated = Vault([new, old]).rotate(token)
    assert Vault([new]).decrypt(rotated) == "pem"
    with pytest.raises(InvalidToken):
        Vault([new]).decrypt(token)


def test_certificate_and_key_credential():
    cert = generate_certificate("unit")
    parsed = x509.load_der_x509_certificate(cert_der(cert.cert_pem))
    assert parsed.subject.rfc4514_string() == "CN=unit"
    cred = key_credential(cert.cert_pem)
    # v1 sent "...+00:00Z", which Graph rejects.
    assert cred["endDateTime"].endswith("Z") and "+" not in cred["endDateTime"]
    assert cred["type"] == "AsymmetricX509Cert"
    assert len(cert.thumbprint) == 40
