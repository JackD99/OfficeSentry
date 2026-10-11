"""Runtime configuration.

Everything comes from environment variables. Secrets that are not supplied are
generated once and kept in ``secrets.json`` with 0600 permissions: at
``OFFICESENTRY_SECRETS_FILE`` when it's set (the Docker setup keeps it in its own
volume, ``/keys``, so the data volume and its backups never hold the key that
decrypts them), else in the data folder. A ``secrets.json`` left in the data
folder moves to ``OFFICESENTRY_SECRETS_FILE`` on the next start.

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
    collect_cron: str = "0 2 * * *"
    max_age_hours: float = 12
    keep_daily: int = 30
    keep_monthly: int = 12
    # Collector runs the worker executes at once, each for a different tenant.
    worker_concurrency: int = 4
    secure_cookies: bool = True
    # Who must use two-step sign-in: "staff" (admins and analysts), "all", or "off".
    # Without OFFICESENTRY_REQUIRE_MFA, the database's own default applies (mfa_policy).
    require_mfa: str = "staff"
    require_mfa_explicit: bool = False
    # Reverse proxies in front of the portal whose X-Forwarded-* headers are trusted.
    trusted_proxies: int = 0
    session_idle_minutes: int = 120
    session_hours: int = 8
    # The public demo site (web/public_demo.py): every visitor is the read-only demo user. Only takes effect on a
    # database made by ``scripts/demo_data.py --public``. The link is where its banner sends people.
    public_demo: bool = False
    public_demo_link: str = ""
    testing: bool = False
    extra: dict = field(default_factory=dict)
    # Where secrets.json is kept, when not in the data folder (OFFICESENTRY_SECRETS_FILE).
    secrets_file: Path | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "officesentry.db"

    @property
    def secrets_path(self) -> Path:
        return self.secrets_file or self.data_dir / SECRETS_FILE

    def mfa_policy(self, conn=None) -> str:
        """The two-step policy in force: OFFICESENTRY_REQUIRE_MFA when it is set, else the install's
        default from the database ("all" for installs set up from 2026-10, see migration 0012),
        else "staff"."""
        if self.require_mfa_explicit or conn is None:
            return self.require_mfa
        row = conn.execute("SELECT value FROM settings WHERE key = 'require_mfa'").fetchone()
        return row[0] if row and row[0] in MFA_POLICIES else self.require_mfa

    def mfa_required(self, role: str, conn=None) -> bool:
        policy = self.mfa_policy(conn)
        if policy == "all":
            return True
        return policy == "staff" and role in ("admin", "analyst")


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
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        return
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def new_secrets() -> dict:
    return {
        "secret_key": secrets.token_urlsafe(48),
        "encryption_keys": [Fernet.generate_key().decode()],
    }


def _secrets_file() -> Path | None:
    return Path(path).resolve() if (path := _env("SECRETS_FILE").strip()) else None


def move_secrets(old: Path, new: Path) -> None:
    """Move a ``secrets.json`` left in the data folder to its own place (``OFFICESENTRY_SECRETS_FILE``).

    The copy is written and flushed before the old file goes, so a crash in between leaves two identical
    files, which the next start tidies. Two *different* files stop the start: only a person can tell which
    one decrypts the stored certificates.
    """
    if old == new or not old.exists():
        return
    try:
        new.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            write_private(new, old.read_text(encoding="utf-8"), exclusive=True)
        except FileExistsError:
            if new.read_bytes() != old.read_bytes():
                raise ValueError(
                    f"Configuration problem: there are two different key files, {old} and {new}. Office Sentry "
                    f"won't guess which one decrypts your data. Keep the right one at {new} and delete {old} "
                    '(DEPLOY.md, "Where the keys are").'
                ) from None
        old.unlink()
    except OSError as exc:
        raise ValueError(
            f"Configuration problem: couldn't move {old} to {new}: {exc.strerror or exc}. "
            f"The folder {new.parent} must be writable by the user Office Sentry runs as "
            "(in Docker, the officesentry-keys volume)."
        ) from exc
    log.warning("Moved the key file %s to %s, out of the data folder", old, new)


def _load_or_create_secrets(path: Path) -> dict:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            write_private(path, json.dumps(new_secrets()), exclusive=True)
        except FileExistsError:
            tighten(path)
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(
            f"Configuration problem: couldn't create the key file {path}: {exc.strerror or exc}. "
            f"The folder {path.parent} must be writable by the user Office Sentry runs as "
            "(in Docker, the officesentry-keys volume)."
        ) from exc


# -- First-run setup token ------------------------------------------------------
# Until the first admin exists, /setup needs this token, so whoever happens to
# reach a fresh install first can't take it over. Every web worker shares the file.


def setup_token(data_dir: Path) -> str:
    path = data_dir / SETUP_TOKEN_FILE
    try:
        write_private(path, secrets.token_urlsafe(24), exclusive=True)
    except FileExistsError:
        tighten(path)
    return path.read_text(encoding="utf-8").strip()


def clear_setup_token(data_dir: Path) -> None:
    (data_dir / SETUP_TOKEN_FILE).unlink(missing_ok=True)


def load_config(**overrides) -> Config:
    data_dir = Path(overrides.pop("data_dir", None) or _env("DATA_DIR", "data")).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(data_dir, 0o700)

    secret_key = _env("SECRET_KEY")
    keys = [k.strip() for k in _env("ENCRYPTION_KEYS").split(",") if k.strip()]
    secrets_file = Path(f).resolve() if (f := overrides.pop("secrets_file", None)) else _secrets_file()
    if not secret_key or not keys:
        if secrets_file:
            move_secrets(data_dir / SECRETS_FILE, secrets_file)
        stored = _load_or_create_secrets(secrets_file or data_dir / SECRETS_FILE)
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
        worker_concurrency=int(_env("WORKER_CONCURRENCY", "4")),
        secure_cookies=_env("INSECURE_COOKIES", "") != "1",
        require_mfa=_env("REQUIRE_MFA", "staff").strip().lower(),
        require_mfa_explicit=bool(_env("REQUIRE_MFA").strip()) or "require_mfa" in overrides,
        trusted_proxies=int(_env("TRUSTED_PROXIES", "0")),
        session_idle_minutes=int(_env("SESSION_IDLE_MINUTES", "120")),
        session_hours=int(_env("SESSION_HOURS", "8")),
        public_demo=_env("PUBLIC_DEMO", "") == "1",
        public_demo_link=_env("PUBLIC_DEMO_LINK", "").strip(),
        secrets_file=secrets_file,
    )
    if cfg.require_mfa not in MFA_POLICIES:
        raise ValueError(f"{ENV_PREFIX}REQUIRE_MFA must be one of {', '.join(MFA_POLICIES)}")
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
    if not 1 <= cfg.worker_concurrency <= 32:
        problems.append(f"{ENV_PREFIX}WORKER_CONCURRENCY must be between 1 and 32")
    # 0 here would sign everyone out on their next request, with nothing in the log to say why.
    if cfg.session_idle_minutes < 1 or cfg.session_hours < 1:
        problems.append(f"{ENV_PREFIX}SESSION_IDLE_MINUTES and {ENV_PREFIX}SESSION_HOURS must be at least 1")
    if cfg.trusted_proxies < 0:
        problems.append(f"{ENV_PREFIX}TRUSTED_PROXIES must be 0 or the number of proxies in front of the portal")
    if problems:
        raise ValueError("Configuration problem: " + "; ".join(problems))
