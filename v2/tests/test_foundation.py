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
    (bad / "0001_bad.sql").write_text("CREATE TABLE a (id INTEGER);\nCREATE TABLE a (id INTEGER);\n")
    monkeypatch.setattr(dbmod, "MIGRATIONS_DIR", bad)
    conn = dbmod.connect(tmp_path / "x.db")
    with pytest.raises(sqlite3.OperationalError):
        dbmod.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'a'").fetchone() is None


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
