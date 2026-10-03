"""Runtime configuration.

Everything comes from environment variables. Secrets that are not supplied are
generated once and kept in ``<data_dir>/secrets.json`` with 0600 permissions.

Two secrets are deliberately separate:

* ``secret_key`` signs session cookies.
* ``encryption_keys`` (Fernet keys) encrypt credentials at rest. The first key
  encrypts; every key decrypts, so a new key can be prepended to rotate.

Both can be replaced with ``python -m officesentry rotate-keys --new``.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet

ENV_PREFIX = "OFFICESENTRY_"
SECRETS_FILE = "secrets.json"
SETUP_TOKEN_FILE = "setup-token"  # noqa: S105 (a file name, not a secret)
MFA_POLICIES = ("staff", "all", "off")

log = logging.getLogger(__name__)


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
    # Who must use two-step sign-in: "staff" (admins and analysts), "all", or "off".
    require_mfa: str = "staff"
    # Reverse proxies in front of the portal whose X-Forwarded-* headers are trusted.
    trusted_proxies: int = 0
    session_idle_minutes: int = 120
    session_hours: int = 8
    testing: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "officesentry.db"

    @property
    def secrets_path(self) -> Path:
        return self.data_dir / SECRETS_FILE

    def mfa_required(self, role: str) -> bool:
        if self.require_mfa == "all":
            return True
        return self.require_mfa == "staff" and role in ("admin", "analyst")


def tighten(path: Path) -> None:
    """Make a key file readable by its owner only (no-op where POSIX modes don't apply)."""
    if os.name == "nt" or not path.exists():
        return
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        log.warning("%s was readable by other users; setting it to 0600", path)
        os.chmod(path, 0o600)


def write_private(path: Path, text: str, exclusive: bool = False) -> None:
    """Write a file created as 0600 from the start. Non-exclusive writes replace it atomically."""
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        return
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def new_secrets() -> dict:
    return {
        "secret_key": secrets.token_urlsafe(48),
        "encryption_keys": [Fernet.generate_key().decode()],
    }


def _load_or_create_secrets(data_dir: Path) -> dict:
    path = data_dir / SECRETS_FILE
    try:
        write_private(path, json.dumps(new_secrets()), exclusive=True)
    except FileExistsError:
        tighten(path)
    return json.loads(path.read_text())


# -- First-run setup token ------------------------------------------------------
# Until the first admin exists, /setup needs this token, so whoever happens to
# reach a fresh install first can't take it over. Every web worker shares the file.


def setup_token(data_dir: Path) -> str:
    path = data_dir / SETUP_TOKEN_FILE
    try:
        write_private(path, secrets.token_urlsafe(24), exclusive=True)
    except FileExistsError:
        tighten(path)
    return path.read_text().strip()


def clear_setup_token(data_dir: Path) -> None:
    (data_dir / SETUP_TOKEN_FILE).unlink(missing_ok=True)


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
        require_mfa=_env("REQUIRE_MFA", "staff").strip().lower(),
        trusted_proxies=int(_env("TRUSTED_PROXIES", "0")),
        session_idle_minutes=int(_env("SESSION_IDLE_MINUTES", "120")),
        session_hours=int(_env("SESSION_HOURS", "8")),
    )
    if cfg.require_mfa not in MFA_POLICIES:
        raise ValueError(f"{ENV_PREFIX}REQUIRE_MFA must be one of {', '.join(MFA_POLICIES)}")
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg
