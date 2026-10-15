"""New releases of Office Sentry, and asking the updater to install one (Settings → Updates).

The portal never touches Docker. Installing a release is the updater's job (``updater.py``, the optional
``updater`` service in ``docker-compose.yml``), and the two only talk through two small files in the data
volume:

* ``<data dir>/updates/request.json``, written here when an admin presses Update: an id, the release
  number, who asked and when. The updater re-checks all of it and trusts none of it.
* ``<data dir>/updates/updater.json``, written by the updater: when it last checked in, whether it can
  update this install (and why not), and the progress of the last update it ran.

The worker's ``updates`` periodic job looks at GitHub's list of releases once a day (``check``; an admin
can turn that off or press *Check now*) so the page can say what's new, and records each finished update
in the activity log. Nothing here ever starts an update by itself.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import stat
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

from . import __version__
from .db import audit, transaction
from .formatting import parse as parse_time
from .schedule import periodic

log = logging.getLogger(__name__)

REPOSITORY = "JackD99/OfficeSentry"
IMAGE = "ghcr.io/jackd99/officesentry"
RELEASES_URL = f"https://api.github.com/repos/{REPOSITORY}/releases"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases"
DEPLOY_DOCS = "https://officesentry.co.uk/docs/install/"  # DEPLOY.md on the website

FOLDER = "updates"
REQUEST_FILE = "request.json"
STATUS_FILE = "updater.json"
FORMAT = 1  # of both files; a reader ignores a file of a format it doesn't know
MAX_FILE_BYTES = 256 * 1024
# The updater checks in every 30 seconds; a minute and a half without one means it isn't running.
FRESH = timedelta(seconds=90)
# A request the updater hasn't picked up in this long is refused rather than acted on later.
REQUEST_MAX_AGE = timedelta(minutes=10)

CHECK_KEY = "updates.check"  # "off" turns the daily release check off
RELEASES_KEY = "updates.releases"
CHECKED_KEY = "updates.checked_at"
ERROR_KEY = "updates.check_error"
AUDITED_KEY = "updates.audited_job"
CHECK_EVERY = timedelta(hours=24)
RETRY_EVERY = timedelta(hours=1)
KEEP_RELEASES = 15
SECTION_CHARS = 12_000
TIMEOUT = 10

VERSION = re.compile(r"(\d{1,4})\.(\d{1,4})\.(\d{1,4})")
JOB_ID = re.compile(r"[0-9a-f]{16}")
# The release notes' own sections, in the order the page shows them (CHANGELOG.md, "Action required" ...).
SECTIONS = {
    "action required": "action",
    "new microsoft permissions": "permissions",
    "database upgrade": "database",
    "security": "security",
}
FINISHED = ("done", "rolled_back", "failed", "refused")


@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str | None) -> Version | None:
        """``2.3.0`` (or ``v2.3.0``) as a Version; None for anything else, pre-releases included."""
        match = VERSION.fullmatch((text or "").strip().removeprefix("v"))
        return cls(*(int(p) for p in match.groups())) if match else None

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def running() -> Version | None:
    """The version of this code, or None for a development build (``2.3.0.dev0``), which is never offered
    an update: it isn't a release."""
    return Version.parse(__version__)


# -- Releases ------------------------------------------------------------------------------------------------


@dataclass
class Release:
    version: str
    published_at: str
    url: str
    summary: str = ""
    sections: dict[str, str] = field(default_factory=dict)  # SECTIONS values -> the section's Markdown

    @property
    def parsed(self) -> Version:
        return Version.parse(self.version) or Version(0, 0, 0)

    @property
    def action(self) -> str:
        return self.sections.get("action", "")

    @property
    def asks(self) -> bool:
        """The notes' Action required section asks something of people. Every release fills it in, saying
        "None" when there's nothing to do (CONTRIBUTING.md, "Releases")."""
        text = self.action.strip().lower()
        return bool(text) and not re.match(r"none\b", text)  # "Nonetheless, ..." still asks

    @property
    def permissions(self) -> str:
        return self.sections.get("permissions", "")

    @property
    def database(self) -> str:
        return self.sections.get("database", "")

    @property
    def security(self) -> str:
        return self.sections.get("security", "")

    @property
    def upgrades_database(self) -> bool:
        """The notes' Database upgrade section says yes (it starts "No" when there's none)."""
        text = self.database.strip().lower()
        return bool(text) and not re.match(r"(no|none)\b", text)  # "Note: ..." is still an upgrade


