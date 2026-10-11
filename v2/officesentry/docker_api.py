"""A small client for the Docker Engine API on its Unix socket, for the updater (``updater.py``) alone.

Only what installing a release needs: read containers and images, pull an image by digest, and run a
short-lived helper container (cosign, or the Docker CLI for ``docker compose``) to the end. The portal and
the worker never import this; only the updater's container has the socket.
"""

from __future__ import annotations

import http.client
import json
import socket
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

SOCKET = "/var/run/docker.sock"
OUTPUT_TAIL = 400  # lines of a helper's output kept


class DockerError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("docker", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def split_reference(ref: str) -> tuple[str, str]:
    """``name[:tag]@sha256:...`` -> (name, digest). Refuses a reference without a digest: the updater only
    ever pulls exactly what it checked."""
    name, sep, digest = ref.partition("@")
    if not sep or not digest.startswith("sha256:") or len(digest) != 71:
        raise ValueError(f"not pinned by digest: {ref}")
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name = name[: len(name) - len(last)] + last.split(":", 1)[0]
    return name, digest


def _quote(ref: str) -> str:
    return urllib.parse.quote(ref, safe="/:@")


def _message(data: bytes) -> str:
    try:
        return str(json.loads(data).get("message") or "")[:500]
    except (ValueError, AttributeError):
        return data.decode("utf-8", "replace")[:500]


@dataclass
class Result:
    code: int
    output: str


class Docker:
    def __init__(self, socket_path: str = SOCKET, timeout: float = 60.0):
        self.socket_path = socket_path
        self.timeout = timeout

    def _send(self, method: str, path: str, query=None, body=None, timeout: float | None = None):
        conn = _UnixConnection(self.socket_path, timeout or self.timeout)
        url = path + ("?" + urllib.parse.urlencode(query, doseq=True) if query else "")
        headers = {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, url, body=data, headers=headers)
        return conn, conn.getresponse()

    def call(self, method: str, path: str, query=None, body=None, timeout: float | None = None):
        conn, response = self._send(method, path, query, body, timeout)
        try:
            data = response.read()
        finally:
            conn.close()
        if response.status >= 400:
            raise DockerError(response.status, _message(data) or f"Docker answered {response.status}")
        if not data:
            return None
        if "json" in (response.getheader("Content-Type") or ""):
            return json.loads(data)
        return data

    def get(self, path: str, **query):
        return self.call("GET", path, query or None)

    # -- Reading ------------------------------------------------------------------------------------------------

    def ping(self) -> bool:
        try:
            return self.call("GET", "/_ping") in (b"OK", "OK")
        except (OSError, DockerError, http.client.HTTPException):
            return False

    def container(self, ref: str) -> dict | None:
        try:
            return self.get(f"/containers/{_quote(ref)}/json")
        except DockerError as exc:
            if exc.status == 404:
                return None
            raise

    def containers(self, labels: dict[str, str]) -> list[dict]:
        """Containers (running or not) carrying every one of ``labels``."""
        filters = json.dumps({"label": [f"{k}={v}" for k, v in labels.items()]})
        return self.get("/containers/json", all="1", filters=filters) or []

    def image(self, ref: str) -> dict | None:
        try:
            return self.get(f"/images/{_quote(ref)}/json")
        except DockerError as exc:
            if exc.status == 404:
                return None
            raise

    # -- Changing -----------------------------------------------------------------------------------------------

    def pull(self, ref: str, progress: Callable[[str], None] | None = None, timeout: float = 1800) -> None:
        """Pull ``name@sha256:...``. Docker checks every layer against the digest, so what arrives is exactly
        what the digest names."""
        name, digest = split_reference(ref)
        conn, response = self._send("POST", "/images/create", {"fromImage": name, "tag": digest}, timeout=timeout)
        try:
            if response.status >= 400:
                raise DockerError(response.status, _message(response.read()) or "the pull was refused")
            for line in response:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("error") or event.get("errorDetail"):
                    detail = event.get("errorDetail") or {}
                    raise DockerError(500, str(event.get("error") or detail.get("message") or "the pull failed"))
                if progress and event.get("status"):
                    progress(str(event["status"]))
        finally:
            conn.close()

    def ensure(self, ref: str, timeout: float = 900) -> None:
        """Pull a pinned helper image unless it's here already."""
        if self.image(ref) is None:
            self.pull(ref, timeout=timeout)

    def tag(self, image_id: str, repo: str, tag: str) -> None:
        self.call("POST", f"/images/{_quote(image_id)}/tag", {"repo": repo, "tag": tag})

    def _create(
        self,
        image: str,
        cmd: list[str],
        *,
        binds=(),
        env=(),
        workdir=None,
        network=None,
        user=None,
        tmpfs=None,
        read_only=False,
        cap_drop=None,
        labels=None,
        auto_remove=False,
    ) -> str:
        host = {"Binds": list(binds), "AutoRemove": auto_remove, "SecurityOpt": ["no-new-privileges:true"]}
        if network:
            host["NetworkMode"] = network
        if tmpfs:
            host["Tmpfs"] = tmpfs
        if read_only:
            host["ReadonlyRootfs"] = True
        if cap_drop:
            host["CapDrop"] = list(cap_drop)
        config = {
            "Image": image,
            "Cmd": list(cmd),
            "Env": list(env),
            "Tty": True,  # one plain stream of output, no multiplexing headers to take apart
            "Labels": dict(labels or {}),
            "HostConfig": host,
        }
        if workdir:
            config["WorkingDir"] = workdir
        if user:
            config["User"] = user
        created = self.call("POST", "/containers/create", body=config)
        return created["Id"]

    def run(self, image: str, cmd: list[str], *, timeout: float = 600, **options) -> Result:
        """Run a helper container to the end and return its exit code and the end of its output. It's removed
        afterwards, and killed first if it outlives ``timeout``."""
        cid = self._create(image, cmd, **options)
        try:
            self.call("POST", f"/containers/{cid}/start")
            try:
                waited = self.call("POST", f"/containers/{cid}/wait", timeout=timeout)
                code = int((waited or {}).get("StatusCode", -1))
            except TimeoutError:
                self._quietly("POST", f"/containers/{cid}/kill")
                code = -1
            logs = self.call("GET", f"/containers/{cid}/logs", {"stdout": "1", "stderr": "1", "tail": OUTPUT_TAIL})
            output = logs.decode("utf-8", "replace") if isinstance(logs, bytes) else str(logs or "")
            if code == -1:
                output += "\n(stopped: it took too long)"
            return Result(code, output.replace("\r\n", "\n"))
        finally:
            self._quietly("DELETE", f"/containers/{cid}", {"force": "1"})

    def start(self, image: str, cmd: list[str], **options) -> str:
        """Start a helper container and leave it: Docker removes it when it ends. For the one step that
        restarts the updater itself, which can't wait for it."""
        cid = self._create(image, cmd, auto_remove=True, **options)
        self.call("POST", f"/containers/{cid}/start")
        return cid

    def _quietly(self, method: str, path: str, query=None) -> None:
        try:
            self.call(method, path, query)
        except (OSError, DockerError, http.client.HTTPException):
            pass
