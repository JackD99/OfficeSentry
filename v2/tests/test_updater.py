"""The updater container (``updater.py``): what it checks before installing a release, the switch, going back
by itself when the new version doesn't start, and picking up after being cut off.

Docker is a fake that behaves like a small compose install (``FakeDocker``): ``docker compose up`` reads
``.env`` to choose the image, as compose does, and a start can upgrade the database the way ``db.migrate``
does, copy first. GitHub is ``responses`` for its own tests and a stand-in for the rest.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import requests
import responses

from officesentry import updater as up
from officesentry import updates
from officesentry.docker_api import DockerError, Result
from officesentry.updater import COMPOSE_IMAGE, COSIGN_IMAGE, IMAGE, Refused
from officesentry.updates import Version

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
OLD_ID = "sha256:" + "1" * 64
NEW_ID = "sha256:" + "2" * 64
DIGEST = "sha256:" + "d" * 64
COMMIT = "c" * 40
LABEL = "com.docker.compose."


class Killed(BaseException):
    """What happens to the updater's process when compose replaces its container."""


def statement(digest: str = DIGEST, commit: str = COMMIT, name: str = IMAGE) -> dict:
    return {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": name, "digest": {"sha256": digest.removeprefix("sha256:")}}],
        "predicateType": up.PROVENANCE,
        "predicate": {
            "buildDefinition": {
                "resolvedDependencies": [
                    {
                        "uri": "git+https://github.com/JackD99/OfficeSentry@refs/tags/v2.4.0",
                        "digest": {"gitCommit": commit},
                    }
                ]
            }
        },
    }


def cosign_output(*statements: dict) -> str:
    lines = [
        json.dumps(
            {
                "payloadType": "application/vnd.in-toto+json",
                "payload": base64.b64encode(json.dumps(s).encode()).decode(),
            }
        )
        for s in statements
    ]
    return "Verification for ghcr.io/jackd99/officesentry@... --\nThe following checks were performed\n" + "\n".join(
        lines
    )


def schema(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def set_schema(path: Path, version: int) -> None:
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {version}")
    conn.commit()
    conn.close()


class FakeDocker:
    """A compose install of 2.2.0 (web, worker, and the updater itself), and a registry holding 2.4.0."""

    def __init__(self, project: Path, data_dir: Path):
        self.project = project
        self.data_dir = data_dir
        self.calls: list[tuple] = []
        self.up = True
        self.health = {OLD_ID: "healthy", NEW_ID: "healthy"}
        self.migrates = False  # the new version upgrades the database when it starts
        self.fail: set[str] = set()  # compose verbs that fail ("up", "stop", "run"); "up-new" fails only on 2.4.0
        self.cosign = Result(0, cosign_output(statement()))
        self.registry = {f"{IMAGE}@{DIGEST}": NEW_ID}
        self.images = {OLD_ID: {"Id": OLD_ID, "Config": {"Labels": {up.VERSION_LABEL: "2.2.0"}}}}
        self.names = {f"{IMAGE}:2.2.0": OLD_ID}
        self.restarted: list[list[str]] = []
        self.kill_on_restart = True
        labels = {
            f"{LABEL}project": "officesentry",
            f"{LABEL}project.working_dir": str(project),
            f"{LABEL}project.config_files": str(project / "docker-compose.yml"),
            f"{LABEL}oneoff": "False",
        }
        self.box = {}
        for name in ("web", "worker", "updater"):
            self.box[name] = {
                "Id": f"{name}-id",
                "Created": 1,
                "Image": OLD_ID,
                "State": {"Running": True, "Status": "running", "Health": {"Status": "healthy"}},
                "Config": {
                    "Image": f"{IMAGE}:2.2.0",
                    "Labels": {**labels, f"{LABEL}service": name, up.VERSION_LABEL: "2.2.0"},
                },
                "Mounts": [],
            }
        self.box["caddy"] = {  # the https profile's; the updater never restarts it
            "Id": "caddy-id",
            "Created": 1,
            "Image": "sha256:" + "c" * 64,
            "State": {"Running": True, "Status": "running"},
            "Config": {"Image": "caddy:2", "Labels": {**labels, f"{LABEL}service": "caddy"}},
            "Mounts": [],
        }
        self.box["updater"]["Mounts"] = [
            {"Destination": up.SOCKET, "Source": "/var/run/docker.sock"},
            {"Destination": str(project), "Source": str(project)},
            {"Destination": "/data", "Source": "/var/lib/docker/volumes/officesentry_officesentry-data/_data"},
        ]

    # -- Reading

    def ping(self) -> bool:
        return self.up

    def container(self, ref: str):
        """By id, or by the updater's own hostname, which is how it finds itself."""
        found = (
            self.box["updater"]
            if ref == "updater-host"
            else next((c for c in self.box.values() if c["Id"] == ref), None)
        )
        return json.loads(json.dumps(found)) if found else None

    def containers(self, labels: dict) -> list[dict]:
        return [
            {"Id": c["Id"], "State": c["State"]["Status"], "Created": c["Created"]}
            for c in self.box.values()
            if all(c["Config"]["Labels"].get(k) == v for k, v in labels.items())
        ]

    def image(self, ref: str):
        return self.images.get(self.names.get(ref, ref))

    # -- Changing

    def pull(self, ref: str, progress=None, timeout: float = 0) -> None:
        self.calls.append(("pull", ref))
        if ref not in self.registry:
            raise DockerError(404, "manifest unknown")
        self.images[self.registry[ref]] = {"Id": self.registry[ref], "Config": {"Labels": {up.VERSION_LABEL: "2.4.0"}}}
        self.names[ref] = self.registry[ref]

    def ensure(self, ref: str, timeout: float = 0) -> None:
        self.calls.append(("ensure", ref))

    def tag(self, image_id: str, repo: str, tag: str) -> None:
        self.names[f"{repo}:{tag}"] = image_id

    def run(self, image: str, cmd: list[str], *, timeout: float = 600, **options) -> Result:
        if image == COSIGN_IMAGE:
            self.calls.append(("cosign", cmd[-1]))
            return self.cosign
        assert image == COMPOSE_IMAGE
        assert options["network"] == "none" and f"{self.project}:{self.project}" in options["binds"]
        args = cmd[cmd.index("--env-file") + 2 :]
        assert cmd[cmd.index("--env-file") + 1] == str(self.project / ".env")
        self.calls.append(("compose", *args))
        verb = args[0]
        if verb in self.fail:
            return Result(1, f"Error response from daemon: {verb} went wrong\n")
        if verb == "stop":
            for name in args[1:]:
                self.box[name]["State"] = {"Running": False, "Status": "exited"}
        elif verb == "up":
            value = self._env_version()
            if "@" in value:
                image_id = self.names.get(f"{IMAGE}@{value.split('@', 1)[1]}")
            else:
                image_id = self.names.get(f"{IMAGE}:{value}")
            if image_id is None:
                return Result(1, f"No such image: {IMAGE}:{value}")
            if image_id == NEW_ID and "up-new" in self.fail:  # as Docker's CLI writes it to its terminal
                return Result(
                    1,
                    "\x1b[?25l[+] Running 2/2\r\n \x1b[32m✔\x1b[0m Container officesentry-web-1  Started\r\n"
                    "\x1b[?25hdependency failed to start: container officesentry-web-1 is unhealthy\r\n",
                )
            version = self.images[image_id]["Config"]["Labels"][up.VERSION_LABEL]
            for name in [a for a in args[1:] if not a.startswith("-") and a != "never"]:
                state = self.health.get(image_id, "healthy")
                self.box[name].update(
                    Image=image_id,
                    State={"Running": True, "Status": "running", "Health": {"Status": state}},
                )
                self.box[name]["Config"].update(Image=f"{IMAGE}:{value}")
                self.box[name]["Config"]["Labels"][up.VERSION_LABEL] = version
            if image_id == NEW_ID and self.migrates and schema(self.db) == 41:
                backups = self.data_dir / "backups"
                backups.mkdir(exist_ok=True)
                copy = backups / "pre-upgrade-v41-to-v42-20261102-090000-from-2.2.0.db"
                copy.write_bytes(self.db.read_bytes())
                os.utime(copy, (NOW.timestamp(), NOW.timestamp()))  # made now, by the fake clock
                set_schema(self.db, 42)
        elif verb == "run":
            assert args[-5:-2] == ["-m", "officesentry", "restore"] and args[-1] == "--force"
            copy = self.data_dir / "backups" / Path(args[-2]).name
            self.db.write_bytes(copy.read_bytes())
        return Result(0, "done\n")

    def start(self, image: str, cmd: list[str], **options) -> str:
        assert image == COMPOSE_IMAGE
        self.restarted.append(cmd[cmd.index("--env-file") + 2 :])
        self.box["updater"]["Image"] = self.names.get(f"{IMAGE}@{self._env_version().split('@')[-1]}", OLD_ID)
        if self.kill_on_restart:
            raise Killed
        return "helper-id"

    @property
    def db(self) -> Path:
        return self.data_dir / "officesentry.db"

    def _env_version(self) -> str:
        for line in reversed((self.project / ".env").read_text(encoding="utf-8").splitlines()):
            if line.startswith("OFFICESENTRY_VERSION="):
                return line.split("=", 1)[1]
        return "2"


# The install files as the repository keeps them; release.yml attaches the compose file with the release's
# number as its default version.
COMPOSE = "services:\n  web:\n    image: ghcr.io/jackd99/officesentry:${OFFICESENTRY_VERSION:-2}\n"
CADDYFILE = "{$OFFICESENTRY_DOMAIN} {\n\treverse_proxy web:8000\n}\n"


class FakeGitHub:
    def __init__(self):
        self.releases = {"2.3.0": {}, "2.4.0": {"published_at": "2026-11-01T10:00:00Z"}}
        self.digests = {"2.4.0": DIGEST}
        self.commit = COMMIT
        self.merged = True  # the tag's commit is on main
        self.files = {"v2/docker-compose.yml": COMPOSE.encode(), "v2/Caddyfile": CADDYFILE.encode()}

    def release(self, version: Version) -> dict:
        if str(version) not in self.releases:
            raise Refused(f"There's no published release {version} of Office Sentry on GitHub.")
        return self.releases[str(version)]

    def digest(self, version: Version) -> str:
        return self.digests[str(version)]

    def tag_commit(self, version: Version) -> str:
        return self.commit

    def on_main(self, commit: str) -> bool:
        assert commit == self.commit
        return self.merged

    def file(self, path: str, commit: str) -> bytes | None:
        assert commit == self.commit  # the files of the commit the image is checked against
        return self.files.get(path)


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def server(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "docker-compose.yml").write_text(COMPOSE.replace(":-2}", ":-2.2.0}"), encoding="utf-8")
    (project / "Caddyfile").write_text(CADDYFILE, encoding="utf-8")
    (project / ".env").write_text(
        "OFFICESENTRY_BASE_URL=https://sentry.example.com\nOFFICESENTRY_VERSION=2.2.0\n", encoding="utf-8"
    )
    (project / ".env").chmod(0o640)
    data = tmp_path / "data"
    data.mkdir()
    set_schema(data / "officesentry.db", 41)
    state = tmp_path / "state"
    state.mkdir()
    return tmp_path


@pytest.fixture
def docker(server):
    return FakeDocker(server / "project", server / "data")


@pytest.fixture
def clock():
    return Clock()


def make(server, docker, clock, github=None) -> up.Updater:
    """An updater starting up, as the container does."""
    u = up.Updater(
        docker,
        server / "data",
        project_dir=server / "project",
        github=github or FakeGitHub(),
        clock=clock,
        sleep=clock.sleep,
        hostname="updater-host",
        state_dir=server / "state",
    )
    u.prepare()
    u.refresh()
    return u


def ask(server, version="2.4.0", at=NOW, by="admin") -> None:
    """What the portal writes when someone presses Update (any version: the updater trusts none of it)."""
    folder = updates.folder(server / "data")
    folder.mkdir(exist_ok=True)
    stamp = at.strftime("%Y-%m-%dT%H:%M:%SZ")
    request = {"format": updates.FORMAT, "id": hashlib.sha256(f"{version}{stamp}".encode()).hexdigest()[:16]}
    updates.write_json(folder / updates.REQUEST_FILE, {**request, "version": version, "by": by, "at": stamp})


def env(server) -> str:
    return (server / "project" / ".env").read_text(encoding="utf-8")


def steps(job) -> dict:
    return {s.key: s.state for s in job.steps}


# -- Finding the install -------------------------------------------------------------------------------------


def test_it_finds_the_install_from_composes_labels(server, docker, clock):
    u = make(server, docker, clock)
    assert u.ready and u.problem == ""
    i = u.install
    assert (i.project, i.working_dir, i.current, i.image_id) == (
        "officesentry",
        str(server / "project"),
        Version(2, 2, 0),
        OLD_ID,
    )
    assert i.env_file == str(server / "project" / ".env") and i.socket == "/var/run/docker.sock"
    published = updates.status(server / "data")
    assert published.ready and published.install["folder"] == str(server / "project")


@pytest.mark.parametrize(
    ("change", "said"),
    [
        (lambda d, s: setattr(d, "up", False), "can't reach Docker"),
        (lambda d, s: d.box["updater"]["Config"]["Labels"].clear(), "wasn't started by docker compose"),
        (lambda d, s: d.box["updater"]["Mounts"].pop(0), "doesn't have the Docker socket"),
        (lambda d, s: d.box["updater"]["Mounts"].pop(1), "can't see the folder with docker-compose.yml"),
        (
            lambda d, s: d.box["updater"]["Config"]["Labels"].update(
                {f"{LABEL}project.config_files": "/etc/other.yml"}
            ),
            "in one folder",
        ),
        (lambda d, s: (s / "project" / ".env").unlink(), "can't find .env"),
        (lambda d, s: d.box.pop("web"), "can't find the portal's container"),
        (
            lambda d, s: d.box["web"]["Config"].update(Image="officesentry:local"),
            "builds Office Sentry from its source",
        ),
        (lambda d, s: (s / "state").rmdir(), "needs its own volume"),
    ],
)
def test_it_says_what_to_fix_when_it_cant_update(server, docker, clock, change, said):
    change(docker, server)
    u = make(server, docker, clock)
    assert not u.ready and said in u.problem
    assert said in updates.status(server / "data").problem


def test_the_version_comes_from_the_image_or_its_exact_tag(server, docker, clock):
    docker.box["web"]["Config"]["Labels"].pop(up.VERSION_LABEL)
    assert make(server, docker, clock).install.current == Version(2, 2, 0)  # from :2.2.0
    docker.box["web"]["Config"]["Image"] = f"{IMAGE}:2"
    assert make(server, docker, clock).install.current is None
    assert up.tag_version(f"{IMAGE}:2.3.1@{DIGEST}") == Version(2, 3, 1)
    assert up.tag_version("localhost:5000/officesentry") is None


# -- .env ----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("A=1\nOFFICESENTRY_VERSION=2.2.0\nB=2\n", "A=1\nOFFICESENTRY_VERSION=2.4.0@x\nB=2\n"),
        ("A=1\r\nOFFICESENTRY_VERSION = 2.2.0\r\n", "A=1\r\nOFFICESENTRY_VERSION=2.4.0@x\r\n"),
        ("export OFFICESENTRY_VERSION=2\n", "export OFFICESENTRY_VERSION=2.4.0@x\n"),
        # compose takes the last line, so that's the one changed; the commented example stays.
        (
            "# OFFICESENTRY_VERSION=2.0.0\nOFFICESENTRY_VERSION=2.1.0\nOFFICESENTRY_VERSION=2.2.0\n",
            "# OFFICESENTRY_VERSION=2.0.0\nOFFICESENTRY_VERSION=2.1.0\nOFFICESENTRY_VERSION=2.4.0@x\n",
        ),
        ("A=1", "A=1\n# Set by the Update button (Settings > Updates).\nOFFICESENTRY_VERSION=2.4.0@x\n"),
        ("", "# Set by the Update button (Settings > Updates).\nOFFICESENTRY_VERSION=2.4.0@x\n"),
    ],
)
def test_only_the_version_line_changes(before, after):
    assert up.set_version(before, "2.4.0@x") == after


def test_env_keeps_its_owner_and_permissions(server):
    path = server / "project" / ".env"
    copy = up.keep_copy(path)
    assert copy.name == ".env.before-update" and copy.read_text(encoding="utf-8") == path.read_text(encoding="utf-8")
    assert oct(copy.stat().st_mode & 0o777) == "0o640"
    inode = path.stat().st_ino
    up.rewrite(path, b"OFFICESENTRY_VERSION=2.4.0\n")
    assert path.stat().st_ino == inode and oct(path.stat().st_mode & 0o777) == "0o640"
    assert not list((server / "project").glob(".*.tmp"))


# -- The release's checks --------------------------------------------------------------------------------------


GH = "https://api.github.com/repos/JackD99/OfficeSentry"


@responses.activate
def test_github_release_and_tag():
    responses.get(f"{GH}/releases/tags/v2.4.0", json={"tag_name": "v2.4.0", "draft": False, "prerelease": False})
    responses.get(f"{GH}/releases/tags/v2.5.0", json={"tag_name": "v2.5.0", "draft": True})
    responses.get(f"{GH}/releases/tags/v2.6.0", status=404)
    gh = up.GitHub()
    assert gh.release(Version(2, 4, 0))["tag_name"] == "v2.4.0"
    with pytest.raises(Refused, match="draft or a pre-release"):
        gh.release(Version(2, 5, 0))
    with pytest.raises(Refused, match="no published release 2.6.0"):
        gh.release(Version(2, 6, 0))

    # An annotated tag is followed to its commit.
    responses.get(f"{GH}/git/ref/tags/v2.4.0", json={"object": {"type": "tag", "sha": "a" * 40}})
    responses.get(f"{GH}/git/tags/{'a' * 40}", json={"object": {"type": "commit", "sha": COMMIT}})
    assert gh.tag_commit(Version(2, 4, 0)) == COMMIT
    responses.get(f"{GH}/git/ref/tags/v2.5.0", json={"object": {"type": "commit", "sha": "not-a-sha"}})
    with pytest.raises(Refused, match="which commit"):
        gh.tag_commit(Version(2, 5, 0))


@responses.activate
@pytest.mark.parametrize(
    ("status", "on_main"), [("behind", True), ("identical", True), ("ahead", False), ("diverged", False)]
)
def test_github_says_whether_the_tags_commit_is_on_main(status, on_main):
    """main is the base, the tag's commit the head: on main, it's behind main or the same commit."""
    responses.get(f"{GH}/compare/main...{COMMIT}", json={"status": status, "behind_by": 3})
    assert up.GitHub().on_main(COMMIT) is on_main
    responses.replace(responses.GET, f"{GH}/compare/main...{COMMIT}", status=404)
    with pytest.raises(Refused, match="whether"):
        up.GitHub().on_main(COMMIT)


@responses.activate
def test_github_file_at_the_release_commit():
    match = [responses.matchers.query_param_matcher({"ref": COMMIT})]
    responses.get(f"{GH}/contents/v2/docker-compose.yml", body=COMPOSE, match=match)
    responses.get(f"{GH}/contents/v2/Caddyfile", status=404, match=match)
    responses.get(f"{GH}/contents/v2/other", status=502, match=match)
    gh = up.GitHub()
    assert gh.file("v2/docker-compose.yml", COMMIT) == COMPOSE.encode()
    assert responses.calls[0].request.headers["Accept"] == "application/vnd.github.raw+json"
    assert gh.file("v2/Caddyfile", COMMIT) is None
    with pytest.raises(Refused, match="GitHub answered 502 when asked for v2/other"):
        gh.file("v2/other", COMMIT)


@responses.activate
def test_the_digest_is_worked_out_from_the_manifest():
    manifest = json.dumps({"schemaVersion": 2, "manifests": []}).encode()
    digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
    responses.get("https://ghcr.io/token", json={"token": "anon"})
    index = "application/vnd.oci.image.index.v1+json"
    responses.get(
        "https://ghcr.io/v2/jackd99/officesentry/manifests/2.4.0",
        body=manifest,
        headers={"Docker-Content-Digest": digest, "Content-Type": index},
    )
    gh = up.GitHub()
    assert gh.digest(Version(2, 4, 0)) == digest
    assert responses.calls[1].request.headers["Authorization"] == "Bearer anon"

    responses.replace(
        responses.GET,
        "https://ghcr.io/v2/jackd99/officesentry/manifests/2.4.0",
        body=manifest,
        headers={"Docker-Content-Digest": "sha256:" + "0" * 64, "Content-Type": index},
    )
    with pytest.raises(Refused, match="doesn't match its own digest"):
        gh.digest(Version(2, 4, 0))
    responses.replace(
        responses.GET,
        "https://ghcr.io/v2/jackd99/officesentry/manifests/2.4.0",
        body=manifest,
        headers={"Content-Type": "application/vnd.oci.image.manifest.v1+json"},
    )
    with pytest.raises(Refused, match="isn't the multi-platform image"):
        gh.digest(Version(2, 4, 0))


def test_unreachable_github_is_a_refusal():
    with responses.RequestsMock() as rsps:
        rsps.get(f"{GH}/releases/tags/v2.4.0", body=requests.ConnectionError("down"))
        with pytest.raises(Refused, match="couldn't reach api.github.com"):
            up.GitHub().release(Version(2, 4, 0))


def test_the_signed_record_must_name_this_image_and_commit():
    good = statement()
    assert up.matches(good, DIGEST, COMMIT)
    assert not up.matches(good, "sha256:" + "e" * 64, COMMIT)
    assert not up.matches(good, DIGEST, "f" * 40)
    assert not up.matches(statement(name="ghcr.io/someone/else"), DIGEST, COMMIT)
    assert not up.matches({**good, "predicateType": "https://example.com/other"}, DIGEST, COMMIT)
    assert up.provenance(cosign_output(good, statement(commit="f" * 40))) == [good, statement(commit="f" * 40)]
    assert up.provenance("Error: no matching attestations\n{not json") == []


def test_the_signer_is_this_repositorys_release_workflow():
    import re

    ok = "https://github.com/JackD99/OfficeSentry/.github/workflows/release.yml@refs/tags/v2.4.0"
    assert re.match(up.SIGNER, ok) and re.match(up.SIGNER, ok.replace("tags/v2.4.0", "heads/main"))
    for bad in (
        ok.replace("JackD99/OfficeSentry", "JackD99/OfficeSentry-fork"),
        ok.replace("release.yml", "v2.yml"),
        ok.replace("tags/v2.4.0", "heads/feature"),
        ok + "x",
    ):
        assert not re.match(up.SIGNER, bad)


# -- One update ----------------------------------------------------------------------------------------------


def test_an_update_installs_the_checked_image_and_restarts_the_updater(server, docker, clock, monkeypatch):
    u = make(server, docker, clock)
    ask(server)
    request = updates.read_request(server / "data")
    with pytest.raises(Killed):  # compose replaced the updater's own container
        u.tick()
    assert ("pull", f"{IMAGE}@{DIGEST}") in docker.calls
    assert ("cosign", f"{IMAGE}@{DIGEST}") in docker.calls
    assert ("compose", "up", "-d", "--no-build", "--pull", "never", "web", "worker") in docker.calls
    assert docker.restarted == [["up", "-d", "--no-deps", "--no-build", "--pull", "never", "updater"]]
    # .env pins the version people read and the digest Docker uses; the old one is kept.
    assert f"OFFICESENTRY_VERSION=2.4.0@{DIGEST}\n" in env(server)
    assert "OFFICESENTRY_VERSION=2.2.0" in (server / "project" / ".env.before-update").read_text(encoding="utf-8")
    assert docker.box["web"]["Image"] == docker.box["worker"]["Image"] == NEW_ID
    job = updates.status(server / "data").job
    assert job.id == request.id and job.state == "done" and job.restarted_updater == "pending"
    assert job.message == "Office Sentry 2.4.0 is running." and job.current == "2.2.0"
    assert steps(job) == {
        "check": "done",
        "verify": "done",
        "download": "done",
        "switch": "done",
        "health": "done",
        "finish": "running",
    }

    # The new updater (2.4.0) finishes the job when it starts.
    monkeypatch.setattr(up, "__version__", "2.4.0")
    again = make(server, docker, clock)
    again.resume()
    job = updates.status(server / "data").job
    assert job.restarted_updater == "done" and steps(job)["finish"] == "done"
    again.tick()  # the same request is never handled twice
    assert docker.restarted == [["up", "-d", "--no-deps", "--no-build", "--pull", "never", "updater"]]


def test_a_failed_self_restart_is_shown_but_the_update_stands(server, docker, clock):
    docker.kill_on_restart = False
    docker.box["updater"]["Image"] = OLD_ID
    docker.start = lambda image, cmd, **kw: "helper-id"  # the helper ran, but the updater wasn't replaced
    u = make(server, docker, clock)
    ask(server)
    u.tick()
    job = updates.status(server / "data").job
    assert job.state == "done" and job.restarted_updater == "failed"
    assert steps(job)["finish"] == "failed" and "docker compose up -d updater" in job.steps[-1].detail


@pytest.mark.parametrize(
    ("version", "at", "said"),
    [
        ("2.4.0", NOW - timedelta(minutes=11), "more than ten minutes old"),
        ("2.2.0", NOW, "isn't newer than 2.2.0"),
        ("2.1.0", NOW, "isn't newer than 2.2.0"),
        ("3.0.0", NOW, "new major version"),
        ("2.5.0", NOW, "no published release 2.5.0"),
        ("latest", NOW, "isn't a release number"),
    ],
)
def test_requests_it_refuses(server, docker, clock, version, at, said):
    u = make(server, docker, clock)
    ask(server, version, at)
    u.tick()
    job = updates.status(server / "data").job
    assert job.state == "refused" and said in job.message
    assert env(server).endswith("OFFICESENTRY_VERSION=2.2.0\n")
    assert not [c for c in docker.calls if c[0] in ("pull", "compose")]


def test_an_unsigned_image_is_never_installed(server, docker, clock):
    docker.cosign = Result(1, "Error: no matching attestations: ...")
    u = make(server, docker, clock)
    ask(server)
    u.tick()
    job = updates.status(server / "data").job
    assert job.state == "refused" and "isn't signed by Office Sentry's release workflow" in job.message
    assert steps(job) == {
        "check": "done",
        "verify": "failed",
        "download": "skipped",
        "switch": "skipped",
        "health": "skipped",
        "finish": "skipped",
    }
    assert not [c for c in docker.calls if c[0] in ("pull", "compose")]


def test_a_tag_on_a_commit_that_isnt_on_main_is_refused(server, docker, clock):
    """A tag pushed on a branch's commit gets its own release.yml run, signed like a real release."""
    github = FakeGitHub()
    github.merged = False
    u = make(server, docker, clock, github)
    ask(server)
    u.tick()
    job = updates.status(server / "data").job
    assert job.state == "refused" and "isn't on main" in job.message
    assert steps(job)["check"] == "failed"
    assert not [c for c in docker.calls if c[0] in ("cosign", "pull", "compose")]


def test_a_signature_for_another_commit_is_refused(server, docker, clock):
    docker.cosign = Result(0, cosign_output(statement(commit="f" * 40)))
    u = make(server, docker, clock)
    ask(server)
    u.tick()
    job = updates.status(server / "data").job
    assert job.state == "refused" and "doesn't name the commit" in job.message


@pytest.mark.parametrize(
    ("change", "said"),
    [
        (
            lambda gh, s: gh.files.update({"v2/docker-compose.yml": COMPOSE.encode() + b"    pids_limit: 512\n"}),
            "2.4.0 comes with a docker-compose.yml that isn't the same as yours",
        ),
        (lambda gh, s: gh.files.update({"v2/Caddyfile": b"{\n\tadmin off\n}\n"}), "a Caddyfile that isn't"),
        (lambda gh, s: gh.files.pop("v2/Caddyfile"), "a Caddyfile that isn't"),
        # A file edited here is told apart from the release's the same way.
        (
            lambda gh, s: (s / "project" / "docker-compose.yml").write_text(COMPOSE + "  # mine\n", encoding="utf-8"),
            "a docker-compose.yml that isn't",
        ),
        (
            lambda gh, s: (gh.files.clear(), (s / "project" / "Caddyfile").write_text("{}\n", encoding="utf-8")),
            "a docker-compose.yml and Caddyfile that aren't the same as yours",
        ),
    ],
)
def test_a_release_with_other_install_files_is_left_to_a_hand_update(server, docker, clock, change, said):
    github = FakeGitHub()
    change(github, server)
    u = make(server, docker, clock, github)
    ask(server)
    u.tick()
    job = updates.status(server / "data").job
    assert job.state == "refused" and said in job.message
    assert 'Update by hand this time, with the files attached to the release (DEPLOY.md, "Updating").' in job.message
    assert steps(job)["check"] == "failed" and steps(job)["verify"] == "skipped"
    assert env(server).endswith("OFFICESENTRY_VERSION=2.2.0\n")
    assert not [c for c in docker.calls if c[0] in ("cosign", "pull", "compose")]


def test_line_endings_the_default_version_and_no_caddy_dont_count(server, docker, clock):
    compose = server / "project" / "docker-compose.yml"
    compose.write_bytes(compose.read_bytes().replace(b"\n", b"\r\n"))
    docker.box.pop("caddy")  # a server behind a reverse proxy of its own: its Caddyfile isn't used
    github = FakeGitHub()
    github.files["v2/Caddyfile"] = b"{\n\tadmin off\n}\n"
    u = make(server, docker, clock, github)
    ask(server)
    with pytest.raises(Killed):
        u.tick()
    assert updates.status(server / "data").job.state == "done"
    assert up.comparable(b"image: x:${OFFICESENTRY_VERSION:-2.4.0}\r\n") == "image: x:${OFFICESENTRY_VERSION}"


def test_no_room_for_the_database_copy_is_refused_before_any_downtime(server, docker, clock, monkeypatch):
    def full(folder, size, compress=True):
        raise OSError(28, "a copy of the database needs about 900 MB free")

    monkeypatch.setattr(up.backups, "check_space", full)
    u = make(server, docker, clock)
    ask(server)
    u.tick()
    job = updates.status(server / "data").job
    assert job.state == "refused" and "isn't enough disk space" in job.message and "900 MB" in job.message


def test_a_download_that_fails_changes_nothing(server, docker, clock):
    docker.registry.clear()
    u = make(server, docker, clock)
    ask(server)
    u.tick()
    job = updates.status(server / "data").job
    assert job.state == "refused" and "couldn't download 2.4.0" in job.message
    assert env(server).endswith("OFFICESENTRY_VERSION=2.2.0\n")


# -- Going back ------------------------------------------------------------------------------------------------


def test_an_unhealthy_new_version_is_rolled_back(server, docker, clock):
    docker.health[NEW_ID] = "unhealthy"
    u = make(server, docker, clock)
    ask(server)
    u.tick()
    job = updates.status(server / "data").job
    assert job.state == "rolled_back"
    assert job.message == "Back on 2.2.0: The portal and the worker reported unhealthy."
    assert steps(job) == {
        "check": "done", "verify": "done", "download": "done", "switch": "done",
        "health": "failed", "finish": "skipped", "rollback": "done",
    }  # fmt: skip
    assert env(server).endswith("OFFICESENTRY_VERSION=2.2.0\n") and "@" not in env(server)
    assert docker.box["web"]["Image"] == docker.box["worker"]["Image"] == OLD_ID
    composed = [c[1] for c in docker.calls if c[0] == "compose"]
    assert composed == ["up", "stop", "up"]  # no restore: the database wasn't upgraded
    assert not docker.restarted


def test_a_version_that_never_reports_healthy_is_rolled_back(server, docker, clock):
    docker.health[NEW_ID] = "starting"
    u = make(server, docker, clock)
    ask(server)
    u.tick()
    job = updates.status(server / "data").job
    assert job.state == "rolled_back" and "didn't report healthy within 6 minutes" in job.message


def test_the_database_goes_back_with_the_version(server, docker, clock):
    docker.migrates = True
    docker.health[NEW_ID] = "unhealthy"
    u = make(server, docker, clock)
    ask(server)
    u.tick()
    job = updates.status(server / "data").job
    assert job.state == "rolled_back" and job.schema == "41"
    assert "put back from pre-upgrade-v41-to-v42-20261102-090000-from-2.2.0.db" in job.message
    assert schema(server / "data" / "officesentry.db") == 41
    restore = [c for c in docker.calls if c[:2] == ("compose", "run")][0]
    assert restore[-3:] == ("restore", "/data/backups/pre-upgrade-v41-to-v42-20261102-090000-from-2.2.0.db", "--force")
    assert restore[1:7] == ("run", "--rm", "--no-deps", "--pull", "never", "-T")
    assert [c[1] for c in docker.calls if c[0] == "compose"] == ["up", "stop", "run", "up"]


def test_compose_failing_to_start_it_is_rolled_back(server, docker, clock):
    docker.fail.add("up-new")
    u = make(server, docker, clock)
    ask(server)
    u.tick()
    job = updates.status(server / "data").job
    assert job.state == "rolled_back"
    said = "Back on 2.2.0: 2.4.0 didn't start: dependency failed to start: container officesentry-web-1 is unhealthy."
    assert job.message == said
    assert steps(job)["switch"] == "failed"


@pytest.mark.parametrize(
    ("output", "line"),
    [
        ("\x1b[?25hError: it went wrong\x1b[0m\r\n", "Error: it went wrong"),
        ("\x1b]0;docker\x07Starting\rStarted\n\n", "Started"),
        ("\x1b[1A\x1b[2K\x1b[31mno space left on device\x1b[39m", "no space left on device"),
        ("bell\x07 and\x00 nulls", "bell and nulls"),
        ("", "no output"),
    ],
)
def test_what_a_helper_said_is_plain_text(output, line):
    assert up.last_line(output) == line


def test_when_going_back_fails_too_the_page_has_the_manual_steps(server, docker, clock):
    docker.migrates = True
    docker.health[NEW_ID] = "unhealthy"
    u = make(server, docker, clock)
    for copy in (server / "data" / "backups").glob("*"):
        copy.unlink()
    original = docker.run

    def no_copy(image, cmd, **kw):  # the upgrade made no copy (it was deleted)
        result = original(image, cmd, **kw)
        for copy in (server / "data" / "backups").glob("*"):
            copy.unlink()
        return result

    docker.run = no_copy
    ask(server)
    u.tick()
    job = updates.status(server / "data").job
    assert job.state == "failed" and steps(job)["rollback"] == "failed"
    assert "copy made before (version 41) wasn't found" in job.message and "Rolling back an update" in job.message


# -- Cut off part way ---------------------------------------------------------------------------------------


def _cut_off(server, docker, clock, at_step: str):
    """An update that stopped (the server restarted) once ``at_step`` had begun."""
    u = make(server, docker, clock)
    ask(server)

    class Stop(BaseException):
        pass

    begin = u.begin

    def stop_at(key, detail=""):
        begin(key, detail)
        if key == at_step:
            raise Stop

    u.begin = stop_at
    with pytest.raises(Stop):
        u.tick()
    return updates.status(server / "data").job


def test_cut_off_before_the_switch_is_just_over(server, docker, clock):
    _cut_off(server, docker, clock, "download")
    u = make(server, docker, clock)
    u.resume()
    job = updates.status(server / "data").job
    assert job.state == "failed" and "before anything was installed" in job.message


def test_cut_off_during_the_health_check_finishes_when_healthy(server, docker, clock, monkeypatch):
    _cut_off(server, docker, clock, "health")
    u = make(server, docker, clock)
    with pytest.raises(Killed):
        u.resume()
    job = updates.status(server / "data").job
    assert job.state == "done" and steps(job)["health"] == "done" and job.restarted_updater == "pending"


def test_cut_off_during_the_health_check_goes_back_when_not(server, docker, clock):
    _cut_off(server, docker, clock, "health")
    docker.box["web"]["State"] = {"Running": False, "Status": "exited"}
    u = make(server, docker, clock)
    u.resume()
    job = updates.status(server / "data").job
    assert job.state == "rolled_back" and "cut off part way" in job.message
    assert env(server).endswith("OFFICESENTRY_VERSION=2.2.0\n")


def test_its_record_is_its_own_not_the_portals(server, docker, clock):
    """The copy in the data folder is for the portal to show; a portal that changed it changes nothing."""
    _cut_off(server, docker, clock, "health")
    published = server / "data" / "updates" / updates.STATUS_FILE
    data = json.loads(published.read_text(encoding="utf-8"))
    data["job"]["previous_image"] = "sha256:" + "6" * 64
    data["job"]["id"] = "0" * 16
    published.write_text(json.dumps(data), encoding="utf-8")
    u = make(server, docker, clock)
    assert u.job.previous_image == OLD_ID and u.job.id != "0" * 16