def _cap(text: str) -> str:
    text = text.strip()
    return text if len(text) <= SECTION_CHARS else text[:SECTION_CHARS].rsplit("\n", 1)[0] + "\n\n…"


def parse_notes(body: str) -> tuple[str, dict[str, str]]:
    """A release's notes (its CHANGELOG.md section) as the opening summary and the sections people must read
    before updating; the rest (Added, Changed, Fixed) stays on GitHub."""
    summary_lines: list[str] = []
    sections: dict[str, list[str]] = {}
    current: list[str] | None = summary_lines
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        heading = re.match(r"#{2,4}\s+(.*?)\s*$", line)
        if heading:
            key = SECTIONS.get(heading[1].strip().lower())
            current = sections.setdefault(key, []) if key else None
            continue
        if current is not None:
            current.append(line)
    return _cap("\n".join(summary_lines)), {k: _cap("\n".join(v)) for k, v in sections.items() if "".join(v).strip()}


def from_github(item: dict) -> Release | None:
    """One entry of GitHub's release list, if it's a published release (not a draft or a pre-release)."""
    if not isinstance(item, dict) or item.get("draft") or item.get("prerelease"):
        return None
    version = Version.parse(str(item.get("tag_name") or ""))
    if version is None:
        return None
    summary, sections = parse_notes(str(item.get("body") or ""))
    url = str(item.get("html_url") or "")
    return Release(
        version=str(version),
        published_at=str(item.get("published_at") or ""),
        url=url if url.startswith(f"https://github.com/{REPOSITORY}/") else f"{RELEASES_PAGE}/tag/v{version}",
        summary=summary,
        sections=sections,
    )


class CheckFailed(Exception):
    pass


def fetch(session=None, timeout: float = TIMEOUT) -> list[Release]:
    """Office Sentry's published releases, newest first, from GitHub. Raises CheckFailed with words for
    people."""
    try:
        response = (session or requests).get(
            RELEASES_URL,
            params={"per_page": 30},
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": f"OfficeSentry/{__version__}",
            },
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise CheckFailed(f"GitHub couldn't be reached ({type(exc).__name__}).") from exc
    if response.status_code in (403, 429):
        raise CheckFailed("GitHub's limit on checks from this server's address was reached; it tries again later.")
    if response.status_code != 200:
        raise CheckFailed(f"GitHub answered {response.status_code}.")
    try:
        items = response.json()
    except ValueError as exc:
        raise CheckFailed("GitHub's answer wasn't readable.") from exc
    if not isinstance(items, list):
        raise CheckFailed("GitHub's answer wasn't a list of releases.")
    found = [r for r in (from_github(i) for i in items) if r]
    found.sort(key=lambda r: r.parsed, reverse=True)
    return found[:KEEP_RELEASES]


def _setting(conn, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def _put(conn, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM settings WHERE key = ?", (key,))
    else:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def checking(conn) -> bool:
    """Whether the daily release check is on (it is unless an admin turned it off)."""
    return _setting(conn, CHECK_KEY) != "off"


def set_checking(conn, on: bool) -> None:
    _put(conn, CHECK_KEY, None if on else "off")


def check(conn, now: datetime | None = None, session=None) -> list[Release]:
    """Ask GitHub for the releases now and keep them. Raises CheckFailed (after recording why)."""
    now = now or datetime.now(UTC)
    stamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        found = fetch(session)
    except CheckFailed as exc:
        with transaction(conn):
            _put(conn, CHECKED_KEY, stamp)
            _put(conn, ERROR_KEY, str(exc))
        raise
    with transaction(conn):
        _put(conn, RELEASES_KEY, json.dumps([asdict(r) for r in found]))
        _put(conn, CHECKED_KEY, stamp)
        _put(conn, ERROR_KEY, None)
    return found


def releases(conn) -> list[Release]:
    """The releases from the last check, newest first."""
    try:
        items = json.loads(_setting(conn, RELEASES_KEY) or "[]")
    except ValueError:
        return []
    out = []
    for item in items if isinstance(items, list) else []:
        try:
            release = Release(**item)
        except TypeError:
            continue
        if Version.parse(release.version):
            out.append(release)
    return out


def newer(conn, current: Version | None = None) -> list[Release]:
    """Releases newer than the one running, newest first. None for a development build."""
    current = current or running()
    if current is None:
        return []
    return [r for r in releases(conn) if r.parsed > current]


def available(conn, current: Version | None = None) -> Release | None:
    """The newest release, when it's newer than the one running."""
    found = newer(conn, current)
    return found[0] if found else None


@dataclass
class CheckState:
    on: bool
    checked_at: str | None
    error: str | None


def check_state(conn) -> CheckState:
    return CheckState(checking(conn), _setting(conn, CHECKED_KEY), _setting(conn, ERROR_KEY))


def due(conn, now: datetime) -> bool:
    if not checking(conn):
        return False
    last = parse_time(_setting(conn, CHECKED_KEY))
    if last is None:
        return True
    return now - last >= (RETRY_EVERY if _setting(conn, ERROR_KEY) else CHECK_EVERY)


# -- The request and the updater's status ---------------------------------------------------------------------

NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
NONBLOCK = getattr(os, "O_NONBLOCK", 0)


def folder(data_dir: Path) -> Path:
    return Path(data_dir) / FOLDER


def read_json(path: Path, dir_fd: int | None = None) -> dict | None:
    """A small JSON object from ``path``, or None when it's missing, too big, not JSON, a symlink or not a
    regular file (a named pipe there is never waited on). With ``dir_fd``, ``path`` is a name in that folder."""
    try:
        fd = os.open(path, os.O_RDONLY | NOFOLLOW | NONBLOCK, dir_fd=dir_fd)
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    with os.fdopen(fd, "rb") as f:
        data = f.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        return None
    try:
        value = json.loads(data)
    except (ValueError, RecursionError):  # RecursionError: nested deeper than Python parses
        return None
    return value if isinstance(value, dict) and value.get("format") == FORMAT else None


def write_json(path: Path, value: dict, mode: int = 0o644, dir_fd: int | None = None) -> None:
    """Replace ``path`` with ``value`` in one step (a reader never sees half a file), never following a link
    someone left in its place. With ``dir_fd``, ``path`` is a name in that folder."""
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, mode, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode, dir_fd=dir_fd)
        os.replace(tmp, path, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        raise


@dataclass
class Step:
    key: str
    title: str
    state: str = "pending"  # pending, running, done, failed, skipped
    detail: str = ""


@dataclass
class Job:
    id: str
    target: str
    current: str = ""
    requested_by: str = ""
    state: str = "running"  # running, then one of FINISHED
    message: str = ""
    started_at: str = ""
    finished_at: str = ""
    steps: list[Step] = field(default_factory=list)
    restarted_updater: str = ""  # "", "pending", "done" or "failed"
    # What going back needs if the updater itself restarts part way: the images before and after, and the
    # database's version before the switch.
    image: str = ""
    previous_image: str = ""
    schema: str = ""

    @property
    def finished(self) -> bool:
        return self.state in FINISHED

    @property
    def step(self) -> Step | None:
        """The step under way, or the one that failed."""
        return next((s for s in self.steps if s.state in ("running", "failed")), None)

    @classmethod
    def load(cls, data) -> Job | None:
        if not isinstance(data, dict):
            return None
        try:
            steps = [Step(**s) for s in data.get("steps") or [] if isinstance(s, dict)]
            values = {k: str(v) for k, v in data.items() if k in cls.__dataclass_fields__ and k != "steps"}
            return cls(**values, steps=steps)
        except TypeError:
            return None


@dataclass
class Status:
    """What the updater last said about itself (``updater.json``), as the Updates page reads it."""

    connected: bool = False
    seen_at: str | None = None
    version: str = ""
    ready: bool = False
    problem: str = ""
    install: dict = field(default_factory=dict)
    job: Job | None = None

    @property
    def running_job(self) -> Job | None:
        return self.job if self.job and not self.job.finished else None


def status(data_dir: Path, now: datetime | None = None) -> Status:
    data = read_json(folder(data_dir) / STATUS_FILE)
    if not data:
        return Status()
    now = now or datetime.now(UTC)
    seen = parse_time(data.get("seen_at"))
    install = data.get("install") if isinstance(data.get("install"), dict) else {}
    return Status(
        connected=bool(seen and now - seen <= FRESH),
        seen_at=data.get("seen_at") if seen else None,
        version=str(data.get("version") or ""),
        ready=data.get("ready") is True,
        problem=str(data.get("problem") or ""),
        install={str(k): str(v) for k, v in install.items()},
        job=Job.load(data.get("job")),
    )


@dataclass
class Request:
    id: str
    version: str
    by: str
    at: str


def read_request(data_dir: Path) -> Request | None:
    data = read_json(folder(data_dir) / REQUEST_FILE)
    if not data:
        return None
    request = Request(*(str(data.get(k) or "")[:200] for k in ("id", "version", "by", "at")))
    return request if JOB_ID.fullmatch(request.id) else None


def request_update(data_dir: Path, version: Version, username: str, now: datetime | None = None) -> str:
    """Ask the updater to install ``version``; returns the request's id."""
    place = folder(data_dir)
    place.mkdir(mode=0o700, exist_ok=True)
    job_id = secrets.token_hex(8)
    stamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    write_json(
        place / REQUEST_FILE,
        {"format": FORMAT, "id": job_id, "version": str(version), "by": username, "at": stamp},
    )
    return job_id


def waiting(data_dir: Path, state: Status, now: datetime | None = None) -> Request | None:
    """A request the updater hasn't picked up yet (and still may)."""
    request = read_request(data_dir)
    if not request or (state.job and state.job.id == request.id):
        return None
    at = parse_time(request.at)
    if not at or (now or datetime.now(UTC)) - at > REQUEST_MAX_AGE:
        return None
    return request


# -- The worker's part ----------------------------------------------------------------------------------------

AUDIT_ACTIONS = {
    "done": "update_installed",
    "rolled_back": "update_rolled_back",
    "failed": "update_failed",
    "refused": "update_refused",
}


def record_finished(conn, data_dir: Path) -> bool:
    """Put the updater's last finished update in the activity log, once."""
    job = status(data_dir).job
    if not job or not job.finished or _setting(conn, AUDITED_KEY) == job.id:
        return False
    detail = f"{job.current or '?'} to {job.target}" + (f": {job.message}" if job.message else "")
    with transaction(conn):
        if _setting(conn, AUDITED_KEY) == job.id:
            return False
        audit(conn, job.requested_by or None, AUDIT_ACTIONS[job.state], detail=detail[:500])
        _put(conn, AUDITED_KEY, job.id)
    return True


@periodic("updates")
def _job(ctx) -> None:
    """Registered with the worker through ``worker.JOB_MODULES``: the activity log line for a finished
    update, and the daily look at GitHub's releases."""
    if ctx.cfg.public_demo:
        return
    record_finished(ctx.conn, ctx.cfg.data_dir)
    # Tests never ask GitHub: tests/test_updates.py runs this with GitHub's answers mocked.
    if not ctx.cfg.testing and due(ctx.conn, ctx.now):
        try:
            check(ctx.conn, ctx.now)
        except CheckFailed as exc:
            log.info("Couldn't check for new releases: %s", exc)
