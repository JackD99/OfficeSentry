"""rotate-keys re-encrypts every encrypted value; nothing is left readable only with the old key."""

import re
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

import officesentry
from officesentry import alerts
from officesentry.__main__ import ENCRYPTED_COLUMNS, ENCRYPTED_SETTINGS, reencrypt_all
from officesentry.crypto import Vault

SOURCE = Path(officesentry.__file__).parent


def test_rotation_covers_the_webhook(db, cfg, vault):
    alerts.save_settings(db, {"alerts.url_enc": vault.encrypt("https://hooks.example.com/x?sig=s3cret"),
                              "alerts.secret_enc": vault.encrypt("signing")})
    new = Fernet.generate_key().decode()
    assert reencrypt_all(db, Vault([new, *cfg.encryption_keys])) == 2  # what rotate-keys --new does...
    only_new = Vault([new])  # ...before it drops the old key
    s = alerts.settings(db)
    assert alerts.masked_url(only_new, s) == "https://hooks.example.com/…"
    assert only_new.decrypt(s["alerts.secret_enc"]) == "signing"


def _schema_enc_columns(db) -> set[tuple[str, str]]:
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
    return {(t, c["name"]) for t in tables for c in db.execute(f"PRAGMA table_info({t})")
            if c["name"].endswith("_enc")}


def test_every_encrypted_value_is_registered_for_rotation(db):
    # Every *_enc column in the schema...
    columns = _schema_enc_columns(db)
    assert columns == {(t, c) for t, _key, c in ENCRYPTED_COLUMNS}
    # ...and every other *_enc name the code uses, which is a key in the settings table.
    names = set()
    for path in SOURCE.rglob("*.py"):
        names |= set(re.findall(r"[\"']([a-z0-9_.]+_enc)[\"']", path.read_text(encoding="utf-8")))
    settings = names - {c for _t, c in columns}
    assert {"alerts.url_enc", "alerts.secret_enc", "smtp_password_enc", "graph_secret_enc"} <= settings
    assert settings == set(ENCRYPTED_SETTINGS)


def test_rotation_refuses_values_it_doesnt_know(db, cfg, vault):
    db.execute("INSERT INTO settings (key, value) VALUES ('someday_token_enc', ?)", (vault.encrypt("x"),))
    smtp = vault.encrypt("pw")
    db.execute("INSERT INTO settings (key, value) VALUES ('smtp_password_enc', ?)", (smtp,))
    with pytest.raises(RuntimeError, match="someday_token_enc"):
        reencrypt_all(db, Vault([Fernet.generate_key().decode(), *cfg.encryption_keys]))
    # Nothing changed: it stops before re-encrypting anything.
    assert db.execute("SELECT value FROM settings WHERE key = 'smtp_password_enc'").fetchone()[0] == smtp


def test_new_keys_are_saved_before_anything_is_reencrypted(db, cfg, vault, monkeypatch):
    """A crash part way through rotate-keys --new leaves every value readable with what's in secrets.json."""
    import json
    from types import SimpleNamespace

    from officesentry import __main__ as cli
    from officesentry.config import write_private

    smtp = vault.encrypt("pw")
    db.execute("INSERT INTO settings (key, value) VALUES ('smtp_password_enc', ?)", (smtp,))
    db.commit()
    write_private(cfg.secrets_path, json.dumps({"secret_key": cfg.secret_key,
                                                "encryption_keys": list(cfg.encryption_keys)}))

    def crash(conn, new_vault):
        reencrypt_all(conn, new_vault)
        raise KeyboardInterrupt  # stopped after re-encrypting, before the final write

    monkeypatch.setattr(cli, "reencrypt_all", crash)
    with pytest.raises(KeyboardInterrupt):
        cli.cmd_rotate_keys(SimpleNamespace(new=True), cfg)
    saved = json.loads(cfg.secrets_path.read_text(encoding="utf-8"))
    assert saved["secret_key"] == cfg.secret_key
    assert saved["encryption_keys"][1:] == list(cfg.encryption_keys)
    stored = db.execute("SELECT value FROM settings WHERE key = 'smtp_password_enc'").fetchone()[0]
    assert stored != smtp and Vault(saved["encryption_keys"]).decrypt(stored) == "pw"
