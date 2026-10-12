"""The Office 365 Management Activity feed: how the audit log add-on reads a client's unified audit log.

Signed in as the add-on's own app (``app_connections.kind = 'audit'``), which holds one permission,
ActivityFeed.Read on the Office 365 Management APIs, and never the shared app's. Base address
``<management host>/api/v1.0/<tenant GUID>/activity/feed`` (``clouds.Cloud.management``).

Reading is GET only:

* ``subscriptions()``: which content types the feed is on for (``GET subscriptions/list``). It's also how a
  client's approval is confirmed: Microsoft only answers once the client has approved the add-on.
* ``content()``: the batches of audit records Microsoft has ready (``GET subscriptions/content``), at most 24
  hours a call and only within its last 7 days, following ``NextPageUri``.
* ``blob()``: one batch of records (``GET <contentUri>``).

The two calls that aren't reads, and the only ones in Office Sentry that aren't reads of a client's Microsoft
365, are here and nowhere else (``tests/test_no_writes.py`` names this file):

* ``start()`` (``POST subscriptions/start``) asks Microsoft to start making the client's new audit records
  available to this app. Sent once per content type, after the client has approved the add-on and an admin
  asked for it on (``addon.confirm``), and when an admin switches it on again. Never by a collection.
* ``stop()`` (``POST subscriptions/stop``) ends that, when an admin switches the add-on off or deletes the
  client.

Neither changes anything in the client's tenant: no user, mailbox, file, policy or setting, and the audit log
itself is untouched. The rules match ``graph.py``: a token only ever goes to the feed's own host for this
tenant (a ``contentUri`` or ``NextPageUri`` anywhere else is refused), 429 and 5xx are retried after
``Retry-After``, and every request is counted in ``telemetry`` and, with the worker's ``limiter``, paced
against the tenant's feed limit (``endpoints.MANAGEMENT``).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import requests

from .. import clouds, endpoints
from ..graph import USER_AGENT
from ..ratelimit import Limiter, Telemetry, claims, to_pause

log = logging.getLogger(__name__)

# What the add-on reads: SharePoint and OneDrive, and Exchange. Microsoft's Entra ID records duplicate the
# directory audit log the shared app already keeps, and Audit.General (Teams and the rest) waits for Teams reports.
CONTENT_TYPES = ("Audit.SharePoint", "Audit.Exchange")
CONTENT_LABELS = {"Audit.SharePoint": "SharePoint and OneDrive", "Audit.Exchange": "Exchange (mailboxes)"}
WINDOW = timedelta(hours=24)  # the longest span one content listing may ask for
KEPT_FOR = timedelta(days=7)  # how far back Microsoft lists content
MAX_PAGES = 200  # NextPageUri pages followed for one window
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER = 300
GUID = re.compile(r"^[0-9a-f]{8}-([0-9a-f]{4}-){3}[0-9a-f]{12}$")

# Microsoft's feed error codes (``{"error": {"code": "AF…"}}``) that need more than "it failed".
NOT_APPROVED = {"AF10001"}  # the token doesn't carry ActivityFeed.Read: the client hasn't approved the add-on
NOT_STARTED = {"AF20022", "AF20023"}  # no subscription for that content type, or a tenant admin disabled it
EXPIRED = {"AF20050", "AF20051"}  # the batch is gone (Microsoft keeps them 7 days)


class FeedError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"Audit feed {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class FeedAccessDenied(FeedError):
    """The add-on can't sign in to this tenant or read its feed: the client hasn't approved it, or removed it."""


class FeedNotStarted(FeedError):
    """The feed isn't on for that content type (never started, stopped, or disabled by the client's admin)."""


class FeedExpired(FeedError):
    """A batch of records that Microsoft no longer keeps."""


def _parse_error(resp: requests.Response) -> FeedError:
    code, message = str(resp.status_code), resp.text[:300] or resp.reason or ""
    try:
        err = resp.json().get("error") or {}
        code = str(err.get("code") or code)
        message = str(err.get("message") or message)
    except (ValueError, AttributeError):
        pass
    if code in NOT_STARTED:
        return FeedNotStarted(resp.status_code, code, message)
    if code in EXPIRED:
        return FeedExpired(resp.status_code, code, message)
    if resp.status_code in (401, 403) or code in NOT_APPROVED:
        return FeedAccessDenied(resp.status_code, code, message)
    return FeedError(resp.status_code, code, message)


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


class FeedClient:
    """One client tenant's feed. ``tenant`` must be its directory (tenant) ID: Microsoft refuses a domain."""

    def __init__(
        self,
        tenant: str,
        token_provider: Callable[[], str],
        *,
        cloud: str | None = None,
        publisher: str | None = None,
        session: requests.Session | None = None,
        max_retries: int = 4,
        timeout: int = 60,
        sleep: Callable[[float], None] = time.sleep,
        limiter: Limiter | None = None,
        telemetry: Telemetry | None = None,
        app: str = "",
    ):
        self.cloud = clouds.get(cloud)
        if not self.cloud.management:
            raise ValueError(f"Microsoft offers no audit log feed in {self.cloud.label}")
        tenant = (tenant or "").lower()
        if not GUID.match(tenant):
            raise ValueError("the audit log feed needs the client's tenant ID, not a domain")
        self.tenant = tenant
        self.root = f"{self.cloud.management}/api/v1.0/{tenant}/activity/feed"
        # The MSP's own tenant ID, which Microsoft asks for so this app's requests share one quota.
        self.publisher = publisher if publisher and GUID.match(publisher.lower()) else None
        self.limiter = limiter
        self.telemetry = telemetry or Telemetry()
        self.app = app
        self._token_provider = token_provider
        self._session = session or requests.Session()
        self._max_retries = max_retries
        self._timeout = timeout
        self._sleep = sleep

    def _check(self, url: str) -> None:
        """Refuse to send this tenant's token anywhere but its own feed on the cloud's feed host."""
        parts = urlsplit(url)
        here = f"{parts.scheme}://{parts.netloc}".lower() == self.cloud.management
        mine = re.match(rf"/api/v1(\.0)?/{self.tenant}/activity/feed/", parts.path, re.IGNORECASE)
        if not (here and mine):
            raise ValueError(f"refusing to send an audit feed token to {parts.netloc}{parts.path[:60]}")

    def _params(self, params: dict | None) -> dict:
        out = dict(params or {})
        if self.publisher:
            out["PublisherIdentifier"] = self.publisher
        return out

    def _send(self, method: str, url: str, params: dict | None = None) -> requests.Response:
        self._check(url)
        endpoint = endpoints.management(url)
        key = endpoints.management_key(url)
        taken = claims(endpoint, self.app, self.tenant, graph=False) if self.limiter else []
        attempt = 0
        while True:
            waited = self.limiter.acquire(taken) if self.limiter else 0.0
            headers = {"Authorization": f"Bearer {self._token_provider()}", "User-Agent": USER_AGENT}
            try:
                if method == "POST":
                    resp = self._session.post(url, params=params, headers=headers, timeout=self._timeout)
                else:
                    resp = self._session.get(url, params=params, headers=headers, timeout=self._timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                retry = attempt < self._max_retries
                delay = float(min(2**attempt, 60)) if retry else 0.0
                self.telemetry.request(key, endpoint, status=0, limiter_wait=waited, retry_wait=delay)
                if not retry:
                    raise FeedError(0, "network_error", str(exc)) from exc
                self._sleep(delay)
                attempt += 1
                continue
            retry = resp.status_code in RETRY_STATUSES and attempt < self._max_retries
            delay = 0.0
            if retry:
                value = resp.headers.get("Retry-After", "").strip()
                delay = min(float(value), MAX_RETRY_AFTER) if value.isdigit() else float(min(2**attempt, 60))
            self.telemetry.request(
                key, endpoint, status=resp.status_code, limiter_wait=waited, retry_wait=delay, headers=resp.headers
            )
            if retry:
                log.info("Audit feed %s on %s; retrying in %ss", resp.status_code, key, delay)
                if resp.status_code == 429 and self.limiter:
                    self.limiter.pause(to_pause(taken), delay)
                self._sleep(delay)
                attempt += 1
                continue
            if not resp.ok:
                raise _parse_error(resp)
            return resp

    @staticmethod
    def _json(resp: requests.Response):
        try:
            return resp.json() if resp.content else None
        except ValueError as exc:
            raise FeedError(resp.status_code, "bad_json", "Microsoft's answer wasn't JSON") from exc

    # -- Reading (GET) ------------------------------------------------------------------------------

    def subscriptions(self) -> dict[str, str]:
        """Each content type the feed knows for this tenant -> its status ("enabled" or "disabled")."""
        data = self._json(self._send("GET", f"{self.root}/subscriptions/list", self._params(None)))
        return {
            str(s.get("contentType")): str(s.get("status") or "").lower()
            for s in data or []
            if isinstance(s, dict) and s.get("contentType")
        }

    def content(self, content_type: str, start: datetime, end: datetime) -> tuple[list[dict], bool]:
        """The batches Microsoft made available from ``start`` up to ``end`` (at most a day apart), and whether
        there were more pages than ``MAX_PAGES``."""
        if end - start > WINDOW:
            raise ValueError("the audit feed lists at most 24 hours at a time")
        url: str | None = f"{self.root}/subscriptions/content"
        params: dict | None = self._params(
            {"contentType": content_type, "startTime": _stamp(start), "endTime": _stamp(end)}
        )
        found: list[dict] = []
        pages = 0
        while url:
            if pages >= MAX_PAGES:
                return found, True
            resp = self._send("GET", url, params)
            data = self._json(resp)
            found.extend(b for b in data or [] if isinstance(b, dict) and b.get("contentUri"))
            pages += 1
            url = resp.headers.get("NextPageUri") or None
            params = None  # the next page's address carries every parameter
        return found, False

    def blob(self, uri: str) -> list[dict]:
        """One batch of audit records."""
        data = self._json(self._send("GET", uri, self._params(None)))
        return [r for r in data or [] if isinstance(r, dict)]

    # -- The two calls that aren't reads (POST) ------------------------------------------------------

    def start(self, content_type: str) -> str:
        """Start the feed for one content type. Returns the status Microsoft gives it ("enabled")."""
        if content_type not in CONTENT_TYPES:
            raise ValueError(f"the add-on doesn't read {content_type}")
        resp = self._send("POST", f"{self.root}/subscriptions/start", self._params({"contentType": content_type}))
        data = self._json(resp)
        return str((data or {}).get("status") or "enabled").lower() if isinstance(data, dict) else "enabled"

    def stop(self, content_type: str) -> None:
        """Stop the feed for one content type."""
        if content_type not in CONTENT_TYPES:
            raise ValueError(f"the add-on doesn't read {content_type}")
        self._send("POST", f"{self.root}/subscriptions/stop", self._params({"contentType": content_type}))


def client(
    tenant_id: str,
    connection,
    key_pem: str,
    *,
    shared: bool = False,
    **kwargs,
) -> FeedClient:
    """A feed client signing in as the add-on app ``connection`` with its private key ``key_pem``. ``shared``
    reuses token providers across runs (the worker); checks and admin actions sign in afresh."""
    from ..msauth import TokenError, certificate_token_provider, shared_certificate_token_provider

    cloud = clouds.of(connection)
    make = shared_certificate_token_provider if shared else certificate_token_provider
    provider = make(tenant_id, connection["client_id"], key_pem, connection["thumbprint"], cloud.management, cloud.key)

    def token() -> str:
        try:
            return provider()
        except TokenError as exc:
            if exc.code == "network_error":
                raise FeedError(0, exc.code, exc.message) from exc
            raise FeedAccessDenied(401, exc.code, exc.message) from exc

    try:
        home = connection["home_tenant"]
    except (KeyError, IndexError):
        home = None
    return FeedClient(tenant_id, token, cloud=cloud.key, publisher=home, app=connection["client_id"], **kwargs)
