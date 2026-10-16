"""SharePoint's own REST API: app-only, read-only.

Graph lists sites, libraries and the people SharePoint has recorded on a site, but not who is in a
SharePoint group, which permission level each person or group holds on the site, or which lists and
subsites break inheritance. Those come from the site's REST API:

    GET https://<tenant>.sharepoint.com/sites/<site>/_api/web/roleassignments

(``*.sharepoint.us``, ``*.sharepoint-mil.us`` or ``*.sharepoint.cn`` in the national clouds, see
``clouds.py``). It needs the application permission Sites.Read.All on the "Office 365 SharePoint
Online" resource, not Graph's, and a token signed with the app's certificate: SharePoint refuses
app-only tokens from a client secret.

The rules match ``graph.py``: GET only, a token is only ever sent to the host it was issued for (a site
of the tenant, never another host), 429 and 503 are retried after ``Retry-After``, every request is
counted in ``telemetry`` and, with the worker's ``limiter``, paced against the tenant's SharePoint
limit (``endpoints.SHAREPOINT``). A client is made beside a ``GraphClient`` (:func:`beside`), so it
shares the run's session, limiter and telemetry and signs in as the same app.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from urllib.parse import urlsplit

import requests

from . import clouds, endpoints
from .graph import USER_AGENT, GraphClient, PagedResult
from .ratelimit import Limiter, Telemetry, claims, to_pause

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER = 300
ACCESS_HELP = (
    "SharePoint refused access. The client's Global Admin needs to approve the SharePoint permission "
    "(Sites.Read.All on Office 365 SharePoint Online) from the consent link on the client page."
)


class SharePointError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"SharePoint {status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class SharePointAccessDenied(SharePointError):
    """No SharePoint permission granted to the app in this tenant (or a token SharePoint won't take)."""


def host_of(url: str) -> str:
    """``https://contoso.sharepoint.com`` for any URL on that host."""
    parts = urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}".lower()


def _parse_error(resp: requests.Response) -> SharePointError:
    code, message = "unknown", resp.text[:300] or resp.reason or ""
    try:
        err = resp.json()
        err = err.get("odata.error") or err.get("error") or {}
        code = err.get("code", code)
        raw = err.get("message", message)
        message = raw.get("value", message) if isinstance(raw, dict) else raw
    except (ValueError, AttributeError):
        pass
    if resp.status_code in (401, 403):
        return SharePointAccessDenied(resp.status_code, code, str(message))
    return SharePointError(resp.status_code, code, str(message))


class SharePointClient:
    def __init__(
        self,
        host: str,
        token_provider: Callable[[], str],
        *,
        session: requests.Session | None = None,
        max_retries: int = 6,
        timeout: int = 60,
        sleep: Callable[[float], None] = time.sleep,
        cloud: str | None = None,
        limiter: Limiter | None = None,
        telemetry: Telemetry | None = None,
        app: str = "",
        tenant: str = "",
    ):
        self.cloud = clouds.get(cloud)
        if not self.cloud.is_sharepoint(host):
            raise ValueError(f"{host!r} isn't a {self.cloud.label} SharePoint host")
        self.host = host
        self.limiter = limiter
        self.telemetry = telemetry or Telemetry()
        self.app, self.tenant = app, tenant
        self._token_provider = token_provider
        self._session = session or requests.Session()
        self._max_retries = max_retries
        self._timeout = timeout
        self._sleep = sleep

    def url(self, site_url: str, path: str) -> str:
        """``<site>/_api/<path>``, refusing a site on any other host than this client's."""
        if host_of(site_url) != self.host:
            raise ValueError(f"refusing to send a SharePoint token to {urlsplit(site_url).netloc}")
        if not path.startswith("/"):
            raise ValueError(f"SharePoint path must start with '/': {path!r}")
        return f"{site_url.rstrip('/')}/_api{path}"

    def _get(self, url: str, params: dict | None) -> dict:
        if host_of(url) != self.host:
            raise ValueError(f"refusing to send a SharePoint token to {urlsplit(url).netloc}")
        endpoint = endpoints.sharepoint(url)
        key = endpoints.sharepoint_key(url)
        taken = claims(endpoint, self.app, self.tenant, graph=False) if self.limiter else []
        attempt = 0
        while True:
            waited = self.limiter.acquire(taken) if self.limiter else 0.0
            headers = {
                "Authorization": f"Bearer {self._token_provider()}",
                "Accept": "application/json;odata=nometadata",
                "User-Agent": USER_AGENT,
            }
            try:
                resp = self._session.get(url, params=params, headers=headers, timeout=self._timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                retry = attempt < self._max_retries
                delay = float(min(2**attempt, 60)) if retry else 0.0
                self.telemetry.request(key, endpoint, status=0, limiter_wait=waited, retry_wait=delay)
                if not retry:
                    raise SharePointError(0, "network_error", str(exc)) from exc
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
                log.info("SharePoint %s on %s; retrying in %ss", resp.status_code, url, delay)
                if resp.status_code in (429, 503) and self.limiter:
                    self.limiter.pause(to_pause(taken), delay)
                self._sleep(delay)
                attempt += 1
                continue
            if not resp.ok:
                raise _parse_error(resp)
            return resp.json() if resp.content else {}

    def get(self, site_url: str, path: str, params: dict | None = None) -> dict:
        return self._get(self.url(site_url, path), params)

    def get_all(self, site_url: str, path: str, params: dict | None = None, *, max_pages: int = 50) -> PagedResult:
        """Every item of ``value``, following ``odata.nextLink`` (on the same host only)."""
        result = PagedResult()
        url: str | None = self.url(site_url, path)
        first = True
        while url:
            if result.pages >= max_pages:
                result.truncated = True
                break
            data = self._get(url, params if first else None)
            first = False
            value = data.get("value")
            result.items.extend(value if isinstance(value, list) else [])
            result.pages += 1
            url = data.get("odata.nextLink") or data.get("@odata.nextLink")
        return result


def beside(graph: GraphClient, host: str) -> SharePointClient | None:
    """A client for one SharePoint host that signs in as ``graph``'s app with the same certificate and shares
    its session, limiter and telemetry. None when ``graph`` can't sign in to other resources (a client made
    from a secret, which SharePoint wouldn't accept anyway)."""
    if graph.resource_tokens is None or not graph.cloud.is_sharepoint(host):
        return None
    provider = graph.resource_tokens(host)

    def token() -> str:
        try:
            return provider()
        except SharePointError:
            raise
        except Exception as exc:
            code, message = getattr(exc, "code", "token"), getattr(exc, "message", str(exc))
            if code == "network_error":  # not reaching Microsoft is a network failure, as Graph's is
                raise SharePointError(0, code, message) from exc
            # a sign-in refused for this resource: no consent for it yet
            raise SharePointAccessDenied(401, code, message) from exc

    return SharePointClient(
        host,
        token,
        session=graph._session,
        sleep=graph._sleep,
        cloud=graph.cloud.key,
        limiter=graph.limiter,
        telemetry=graph.telemetry,
        app=graph.app,
        tenant=graph.tenant,
    )