def test_main_runs_without_configuration(monkeypatch, tmp_path):
    """``python -m officesentry updater`` makes no secrets.json and opens no configuration."""
    from officesentry import __main__

    ran = []
    monkeypatch.setattr(up, "main", lambda: ran.append(True))
    monkeypatch.setattr(__main__, "load_config", lambda: pytest.fail("the updater loaded the configuration"))
    __main__.main(["updater"])
    assert ran == [True]


# -- The shared folder is the portal's -----------------------------------------------------------------------
#
# The updater runs as root and the portal owns the data volume, so whatever the updater does in
# <data>/updates, the portal may have put a link, a pipe or a file there first, or swap one in at any moment.


def _fd_path(fd: int) -> str:
    return os.readlink(f"/proc/self/fd/{fd}")


def test_the_shared_folder_is_handed_to_the_portal_by_the_folder_made(tmp_path, monkeypatch):
    """prepare() makes <data>/updates and gives it the data folder's owner through the folder it opened, so a
    link swapped in after mkdir (to the compose folder's .env, or the Docker socket) is never handed over."""
    data, victim = tmp_path / "data", tmp_path / "project" / ".env"
    data.mkdir()
    victim.parent.mkdir()
    victim.write_text("OFFICESENTRY_VERSION=2.2.0\n", encoding="utf-8")
    folder = updates.folder(data)
    owned = []
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: owned.append(_fd_path(fd)))
    monkeypatch.setattr(os, "chown", lambda *a, **k: pytest.fail("chown by name"))

    up.Updater(None, data, project_dir=victim.parent, state_dir=tmp_path / "state").prepare()
    assert owned == [str(folder)]

    shutil.rmtree(folder)
    owned.clear()
    real_mkdir = Path.mkdir

    def racing_mkdir(self, *args, **kwargs):
        real_mkdir(self, *args, **kwargs)
        if self == folder:  # what a compromised portal does the moment the folder appears
            os.rename(self, data / "moved-aside")
            os.symlink(victim, self)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    with pytest.raises(SystemExit, match="isn't a folder"):
        up.Updater(None, data, project_dir=victim.parent, state_dir=tmp_path / "state").prepare()
    assert owned == []


def test_a_link_in_the_shared_folders_place_is_never_written_through(server, docker, clock, caplog):
    """Swapped for a link after start-up, the folder isn't written into (here the compose folder would get
    root's files); the updater's own record still is, and the update or going back carries on."""
    u = make(server, docker, clock)
    folder = updates.folder(server / "data")
    shutil.rmtree(folder)
    os.symlink(server / "project", folder)
    u.save()
    assert sorted(p.name for p in (server / "project").iterdir()) == [".env", "Caddyfile", "docker-compose.yml"]
    assert json.loads((server / "state" / updates.STATUS_FILE).read_text(encoding="utf-8"))["seen_at"]
    assert "couldn't write its status for the portal" in caplog.text


def test_a_full_data_volume_doesnt_stop_it_going_back(server, docker, clock, monkeypatch):
    """The portal's copy of the status failing to write (a full data volume) once the new version is running
    must not leave it there: going back still happens, and the updater's own record says so."""
    docker.health[NEW_ID] = "unhealthy"
    u = make(server, docker, clock)
    ask(server)
    real = updates.write_json

    def full(path, value, mode=0o644, dir_fd=None):
        if dir_fd is not None and any(c[:2] == ("compose", "up") for c in docker.calls):
            raise OSError(28, "No space left on device")
        return real(path, value, mode, dir_fd)

    monkeypatch.setattr(updates, "write_json", full)
    u.tick()
    job = updates.Job.load(json.loads((server / "state" / updates.STATUS_FILE).read_text(encoding="utf-8"))["job"])
    assert job.state == "rolled_back"
    assert env(server).endswith("OFFICESENTRY_VERSION=2.2.0\n")
    assert docker.box["web"]["Image"] == OLD_ID


def test_a_failure_after_the_switch_never_says_nothing_changed(server, docker, clock, monkeypatch):
    """Going back itself failing unexpectedly is reported as part way, with the manual steps."""
    docker.health[NEW_ID] = "unhealthy"
    u = make(server, docker, clock)
    ask(server)
    monkeypatch.setattr(up.Updater, "rollback", lambda self, install, reason: (_ for _ in ()).throw(OSError("disk")))
    u.tick()
    job = updates.status(server / "data").job
    assert job.state == "failed" and "part way" in job.message and "Rolling back an update" in job.message
    assert "before anything was changed" not in job.message


@pytest.mark.parametrize("planted", ["fifo", "nested", "folder"])
def test_a_request_the_portal_made_odd_is_ignored(server, docker, clock, planted):
    """A named pipe (which would block the updater in open()), JSON nested deeper than Python parses, or a
    folder: none is a request, and the updater keeps checking in."""
    u = make(server, docker, clock)
    request = updates.folder(server / "data") / updates.REQUEST_FILE
    if planted == "fifo":
        os.mkfifo(request)
    elif planted == "nested":
        request.write_text('{"format": 1, "a": ' + "[" * 100_000 + "]" * 100_000 + "}", encoding="utf-8")
    else:
        request.mkdir()
    assert updates.read_request(server / "data") is None
    u.tick()
    assert u.job is None
