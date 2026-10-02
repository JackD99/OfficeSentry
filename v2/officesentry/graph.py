"""Microsoft Graph client: app-only, read-only, throttling-aware.

Design rules (each one fixes a v1 bug):

* Paths are relative to the API version, e.g. ``client.get_all("/users")``.
  Passing ``"/v1.0/..."`` or ``"/beta/..."`` raises instead of silently
  requesting ``/v1.0/v1.0/...``. Use ``version="beta"`` for beta endpoints.
* Absolute URLs (``@odata.nextLink``) must point at graph.microsoft.com, so a
  bearer token is never sent anywhere else.
* 429 / 503 / 504 are retried, honouring ``Retry-After``.
* Paging never stops silently: ``PagedResult.truncated`` says if it did.
* Only GET is exposed. Office Sentry never changes a tenant.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import requests

log = logging.getLogger(__name__)

GRAPH_ROOT = "https://graph.microsoft.com"
VERSIONS = ("v1.0", "beta")
RETRY_STATUSES = {429, 500, 502, 503, 504}

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
    ):
        self._token_provider = token_provider
        self._session = session or requests.Session()
        self._max_retries = max_retries
        self._timeout = timeout
        self._sleep = sleep

    @classmethod
    def from_certificate(cls, tenant_ref: str, client_id: str, key_pem: str,
                         thumbprint: str, **kwargs) -> GraphClient:
        import msal

        app = msal.ConfidentialClientApplication(
            client_id=client_id,
            authority=f"https://login.microsoftonline.com/{tenant_ref}",
            client_credential={"private_key": key_pem, "thumbprint": thumbprint},
        )

        def token() -> str:
            # MSAL caches the token in memory and renews it before expiry.
            result = app.acquire_token_for_client(scopes=[f"{GRAPH_ROOT}/.default"])
            if "access_token" not in result:
                raise GraphError(401, result.get("error", "auth_failed"),
                                 result.get("error_description", "token request failed"))
            return result["access_token"]

        return cls(token, **kwargs)

    # -- URL handling ------------------------------------------------------

    @staticmethod
    def build_url(path: str, version: str = "v1.0") -> str:
        if version not in VERSIONS:
            raise ValueError(f"unknown Graph version {version!r}")
        if path.startswith("https://"):
            if not path.startswith(GRAPH_ROOT + "/"):
                raise ValueError(f"refusing to send a Graph token to {path.split('/')[2]}")
            return path
        if not path.startswith("/"):
            raise ValueError(f"Graph path must start with '/': {path!r}")
        if path.split("/")[1] in VERSIONS:
            raise ValueError(
                f"Graph path {path!r} includes the API version; pass version= instead"
            )
        return f"{GRAPH_ROOT}/{version}{path}"

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
                self._sleep(min(2 ** attempt, 60))
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

    @staticmethod
    def _retry_after(resp: requests.Response, attempt: int) -> float:
        value = resp.headers.get("Retry-After", "")
        if value.isdigit():
            return min(float(value), 300)
        return float(min(2 ** attempt, 60))

    def get(self, path: str, params: dict | None = None, *, version: str = "v1.0",
            headers: dict | None = None) -> dict:
        return self._get(self.build_url(path, version), params, headers)

    def get_all(self, path: str, params: dict | None = None, *, version: str = "v1.0",
                headers: dict | None = None, max_pages: int = 1000) -> PagedResult:
        """Follow ``@odata.nextLink`` and return every item of ``value``."""
        result = PagedResult()
        url: str | None = self.build_url(path, version)
        first = True
        while url:
            if result.pages >= max_pages:
                result.truncated = True
                log.warning("Stopped paging %s after %s pages", path, max_pages)
                break
            data = self._get(self.build_url(url, version), params if first else None, headers)
            first = False
            result.items.extend(data.get("value", []))
            result.pages += 1
            url = data.get("@odata.nextLink")
        return result
