"""Runtime configuration.

Everything comes from environment variables. Secrets that are not supplied are
generated once and kept in ``<data_dir>/secrets.json`` with 0600 permissions.

Two secrets are deliberately separate:

* ``secret_key`` signs session cookies.
* ``encryption_keys`` (Fernet keys) encrypt credentials at rest. The first key
  encrypts; every key decrypts, so a new key can be prepended to rotate.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet

ENV_PREFIX = "OFFICESENTRY_"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(ENV_PREFIX + name, default)


@dataclass
class Config:
    data_dir: Path
    secret_key: str
    encryption_keys: list[str]
    base_url: str = "http://localhost:8000"
    timezone: str = "UTC"
    nightly_hour: int = 2
    retention_days: int = 730
    secure_cookies: bool = True
    testing: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "officesentry.db"


def _load_or_create_secrets(data_dir: Path) -> dict:
    path = data_dir / "secrets.json"
    if path.exists():
        return json.loads(path.read_text())
    values = {
        "secret_key": secrets.token_urlsafe(48),
        "encryption_keys": [Fernet.generate_key().decode()],
    }
    # Create with restrictive permissions from the start.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(values, fh)
    return values


def load_config(**overrides) -> Config:
    data_dir = Path(overrides.pop("data_dir", None) or _env("DATA_DIR", "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)

    secret_key = _env("SECRET_KEY")
    keys = [k.strip() for k in _env("ENCRYPTION_KEYS").split(",") if k.strip()]
    if not secret_key or not keys:
        stored = _load_or_create_secrets(data_dir)
        secret_key = secret_key or stored["secret_key"]
        keys = keys or stored["encryption_keys"]

    cfg = Config(
        data_dir=data_dir,
        secret_key=secret_key,
        encryption_keys=keys,
        base_url=_env("BASE_URL", "http://localhost:8000").rstrip("/"),
        timezone=_env("TIMEZONE", "UTC"),
        nightly_hour=int(_env("NIGHTLY_HOUR", "2")),
        retention_days=int(_env("RETENTION_DAYS", "730")),
        secure_cookies=_env("INSECURE_COOKIES", "") != "1",
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg
