"""Microsoft Graph client: app-only, read-only, throttling-aware.

Design rules (each one fixes a v1 bug):

* Paths are relative to the API version, e.g. ``client.get_all("/users")``.
  Passing ``"/v1.0/..."`` or ``"/beta/..."`` raises instead of silently
  requesting ``/v1.0/v1.0/...``. Use ``version="beta"`` for beta endpoints.
* Absolute URLs (``@odata.nextLink``) must point at the client's own cloud's
  Graph host (graph.microsoft.com, graph.microsoft.us...; see ``clouds.py``),
  so a bearer token is never sent anywhere else.
* 429 and 5xx are retried, honouring ``Retry-After`` (seconds or an HTTP
  date); without it the exponential backoff is jittered so retries from
  parallel callers don't land together. :func:`is_transient` tells callers
  when a failure (timeout, 5xx) might succeed as a smaller query.
* Paging never stops silently: ``PagedResult.truncated`` says if it did.
* Only GET is exposed. Office Sentry never changes a tenant.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import requests

from . import clouds

log = logging.getLogger(__name__)

GRAPH_ROOT = clouds.get(clouds.DEFAULT).graph  # commercial; a client for another cloud uses its own
VERSIONS = ("v1.0", "beta")
RETRY_STATUSES = {429, 500, 502, 503, 504}
# Failures that a smaller or narrower query may avoid: no response (status 0)
# or a server error. 429 is excluded: more, smaller queries only throttle harder.
TRANSIENT_STATUSES = {0, 500, 502, 503, 504}
MAX_RETRY_AFTER = 300

# Error codes Graph returns when a feature needs a licence the tenant lacks
# (for example signInActivity or sign-in logs without Entra ID P1).
LICENSE_ERROR_CODES = {
    "Authentication_RequestFromNonPremiumTenantOrB2CTenant",
    "Authentication_RequestFromUnsupportedUserRole",
    "AadPremiumLicenseRequired",
}


class GraphError(Exception):
    def __init__(self, status: int, code: str, message: str, request_id: str = ""):
        super().__init__(f"Graph {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id


class LicenseRequired(GraphError):
    """The tenant's licences don't include the feature this call needs."""


class PermissionMissing(GraphError):
    """The app lacks a permission, or admin consent was not granted."""


def is_transient(exc: Exception) -> bool:
    """True for a Graph timeout or server error that survived the client's retries."""
    return isinstance(exc, GraphError) and exc.status in TRANSIENT_STATUSES


@dataclass
class PagedResult:
    items: list = field(default_factory=list)
    truncated: bool = False
    pages: int = 0


def _parse_error(resp: requests.Response) -> GraphError:
    code, message, request_id = "unknown", resp.text[:300], ""
    try:
        err = resp.json().get("error", {})
        code = err.get("code", code)
        message = err.get("message", message)
        request_id = (err.get("innerError") or {}).get("request-id", "")
    except ValueError:
        pass
    if code in LICENSE_ERROR_CODES or "premium license" in message.lower():
        cls = LicenseRequired
    elif resp.status_code == 403 or code == "Authorization_RequestDenied":
        cls = PermissionMissing
    else:
        cls = GraphError
    return cls(resp.status_code, code, message, request_id)


class GraphClient:
    def __init__(
        self,
        token_provider: Callable[[], str],
        *,
        session: requests.Session | None = None,
        max_retries: int = 6,
        timeout: int = 60,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
        cloud: str | None = None,
    ):
        self.cloud = clouds.get(cloud)
        self.root = self.cloud.graph
        self._token_provider = token_provider
        self._session = session or requests.Session()
        self._max_retries = max_retries
        self._timeout = timeout
        self._sleep = sleep
        self._jitter = jitter

    @classmethod
    def from_certificate(cls, tenant_ref: str, client_id: str, key_pem: str,
                         thumbprint: str, cloud: str | None = None, **kwargs) -> GraphClient:
        from .msauth import TokenError, shared_certificate_token_provider

        root = clouds.get(cloud).graph
        provider = shared_certificate_token_provider(tenant_ref, client_id, key_pem, thumbprint, root, cloud)

        def token() -> str:
            try:
                return provider()
            except TokenError as exc:
                raise GraphError(401, exc.code, exc.message) from exc

        return cls(token, cloud=cloud, **kwargs)

    # -- URL handling ------------------------------------------------------

    @staticmethod
    def build_url(path: str, version: str = "v1.0", root: str = GRAPH_ROOT) -> str:
        """The full URL for a version-relative path, on ``root`` (the client's cloud's Graph host)."""
        if version not in VERSIONS:
            raise ValueError(f"unknown Graph version {version!r}")
        if path.startswith("https://"):
            if not path.startswith(root + "/"):
                raise ValueError(f"refusing to send a Graph token to {path.split('/')[2]}")
            return path
        if not path.startswith("/"):
            raise ValueError(f"Graph path must start with '/': {path!r}")
        if path.split("/")[1] in VERSIONS:
            raise ValueError(
                f"Graph path {path!r} includes the API version; pass version= instead"
            )
        return f"{root}/{version}{path}"

    # -- Requests ----------------------------------------------------------

    def _get(self, url: str, params: dict | None, headers: dict | None) -> dict:
        attempt = 0
        while True:
            hdrs = {"Authorization": f"Bearer {self._token_provider()}", "Accept": "application/json"}
            if headers:
                hdrs.update(headers)
            try:
                resp = self._session.get(url, params=params, headers=hdrs, timeout=self._timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                if attempt >= self._max_retries:
                    raise GraphError(0, "network_error", str(exc)) from exc
                self._sleep(self._backoff(attempt))
                attempt += 1
                continue

            if resp.status_code in RETRY_STATUSES and attempt < self._max_retries:
                delay = self._retry_after(resp, attempt)
                log.info("Graph %s on %s; retrying in %ss", resp.status_code, url, delay)
                self._sleep(delay)
                attempt += 1
                continue
            if not resp.ok:
                raise _parse_error(resp)
            return resp.json()

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with "equal jitter": between half and all of 2**attempt (max 60s)."""
        base = float(min(2 ** attempt, 60))
        return base / 2 + self._jitter() * base / 2

    def _retry_after(self, resp: requests.Response, attempt: int) -> float:
        """Wait what Microsoft asks (plus up to a second of jitter), else back off."""
        value = resp.headers.get("Retry-After", "").strip()
        seconds: float | None = None
        if value.isdigit():
            seconds = float(value)
        elif value:
            try:
                seconds = (parsedate_to_datetime(value) - datetime.now(UTC)).total_seconds()
            except (TypeError, ValueError, IndexError):
                seconds = None
        if seconds is None:
            return self._backoff(attempt)
        return min(max(seconds, 0.0), MAX_RETRY_AFTER) + self._jitter()

    def get(self, path: str, params: dict | None = None, *, version: str = "v1.0",
            headers: dict | None = None) -> dict:
        return self._get(self.build_url(path, version, self.root), params, headers)

    def get_all(self, path: str, params: dict | None = None, *, version: str = "v1.0",
                headers: dict | None = None, max_pages: int = 1000) -> PagedResult:
        """Follow ``@odata.nextLink`` and return every item of ``value``."""
        result = PagedResult()
        url: str | None = self.build_url(path, version, self.root)
        first = True
        while url:
            if result.pages >= max_pages:
                result.truncated = True
                log.warning("Stopped paging %s after %s pages", path, max_pages)
                break
            data = self._get(self.build_url(url, version, self.root), params if first else None, headers)
            first = False
            result.items.extend(data.get("value", []))
            result.pages += 1
            url = data.get("@odata.nextLink")
        return result
