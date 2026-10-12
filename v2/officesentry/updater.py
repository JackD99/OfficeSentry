"""The updater: the one part of Office Sentry that can restart it, for the Update button (Settings → Updates).

It runs as its own container, the ``updater`` service in ``docker-compose.yml`` (off until an admin turns it
on), from the same signed image as the portal, and it is the only container given the Docker socket and the
folder with ``docker-compose.yml``. The portal never gets either: it asks for a release through
``<data dir>/updates/request.json`` and reads this module's progress from ``updater.json`` (``updates.py``).
So the most a compromised portal can do is ask for a newer, genuine, signed release.

Installing release X.Y.Z, each step shown on the Updates page:

1. **check**: X.Y.Z is a published GitHub release of this repository, newer than the one running, with the
   same major version, its ``docker-compose.yml`` and ``Caddyfile`` are the ones in the install's folder
   (the button only changes the image), and there's room for the copy of the database an upgrade makes first.
2. **verify**: the image digest GHCR gives for the tag has build provenance signed by this repository's own
   release workflow, naming the commit the release tag points at (cosign, as a helper container pinned by
   digest, so the image gains no tools).
3. **download**: pull that digest. Docker checks every layer against it.
4. **switch**: the ``OFFICESENTRY_VERSION`` line in ``.env`` becomes ``X.Y.Z@<digest>`` (the file as it was
   is kept as ``.env.before-update``), then ``docker compose up -d web worker`` in a pinned Docker CLI helper:
   the manual update's own steps. The portal is down until the new one passes its health check; when the
   database changes it first copies it to ``backups/pre-upgrade-*`` (``db.migrate``), as it always does.
5. **health**: the portal and the worker report healthy on exactly the pulled image.
6. **finish**: the updater restarts itself on the new version.

When 4 or 5 fails it goes back by itself: stop both, put ``.env`` back, restore the pre-upgrade copy with
the old version's ``restore`` command when the database was upgraded, start the old version and wait for
it to be healthy. An update cut off part way (the server restarted) is finished or rolled back the same way
when the updater starts again. Nothing here runs unless an admin asked, and a request older than ten
minutes is refused rather than acted on late.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import signal
import socket
import sqlite3
import time
from base64 import b64decode
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests

from . import __version__, backups, updates
from .docker_api import Docker, DockerError
from .formatting import parse as parse_time
from .updates import IMAGE, REPOSITORY, Job, Step, Version

log = logging.getLogger(__name__)

PROJECT = Path("/project")  # where docker-compose.yml's folder is mounted
SOCKET = "/var/run/docker.sock"  # where the Docker socket is mounted
STATE = Path("/state")  # the updater's own volume, out of the portal's reach: its record of the last update
# The helpers, pinned by digest (CONTRIBUTING.md, "Dependencies"). Each release brings its own.
COMPOSE_IMAGE = "docker:29.9.0-cli@sha256:1a4c7cb63513f349bdad01fcc6e0f3f2f67d37b9da86f14dc0d4a0942eecda00"
COSIGN_IMAGE = (
    "ghcr.io/sigstore/cosign/cosign:v3.0.2@sha256:b29487e48205d875c324c79583e2806d9d269c0fa299e0861bbec023d8430c8b"
)
# Who must have signed the image's build provenance: this repository's release workflow, run from main or
# from a release tag, with GitHub's own sign-in for workflows.
OIDC_ISSUER = "https://token.actions.githubusercontent.com"
SIGNER = (
    r"^https://github\.com/"
    + re.escape(REPOSITORY)
    + r"/\.github/workflows/release\.yml@refs/(heads/main|tags/v[0-9]+\.[0-9]+\.[0-9]+)$"
)
PROVENANCE = "https://slsa.dev/provenance/v1"
GITHUB_API = f"https://api.github.com/repos/{REPOSITORY}"
REGISTRY = "https://ghcr.io"
INDEX_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)
# The files a release comes with besides the image (release.yml attaches them), where the repository keeps
# them. The button only changes the image, so it only installs a release whose files are the ones here.
RELEASE_FILES = {"docker-compose.yml": "v2/docker-compose.yml", "Caddyfile": "v2/Caddyfile"}
# release.yml writes the release into the compose file's default version, so that alone isn't a change.
DEFAULT_VERSION = re.compile(r"\$\{OFFICESENTRY_VERSION:-[^}]*\}")
PROXY_VARIABLES = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")
LABEL = "com.docker.compose."
VERSION_LABEL = "org.opencontainers.image.version"
ENV_KEPT = ".before-update"
DATABASE = "officesentry.db"
# Escape sequences (colours, cursor moves, window titles) and control characters other than line ends.
TERMINAL_CODES = re.compile(
    r"\x1b(\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(\x07|\x1b\\)?|[@-_])|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]"
)

POLL_SECONDS = 3
HEARTBEAT = timedelta(seconds=30)
COMPOSE_TIMEOUT = 20 * 60  # the portal's health check allows 15 minutes for a database upgrade
STOP_TIMEOUT = 5 * 60  # the worker gets 2 minutes to finish a collection
VERIFY_TIMEOUT = 5 * 60
PULL_TIMEOUT = 30 * 60
HEALTHY_WITHIN = timedelta(minutes=6)  # once compose is done: the worker's first health check is a minute in
SETTLE = timedelta(seconds=30)  # a container without a health check counts as healthy once up this long
SELF_RESTART_WAIT = timedelta(minutes=2)
REQUEST_TIMEOUT = 20

STEPS = (
    ("check", "Check the release"),
    ("verify", "Verify its signature"),
    ("download", "Download it"),
    ("switch", "Restart on the new version"),
    ("health", "Check it's working"),
    ("finish", "Restart the updater"),
)
NAMES = {"web": "portal", "worker": "worker"}


class Refused(Exception):
    """The update stopped before anything changed; the message says why, for people."""


class Broken(Exception):
    """Something went wrong once the switch had started, so the update is rolled back."""


@dataclass
class Install:
    """The install this updater belongs to, read from Docker: compose's labels on the updater's own container
    and on the portal's."""

    project: str
    working_dir: str  # on the server
    config_files: list[str]  # on the server
    env_file: str  # on the server
    env_path: Path  # the same file, as this container sees it
    socket: str  # the Docker socket's path on the server
    image: str  # the portal's image, as docker-compose.yml names it
    image_id: str  # the image the portal runs
    current: Version | None

    def summary(self) -> dict:
        return {"project": self.project, "folder": self.working_dir, "image": self.image}


