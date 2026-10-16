"""The updater's Docker client (``docker_api.py``) against a small fake Docker Engine on a Unix socket."""

from __future__ import annotations

import json
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from officesentry.docker_api import Docker, DockerError, split_reference

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Docker's socket is a Unix socket")

DIGEST = "sha256:" + "d" * 64


class Engine(BaseHTTPRequestHandler):
    """Answers the few calls the updater makes, and remembers them."""

    seen: list = []
    wait_seconds = 0.0

    def address_string(self) -> str:  # a Unix socket has no client address
        return "docker"

    def log_message(self, *args) -> None:
        pass

    def _send(self, status: int, body=b"", content_type="application/json") -> None:
        data = json.dumps(body).encode() if not isinstance(body, bytes) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self) -> None:
        url = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        self.seen.append((self.command, url.path, query, body))
        route = (self.command, url.path)
        if route == ("GET", "/_ping"):
            return self._send(200, b"OK", "text/plain")
        if route == ("GET", "/containers/web-id/json"):
            return self._send(200, {"Id": "web-id", "Image": "sha256:1"})
        if route == ("GET", "/containers/broken/json"):
            return self._send(500, {"message": "the daemon is unwell"})
        if route == ("GET", "/containers/json"):
            return self._send(200, [{"Id": "web-id", "Filters": query.get("filters")}])
        if self.command == "GET" and url.path.startswith("/containers/") and url.path.endswith("/json"):
            return self._send(404, {"message": "No such container"})
        if route == ("POST", "/images/create"):
            if query.get("tag") != DIGEST:
                lines = [
                    {"status": "Pulling"},
                    {"error": "manifest unknown", "errorDetail": {"message": "manifest unknown"}},
                ]
            else:
                lines = [{"status": "Pulling from jackd99/officesentry"}, {"status": f"Digest: {DIGEST}"}]
            return self._send(200, b"".join(json.dumps(line).encode() + b"\r\n" for line in lines))
        if route == ("POST", "/containers/create"):
            return self._send(201, {"Id": "helper-id", "Warnings": []})
        if route == ("POST", "/containers/helper-id/wait"):
            time.sleep(self.wait_seconds)
            return self._send(200, {"StatusCode": 3})
        if route == ("GET", "/containers/helper-id/logs"):
            return self._send(200, b"pulling\r\nError: it went wrong\r\n", "application/vnd.docker.raw-stream")
        if self.command in ("POST", "DELETE"):
            return self._send(204)
        return self._send(404, {"message": "page not found"})

    do_GET = do_POST = do_DELETE = _handle


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        """A client that hung up first is no error: after a 204's headers it has the whole answer and may close
        before the fake writes the empty body, and the helper that takes too long stops waiting for it. Those
        printed a traceback into the test run's log now and then; anything else still does."""
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


@pytest.fixture
def engine():
    folder = Path(tempfile.mkdtemp(prefix="dk"))  # a short path: Unix socket paths are limited to ~100 bytes
    path = folder / "d.sock"
    Engine.seen = []
    Engine.wait_seconds = 0.0
    server = Server(str(path), Engine)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield Docker(str(path), timeout=5)
    server.shutdown()
    server.server_close()
    shutil.rmtree(folder, ignore_errors=True)


def test_reading(engine):
    assert engine.ping()
    assert engine.container("web-id")["Image"] == "sha256:1"
    assert engine.container("gone") is None
    with pytest.raises(DockerError, match="the daemon is unwell") as caught:
        engine.container("broken")
    assert caught.value.status == 500
    found = engine.containers({"com.docker.compose.project": "officesentry", "com.docker.compose.service": "web"})
    filters = json.loads(found[0]["Filters"])
    assert filters == {"label": ["com.docker.compose.project=officesentry", "com.docker.compose.service=web"]}
    assert Engine.seen[-1][2]["all"] == "1"


def test_no_engine_is_no_ping(tmp_path):
    assert not Docker(str(tmp_path / "missing.sock")).ping()


def test_pull_by_digest_only(engine):
    said = []
    engine.pull(f"ghcr.io/jackd99/officesentry:2.4.0@{DIGEST}", progress=said.append)
    assert said == ["Pulling from jackd99/officesentry", f"Digest: {DIGEST}"]
    assert Engine.seen[-1][2] == {"fromImage": "ghcr.io/jackd99/officesentry", "tag": DIGEST}
    with pytest.raises(DockerError, match="manifest unknown"):
        engine.pull("ghcr.io/jackd99/officesentry@sha256:" + "0" * 64)
    with pytest.raises(ValueError, match="not pinned by digest"):
        engine.pull("ghcr.io/jackd99/officesentry:2.4.0")


@pytest.mark.parametrize(
    ("ref", "parts"),
    [
        (f"ghcr.io/jackd99/officesentry@{DIGEST}", ("ghcr.io/jackd99/officesentry", DIGEST)),
        (f"ghcr.io/jackd99/officesentry:2.4.0@{DIGEST}", ("ghcr.io/jackd99/officesentry", DIGEST)),
        (f"localhost:5000/officesentry:x@{DIGEST}", ("localhost:5000/officesentry", DIGEST)),
        (f"docker:29.9.0-cli@{DIGEST}", ("docker", DIGEST)),
    ],
)
def test_references(ref, parts):
    assert split_reference(ref) == parts


@pytest.mark.parametrize("ref", ["docker:cli", "docker@sha256:abc", "docker@md5:" + "0" * 64])
def test_references_without_a_digest_are_refused(ref):
    with pytest.raises(ValueError):
        split_reference(ref)


def test_a_helper_runs_locked_down_and_is_removed(engine):
    result = engine.run(
        "docker:cli",
        ["docker", "compose", "up"],
        binds=["/var/run/docker.sock:/var/run/docker.sock"],
        network="none",
        read_only=True,
        cap_drop=["ALL"],
        labels={"officesentry.updater": "compose"},
    )
    assert result.code == 3 and result.output == "pulling\nError: it went wrong\n"
    calls = [(method, path) for method, path, _, _ in Engine.seen]
    assert calls == [
        ("POST", "/containers/create"),
        ("POST", "/containers/helper-id/start"),
        ("POST", "/containers/helper-id/wait"),
        ("GET", "/containers/helper-id/logs"),
        ("DELETE", "/containers/helper-id"),
    ]
    created = Engine.seen[0][3]
    assert created["Image"] == "docker:cli" and created["Cmd"] == ["docker", "compose", "up"] and created["Tty"]
    host = created["HostConfig"]
    assert host["SecurityOpt"] == ["no-new-privileges:true"] and host["NetworkMode"] == "none"
    assert host["ReadonlyRootfs"] and host["CapDrop"] == ["ALL"] and not host["AutoRemove"]
    assert Engine.seen[-1][2] == {"force": "1"}


def test_a_helper_that_takes_too_long_is_killed(engine):
    Engine.wait_seconds = 2
    result = engine.run("docker:cli", ["sleep", "1000"], timeout=0.5)
    assert result.code == -1 and "took too long" in result.output
    calls = [(method, path) for method, path, _, _ in Engine.seen]
    assert ("POST", "/containers/helper-id/kill") in calls and calls[-1] == ("DELETE", "/containers/helper-id")


def test_a_detached_helper_removes_itself(engine):
    assert engine.start("docker:cli", ["docker", "compose", "up", "-d", "updater"]) == "helper-id"
    assert Engine.seen[0][3]["HostConfig"]["AutoRemove"] is True
    assert [(m, p) for m, p, _, _ in Engine.seen] == [
        ("POST", "/containers/create"),
        ("POST", "/containers/helper-id/start"),
    ]
