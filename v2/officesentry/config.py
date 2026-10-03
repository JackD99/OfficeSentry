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
    collect_cron: str = "0 2 * * *"
    max_age_hours: float = 12
    keep_daily: int = 30
    keep_monthly: int = 12
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
        collect_cron=_collect_cron(),
        max_age_hours=float(_env("MAX_AGE_HOURS", "12")),
        keep_daily=int(_env("KEEP_DAILY", "30")),
        keep_monthly=int(_env("KEEP_MONTHLY", "12")),
        secure_cookies=_env("INSECURE_COOKIES", "") != "1",
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    _check(cfg)
    return cfg


def _collect_cron() -> str:
    if cron := _env("COLLECT_CRON").strip():
        return cron
    # OFFICESENTRY_NIGHTLY_HOUR is the older, simpler setting.
    return f"0 {int(_env('NIGHTLY_HOUR', '2'))} * * *"


def _check(cfg: Config) -> None:
    """Fail at startup with a plain message rather than later in the worker."""
    from .schedule import cron_problem, timezone_problem

    problems = []
    if p := timezone_problem(cfg.timezone):
        problems.append(f"{ENV_PREFIX}TIMEZONE: {p}")
    if p := cron_problem(cfg.collect_cron):
        problems.append(f"{ENV_PREFIX}COLLECT_CRON: {p}")
    if cfg.max_age_hours < 0 or cfg.keep_daily < 1 or cfg.keep_monthly < 0:
        problems.append(f"{ENV_PREFIX}MAX_AGE_HOURS, KEEP_DAILY and KEEP_MONTHLY must be positive")
    if problems:
        raise ValueError("Configuration problem: " + "; ".join(problems))