def _under(path: str, folder: str) -> bool:
    return os.path.commonpath([os.path.normpath(path), os.path.normpath(folder)]) == os.path.normpath(folder)


def service(docker: Docker, project: str, name: str) -> dict | None:
    """The newest container of a compose service, running ones first."""
    found = docker.containers({f"{LABEL}project": project, f"{LABEL}service": name, f"{LABEL}oneoff": "False"})
    found.sort(key=lambda c: (c.get("State") == "running", c.get("Created", 0)), reverse=True)
    return docker.container(found[0]["Id"]) if found else None


def discover(docker: Docker, project_dir: Path = PROJECT, hostname: str | None = None) -> Install:
    """What the updater needs to know about the install, or Refused saying what to fix."""
    if not docker.ping():
        raise Refused(
            "The updater can't reach Docker: its service in docker-compose.yml mounts the Docker socket "
            "(/var/run/docker.sock, or OFFICESENTRY_DOCKER_SOCKET in .env when it's elsewhere)."
        )
    me = docker.container(hostname or socket.gethostname()) or {}
    labels = me.get("Config", {}).get("Labels") or {}
    project, working_dir = labels.get(f"{LABEL}project"), labels.get(f"{LABEL}project.working_dir")
    if not project or not working_dir:
        raise Refused(
            "The updater wasn't started by docker compose, so it can't tell which install it belongs to. "
            "Start it with docker compose in the folder with docker-compose.yml."
        )
    mounts = {m.get("Destination"): m.get("Source") for m in me.get("Mounts") or []}
    sock = mounts.get(SOCKET)
    if not sock:
        raise Refused("The updater's container doesn't have the Docker socket mounted (see docker-compose.yml).")
    if mounts.get(str(project_dir)) != working_dir:
        raise Refused(
            f"The updater can't see the folder with docker-compose.yml: its service mounts it as {project_dir} (\".:"
            f'{project_dir}" in docker-compose.yml).'
        )
    files = [f for f in labels.get(f"{LABEL}project.config_files", "").split(",") if f]
    if not files or not all(_under(f, working_dir) for f in files):
        raise Refused("Keep docker-compose.yml, and any override file, in one folder, as DEPLOY.md does.")
    env_path = project_dir / ".env"
    if env_path.is_symlink() or not env_path.is_file():
        raise Refused(f"The updater can't find .env next to docker-compose.yml in {working_dir}.")
    web = service(docker, project, "web")
    if not web:
        raise Refused("The updater can't find the portal's container. Start Office Sentry with docker compose up -d.")
    image = str(web.get("Config", {}).get("Image") or "")
    if not image.startswith((f"{IMAGE}:", f"{IMAGE}@")):
        raise Refused(
            "This server builds Office Sentry from its source (docker-compose.build.yml) or runs another image, "
            "so the Update button can't install releases here. Update the way you set it up (DEPLOY.md, "
            '"Updating").'
        )
    return Install(
        project=project,
        working_dir=working_dir,
        config_files=files,
        env_file=os.path.join(working_dir, ".env"),
        env_path=env_path,
        socket=sock,
        image=image,
        image_id=str(web.get("Image") or ""),
        current=Version.parse((web.get("Config", {}).get("Labels") or {}).get(VERSION_LABEL)) or tag_version(image),
    )


def tag_version(image: str) -> Version | None:
    """The release an image name names exactly (``...officesentry:2.2.0``, with or without ``@sha256:...``)."""
    name = image.split("@", 1)[0].rsplit("/", 1)[-1]
    return Version.parse(name.split(":", 1)[1]) if ":" in name else None


# -- .env ----------------------------------------------------------------------------------------------------

ASSIGNMENT = re.compile(r"^(\s*(?:export\s+)?)OFFICESENTRY_VERSION\s*=(.*)$")


def set_version(text: str, value: str) -> str:
    """``.env`` with ``OFFICESENTRY_VERSION`` set to ``value``: the last line that sets it (the one compose
    uses) changes and nothing else does, line endings included; without one, a line is added."""
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    for i in range(len(lines) - 1, -1, -1):
        match = ASSIGNMENT.match(lines[i])
        if match:
            lines[i] = f"{match[1]}OFFICESENTRY_VERSION={value}"
            return newline.join(lines)
    if text and not text.endswith(newline):
        text += newline
    return f"{text}# Set by the Update button (Settings > Updates).{newline}OFFICESENTRY_VERSION={value}{newline}"


def rewrite(path: Path, data: bytes) -> None:
    """Write ``data`` into the file in place, so it keeps its owner and permissions: it can hold secrets, and
    the admin who made it must still be able to edit it."""
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC | updates.NOFOLLOW)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def keep_copy(path: Path) -> Path:
    """``.env.before-update``: the file as it was, with its owner and permissions. It replaces the last
    update's copy in one step, so there's always a whole copy."""
    copy = path.with_name(path.name + ENV_KEPT)
    st = os.stat(path, follow_symlinks=False)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | updates.NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(path.read_bytes())
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except OSError:  # not root (tests): the copy is ours, like the file
            pass
        os.chmod(tmp, st.st_mode & 0o777)
        os.replace(tmp, copy)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return copy


# -- The release ---------------------------------------------------------------------------------------------


@dataclass
class Checked:
    """A release that passed every check, and what was checked."""

    version: Version
    digest: str  # the multi-platform image's
    commit: str

    @property
    def reference(self) -> str:
        return f"{IMAGE}@{self.digest}"

    @property
    def pin(self) -> str:
        """What ``OFFICESENTRY_VERSION`` becomes: the version people read, and the digest Docker uses."""
        return f"{self.version}@{self.digest}"


class GitHub:
    """The public, read-only calls the checks make: GitHub's release and tag, and GHCR's manifest."""

    def __init__(self, session=None):
        self.session = session or requests.Session()

    def _get(self, url: str, headers: dict | None = None, **kwargs):
        headers = {"User-Agent": f"OfficeSentry-updater/{__version__}", **(headers or {})}
        try:
            return self.session.get(url, timeout=REQUEST_TIMEOUT, headers=headers, **kwargs)
        except requests.RequestException as exc:
            raise Refused(f"The updater couldn't reach {url.split('/')[2]} ({type(exc).__name__}).") from exc

    def _github(self, path: str):
        response = self._get(f"{GITHUB_API}{path}", {"Accept": "application/vnd.github+json"})
        try:
            data = response.json() if response.status_code == 200 else None
        except ValueError:
            data = None
        return response.status_code, data if isinstance(data, dict) else None

    def release(self, version: Version) -> dict:
        code, data = self._github(f"/releases/tags/v{version}")
        if code == 404:
            raise Refused(f"There's no published release {version} of Office Sentry on GitHub.")
        if data is None:
            raise Refused(f"GitHub answered {code} when asked about release {version}; try again later.")
        if data.get("draft") or data.get("prerelease"):
            raise Refused(f"{version} is a draft or a pre-release, which the Update button doesn't install.")
        return data

    def tag_commit(self, version: Version) -> str:
        """The commit the release's tag points at (through an annotated tag, when it's one)."""
        _, data = self._github(f"/git/ref/tags/v{version}")
        target = (data or {}).get("object") or {}
        for _ in range(3):
            if target.get("type") == "commit" and re.fullmatch(r"[0-9a-f]{40}", str(target.get("sha"))):
                return target["sha"]
            if target.get("type") != "tag" or not re.fullmatch(r"[0-9a-f]{40}", str(target.get("sha"))):
                break
            _, data = self._github(f"/git/tags/{target['sha']}")
            target = (data or {}).get("object") or {}
        raise Refused(f"GitHub didn't say which commit release {version} was built from.")

    def file(self, path: str, commit: str) -> bytes | None:
        """A file of the repository as it is at ``commit``, or None when there's none."""
        response = self._get(
            f"{GITHUB_API}/contents/{path}", {"Accept": "application/vnd.github.raw+json"}, params={"ref": commit}
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise Refused(f"GitHub answered {response.status_code} when asked for {path}; try again later.")
        return response.content

    def digest(self, version: Version) -> str:
        """The digest of the multi-platform image GHCR holds under the release's tag, worked out from the
        manifest itself rather than taken on trust."""
        path = IMAGE.removeprefix("ghcr.io/")
        token = self._get(f"{REGISTRY}/token", params={"service": "ghcr.io", "scope": f"repository:{path}:pull"})
        try:
            bearer = token.json().get("token") if token.status_code == 200 else None
        except ValueError:
            bearer = None
        if not bearer:
            raise Refused("GHCR, where the release images are, didn't answer; try again later.")
        response = self._get(
            f"{REGISTRY}/v2/{path}/manifests/{version}",
            {"Authorization": f"Bearer {bearer}", "Accept": ", ".join(INDEX_TYPES)},
        )
        if response.status_code == 404:
            raise Refused(f"There's no image for {version} on GHCR.")
        if response.status_code != 200:
            raise Refused(f"GHCR answered {response.status_code} about {version}; try again later.")
        digest = "sha256:" + hashlib.sha256(response.content).hexdigest()
        stated = response.headers.get("Docker-Content-Digest")
        if stated and stated != digest:
            raise Refused(f"GHCR's image for {version} doesn't match its own digest.")
        if (response.headers.get("Content-Type") or "").split(";")[0].strip() not in INDEX_TYPES:
            raise Refused(f"GHCR's image for {version} isn't the multi-platform image a release publishes.")
        return digest


def comparable(data: bytes) -> str:
    """A compose file or Caddyfile as it's compared: line endings and the default version don't count."""
    text = data.decode("utf-8", "replace").replace("\r\n", "\n")
    return DEFAULT_VERSION.sub("${OFFICESENTRY_VERSION}", text).strip()


def provenance(output: str) -> list[dict]:
    """The in-toto statements in cosign's output: each verified one is a JSON envelope on a line of its own."""
    statements = []
    for line in output.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            statement = json.loads(b64decode(json.loads(line)["payload"]))
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(statement, dict):
            statements.append(statement)
    return statements


def matches(statement: dict, digest: str, commit: str) -> bool:
    """A statement that this image (``digest``) was built from ``commit``."""
    if statement.get("predicateType") != PROVENANCE:
        return False
    subjects = [s for s in statement.get("subject") or [] if isinstance(s, dict)]
    if not any(
        s.get("name") == IMAGE and f"sha256:{(s.get('digest') or {}).get('sha256')}" == digest for s in subjects
    ):
        return False
    build = (statement.get("predicate") or {}).get("buildDefinition") or {}
    sources = [d for d in build.get("resolvedDependencies") or [] if isinstance(d, dict)]
    return any((d.get("digest") or {}).get("gitCommit") == commit for d in sources)


# -- Docker's view of the install ------------------------------------------------------------------------------


def health(container: dict, now: datetime) -> str:
    """healthy, unhealthy or starting, from Docker's own health check (or, without one, from how long the
    container has been up)."""
    state = container.get("State") or {}
    if state.get("Restarting") or state.get("Status") in ("exited", "dead"):
        return "unhealthy"
    if not state.get("Running"):
        return "starting"
    checked = (state.get("Health") or {}).get("Status")
    if checked:
        return checked
    started = parse_time(state.get("StartedAt"))
    return "healthy" if started and now - started >= SETTLE else "starting"


def database_version(path: Path) -> int | None:
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        try:
            return int(conn.execute("PRAGMA user_version").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def pre_upgrade_copy(data_dir: Path, schema: int, since: datetime | None) -> Path | None:
    """The copy ``db.migrate`` made of the database (at version ``schema``) before this update upgraded it:
    the newest made since the update started, or in the hour before (``backups.before_upgrade`` reuses a
    copy that recent only when the database hasn't changed since)."""
    earliest = (since or datetime.now(UTC)) - timedelta(hours=1)
    found = [
        p
        for p in (Path(data_dir) / "backups").glob(f"{backups.PRE_UPGRADE_PREFIX}v{schema}-to-v*.db")
        if p.is_file() and datetime.fromtimestamp(p.stat().st_mtime, UTC) >= earliest
    ]
    for copy in sorted(found, key=lambda p: p.stat().st_mtime, reverse=True):
        if backups.copy_version(copy) == schema:
            return copy
    return None


def last_line(output: str) -> str:
    """The last thing a helper said, without the terminal codes Docker's CLI writes to its terminal."""
    output = TERMINAL_CODES.sub("", output)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1][:300] if lines else "no output"


def sentence(text: str) -> str:
    """``text`` ending in a full stop, so another sentence can follow it."""
    text = text.strip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# -- The updater -----------------------------------------------------------------------------------------------


class Updater:
    def __init__(
        self,
        docker: Docker,
        data_dir: Path,
        project_dir: Path = PROJECT,
        github: GitHub | None = None,
        clock=lambda: datetime.now(UTC),
        sleep=time.sleep,
        hostname: str | None = None,
        state_dir: Path = STATE,
    ):
        self.docker = docker
        self.data_dir = Path(data_dir)
        self.project_dir = Path(project_dir)
        self.github = github or GitHub()
        self.clock = clock
        self.sleep = sleep
        self.hostname = hostname
        self.state_dir = Path(state_dir)
        self.folder = updates.folder(self.data_dir)
        self.job: Job | None = None
        self.ready = False
        self.problem = ""
        self.install: Install | None = None
        self.last_seen: datetime | None = None
        self.handled: set[str] = set()

    # -- The status file -----------------------------------------------------------------------------------------

    def prepare(self) -> None:
        """The shared folder, owned like the data folder so the portal can write its request into it, and the
        last update, if there was one. That comes from the updater's own volume: the copy in the data folder
        is for the portal to show, and the portal could have changed it."""
        if not self.folder.exists():
            self.folder.mkdir(mode=0o700)
            st = self.data_dir.stat()
            try:
                os.chown(self.folder, st.st_uid, st.st_gid)
            except OSError:  # not root (tests)
                pass
        if self.folder.is_symlink() or not self.folder.is_dir():
            raise SystemExit(f"{self.folder} isn't a folder; the updater won't write through it.")
        if self.state_dir.is_dir():
            self.job = Job.load((updates.read_json(self.state_dir / updates.STATUS_FILE) or {}).get("job"))
        if self.job:
            self.handled.add(self.job.id)

    def save(self) -> None:
        self.last_seen = self.clock()
        status = {
            "format": updates.FORMAT,
            "seen_at": _stamp(self.last_seen),
            "version": __version__,
            "ready": self.ready,
            "problem": self.problem,
            "install": self.install.summary() if self.install else {},
            "job": asdict(self.job) if self.job else None,
        }
        if self.state_dir.is_dir():
            updates.write_json(self.state_dir / updates.STATUS_FILE, status, mode=0o600)
        updates.write_json(self.folder / updates.STATUS_FILE, status)

    def refresh(self) -> None:
        """Look at the install again (its containers change with every update) and check in."""
        try:
            if not self.state_dir.is_dir():
                raise Refused(f"The updater's service needs its own volume on {self.state_dir} (docker-compose.yml).")
            self.install = discover(self.docker, self.project_dir, self.hostname)
            self.ready, self.problem = True, ""
        except Refused as exc:
            self.install, self.ready, self.problem = None, False, str(exc)
        except (OSError, DockerError) as exc:
            self.install, self.ready, self.problem = None, False, f"The updater can't read Docker's state: {exc}"
        self.save()

    def _step(self, key: str) -> Step:
        return next(s for s in self.job.steps if s.key == key)

    def begin(self, key: str, detail: str = "") -> None:
        step = self._step(key)
        step.state, step.detail = "running", detail
        self.save()

    def end(self, key: str, detail: str, state: str = "done") -> None:
        step = self._step(key)
        step.state, step.detail = state, detail
        self.save()

    def finish(self, state: str, message: str) -> None:
        """The job is over: the step under way failed (unless it's done), and what never started is skipped."""
        for s in self.job.steps:
            if s.state == "running":
                s.state = "done" if state == "done" else "failed"
            elif s.state == "pending":
                s.state = "skipped"
        self.job.state, self.job.message, self.job.finished_at = state, message, _stamp(self.clock())
        (log.info if state == "done" else log.warning)("Update to %s: %s", self.job.target, message)
        self.save()

    # -- The loop ------------------------------------------------------------------------------------------------

    def tick(self) -> None:
        if self.last_seen is None or self.clock() - self.last_seen >= HEARTBEAT:
            self.refresh()
        request = updates.read_request(self.data_dir)
        if request and request.id not in self.handled:
            self.handled.add(request.id)
            self.handle(request)

    def run_forever(self) -> None:
        self.prepare()
        with open((self.state_dir if self.state_dir.is_dir() else self.folder) / ".updater.lock", "ab") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)  # a second updater waits here rather than act twice
            self.refresh()
            self.resume()
            if self.ready:
                log.info("The updater is ready for %s", self.install.working_dir)
            else:
                log.warning("The updater can't update this install: %s", self.problem)
            while True:
                self.tick()
                self.sleep(POLL_SECONDS)

    # -- One update ----------------------------------------------------------------------------------------------

    def handle(self, request: updates.Request) -> None:
        self.job = Job(
            id=request.id,
            target=request.version[:20],
            requested_by=request.by[:64],
            started_at=_stamp(self.clock()),
            steps=[Step(key, title) for key, title in STEPS],
        )
        log.info("%s asked for %s", request.by or "Someone", request.version[:20])
        try:
            checked, install = self.check(request)
            self.switch(install, checked)
        except Refused as exc:
            self.finish("refused", str(exc))
        except Exception as exc:  # before the switch nothing has changed, so this only reports
            log.exception("The update stopped before anything was changed")
            self.finish("failed", f"Something unexpected stopped the update before anything was changed: {exc}")

    def check(self, request: updates.Request) -> tuple[Checked, Install]:
        """Steps 1 to 3. Refused when anything doesn't hold; nothing on the server has changed by then."""
        self.begin("check")
        at = parse_time(request.at)
        if not at or self.clock() - at > updates.REQUEST_MAX_AGE:
            raise Refused(
                "The request was more than ten minutes old when the updater saw it, so it wasn't acted on. "
                "Press Update again."
            )
        target = Version.parse(request.version)
        if target is None:
            raise Refused(f"{request.version[:20]!r} isn't a release number.")
        self.refresh()
        install = self.install
        if install is None:
            raise Refused(self.problem)
        current = install.current
        if current is None:
            raise Refused("The version the portal runs couldn't be read from its image. Update by hand (DEPLOY.md).")
        self.job.current = str(current)
        if target <= current:
            raise Refused(f"{target} isn't newer than {current}, the version running.")
        if target.major != current.major:
            raise Refused(f"{target} is a new major version. Read its release notes and update by hand (DEPLOY.md).")
        release = self.github.release(target)
        commit = self.github.tag_commit(target)
        self.same_files(install, target, commit)
        self.room()
        self.end("check", f"Release {target}, published {str(release.get('published_at') or '')[:10]}")

        self.begin("verify", "Checking its image's signature…")
        checked = Checked(target, self.github.digest(target), commit)
        self.verify(checked)
        self.end(
            "verify",
            f"Signed by Office Sentry's release workflow on GitHub, built from the commit tagged v{target} "
            f"({checked.commit[:7]})",
        )

        self.begin("download", f"Downloading {target}…")
        try:
            self.docker.pull(checked.reference, timeout=PULL_TIMEOUT)
            image = self.docker.image(checked.reference)
            if not image:
                raise Refused(f"{target} didn't arrive.")
            try:
                self.docker.tag(image["Id"], IMAGE, str(target))  # a name for people: the digest is what runs
            except DockerError:
                pass
            self.docker.ensure(COMPOSE_IMAGE)  # to switch, and to go back
        except DockerError as exc:
            raise Refused(f"Docker couldn't download {target}: {exc}") from exc
        self.job.image, self.job.previous_image = image["Id"], install.image_id
        self.end("download", f"Downloaded {target}")
        return checked, install

    def same_files(self, install: Install, target: Version, commit: str) -> None:
        """Refuse a release whose docker-compose.yml or Caddyfile isn't the one in the install's folder: a new
        start-up allowance, volume or Caddy setting only arrives with the file, which the button doesn't
        install, and Caddy is only recreated when they change. The Caddyfile only counts where Caddy runs
        (not behind a reverse proxy of the server's own)."""
        changed = []
        for name, path in RELEASE_FILES.items():
            mine = self.project_dir / name
            if name == "Caddyfile" and not service(self.docker, install.project, "caddy"):
                continue
            theirs = self.github.file(path, commit)
            if theirs is None or not mine.is_file() or comparable(mine.read_bytes()) != comparable(theirs):
                changed.append(name)
        if changed:
            raise Refused(
                f"{target} comes with a {' and '.join(changed)} that {'aren' if len(changed) > 1 else 'isn'}'t the "
                "same as yours, and the Update button only changes the image. Update by hand this time, with the "
                'files attached to the release (DEPLOY.md, "Updating").'
            )

    def room(self) -> None:
        """Refuse, before any downtime, when there's no room for the copy of the database an upgrade makes first
        and for keeping the upgraded one if it has to go back."""
        db = self.data_dir / DATABASE
        if not db.exists():
            return
        size = sum(p.stat().st_size for p in (db, db.with_name(DATABASE + "-wal")) if p.exists())
        try:
            backups.check_space(self.data_dir, size, compress=True)
        except OSError as exc:
            raise Refused(
                f"There isn't enough disk space to update safely: {exc.strerror}. Free some and press Update again."
            ) from exc

    def verify(self, checked: Checked) -> None:
        try:
            self.docker.ensure(COSIGN_IMAGE)
            result = self.docker.run(
                COSIGN_IMAGE,
                [
                    "verify-attestation",
                    "--new-bundle-format",
                    "--type",
                    "slsaprovenance1",
                    "--certificate-oidc-issuer",
                    OIDC_ISSUER,
                    "--certificate-identity-regexp",
                    SIGNER,
                    checked.reference,
                ],
                env=[f"{k}={v}" for k, v in os.environ.items() if k.upper() in PROXY_VARIABLES] + ["HOME=/tmp"],
                tmpfs={"/tmp": "rw,size=64m"},  # noqa: S108 (the helper container's own, in memory)
                read_only=True,
                cap_drop=["ALL"],
                labels={"officesentry.updater": "verify"},
                timeout=VERIFY_TIMEOUT,
            )
        except DockerError as exc:
            raise Refused(f"The signature couldn't be checked: {exc}") from exc
        if result.code != 0:
            log.warning("cosign refused %s:\n%s", checked.reference, result.output[-2000:])
            raise Refused(
                f"The image for {checked.version} isn't signed by Office Sentry's release workflow, or the signature "
                "couldn't be checked (the server needs to reach ghcr.io and tuf-repo-cdn.sigstore.dev). Nothing "
                "was installed."
            )
        if not any(matches(s, checked.digest, checked.commit) for s in provenance(result.output)):
            raise Refused(f"The signed record for {checked.version} doesn't name the commit its release tag points at.")

    # -- Switching, and going back -------------------------------------------------------------------------------

    def _compose_command(self, install: Install, args: list[str]) -> tuple[list[str], dict]:
        cmd = ["docker", "compose", "--project-name", install.project, "--project-directory", install.working_dir]
        for f in install.config_files:
            cmd += ["--file", f]
        cmd += ["--env-file", install.env_file, *args]
        options = {
            "binds": [f"{install.socket}:{SOCKET}", f"{install.working_dir}:{install.working_dir}"],
            "workdir": install.working_dir,
            "network": "none",
        }
        return cmd, options

    def compose(self, install: Install, args: list[str], timeout: float = COMPOSE_TIMEOUT):
        """``docker compose <args>`` for this install, as an admin would run it in its folder."""
        cmd, options = self._compose_command(install, args)
        return self.docker.run(
            COMPOSE_IMAGE, cmd, labels={"officesentry.updater": "compose"}, timeout=timeout, **options
        )

    def switch(self, install: Install, checked: Checked) -> None:
        """Steps 4 to 6, going back by itself when 4 or 5 fails."""
        schema = database_version(self.data_dir / DATABASE)
        self.job.schema = "" if schema is None else str(schema)
        # Before the step starts, so going back always finds this update's copy.
        text = install.env_path.read_bytes().decode("utf-8")
        keep_copy(install.env_path)
        self.begin("switch", "Restarting the portal and the worker. The portal is unavailable until it's back.")
        try:
            rewrite(install.env_path, set_version(text, checked.pin).encode("utf-8"))
            result = self.compose(install, ["up", "-d", "--no-build", "--pull", "never", "web", "worker"])
            if result.code != 0:
                raise Broken(f"{checked.version} didn't start: {last_line(result.output)}")
            self.end("switch", f"Started {checked.version}")
            self.begin("health", "Waiting for the portal and the worker to report healthy…")
            self.wait_healthy(install, self.job.image)
            self.end("health", f"The portal and the worker are healthy on {checked.version}")
        except Exception as exc:
            reason = str(exc) if isinstance(exc, Broken) else f"Something unexpected went wrong: {exc}"
            self.rollback(install, reason)
            return
        self.job.state, self.job.message = "done", f"Office Sentry {checked.version} is running."
        self.job.finished_at = _stamp(self.clock())
        self.restart_self(install)

    def wait_healthy(self, install: Install, image_id: str) -> None:
        deadline = self.clock() + HEALTHY_WITHIN
        while True:
            states = {}
            for name in ("web", "worker"):
                c = service(self.docker, install.project, name)
                if not c:
                    raise Broken(f"The {NAMES[name]}'s container is missing.")
                if c.get("Image") != image_id:
                    raise Broken(f"The {NAMES[name]} isn't running the image it should.")
                states[name] = health(c, self.clock())
            bad = [NAMES[n] for n, s in states.items() if s == "unhealthy"]
            if bad:
                raise Broken(f"The {' and the '.join(bad)} reported unhealthy.")
            if all(s == "healthy" for s in states.values()):
                return
            if self.clock() >= deadline:
                slow = " and the ".join(NAMES[n] for n, s in states.items() if s != "healthy")
                raise Broken(f"The {slow} didn't report healthy within {HEALTHY_WITHIN.seconds // 60} minutes.")
            self.sleep(5)

    def rollback(self, install: Install, reason: str) -> None:
        """Back to the version before: stop both, put .env back, put the database back if it was upgraded,
        start the old version and wait for it to be healthy."""
        before = self.job.current or "the version before"
        log.warning("The update to %s failed (%s); going back to %s", self.job.target, reason, before)
        for s in self.job.steps:
            if s.state == "running":
                s.state = "failed"
        step = next((s for s in self.job.steps if s.key == "rollback"), None)  # going back again, after a restart
        if step is None:
            step = Step("rollback", f"Go back to {before}")
            self.job.steps.append(step)
        step.state, step.detail = "running", "Stopping the new version…"
        self.save()
        restored = ""
        try:
            stopped = self.compose(install, ["stop", "web", "worker"], timeout=STOP_TIMEOUT)
            if stopped.code != 0:
                raise Broken(f"the new version wouldn't stop ({last_line(stopped.output)})")
            kept = install.env_path.with_name(install.env_path.name + ENV_KEPT)
            rewrite(install.env_path, kept.read_bytes())
            schema = int(self.job.schema) if self.job.schema.isdigit() else None
            now_schema = database_version(self.data_dir / DATABASE)
            if schema is not None and now_schema is not None and now_schema != schema:
                copy = pre_upgrade_copy(self.data_dir, schema, parse_time(self.job.started_at))
                if copy is None:
                    raise Broken(f"the database was upgraded and the copy made before (version {schema}) wasn't found")
                step.detail = f"Putting the database back from {copy.name}…"
                self.save()
                result = self.compose(
                    install,
                    ["run", "--rm", "--no-deps", "--pull", "never", "-T", "web"]
                    + ["python", "-m", "officesentry", "restore", f"/data/backups/{copy.name}", "--force"],
                )
                if result.code != 0:
                    raise Broken(f"putting the database back failed ({last_line(result.output)})")
                restored = f" The database was put back from {copy.name}; the upgraded one is kept in backups."
            step.detail = f"Starting {before} again…"
            self.save()
            result = self.compose(install, ["up", "-d", "--no-build", "--pull", "never", "web", "worker"])
            if result.code != 0:
                raise Broken(f"{before} didn't start again ({last_line(result.output)})")
            self.wait_healthy(install, self.job.previous_image)
        except Exception as exc:
            self.finish(
                "failed",
                f"{sentence(reason)} Going back to {before} didn't work either: {sentence(str(exc))} "
                'Follow "Rolling back an update" in DEPLOY.md, or ask for help with a support package.',
            )
            return
        step.state, step.detail = "done", f"Back on {before}.{restored}"
        self.finish("rolled_back", f"Back on {before}: {sentence(reason)}{restored}")

    def restart_self(self, install: Install) -> None:
        """Last, the updater restarts on the new version too. The helper outlives this container, which compose
        replaces; if this one is still here once the helper has gone, it didn't work."""
        try:
            me = self.docker.container(self.hostname or socket.gethostname()) or {}
            if me.get("Image") == self.job.image:
                self.job.restarted_updater = "done"
                self.end("finish", "The updater already runs the new version.")
                return
            self.job.restarted_updater = "pending"
            self.begin("finish", "Restarting the updater on the new version…")
            cmd, options = self._compose_command(
                install, ["up", "-d", "--no-deps", "--no-build", "--pull", "never", "updater"]
            )
            cid = self.docker.start(COMPOSE_IMAGE, cmd, labels={"officesentry.updater": "restart"}, **options)
            deadline = self.clock() + SELF_RESTART_WAIT
            while self.clock() < deadline and self.docker.container(cid):
                self.sleep(2)
        except Exception as exc:  # the update itself worked: only this last step failed
            log.warning("The updater couldn't restart itself: %s", exc)
        self.job.restarted_updater = "failed"
        self.end(
            "finish",
            "The updater couldn't restart itself on the new version. Run docker compose up -d updater.",
            state="failed",
        )

    def resume(self) -> None:
        """An update cut off part way: the server or Docker restarted, or this is the updater it just restarted."""
        job = self.job
        if not job:
            return
        if job.state == "done" and job.restarted_updater == "pending":
            ok = Version.parse(__version__) == Version.parse(job.target)
            job.restarted_updater = "done" if ok else "failed"
            self.end(
                "finish",
                "The updater runs the new version too."
                if ok
                else "The updater didn't restart on the new version. Run docker compose up -d updater.",
                state="done" if ok else "failed",
            )
            return
        if job.finished:
            return
        states = {s.key: s.state for s in job.steps}
        if states.get("switch", "pending") == "pending":
            self.finish("failed", "The update was cut off before anything was installed. Press Update again.")
            return
        if self.install is None:
            self.finish(
                "failed",
                f"The update was cut off part way and the updater can't look at the install now ({self.problem}). "
                'Check the portal works; if it doesn\'t, follow "Rolling back an update" in DEPLOY.md.',
            )
            return
        if "rollback" not in states:
            try:
                self.wait_healthy(self.install, job.image)
            except Broken as exc:
                self.rollback(self.install, f"The update was cut off part way ({exc})")
                return
            job.state, job.message = "done", f"Office Sentry {job.target} is running."
            job.finished_at = _stamp(self.clock())
            for key in ("switch", "health"):
                self._step(key).state = "done"
            self.restart_self(self.install)
            return
        self.rollback(self.install, "The update was cut off while going back")


def main() -> None:
    """``python -m officesentry updater``: watch for requests until stopped."""

    def stop(signum, frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    data_dir = Path(os.environ.get("OFFICESENTRY_DATA_DIR", "/data"))
    Updater(Docker(SOCKET), data_dir).run_forever()
